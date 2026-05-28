# Tender Monitor — PostgreSQL, двухэтапная версия

Схема работы:

1. **Stage 1** — массово загружает карточки закупок, делает быстрый 8-фильтровый скоринг по описанию и сохраняет ссылку на тендер. Если активная карточка изменилась, она помечается на повторный Stage 2.
2. **Stage 2** — берёт только кандидатов с высоким первичным скорингом, скачивает ТЗ/документы, извлекает условия и делает детальный 8-фильтровый скоринг. Повторно запускается для изменившихся карточек/документов.
3. **Stage 3** — после дедлайна подтягивает результат закупки: победитель, финальная цена, число участников, снижение. Это нужно для аналитики конкуренции.

SQLite больше не используется. Локальный `tenders.db` не создаётся.

## Установка

```bash
cd tender_monitor
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## PostgreSQL

Создай БД и пользователя, пример:

```sql
CREATE DATABASE tenders_db;
CREATE USER tenders_user WITH PASSWORD 'change_me';
GRANT ALL PRIVILEGES ON DATABASE tenders_db TO tenders_user;
```

Для PostgreSQL 15+ часто ещё нужно выдать права на схему:

```bash
psql -d tenders_db
```

```sql
GRANT ALL ON SCHEMA public TO tenders_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO tenders_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO tenders_user;
```

В `.env` укажи:

```env
DATABASE_URL=postgresql://tenders_user:change_me@localhost:5432/tenders_db
```

Можно вместо `DATABASE_URL` использовать параметры `PG_HOST`, `PG_PORT`, `PG_DBNAME`, `PG_USER`, `PG_PASSWORD`.

## Запуск

Первый этап — только карточки и первичный скоринг:

```bash
python main.py --stage1
```

Второй этап — ТЗ/документы только по сильным кандидатам:

```bash
python main.py --stage2 --limit 10
```

Третий этап — результаты после дедлайна:

```bash
python main.py --stage3 --limit 50
# или
python main.py --results --limit 50
```

Полный цикл:

```bash
python main.py --once
```

Тест без отправки в Telegram:

```bash
python main.py --test
```

Полный сброс PostgreSQL-таблиц проекта:

```bash
python main.py --reset-db --stage1
```

Команда удалит таблицы `tenders`, `runs`, `decisions`, `filter_scores` и создаст их заново.


## 8 фильтров

`filter_engine.py` оценивает каждый тендер по шкале 8–40:

1. Профиль задачи — Битрикс/1С/CRM/API/серверы/складская автоматизация.
2. Финансовый вход — НМЦК, обеспечение заявки, обеспечение исполнения, аванс.
3. Объём и границы работ — лимит часов, перечень работ, риск безлимитных заявок.
4. Сроки и SLA — срок подачи, 24/7, реакция за 1 час, выезды. Короткий срок подачи теперь не является стоп-фактором сам по себе: он только снижает оценку и усиливает риск “под своего”.
5. Требования к участнику — опыт, лицензии, ФСТЭК/ФСБ/СРО, аккредитации.
6. Признаки «под своего» — текущая система, короткий срок, отсутствие описания архитектуры.
7. Поставка / перекуп / логистика — товарная мясорубка или товар + настройка.
8. Договорные риски — штрафы, пени, оплата, приёмка, гарантийные обязательства.

Решения:

- `GO` — от 32/40 без стоп-факторов;
- `CAUTION` — от 24/40 без стоп-факторов;
- `NO-GO` — ниже 24/40 или есть критичный стоп-фактор.

Рекомендуемые пороги в `.env`:

```env
MIN_PRIMARY_SCORE_FOR_DETAIL=24
MIN_DETAILED_SCORE_FOR_NOTIFY=30
MIN_SCORE_FOR_LLM=28
STAGE3_LIMIT=50
```

## Telegram-кнопки

Отдельный процесс:

```bash
python telegram_decisions.py
```

Он принимает callback-кнопки и обновляет `decision/status` в PostgreSQL.

## Веб-интерфейс

Черновики `web_app.py`, `index.html`, `detail.html` встроены в проект.

Запуск:

```bash
uvicorn web_app:app --reload --port 8000
```

Страницы:

- `/` — дашборд тендеров;
- `/tender/{purchase_number}` — карточка тендера;
- `/api/stats` — статистика JSON;
- `/api/tenders` — список JSON.

Веб-интерфейс показывает реальные 8 фильтров из `filter_scores`. `filter_total/filter_decision` заполняются результатом `filter_engine.py`.

## Рекомендуемый cron

```cron
0 */3 * * * cd /path/to/tender_monitor && /path/to/tender_monitor/.venv/bin/python main.py --stage1 >> cron.log 2>&1
30 */6 * * * cd /path/to/tender_monitor && /path/to/tender_monitor/.venv/bin/python main.py --stage2 --limit 10 >> cron.log 2>&1
15 9 * * * cd /path/to/tender_monitor && /path/to/tender_monitor/.venv/bin/python main.py --stage3 --limit 50 >> cron.log 2>&1
```

## Что внутри БД

Главная таблица `tenders` хранит:

- карточку закупки и ссылку;
- результаты stage1: `primary_score`, `primary_reasons`;
- результаты stage2: `detail_score`, `detail_reasons`;
- совместимые поля для веб-интерфейса: `filter_total`, `filter_decision`, `filter_stop`;
- финансовые условия: обеспечение заявки/контракта, аванс, оплата, срок исполнения;
- документы: количество, путь, hash, текстовый excerpt;
- Telegram/ручные решения: `notified_at`, `decision`, `status`;
- изменения карточки: `content_hash`, `last_changed_at`, `needs_detail_refresh`;
- результаты после дедлайна: `result_checked_at`, `winner_name`, `winner_inn`, `final_price`, `participants_count`, `price_drop_percent`.

Таблица `tender_changes` хранит историю изменений карточки/документов/результатов.

Таблица `filter_scores` хранит 8 отдельных оценок по каждому тендеру: профиль, финансы, объём, SLA, требования, признаки заточки, поставка/перекуп, договорные риски.
