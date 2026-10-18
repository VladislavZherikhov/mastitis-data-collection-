#!/usr/bin/env bash
# Единая установка и проверка проекта: зависимости + .env
# Рабочая директория на Raspberry Pi: /home/vlad_zherihov/disk_backup
set -euo pipefail

cd "$(dirname "$0")"

echo "============================================================"
echo "  Установка системы съёмки + S3 + очистки диска"
echo "============================================================"
echo ""

# 1. Python venv
if [[ ! -x "venv/bin/python" ]]; then
    echo "[1/4] Создаём виртуальное окружение venv..."
    python3 -m venv venv
else
    echo "[1/4] Виртуальное окружение venv уже есть"
fi

# 2. Зависимости
echo "[2/4] Устанавливаем зависимости (requirements.txt)..."
./venv/bin/python -m pip install --upgrade pip
./venv/bin/python -m pip install -r requirements.txt

# 3. .env
echo "[3/4] Проверяем файл .env..."
if [[ ! -f .env ]]; then
    echo "   .env не найден — копирую из .env.example"
    cp .env.example .env
    echo "   >>> ОТРЕДАКТИРУЙТЕ .env: впишите S3-ключи, URL камеры, временные окна <<<"
else
    echo "   .env уже существует"
fi

# 4. Права на скрипты
echo "[4/4] Даём права на запуск скриптам..."
chmod +x run_system.sh install_cron.sh

echo ""
echo "============================================================"
echo "  Установка завершена."
echo "  Далее:"
echo "    1. nano .env          # впишите свои данные"
echo "    2. source venv/bin/activate"
echo "    3. df -h /mnt/hdd     # проверьте, что диск смонтирован"
echo "    4. ./run_system.sh    # запуск системы"
echo "============================================================"
