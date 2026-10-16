#!/usr/bin/env bash
# Единый запуск системы «Сева»: съёмка камер → загрузка в S3 → очистка локального диска.
set -euo pipefail

cd "$(dirname "$0")"

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

echo "=== Система Сева: съёмка + загрузка S3 + очистка диска ==="
echo "Python: $PYTHON"
echo "Рабочая папка: $(pwd)"

echo ""
echo "--- Шаг 1: съёмка и загрузка в S3 (main_script/main.py) ---"
DISK_MOUNT_POINT="${DISK_MOUNT_POINT:-/mnt/disk}" \
MEDIA_DIRS="${MEDIA_DIRS:-photos,videos}" \
MIN_FILE_AGE_DAYS="${MIN_FILE_AGE_DAYS:-1}" \
DISK_USAGE_THRESHOLD="${DISK_USAGE_THRESHOLD:-80}" \
DISK_USAGE_TARGET="${DISK_USAGE_TARGET:-70}" \
LOG_FILE="${LOG_FILE:-./logs/backup.log}" \
"$PYTHON" main_script/main.py

echo ""
echo "--- Шаг 2: проверка очистки диска (backup.py) ---"
"$PYTHON" backup.py --dry-run

echo ""
echo "=== Система Сева завершена ==="
