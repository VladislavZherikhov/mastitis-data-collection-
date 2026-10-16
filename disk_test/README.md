# Система «Сева»: съёмка камер + загрузка в S3 + автоматическая очистка диска

Единая система для Raspberry Pi:

- **`main_script/`** — съёмка с термальной и IP-камеры, загрузка снимков в Yandex Object Storage (S3).
- **`backup.py`** — мониторинг заполнения локального диска; при достижении порога (по умолчанию 80 %) удаляет самые старые фото/видео до целевого уровня (70 %).
- **`test_backup.py`** — тест: создаёт скриншотоподобные файлы, забивает диск, проверяет очистку, удаляет следы теста.
- **`run_system.sh** — единый запуск whole системы.

## Структура

```
disk_test/                          # корень проекта «Се­ва»
├── ТАСКИ_СЕВА.md                   # список задач интеграции
├── config.env.example              # единый конфиг (скопировать в .env)
├── requirements.txt                # все зависимости
├── run_system.sh                   # единый запуск: съёмка → S3 → очистка
├── backup.py                       # очистка диска (проценты или MB/KB/GB)
├── test_backup.py                  # тест очистки
├── backup.sh / test_backup.sh      # обёртки для запуска
├── install_cron.sh                 # установка cron на Raspberry Pi
├── logs/                           # логи (создаётся автоматически)
│   ├── main.log
│   ├── backup.log
│   ├── test_backup.log
│   └── cron.log
├── main_script/                    # съёмка камер → S3
│   ├── main.py
│   ├── config.py
│   ├── ip_cam.py
│   ├── thermal.py
│   ├── s3_connection.py
│   ├── .env                        # секреты (не коммитить!)
│   └── requirements.txt
└── venv/                           # виртуальное окружение (создать перед запуском)
```

## Быстрый старт на Raspberry Pi (по SSH)

```bash
# 1. Зайти на Raspberry Pi
ssh pi@IP_АДРЕС

# 2. Перейти в папку проекта
cd /home/vlad_zherihov/disk_test

# 3. Создать и активировать venv
python3 -m venv venv
source venv/bin/activate

# 4. Установить зависимости
pip install --upgrade pip
pip install -r requirements.txt
pip install -r main_script/requirements.txt

# 5. Настроить .env
cp config.env.example .env
nano .env          # впишите свои S3-ключи, URL камеры, точку монтирования диска

# 6. Проверить, что диск смонтирован
df -h /mnt/hdd

# 7. Запустить сухую проверку очистки (без удаления файлов)
DISK_MOUNT_POINT=/mnt/hdd MEDIA_DIRS=photos,videos \
  python3 backup.py --dry-run

# 8. Запустить всю систему целиком
./run_system.sh
```

## Параметры очистки диска

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `DISK_MOUNT_POINT` | `/mnt/disk` | Точка монтирования жёсткого диска |
| `MEDIA_DIRS` | `photos,videos` | Каталоги со старыми фото/видео (через запятую) |
| `DISK_USAGE_THRESHOLD` | `80` | Запуск очистки при заполнении диска (проценты) |
| `DISK_USAGE_TARGET` | `70` | Очищать до этого заполнения (проценты) |
| `DISK_FREE_THRESHOLD` | — | Альтернатива: порог свободного места (например, `100GB`) |
| `DISK_FREE_TARGET` | — | Альтернатива: цель свободного места (например, `120GB`) |
| `MIN_FILE_AGE_DAYS` | `1` | Не трогать файлы моложе этого возраста (дней) |
| `LOG_FILE` | `./logs/backup.log` | Путь к логу очистки |

Проценты и размеры можно комбинировать. Если заданы оба режима, приоритет у `DISK_FREE_*`.
Поддерживаемые единицы: `B`, `KB`, `MB`, `GB`, `TB`, `PB` (и `KiB`, `MiB`, …).

## Тестирование на Windows (безопасно)

Выделите второй диск (например, `E:\`, 3 GB) и не трогайте системный диск `C:\`.

```powershell
cd D:\code\disk_test
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Сухая проверка
$env:DISK_MOUNT_POINT="E:\"
$env:MEDIA_DIRS="disk_test\photos"
$env:MIN_FILE_AGE_DAYS="0"
python backup.py --dry-run

# Полный тест очистки
python test_backup.py --mount "E:\" --dirs "disk_test\photos" --max-files 10 --shot-size-mb 50
```

После теста папка `.backup_test` удалится автоматически, а в `logs/test_backup.log` будет `RESULT: PASS`.

## Cron на Raspberry Pi (запуск очистки каждые 5 минут)

```bash
./install_cron.sh
crontab -l
```

Логи cron пишутся в `logs/cron.log`.

## Логи

Все компоненты пишут в папку `logs/` с временными метками:
- каждая загрузка в S3;
- каждая проверка диска;
- каждый удалённый файл;
- ошибки и предупреждения.

## Важно

- Не храните реальные `S3_KEY_ID` / `S3_SECRET` в коммите. `.env` должен быть в `.gitignore`.
- Не ставьте `MIN_FILE_AGE_DAYS=0` на рабочей Raspberry Pi — это удалит и свежие фото.
- На 512 GB диске используйте процентный режим (`80 %` / `70 %`) — он надёжнее, чем байтовый.
