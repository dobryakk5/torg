# Деплой «Тендерного монитора» на Ubuntu

Комплект для запуска на сервере: 2 постоянных сервиса + периодические прогоны.

```
deploy/
├── install.sh                    # инсталлятор systemd (подставляет пути, ставит юниты)
├── systemd/
│   ├── tender.service            # веб-панель (uvicorn :8000)          — постоянный
│   ├── tg_tender.service         # обработчик Telegram-кнопок           — постоянный
│   ├── tender-zmo.service/.timer # ЗМО-цикл каждые 15 мин  (main.py --zmo)
│   └── tender-daily.service/.timer # полный цикл раз в день (main.py --once)
└── cron/
    └── tender.crontab            # cron-альтернатива таймерам
```

**Архитектура запуска:**
- `tender.service` и `tg_tender.service` — всегда через systemd (долгоживущие демоны).
- Периодику (ЗМО 15 мин + дневной полный) — **либо** systemd-таймеры, **либо** cron. Не оба.
- `python main.py` в режиме демона (без флагов) на сервере **НЕ запускаем** — он поднимает
  свой обработчик Telegram и конфликтует с `tg_tender.service` за `getUpdates`.

---

## 1. Предпосылки

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip postgresql util-linux
```
- **PostgreSQL** — локально или отдельным хостом (адрес в `DATABASE_URL`).
- **util-linux** — даёт `flock` (защита прогонов от наложения).
- **Прокси с российским IP** — обязателен: анти-боты площадок (ЕАТ и др.) пропускают
  только RU-адреса. Строка в `SOURCES_PROXY_URL`.
- **(опционально) Playwright** — для авто-обновления анти-бот куков ЕАТ:
  ```bash
  .venv/bin/pip install playwright && .venv/bin/playwright install chromium
  ```
  Без него ЕАТ работает, только пока куки в `.env` свежие.

---

## 2. Код и зависимости

```bash
sudo mkdir -p /opt/torg && sudo chown $USER:$USER /opt/torg
git clone <repo> /opt/torg          # или скопируй проект
cd /opt/torg
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

---

## 3. Секреты — `.env` в корне проекта

`.env` в git не хранится (в `.gitignore`) — создай на сервере руками. Минимум:

```ini
# --- БД ---
DATABASE_URL=postgresql://user:pass@localhost:5432/torg

# --- Telegram ---
TELEGRAM_BOT_TOKEN=8835608752:AAH...        # ровно 46 символов <id>:<35>, без лишнего
TELEGRAM_CHAT_ID=8064548672                 # куда слать (личка/группа)

# --- LLM (ключ ТОЛЬКО здесь, в /control его нет) ---
OPENROUTER_API_KEY=sk-or-...
# LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY — если прямой Claude вместо OpenRouter

# --- Прокси площадок (RU IP; LLM и Telegram идут мимо него) ---
SOURCES_PROXY_URL=http://user:pass@host:port

# --- Источники ЗМО (1 = вкл) ---
SOURCE_EAT_ENABLED=1
SOURCE_MOS_ENABLED=1
SOURCE_MOSREG_ENABLED=1
SOURCE_SPB_ENABLED=1

# В Telegram слать ТОЛЬКО ЗМО (по умолчанию 1). ЕИС/B2B/Tenderplan при этом
# собираются и разбираются, но видны только в веб-панели. 0 = слать всё.
NOTIFY_ONLY_ZMO=1
```

> ⚠️ `TELEGRAM_BOT_TOKEN` — без пробелов/переносов/лишних символов. Проверка:
> `curl -s "https://api.telegram.org/bot<TOKEN>/getMe"` должен вернуть `"ok":true`.

---

## 4. Инициализация БД (один раз)

```bash
cd /opt/torg
.venv/bin/python init_db.py
```

---

## 5. Проверка ДО установки сервисов

```bash
cd /opt/torg
# прокси жив и RU + все площадки отвечают:
.venv/bin/python test_proxy_sources.py
# один ЗМО-цикл без отправки в Telegram:
.venv/bin/python main.py --zmo --test
```

---

## 6. Установка сервисов

```bash
cd /opt/torg
sudo PROJECT_DIR=/opt/torg PYTHON=/opt/torg/.venv/bin/python RUN_USER=$USER \
     bash deploy/install.sh
```

Инсталлятор подставит пути/пользователя, положит юниты в `/etc/systemd/system/`,
включит и запустит `tender.service`, `tg_tender.service`, а также таймеры
`tender-zmo.timer` и `tender-daily.timer`.

**Только сервисы, периодику — через cron:**
```bash
sudo NO_TIMERS=1 PROJECT_DIR=/opt/torg PYTHON=/opt/torg/.venv/bin/python RUN_USER=$USER \
     bash deploy/install.sh
```

---

## 7. Периодика: таймеры ИЛИ cron

**Вариант А — systemd-таймеры** (ставятся install.sh по умолчанию). Время дневного
прогона правится в `tender-daily.timer` (`OnCalendar=`), затем
`sudo systemctl daemon-reload && sudo systemctl restart tender-daily.timer`.

**Вариант Б — cron** (если ставил с `NO_TIMERS=1`):
```bash
sed -e "s#__PROJECT_DIR__#/opt/torg#g" \
    -e "s#__PYTHON__#/opt/torg/.venv/bin/python#g" \
    deploy/cron/tender.crontab | crontab -
crontab -l          # проверить
```
Cron ставь **под тем же пользователем**, что и сервисы (не root).

---

## 8. Проверка после установки

```bash
systemctl status tender.service tg_tender.service     # оба active (running)
systemctl list-timers 'tender-*'                      # ближайшие запуски
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000   # 200
sudo systemctl start tender-zmo.service               # прогнать ЗМО вручную
journalctl -u tender-zmo.service -f                   # смотреть лог прогона
```
Открой веб-панель: `http://<сервер>:8000` (при внешнем доступе — за nginx/файрволом).
Нажми кнопку в Telegram — если решение записалось, `tg_tender.service` работает.

---

## 9. Управление и логи

| Действие | Команда |
|---|---|
| Логи веба | `journalctl -u tender.service -f` |
| Логи кнопок | `journalctl -u tg_tender.service -f` |
| Логи ЗМО / дневного | `journalctl -u tender-zmo.service -f` · `-u tender-daily.service` |
| Логи cron-прогонов | `tail -f /opt/torg/data/cron-zmo.log` |
| Перезапуск веба | `sudo systemctl restart tender.service` |
| Пауза периодики | `sudo systemctl disable --now tender-zmo.timer tender-daily.timer` |
| Остановить всё | `sudo systemctl disable --now tender.service tg_tender.service tender-{zmo,daily}.timer` |

---

## 10. Обновление кода

```bash
cd /opt/torg && git pull
.venv/bin/pip install -r requirements.txt          # если менялись зависимости
.venv/bin/python init_db.py                        # если менялась схема (идемпотентно)
sudo systemctl restart tender.service tg_tender.service
# юниты в deploy/ поменялись? — переустанови: sudo bash deploy/install.sh
```

---

## Частые проблемы

- **Кнопки в Telegram не реагируют** → `tg_tender.service` не запущен, ИЛИ где-то ещё
  крутится `python main.py` (демон) и перехватывает `getUpdates`. Оставь один потребитель.
- **LLM «недоступна» в веб-панели** → нет `OPENROUTER_API_KEY` в `.env` (в `/control` ключа
  нет — там только выбор модели). Добавь ключ и `sudo systemctl restart tender.service`.
- **ЕАТ/площадки пусто или капча** → проверь `SOURCES_PROXY_URL` (RU IP) через
  `.venv/bin/python test_proxy_sources.py`; для ЕАТ поставь playwright (см. п.1).
- **Дневной и ЗМО столкнулись** → не должны: общий `flock`-лок
  (`/tmp/tender-pipeline.lock`) сериализует их автоматически.
