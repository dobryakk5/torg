"""document_processor.py — скачивание и извлечение текста из документов закупки."""

from __future__ import annotations

import hashlib
import logging
import re
import zipfile
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import config
from scraper import HEADERS, BASE_URL

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".docx", ".pdf", ".txt", ".rtf", ".xlsx", ".xls", ".csv", ".zip"}


def safe_filename(name: str, fallback: str = "document") -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name).strip(" .")
    return name[:180] or fallback


def find_document_links(page_html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(page_html, "html.parser")
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        text = a.get_text(" ", strip=True).lower()
        lower_href = href.lower()
        looks_like_doc = any(ext in lower_href for ext in SUPPORTED_EXTENSIONS) or any(
            marker in lower_href for marker in ["download", "documents", "file", "epz/order/notice"]
        ) or any(word in text for word in ["документ", "извещение", "техническое", "проект контракта", "скачать"])
        if not looks_like_doc:
            continue
        url = urljoin(base_url, href)
        if url not in links:
            links.append(url)
    return links[: config.MAX_DOCUMENTS_PER_TENDER]


def _guess_filename(response: requests.Response, url: str, index: int) -> str:
    cd = response.headers.get("content-disposition", "")
    match = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)", cd, re.I)
    if match:
        raw = match.group(1) or match.group(2)
        try:
            from urllib.parse import unquote
            return safe_filename(unquote(raw))
        except Exception:
            return safe_filename(raw)
    path_name = Path(urlparse(url).path).name
    if path_name and "." in path_name:
        return safe_filename(path_name)
    content_type = response.headers.get("content-type", "").lower()
    ext = ".bin"
    if "pdf" in content_type:
        ext = ".pdf"
    elif "word" in content_type or "officedocument" in content_type:
        ext = ".docx"
    elif "zip" in content_type:
        ext = ".zip"
    elif "excel" in content_type or "spreadsheet" in content_type:
        ext = ".xlsx"
    elif "html" in content_type:
        ext = ".html"
    return f"document_{index}{ext}"


def download_documents(purchase_number: str, page_html: str, tender_url: str) -> dict:
    """Скачивает документы карточки закупки в data/documents/<purchase_number>."""
    target_dir = config.DOCUMENTS_DIR / safe_filename(purchase_number)
    target_dir.mkdir(parents=True, exist_ok=True)

    links = find_document_links(page_html, tender_url)
    saved: list[Path] = []
    for i, link in enumerate(links, start=1):
        try:
            resp = requests.get(link, headers=HEADERS, timeout=30)
            if resp.status_code != 200 or not resp.content:
                logger.info("Документ не скачан %s: HTTP %s", link, resp.status_code)
                continue
            filename = _guess_filename(resp, link, i)
            path = target_dir / filename
            # Если ЕИС отдаёт html вместо файла, всё равно сохраняем: оттуда можно вытащить текст.
            path.write_bytes(resp.content)
            saved.append(path)
        except requests.RequestException as exc:
            logger.warning("Ошибка скачивания документа %s: %s", link, exc)

    return {"dir": str(target_dir), "files": saved, "links": links}


def extract_text_from_file(path: Path) -> str:
    ext = path.suffix.lower()
    try:
        if ext == ".docx":
            return _extract_docx(path)
        if ext == ".pdf":
            return _extract_pdf(path)
        if ext in {".xlsx", ".xls"}:
            return _extract_xlsx(path)
        if ext == ".zip":
            return _extract_zip(path)
        if ext in {".txt", ".csv", ".rtf", ".html", ".htm", ".xml"}:
            return _extract_plain(path)
        # Пробуем как текст, если расширение неизвестно.
        return _extract_plain(path)
    except Exception as exc:
        logger.warning("Не удалось извлечь текст из %s: %s", path.name, exc)
        return ""


def _extract_plain(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8", "cp1251", "latin-1"):
        try:
            text = raw.decode(enc, errors="ignore")
            if path.suffix.lower() in {".html", ".htm", ".xml"}:
                soup = BeautifulSoup(text, "html.parser")
                return soup.get_text("\n", strip=True)
            return text
        except Exception:
            continue
    return ""


def _extract_docx(path: Path) -> str:
    try:
        from docx import Document
    except ImportError:
        logger.warning("python-docx не установлен, пропускаю %s", path.name)
        return ""
    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _extract_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf не установлен, пропускаю %s", path.name)
        return ""
    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages[:30]:
        pages.append(page.extract_text() or "")
    return "\n".join(pages)


def _extract_xlsx(path: Path) -> str:
    try:
        import openpyxl
    except ImportError:
        logger.warning("openpyxl не установлен, пропускаю %s", path.name)
        return ""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets[:5]:
        parts.append(f"# Лист: {ws.title}")
        for row in ws.iter_rows(max_row=200, values_only=True):
            values = [str(v).strip() for v in row if v is not None and str(v).strip()]
            if values:
                parts.append(" | ".join(values))
    return "\n".join(parts)


def _extract_zip(path: Path) -> str:
    parts = []
    extract_dir = path.parent / (path.stem + "_unzipped")
    extract_dir.mkdir(exist_ok=True)
    try:
        with zipfile.ZipFile(path) as zf:
            for member in zf.infolist()[:30]:
                if member.is_dir():
                    continue
                member_name = safe_filename(Path(member.filename).name)
                if not member_name:
                    continue
                out_path = extract_dir / member_name
                out_path.write_bytes(zf.read(member))
                if out_path.suffix.lower() in SUPPORTED_EXTENSIONS | {".html", ".htm", ".xml"}:
                    text = extract_text_from_file(out_path)
                    if text:
                        parts.append(f"\n# Файл из архива: {member.filename}\n{text}")
    except zipfile.BadZipFile:
        return ""
    return "\n".join(parts)


def collect_document_text(files: Iterable[Path], max_chars: int | None = None) -> str:
    limit = max_chars or config.MAX_DOCUMENT_TEXT_CHARS
    chunks = []
    total = 0
    for path in files:
        text = extract_text_from_file(Path(path))
        if not text.strip():
            continue
        clean = normalize_text(text)
        part = f"\n\n# Документ: {Path(path).name}\n{clean}"
        chunks.append(part)
        total += len(part)
        if total >= limit:
            break
    return "".join(chunks)[:limit]


def normalize_text(text: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def hash_files(files: Iterable[Path]) -> str:
    h = hashlib.sha256()
    for path in sorted(Path(p) for p in files):
        if not path.exists() or not path.is_file():
            continue
        h.update(path.name.encode("utf-8", errors="ignore"))
        h.update(str(path.stat().st_size).encode())
    return h.hexdigest()


def extract_financial_terms(text: str) -> dict:
    """Эвристически достаёт обеспечение/аванс/оплату из текста страницы и документов."""
    result = {
        "application_security_amount": None,
        "contract_security_amount": None,
        "warranty_security_amount": None,
        "advance_percent": None,
        "payment_terms": "",
        "execution_days": None,
    }
    if not text:
        return result

    compact = re.sub(r"\s+", " ", text.lower())

    result["application_security_amount"] = _find_money_near(compact, ["обеспечение заявки", "размер обеспечения заявки"])
    result["contract_security_amount"] = _find_money_near(compact, ["обеспечение исполнения контракта", "обеспечение исполнения договора"])
    result["warranty_security_amount"] = _find_money_near(compact, ["обеспечение гарантийных обязательств"])

    advance_match = re.search(r"аванс[^%]{0,80}(\d{1,3})\s*%", compact)
    if advance_match:
        result["advance_percent"] = float(advance_match.group(1))

    payment_match = re.search(r"(оплата[^.]{0,240}(?:дн|рабоч|календар|акт|счет|счёт)[^.]{0,120})", compact)
    if payment_match:
        result["payment_terms"] = payment_match.group(1)[:500]

    days_match = re.search(r"(?:срок[^.]{0,80}|в течение\s+)(\d{1,3})\s*(?:рабочих|календарных)?\s*дн", compact)
    if days_match:
        result["execution_days"] = int(days_match.group(1))

    return result


# Маркеры содержательных требований к участнику (лежат в ИК/ТЗ/проекте контракта,
# а не на странице common-info, где только формулярные ссылки ст. 31 / ст. 14).
_REQUIREMENT_MARKERS: list[tuple[str, str, str]] = [
    ("experience",  r"опыт[а-я ]{0,25}(?:исполнен|поставк|оказан|выполнен|аналогич|сопоставим)",
     "Опыт исполнения аналогичных контрактов"),
    ("license",     r"наличи[ея][^.]{0,40}лиценз|лицензи[ия][^.]{0,40}(?:фстэк|фсб|деятельност)",
     "Лицензия (ФСТЭК/ФСБ/иная)"),
    ("sro",         r"\bсро\b|саморегулируем",
     "Членство в СРО"),
    ("add_req_31_2", r"ч\.?\s*2\s*ст\.?\s*31|дополнительны[ех]\s+требовани|постановлени[ея][^.]{0,40}2571|пп\s*рф\s*2571|№\s*2571",
     "Доптребования (ч. 2 ст. 31 / ПП РФ 2571)"),
    ("qualification", r"квалификац|квалифицированн|сертифицированн[ыйаяое]|сертификат\s+(?:соответстви|специалист)",
     "Квалификация / сертификаты"),
    ("staff",       r"штатн[аыо][^.]{0,30}(?:специалист|сотрудник|персонал)|наличи[ея][^.]{0,30}специалист",
     "Требования к персоналу"),
    ("bid_security", r"обеспечени[ея]\s+заявк",
     "Обеспечение заявки"),
]


def extract_participant_requirements(text: str) -> list[dict]:
    """
    Ищет в тексте документов (ИК/ТЗ/проект контракта) содержательные требования
    к участнику по маркерам. Возвращает список {type, label, snippet}.

    Это не юридически точный разбор, а сигнал «тут есть доптребование/опыт/лицензия» —
    чтобы requirements_special стал осмысленным (на странице common-info этого нет).
    """
    if not text:
        return []
    compact = re.sub(r"\s+", " ", text.lower())
    found: list[dict] = []
    seen: set[str] = set()
    for key, pattern, label in _REQUIREMENT_MARKERS:
        m = re.search(pattern, compact)
        if not m or key in seen:
            continue
        seen.add(key)
        i = m.start()
        snippet = compact[max(0, i - 50): i + 160].strip()
        found.append({"type": key, "label": label, "snippet": snippet})
    return found


def _find_money_near(text: str, markers: list[str]) -> float | None:
    for marker in markers:
        idx = text.find(marker)
        if idx == -1:
            continue
        fragment = text[idx : idx + 600]
        matches = re.findall(r"(\d[\d\s]{1,15}(?:[,.]\d{1,2})?)\s*(?:руб|₽)", fragment)
        for raw in matches:
            value = _parse_money(raw)
            if value and value > 0:
                return value
    return None


def _parse_money(raw: str) -> float | None:
    cleaned = raw.replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None
