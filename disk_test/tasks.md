# Таски — интеграция систем mastitis-data-collection + disk-cleanup

Цель: заставить съёмку (main_script) и очистку диска (backup.py) работать как **единая система** на Raspberry Pi по SSH.

## Статус: в работе

### 1. Объединить папки в один проект
- [ ] `main_script/` — съёмка с термальной и IP-камеры, загрузка в Yandex S3.
- [ ] `backup.py`, `test_backup.py` — мониторинг заполнения диска и удаление старых фото/видео.
- [ ] Единая точка запуска `run_system.sh`.
- [ ] Единый `config.env.example` с настройками обеих частей.

### 2. Интегрировать очистку в основной цикл съёмки
- [ ] После завершения всех загрузок в `main.py` вызывать `backup.cleanup(...)`.
- [ ] Очистка запускается только если свободного места меньше порога (по умолчанию 80 % заполнено).
- [ ] Не удалять свежие файлы: `MIN_FILE_AGE_DAYS=1` (и больше).
- [ ] Логирование очистки пишется в тот же `logs/`, что и основной лог.

### 3. Единая конфигурация
- [ ] `.env` содержит и S3-параметры, и параметры диска (`DISK_MOUNT_POINT`, `MEDIA_DIRS`, `DISK_USAGE_THRESHOLD` и т.д.).
- [ ] `config.py` (main_script) подгружает переменные диска или они читаются в `backup.py` из окружения.

### 4. Тестирование на Windows (безопасно)
- [ ] Выделить второй диск (например, `E:\`, 3 GB).
- [ ] `python backup.py --mount "E:\" --dirs "disk_test\photos" --dry-run`.
- [ ] `python test_backup.py --mount "E:\" --dirs "disk_test\photos" --max-files 10 --shot-size-mb 50`.
- [ ] Убедиться, что после теста тестовые файлы удалены и в логе есть `RESULT: PASS`.

### 5. Развёртывание на Raspberry Pi по SSH
- [ ] Создать `venv`: `python3 -m venv venv` и `source venv/bin/activate`.
- [ ] `pip install -r requirements.txt` (объединённые зависимости: boto3, opencv-python, numpy, Pillow).
- [ ] Проверить монтирование диска: `lsblk -f`, `df -h /mnt/disk`.
- [ ] `DISK_MOUNT_POINT=/mnt/disk MEDIA_DIRS=photos,videos python3 backup.py --dry-run`.
- [ ] `./run_system.sh` — полный цикл съёмка → загрузка → очистка.
- [ ] Cron: `*/5 * * * * /home/pi/disk_test/venv/bin/python /home/pi/disk_test/backup.py >> logs/cron.log 2>&1`.

### 6. Логирование
- [ ] Единая папка `logs/` для `main.log`, `backup.log`, `test_backup.log`, `cron.log`.
- [ ] В каждой записи лога: дата, уровень, сообщение, имя файла (при удалении).

---