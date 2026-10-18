#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
SCRIPT_DIR="$(pwd)"
PYTHON_BIN="$(command -v python3)"
CRON_LINE="*/5 * * * * ${PYTHON_BIN} ${SCRIPT_DIR}/backup.py >> ${SCRIPT_DIR}/logs/cron.log 2>&1"
( crontab -l 2>/dev/null | grep -v "backup.py" || true; echo "${CRON_LINE}" ) | crontab -
echo "Installed cron job:"
echo "${CRON_LINE}"
