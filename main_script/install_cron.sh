#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# --- Загружаем .env, чтобы cron использовал те же значения ---
if [[ -f .env ]]; then
    set -a
    . .env
    set +a
else
    echo "WARNING: .env не найден, будут использованы значения по умолчанию" >&2
fi

SCRIPT_DIR="$(pwd)"
PYTHON_BIN="$(command -v python3)"

# Передаём переменные окружения прямо в команду cron
CRON_ENV="DISK_MOUNT_POINT=${DISK_MOUNT_POINT:-/mnt/hdd} MEDIA_DIRS=${MEDIA_DIRS:-photos,videos} MIN_FILE_AGE_DAYS=${MIN_FILE_AGE_DAYS:-1} DISK_USAGE_THRESHOLD=${DISK_USAGE_THRESHOLD:-80} DISK_USAGE_TARGET=${DISK_USAGE_TARGET:-70} LOG_FILE=${LOG_FILE:-logs/backup.log}"
CRON_LINE="*/5 * * * * ${CRON_ENV} ${PYTHON_BIN} ${SCRIPT_DIR}/disk_backup/backup.py >> ${SCRIPT_DIR}/logs/cron.log 2>&1"

( crontab -l 2>/dev/null | grep -v "backup.py" || true; echo "${CRON_LINE}" ) | crontab -
echo "Установлен cron job:"
echo "${CRON_LINE}"
