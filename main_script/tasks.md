# Таски — интеграция систем  (mastitis-data-collection + disk-cleanup)

Цель: единая, читаемая и надёжная система на Raspberry Pi — съёмка в временных окнах → S3 → очистка диска.

Рабочая директория на Raspberry Pi: `/home/vlad_zherihov/disk_backup`
Рабочий диск: `/mnt/hdd`

## Статус: в работе

### 1. Объединённая структура проекта (корень = disk_backup/)
- [x] `main.py` — основной цикл: окна работы → съёмка → S3 → очистка диска
- [x] `config.py` — единая точка конфигурации (S3, камеры, окна, диск, логи)
- [x] `ip_cam.py`, `thermal.py`, `s3_connection.py` — камеры и S3
- [x] `time_windows.py` (коллега) — временные окна WINDOW_1, WINDOW_2, …
- [x] `log_storage.py` (коллега) — логи по дням + выгрузка в S3
- [x] `disk_backup/backup.py` — очистка диска (проценты или МБ/ГБ)
- [x] `disk_backup/test_backup.py` — тест очистки диска
- [x] `test_system.py` — отдельный скрипт тестирования всей системы
- [x] `run_system.sh` — единая точка запуска
- [x] Общий `requirements.txt` (boto3, numpy, opencv-python, python-dotenv, Pillow)
- [x] Единый `.env` + `.env.example`

### 2. Временные окна съёмки
- [x] Окна задаются в .env: WINDOW_1=06:00-08:00, WINDOW_2=18:00-20:00, …
- [x] Окно может переходить через полночь (например 22:00-02:00)
- [x] Снимки идут каждые INTERVAL_SEC секунд внутри окна
- [x] SHOTS_PER_WINDOW ограничивает количество снимков за окно (0 = без лимита)
- [x] Вне окон скрипт спит и ждёт начала следующего окна

### 3. Очистка диска (интегрирована в main.py)
- [x] После каждого окна вызывает `backup.cleanup()`
- [x] Очистка запускается только при заполнении ≥ DISK_USAGE_THRESHOLD (по умолчанию 80 %)
- [x] Не удаляет файлы моложе MIN_FILE_AGE_DAYS (по умолчанию 1 день)
- [x] Чистит до DISK_USAGE_TARGET (по умолчанию 70 %)
- [x] Порог и цель могут быть в процентах или байтах (МБ/ГБ)

### 4. Логирование
- [x] Логи по дням: logs/YYYY-MM-DD.log (log_storage.py)
- [x] Выгрузка логов прошлых дней в S3 + удаление с диска
- [x] Лог очистки диска: logs/backup.log

### 5. Тестирование (100% надёжность)
- [x] `test_system.py` — тест конфига, окон, очистки диска (проценты и МБ)
- [x] `--dry-run` — проход без удаления файлов
- [x] `--mount`, `--dirs`, `--threshold-pct/--threshold-mb`, `--window`, `--interval`, `--shots`
- [x] Работает без камер и S3
- [x] Чистит за собой тестовые файлы

### 6. Развёртывание на Raspberry Pi по SSH
- [ ] `ssh vlad_zherihov@IP_АДРЕС`
- [ ] `cd /home/vlad_zherihov/disk_backup`
- [ ] `python3 -m venv venv && source venv/bin/activate`
- [ ] `pip install -r requirements.txt`
- [ ] `cp .env.example .env` — вписать ключи, URL камеры, окна
- [ ] `df -h /mnt/hdd` — проверить диск
- [ ] `python test_system.py --mount /mnt/hdd --dirs photos,videos --threshold-pct 80 --dry-run`
- [ ] `./run_system.sh` — полный цикл
- [ ] `./install_cron.sh` — очистка каждые 5 минут

### 7. Надёжность («по пусту не должно падать»)
- [x] Все переменные окружения имеют значения по умолчанию
- [x] Отсутствие окон → выход с понятной ошибкой конфигурации
- [x] Ошибка открытия камер → повтор через CAMERA_RETRY_SEC
- [x] Ошибка S3/загрузки → пропуск снимка, продолжение работы
- [x] SIGTERM → корректная остановка с финальной выгрузкой лога
- [x] Отсутствие тестового диска в test_system → пропуск теста диска, не ошибка

---
Образец репозитория: https://github.com/VladislavZherikhov/mastitis-data-collection
