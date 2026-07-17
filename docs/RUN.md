# Режимы запуска `main.py`

Шпаргалка по всем способам запустить тендерный монитор. Актуально на 17.07.2026.

Флаги режима **взаимоисключающие** (проверяются через `elif`): указываешь один.
Модификаторы (`--test`, `--limit`, `--only-new`, …) добавляются к нему.

---

## Шпаргалка

| Команда | Что делает | Сеть | LLM | Telegram |
|---|---|---|---|---|
| `python main.py` | **Демон**: полный цикл каждые `SCHEDULE_HOURS` + аналитика в 04:00 | да | да | да |
| `python main.py --once` | Один полный цикл: stage1 → stage2 → llm | да | да | да |
| `python main.py --test` | То же, но без записи и без отправки | да | да | нет |
| `python main.py --stage1` | Только сбор карточек + первичный скоринг | да | нет | нет |
| `python main.py --triage` | LLM-триаж карточек (Stage 1.5) | нет | да | нет |
| `python main.py --stage2` | Скачивание документов, детальный скоринг | да | нет | нет |
| `python main.py --llm` | LLM-разбор + отправка по данным из БД | нет | да | да |
| `python main.py --stage3` | После дедлайна: результаты/победители | да | нет | нет |
| `python main.py --rescore` | Пересчёт скоринга по сохранённому тексту | нет | нет | нет |
| `python main.py --redocs` | Переобработка скачанных документов с диска | нет | нет | нет |
| `python main.py --analytics` | Ценовые коридоры, заказчики, изменения ТЗ | да | нет | нет |
| `python main.py --eat-only` | Stage1 только по ЕАТ «Берёзка» | да | нет | нет |
| `python main.py --tenderplan-only` | Stage1 + Stage2 + LLM только по Tenderplan | да | да | да |
| `python main.py --reset-db` | **DROP** + пересоздание схемы (осознанно!) | нет | нет | нет |

---

## Конвейер: из чего состоит полный цикл

```
Stage 1     сбор карточек с площадок, быстрый скоринг по названию (без ТЗ)
   ↓
Stage 1.5   LLM-триаж: БЕРУ/ЧАСТИЧНО/МИМО, ловит перекуп лицензий  ← только вручную
   ↓
Stage 2     скачивание документов/ТЗ, извлечение условий, детальный скоринг
   ↓
Stage 2.5   LLM-разбор топ-кандидатов + отправка в Telegram
   ↓
Stage 3     после дедлайна: кто выиграл, за сколько (аналитика)
```

`--once` = Stage 1 → Stage 2 → Stage 2.5. **Триаж в автоцикл не входит** — гоняется отдельно
(`--triage`), чтобы не жечь дневной лимит LLM на каждом прогоне.

---

## Ежедневная работа

### Полный цикл
```bash
python main.py --once                    # собрать → скачать → разобрать → отправить
python main.py --once --test             # то же вхолостую (ничего не отправит)
python main.py --once --skip-completed-today   # не повторять уже сделанное сегодня
```

### Демон (боевой режим)
```bash
python main.py
```
Крутит `run_once()` каждые `SCHEDULE_HOURS` (сейчас 3 ч), аналитику в 04:00,
плюс поднимает подпроцессы: обработчик кнопок Telegram и детектор изменений ТЗ.
Требует `pip install schedule`.

### LLM-триаж бэклога
```bash
python main.py --triage                  # разметить неразмеченные карточки
python main.py --triage --limit 500      # ограничить (дневной лимит LLM ~900 запросов)
python main.py --triage --test           # печатать вердикты, но не писать в БД
```

---

## Отдельные площадки

### Портал поставщиков Москвы
```bash
python main.py --mos-only
```
### ЕАТ «Берёзка»
```bash
python main.py --eat-only                # поиск только по ЕАТ
python main.py --eat-only --test         # посмотреть, что найдётся
python main.py --eat-only --only-new     # пропустить уже известные лоты
python main.py --stage2 --platform ЕАТ   # скачать контракты/ТЗ найденного
```
Куки антибота обновляются **автоматически** (headless-браузер). Если антибот
покажет слайдер-капчу — один раз вручную: `python refresh_eat_cookies.py --headed`.

### Tenderplan (платный агрегатор — кандидат на отключение)
```bash
python main.py --tenderplan-only         # сразу весь цикл: stage1 + stage2 + llm
```

### Stage 2 по одной площадке
```bash
python main.py --stage2 --platform ЕАТ --limit 10
python main.py --stage2 --platform "ПП Москвы"
python main.py --stage2 --platform "ЭМ СПб"
python main.py --stage2 --platform "ЭМ МО"
python main.py --stage2 --platform ЕИС
python main.py --stage2 --platform B2B-Center
```
Значения `--platform` — ровно как в колонке `tenders.platform`.

Полезно при отладке: `--force` берёт и уже разобранные лоты (иначе после первого
прогона лот выпадает из очереди по `detail_checked_at`):
```bash
python main.py --stage2 --platform ЕАТ --force --limit 3 --test
```

Включение/выключение самих каналов — флаги в `.env`:
`SOURCE_EAT_ENABLED`, `SOURCE_SPB_ENABLED`, `SOURCE_MOS_ENABLED`,
`SOURCE_MOSREG_ENABLED`, `SOURCE_B2B_ENABLED`, `SOURCE_TENDERPLAN_ENABLED`.

---

## Даты: за какой период искать

Управляется настройкой `PUBLISH_DATE_FROM` (в `.env` или через `/control`):

| Значение | Поведение | Страниц на ключ |
|---|---|---|
| `last` | **Инкремент**: с даты прошлого успешного прогона − 2 дня | `SEARCH_PAGES` (2) |
| `auto` | Все активные торги, без фильтра по дате | `BACKFILL_SEARCH_PAGES` (20) |
| `01.07.2026` | С конкретной даты (можно и `2026-07-01`) | `SEARCH_PAGES` (2) |
| пусто | За `PUBLISH_DAYS_BACK` дней назад (30) | `SEARCH_PAGES` (2) |

### Инкрементальный режим (рекомендуется для повседневки)
```
PUBLISH_DATE_FROM=last
```
- Первый запуск: метки нет → берёт `PUBLISH_DAYS_BACK` дней.
- Дальше: ищет с даты прошлого прогона минус перекрытие, в конце двигает метку.
- **Перекрытие 2 дня** (`STAGE1_WATERMARK_OVERLAP_DAYS`): площадки публикуют карточки
  с задержкой, без нахлёста граничные лоты потерялись бы.
- **Метка двигается только после успешного прогона**: были ошибки канала — метка
  на месте, следующий прогон повторит период.
- Метка лежит в `settings.STAGE1_WATERMARK`, сбросить: удалить эту строку.

### Разово перелопатить всё активное
```bash
python main.py --stage1 --backfill-active     # только сбор
python main.py --once --backfill-active       # полный цикл
```
Снимает фильтр по дате и поднимает глубину до 20 страниц на ключ. Настройки не меняет
и **метку инкремента не двигает** — можно спокойно совмещать с `PUBLISH_DATE_FROM=last`.

---

## Дубли: что перекачивается, а что нет

**Документы повторно не качаются.** Stage 1 обновляет карточку через
`ON CONFLICT DO UPDATE`, но не трогает `detail_checked_at`, `documents_dir`,
`document_count`, `document_text_excerpt`, `llm_analysis`, `notified_at`.
Stage 2 берёт только лоты с `detail_checked_at IS NULL OR needs_detail_refresh = TRUE`.

Пере-скачивание происходит **только** если карточка реально изменилась
(цена / срок / название / ссылка) — тогда ставится `needs_detail_refresh`.

`--only-new` дополнительно пропускает уже известные номера ещё на этапе скоринга
(экономит CPU, но страницы поиска всё равно обходятся).

---

## Работа без сети (по данным из БД)

```bash
python main.py --rescore          # пересчитать скоринг всех лотов по сохранённому тексту
python main.py --rescore --test   # показать изменения решений, не записывая
python main.py --redocs           # перечитать скачанные документы с диска
python main.py --redocs --limit 50 --test
python main.py --llm              # LLM-разбор + отправка того, что уже в БД
```

- `--rescore` — после правок `filter_engine` (ценовой коридор, новые фразы),
  чтобы дашборд отразил новые оценки по всей базе.
- `--redocs` — после правок `document_processor` (порядок склейки, извлечение текста).
- `--llm` — не ходит на площадки вообще, только БД → LLM → Telegram.

---

## Модификаторы

| Флаг | Действие |
|---|---|
| `--test` | Dry-run: без записи в БД и без отправки в Telegram |
| `--limit N` | Ограничить число лотов (для stage2/llm/triage/rescore/redocs) |
| `--only-new` | Stage1: пропускать уже известные закупки |
| `--skip-completed-today` | Не повторять стадии, успешно завершённые сегодня |
| `--backfill-active` | Stage1: все активные, без фильтра по дате, 20 страниц |
| `--platform X` | Stage2: только одна площадка |
| `--force` | Stage2: включая уже разобранные лоты |

---

## Вспомогательные скрипты

```bash
python init_db.py                      # ОДИН РАЗ: создать схему + засеять правила
uvicorn web_app:app --host 0.0.0.0 --port 8000 --reload   # веб-панель :8000

python refresh_eat_cookies.py          # обновить куки ЕАТ (headless, автоматом)
python refresh_eat_cookies.py --headed # если антибот требует слайдер — руками
python refresh_eat_cookies.py --check  # обновить и сразу проверить канал
python update_eat_cookie.py            # альтернатива: куки из Copy as cURL в буфере

python -m sources.eat "сайт"           # самотест коннектора ЕАТ
python -m sources.eat "сайт" --raw     # сырая структура ответа API
python -m sources.eat --docs <guid>    # документы лота
python -m sources.eat --download <guid> # скачать документы лота
python -m sources.spb_estore "видеонаблюдение"    # самотест СПб
python -m sources.mos_supplier "сайт"             # самотест Портала поставщиков
python -m sources.mosreg "сайт"                   # самотест ЭМ Московской области

python probe_mos_zmo.py                # разведка API Москвы/МО
python probe_zmo_v3.py                 # добивка разведки
```

---

## Типовые сценарии

**Утро, посмотреть что нового:**
```bash
python main.py --once --test           # вхолостую, глянуть выхлоп
python main.py --once                  # боевой прогон
```

**Разметить накопившийся бэклог LLM:**
```bash
python main.py --triage --limit 800
```

**Проверить конкретную площадку после правок коннектора:**
```bash
python main.py --eat-only --test
python main.py --stage2 --platform ЕАТ --force --limit 3 --test
```

**Наверстать пропущенное за неделю простоя:**
```bash
python main.py --stage1 --backfill-active
python main.py --stage2 --limit 60
python main.py --llm
```

**После правки фильтров/скоринга:**
```bash
python main.py --rescore --test        # посмотреть, что изменится
python main.py --rescore               # применить
```

---

## Что где настраивается

- **`.env`** — секреты и дефолты (`DATABASE_URL`, `TELEGRAM_*`, `OPENROUTER_API_KEY`,
  флаги источников, `PUBLISH_DATE_FROM`, `STAGE2_LIMIT`).
- **`settings` в БД** — runtime-настройки, перебивают `.env`, меняются в `/control`
  без рестарта. Там же живут `STAGE1_WATERMARK` (метка инкремента) и
  `LLM_REQUESTS_<дата>` (счётчик дневного лимита LLM).
- **`search_profiles.py` / `/control`** — поисковые профили и ключевые фразы.
- **`/rules`** — словарь фраз 8-фильтрового движка.
- **`/kb`** — база знаний: компетенции, правила рисков.
