# Тест Tenderplan API

curl --location --request GET 'https://tenderplan.ru/api/info/firm' \
--header 'Authorization: Bearer '

curl -sS -i \
  -H "Authorization: Bearer " \
  -H "Accept: application/json" \
  'https://tenderplan.ru/api/info/user'

echo



## 1. Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Настройка

```bash
cp .env.example .env
nano .env
```

Вставьте токен в:

```env
TENDERPLAN_TOKEN=...
```

Не добавляйте `.env` в Git.

## 3. Запуск

```bash
python tenderplan_test.py
```
python main.py --tenderplan-only --only-new

Результат будет записан в `./tenderplan_export`:

- `_user.json` — проверка авторизации;
- `_raw_tender_list.json` — исходный ответ списка;
- `_index.json` — индекс обработанных торгов;
- отдельная папка на каждый тендер;
- `tender.json` — карточка тендера;
- `attachments.json` — метаданные документов;
- `documents.zip` — архив документации.

## Если список торгов вернул HTTP 400

Откройте в Swagger:

```text
GET /api/tenders/getlist
```

Посмотрите обязательные query-параметры и внесите их JSON-объектом:

```env
TENDERPLAN_LIST_PARAMS_JSON={"limit":10,"...":"..."}
```

Скрипт выводит тело ответа Tenderplan, поэтому будет видно, какого
параметра не хватает.

## Безопасность

Токен храните только в `.env`. Ранее опубликованные или переданные
посторонним токены следует отозвать.
