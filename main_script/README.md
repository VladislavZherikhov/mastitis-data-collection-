# Система съёмки камер в окнах + загрузка в S3 + автоматическая очистка диска

Единая, читаемая и надёжная система для Raspberry Pi.

## Что делает

1. **Съёмка** с термальной и IP-камеры — только во временных окнах (`WINDOW_1`, `WINDOW_2`, … из `.env`)
2. **Загрузка** снимков в Yandex Object Storage (S3)
3. **Очистка диска** `/mnt/hdd` после каждого окна — удаляет старые фото/видео, когда диск заполняется ≥ 80 %
4. **Логи** по дням (`logs/YYYY-MM-DD.log`) с выгрузкой в S3

## Структура

Корень проекта на Raspberry Pi: `/home/vlad_zherihov/disk_backup`
Рабочий диск: `/mnt/hdd`

```
disk_backup/
├── .env                 # секреты и настройки (не коммитить реальные ключи!)
├── .env.example         # шаблон конфига
├── requirements.txt     # все зависимости (общие)
├── config.py            # единая конфигурация (S3, камеры, окна, диск, логи)
├── main.py              # главный скрипт: окна → съёмка → S3 → очистка диска
├── test_system.py       # отдельный скрипт тестирования (конфиг, окна, диск)
├── ip_cam.py
├── thermal.py
├── s3_connection.py
├── time_windows.py      # временные окна (WINDOW_1, WINDOW_2, …)
├── log_storage.py       # логи по дням + выгрузка в S3
├── run_system.sh        # единый запуск
├── install_cron.sh      # установка cron
├── tasks.md             # задачи проекта
├── .gitignore
├── logs/                # ЕДИНАЯ папка логов: YYYY-MM-DD.log, backup.log, cron.log
└── disk_backup/         # скрипт работы с диском
    ├── backup.py        # очистка диска (проценты или МБ/ГБ)
    ├── test_backup.py   # тест очистки диска
    ├── disk_cleanup.py  # алиас backup.py
    ├── backup.sh / test_backup.sh / test.sh
```

## Быстрый старт на Raspberry Pi (по SSH)

```bash
# 1. Зайти на Raspberry Pi
ssh vlad_zherihov@IP_АДРЕС

# 2. Перейти в папку проекта
cd /home/vlad_zherihov/disk_backup

# 3. Создать и активировать venv
python3 -m venv venv
source venv/bin/activate

# 4. Установить зависимости
pip install --upgrade pip
pip install -r requirements.txt

# 5. Настроить .env
cp .env.example .env
nano .env   # вписать S3-ключи, URL камеры, временные окна, диск

# 6. Проверить диск
df -h /mnt/hdd

# 7. Провести тест системы (без удаления файлов)
python test_system.py --mount /mnt/hdd --dirs photos,videos \
  --threshold-pct 80 --target-pct 70 --dry-run

# 8. Запустить всю систему
./run_system.sh
```

## Тест системы (`test_system.py`)

Можно задать все переменные прямо в команде:

```bash
# Тест очистки диска: порог 80%, цель 70%, окна 06:00-08:00 и 18:00-20:00
python test_system.py --mount /mnt/hdd --dirs photos,videos \
  --threshold-pct 80 --target-pct 70 --window 06:00-08:00 --window 18:00-20:00

# Порог в МБ вместо процентов (например, свободного места осталось 5000 МБ)
python test_system.py --mount /mnt/hdd --threshold-mb 5000 --target-mb 4000

# Сухой прогон (без удаления файлов)
python test_system.py --mount /mnt/hdd --dry-run

# Без тестового диска — только конфиг и окна
python test_system.py
```

Параметры:
- `--mount` — точка монтирования тестового диска
- `--dirs` — каталоги со снимками (через запятую)
- `--threshold-pct / --threshold-mb` — порог заполнения (проценты или МБ)
- `--target-pct / --target-mb` — цель после очистки
- `--min-age` — минимальный возраст файла для удаления (дней)
- `--window` — временное окно ЧЧ:ММ-ЧЧ:ММ (можно несколько раз)
- `--interval` — интервал между снимками (сек)
- `--shots` — снимков за окно (0 = без лимита)
- `--dry-run` — только проход без удаления
- `--keep-files` — не удалять тестовую папку после теста

## Переменные окружения (`.env`)

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `WINDOW_1`, `WINDOW_2`, … | `06:00-08:00`, `18:00-20:00` | Временные окна съёмки (ЧЧ:ММ-ЧЧ:ММ) |
| `DISK_MOUNT_POINT` | `/mnt/hdd` | Точка монтирования диска |
| `MEDIA_DIRS` | `photos,videos` | Каталоги со старыми фото/видео |
| `DISK_USAGE_THRESHOLD` | `80` | Запуск очистки при заполнении (%) |
| `DISK_USAGE_TARGET` | `70` | Очищать до этого заполнения (%) |
| `MIN_FILE_AGE_DAYS` | `1` | Не трогать файлы моложе N дней |
| `LOG_FILE` | `logs/backup.log` | Лог очистки диска |
| `S3_BUCKET` | `mastitis-bucket` | S3 бакет |
| `IP_CAM_RTSP_URL` | — | URL IP-камеры (обязательно) |
| `INTERVAL_SEC` | `10` | Интервал между снимками (сек) |
| `SHOTS_PER_WINDOW` | `0` | Лимит снимков за окно (0 = без лимита) |
| `LOG_DIR` | `logs` | Папка с логами |
| `LOG_S3_PREFIX` | `logs` | Префикс для логов в S3 |

## Как работает интеграция

После завершения каждого временного окна `main.py` вызывает `disk_backup/backup.py` — функция `run_disk_cleanup()` проверяет заполнение диска `/mnt/hdd` и, если оно ≥ `DISK_USAGE_THRESHOLD` (по умолчанию 80 %), удаляет старые медиафайлы (старше `MIN_FILE_AGE_DAYS` дней) до достижения `DISK_USAGE_TARGET` (по умолчанию 70 %).

## Надёжность

- Все переменные имеют значения по умолчанию — программа не падает при отсутствии опциональных переменных
- Ошибка открытия камер → повтор через 30 сек
- Ошибка загрузки в S3 → пропуск снимка, продолжение работы
- Ошибка очистки диска → логируется, работа продолжается
- `SIGTERM`/`Ctrl+C` → корректная остановка с финальной выгрузкой лога
- `test_system.py` работает без камер и S3

## Cron (очистка каждые 5 минут)

```bash
./install_cron.sh
crontab -l
```

## Важно

- Не храните реальные `S3_KEY_ID` / `S3_SECRET` в коммите.
- Не ставьте `MIN_FILE_AGE_DAYS=0` на рабочей Raspberry Pi — удалятся свежие фото.

## ТЕСТИРОВАНИЕ на Raspberry Pi 4 (пошагово)

### Подготовка

```bash
# 1. Зайти на Raspberry Pi
ssh vlad_zherihov@IP_АДРЕС

# 2. Перейти в папку проекта
cd /home/vlad_zherihov/disk_backup

# 3. Установить всё одной командой (venv, зависимости, .env)
./setup.sh

# 4. Активировать venv
source venv/bin/activate

# 5. Отредактировать .env — вписать S3-ключи, URL камеры, окна
nano .env

# 6. Проверить, что диск /mnt/hdd смонтирован
df -h /mnt/hdd
```

---

### Тест 1: Съёмка работает только во временных окнах

Цель — убедиться, что камеры включаются только в `WINDOW_1`/`WINDOW_2` и выключаются вне окон.

```bash
# Установим тестовые окна на ближайшие 2–3 минуты (пример)
WINDOW_1=$(date -d '+1 minute' +%H:%M)-$(date -d '+2 minutes' +%H:%M)
WINDOW_2=""

# Запустить систему
./run_system.sh
```

**Что проверить в логах (`logs/YYYY-MM-DD.log`):**
- До начала окна: `Вне окна работы, следующее начало в ...`
- Внутри окна: `Окно работы до HH:MM, интервал 10 с` → снимки → `загружено N пакетов`
- После конца окна: `Окно завершено: загружено N пакетов из N` → `Очистка диска завершена: ...`
- Вне окна снова: `Вне окна работы, следующее начало в ...`

Съёмка не должна начаться до `WINDOW_1` и не должна продолжаться после его конца.

---

### Тест 2: Достижение порога заполненности диска и очистка

Цель — убедиться, что при заполнении диска ≥ 80 % срабатывает удаление старых файлов до 70 %.

```bash
# Сухой прогон (без удаления) — проверить логику
python test_system.py --mount /mnt/hdd --dirs photos,videos \
  --threshold-pct 80 --target-pct 70 --dry-run

# Реальный прогон с удалением (в тестовой папке .test_cleanup)
python test_system.py --mount /mnt/hdd --dirs photos,videos \
  --threshold-pct 80 --target-pct 70 --min-age 1
```

Альтернативно — порог в МБ:

```bash
python test_system.py --mount /mnt/hdd --dirs photos,videos \
  --threshold-mb 1000 --target-mb 500
```

**Что проверить в логах (`logs/backup.log`):**
- `Disk check: mount=/mnt/hdd used=... (XX%), threshold=80%, target=70%`
- Если порог достигнут: `Threshold reached: 80%` → `Deleted ...jpg (size=...)`
- После очистки: `Cleanup finished: deleted=N files, freed=N bytes, used now=XX%`

---

### Тест 3: Полный цикл (окна + S3 + очистка диска)

```bash
source venv/bin/activate
./run_system.sh
```

Система отработает все окна подряд, после каждого — очистит диск.

---

### Проверка всех логов

Вся система пишет в одну папку `logs/`:

| Файл | Что пишется |
|------|-------------|
| `logs/YYYY-MM-DD.log` | Съёмка, загрузка в S3, начало/конец окон (log_storage.py) |
| `logs/backup.log` | Проверка диска, удаление файлов, статистика очистки (backup.py) |
| `logs/cron.log` | Запуски очистки по cron (install_cron.sh) |

**Команды для просмотра:**

```bash
# Все логи за сегодня
ls -la logs/

# Лог съёмки за сегодня
tail -f logs/$(date +%F).log

# Лог очистки диска
tail -f logs/backup.log

# Лог cron
tail -f logs/cron.log

# Поиск ошибок во всех логах
grep -i "error\|exception\|fail" logs/*.log
```

**Признак, что система работает корректно:**
- В `YYYY-MM-DD.log`: строки `Окно работы до`, `загружено N пакетов`, `Окно завершено`
- В `backup.log`: строки `Disk check`, `Cleanup finished`, `deleted=N files`
- Отсутствие строк `ошибка конфигурации`, `скрипт упал`, `CRITICAL`

---

### Cron (автоматическая очистка каждые 5 минут)

```bash
./install_cron.sh
crontab -l
```

Логи cron пишутся в `logs/cron.log`.

