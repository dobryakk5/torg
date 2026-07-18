#!/usr/bin/env bash
#
# install-systemd.sh — установка systemd-таймеров «Торга» на Ubuntu.
#
#   • torg-zmo.timer   — ЗМО-цикл каждые 15 минут (main.py --zmo)
#   • torg-daily.timer — полный цикл раз в день   (main.py --once)
#
# Оба сервиса делят один flock-лок (/tmp/torg-pipeline.lock), поэтому дневной
# полный прогон и 15-минутный ЗМО-тик не наслаиваются на БД.
#
# Запуск на сервере (из корня проекта или из deploy/):
#     sudo bash deploy/install-systemd.sh
#
# Переопределить автоопределённые значения:
#     sudo PROJECT_DIR=/opt/torg PYTHON=/opt/torg/.venv/bin/python \
#          RUN_USER=torg bash deploy/install-systemd.sh
#
set -euo pipefail

# ── Определяем параметры ────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"

# Пользователь, от которого крутить прогоны: тот, кто вызвал sudo (не root).
RUN_USER="${RUN_USER:-${SUDO_USER:-$(id -un)}}"
if [[ "$RUN_USER" == "root" ]]; then
    echo "⚠️  Сервисы будут работать от root. Задай RUN_USER=<пользователь> для безопасности." >&2
fi
RUN_GROUP="$(id -gn "$RUN_USER")"

# Python: приоритет — переменная PYTHON, затем venv в проекте, затем системный.
if [[ -z "${PYTHON:-}" ]]; then
    if   [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then PYTHON="$PROJECT_DIR/.venv/bin/python"
    elif [[ -x "$PROJECT_DIR/venv/bin/python"  ]]; then PYTHON="$PROJECT_DIR/venv/bin/python"
    else PYTHON="$(command -v python3)"; fi
fi
PYTHON_BIN_DIR="$(dirname "$PYTHON")"

# ── Проверки ────────────────────────────────────────────────────────────────
[[ -f "$PROJECT_DIR/main.py" ]] || { echo "❌ Не нашёл main.py в $PROJECT_DIR"; exit 1; }
[[ -x "$PYTHON" ]]             || { echo "❌ Python не исполняемый: $PYTHON"; exit 1; }
[[ -f "$PROJECT_DIR/.env" ]]   || echo "⚠️  Нет $PROJECT_DIR/.env — задай секреты (DATABASE_URL, TELEGRAM_*, SOURCES_PROXY_URL) перед стартом."
command -v flock >/dev/null    || { echo "❌ Нет flock (пакет util-linux)"; exit 1; }
id "$RUN_USER" >/dev/null 2>&1 || { echo "❌ Пользователь не существует: $RUN_USER"; exit 1; }

echo "── Параметры установки ──"
echo "  PROJECT_DIR : $PROJECT_DIR"
echo "  PYTHON      : $PYTHON"
echo "  RUN_USER    : $RUN_USER ($RUN_GROUP)"
echo ""

# ── Подстановка плейсхолдеров и установка юнитов ────────────────────────────
UNIT_DST="/etc/systemd/system"
render() {
    sed -e "s#__PROJECT_DIR__#${PROJECT_DIR}#g" \
        -e "s#__PYTHON__#${PYTHON}#g" \
        -e "s#__PYTHON_BIN_DIR__#${PYTHON_BIN_DIR}#g" \
        -e "s#__USER__#${RUN_USER}#g" \
        -e "s#__GROUP__#${RUN_GROUP}#g" \
        "$1"
}

for unit in torg-zmo.service torg-zmo.timer torg-daily.service torg-daily.timer; do
    src="$SCRIPT_DIR/systemd/$unit"
    [[ -f "$src" ]] || { echo "❌ Нет файла юнита: $src"; exit 1; }
    render "$src" | sudo tee "$UNIT_DST/$unit" >/dev/null
    echo "  ✓ установлен $UNIT_DST/$unit"
done

# ── Активация ───────────────────────────────────────────────────────────────
echo ""
echo "── Активация ──"
sudo systemctl daemon-reload
sudo systemctl enable --now torg-zmo.timer torg-daily.timer
echo ""
echo "✅ Готово. Расписание:"
sudo systemctl list-timers 'torg-*' --no-pager || true
echo ""
echo "Полезное:"
echo "  Прогнать ЗМО прямо сейчас : sudo systemctl start torg-zmo.service"
echo "  Логи ЗМО в реальном времени: journalctl -u torg-zmo.service -f"
echo "  Логи дневного прогона      : journalctl -u torg-daily.service -f"
echo "  Остановить всё             : sudo systemctl disable --now torg-zmo.timer torg-daily.timer"
