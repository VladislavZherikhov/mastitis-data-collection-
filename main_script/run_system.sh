#!/usr/bin/env bash
# Единый запуск системы: съёмка в окнах → загрузка в S3 → очистка диска /mnt/hdd.
# Все ключевые переменные берутся из .env (в той же папке).
set -euo pipefail

cd "$(dirname "$0")"

# --- Загружаем переменные из .env в окружение оболочки ---
if [[ -f .env ]]; then
    set -a
    . .env
    set +a
else
    echo "WARNING: .env не найден, будут использованы значения по умолчанию" >&2
fi

VENV="venv"
PYTHON=""

# Поиск Python из venv, если есть
if [[ -x "$VENV/bin/python" ]]; then
    PYTHON="$VENV/bin/python"
elif command -v python3 &>/dev/null; then
    PYTHON="python3"
elif command -v python &>/dev/null; then
    PYTHON="python"
else
    echo "ERROR: python3 не найден" >&2
    exit 1
fi

echo "=== Система: съёмка в окнах + S3 + очистка диска ==="
echo "Python: $PYTHON"
echo "Рабочая папка: $(pwd)"
echo "Рабочий диск: ${DISK_MOUNT_POINT:-/mnt/hdd}"

echo ""
echo "--- Запуск main.py (съёмка в окнах WINDOW_1, WINDOW_2, ...) ---"
"$PYTHON" main.py

echo ""
echo "--- Проверка очистки диска (disk_backup/backup.py) ---"
"$PYTHON" disk_backup/backup.py --dry-run

echo ""
echo "=== Система завершена ==="
