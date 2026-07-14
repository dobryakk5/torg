ЗАМЕНИТЬ В КОРНЕ ПРОЕКТА:
- config.py
- scraper.py
- web_app.py

Что исправлено:
1. На macOS requests использует /etc/ssl/cert.pem, если EIS_CA_BUNDLE явно не задан.
2. Тип извещения из исходной ссылки ЕИС сохраняется: ea20 не заменяется на zk20.
3. URL страницы документов получает тот же тип извещения.
4. Веб-интерфейс строит ссылки по той же логике.

Проверка:
python -m py_compile config.py scraper.py web_app.py

Проверка настроек:
python - <<'PY'
import config
from scraper import REQUEST_VERIFY, to_common_info_url, to_documents_url
u = 'https://zakupki.gov.ru/epz/order/notice/ea20/view/common-info.html?regNumber=0120100009326000065'
print('EIS_CA_BUNDLE:', config.EIS_CA_BUNDLE)
print('REQUEST_VERIFY:', REQUEST_VERIFY)
print('COMMON:', to_common_info_url(u))
print('DOCS:', to_documents_url(u))
PY

На macOS ожидается:
EIS_CA_BUNDLE: /etc/ssl/cert.pem
REQUEST_VERIFY: /etc/ssl/cert.pem
.../notice/ea20/view/common-info.html?...
.../notice/ea20/view/documents.html?...

Если в .env уже есть неверный EIS_CA_BUNDLE, удалите строку или задайте:
EIS_CA_BUNDLE=/etc/ssl/cert.pem
EIS_VERIFY_SSL=1
