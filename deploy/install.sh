#!/usr/bin/env bash
#
# install.sh — установка systemd-юнитов «Тендерного монитора» на Ubuntu.
#
# Постоянные сервисы:
#   • tender.service      — веб-панель (uvicorn :8000)
#   • tg_tender.service   — обработчик Telegram-кнопок
# Периодика (systemd-таймеры; альтернатива — cron, см. deploy/cron/):
#   • tender-zmo.timer    — ЗМО-цикл каждые 15 минут   (main.py --zmo)
#   • tender-daily.timer  — полный цикл раз в день      (main.py --once)
#
# Запуск на сервере из корня проекта:
#     sudo bash deploy/install.sh
#
# Переопределить автоопределённые значения:
#     sudo PROJECT_DIR=/opt/torg PYTHON=/opt/torg/.venv/bin/python RUN_USER=torg \
#          bash deploy/install.sh
#
# Периодику можно не ставить таймерами, а через cron — флаг:
#     sudo NO_TIMERS=1 bash deploy/install.sh      # поставит только сервисы
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"

RUN_USER="${RUN_USER:-${SUDO_USER:-$(id -un)}}"
if [[ "$RUN_USER" == "root" ]]; then
    echo "⚠️  Сервисы будут работать от root. Задай RUN_USER=<пользователь> для безопасности." >&2
fi
RUN_GROUP="$(id -gn "$RUN_USER")"

if [[ -z "${PYTHON:-}" ]]; then
    if   [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then PYTHON="$PROJECT_DIR/.venv/bin/python"
    elif [[ -x "$PROJECT_DIR/venv/bin/python"  ]]; then PYTHON="$PROJECT_DIR/venv/bin/python"
    else PYTHON="$(command -v python3)"; fi
fi
PYTHON_BIN_DIR="$(dirname "$PYTHON")"

# ── Проверки ────────────────────────────────────────────────────────────────
[[ -f "$PROJECT_DIR/main.py" ]]    || { echo "❌ Не нашёл main.py в $PROJECT_DIR"; exit 1; }
[[ -x "$PYTHON" ]]                 || { echo "❌ Python не исполняемый: $PYTHON"; exit 1; }
[[ -x "$PYTHON_BIN_DIR/uvicorn" ]] || echo "⚠️  Нет $PYTHON_BIN_DIR/uvicorn — установи зависимости: $PYTHON -m pip install -r requirements.txt"
[[ -f "$PROJECT_DIR/.env" ]]       || echo "⚠️  Нет $PROJECT_DIR/.env — задай DATABASE_URL, TELEGRAM_*, OPENROUTER_API_KEY, SOURCES_PROXY_URL перед стартом."
command -v flock >/dev/null        || { echo "❌ Нет flock (пакет util-linux)"; exit 1; }
id "$RUN_USER" >/dev/null 2>&1      || { echo "❌ Пользователь не существует: $RUN_USER"; exit 1; }

echo "── Параметры установки ──"
echo "  PROJECT_DIR : $PROJECT_DIR"
echo "  PYTHON      : $PYTHON"
echo "  RUN_USER    : $RUN_USER ($RUN_GROUP)"
[[ -n "${NO_TIMERS:-}" ]] && echo "  ПЕРИОДИКА   : пропущена (NO_TIMERS=1) — ставь через cron"
echo ""

UNIT_DST="/etc/systemd/system"
render() {
    sed -e "s#__PROJECT_DIR__#${PROJECT_DIR}#g" \
        -e "s#__PYTHON__#${PYTHON}#g" \
        -e "s#__PYTHON_BIN_DIR__#${PYTHON_BIN_DIR}#g" \
        -e "s#__USER__#${RUN_USER}#g" \
        -e "s#__GROUP__#${RUN_GROUP}#g" \
        "$1"
}

SERVICES=(tender.service tg_tender.service)
TIMERS_UNITS=(tender-zmo.service tender-zmo.timer tender-daily.service tender-daily.timer)

units=("${SERVICES[@]}")
[[ -z "${NO_TIMERS:-}" ]] && units+=("${TIMERS_UNITS[@]}")

for unit in "${units[@]}"; do
    src="$SCRIPT_DIR/systemd/$unit"
    [[ -f "$src" ]] || { echo "❌ Нет файла юнита: $src"; exit 1; }
    render "$src" | sudo tee "$UNIT_DST/$unit" >/dev/null
    echo "  ✓ установлен $UNIT_DST/$unit"
done

echo ""
echo "── Активация ──"
sudo systemctl daemon-reload
sudo systemctl enable --now tender.service tg_tender.service
if [[ -z "${NO_TIMERS:-}" ]]; then
    sudo systemctl enable --now tender-zmo.timer tender-daily.timer
fi

echo ""
echo "✅ Готово."
echo ""
echo "Статус сервисов:"
sudo systemctl --no-pager --lines=0 status tender.service tg_tender.service || true
if [[ -z "${NO_TIMERS:-}" ]]; then
    echo ""
    echo "Расписание:"
    sudo systemctl list-timers 'tender-*' --no-pager || true
fi
echo ""
echo "Полезное:"
echo "  Веб-панель                 : http://<сервер>:8000"
echo "  Логи веба                  : journalctl -u tender.service -f"
echo "  Логи кнопок                : journalctl -u tg_tender.service -f"
echo "  Прогнать ЗМО сейчас        : sudo systemctl start tender-zmo.service"
echo "  Логи ЗМО                   : journalctl -u tender-zmo.service -f"
echo "  Остановить периодику       : sudo systemctl disable --now tender-zmo.timer tender-daily.timer"
echo "  Остановить всё             : sudo systemctl disable --now tender.service tg_tender.service tender-zmo.timer tender-daily.timer"
