"""Единая точка конфигурации проекта.
Все настройки берутся из .env (рядом с этим файлом) или из окружения.
"""
import os
from pathlib import Path

from time_windows import parse_windows


def _load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# .env ищем рядом с config.py (корень проекта: /home/vlad_zherihov/disk_backup)
_load_env(Path(__file__).resolve().parent / ".env")


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Не задана переменная окружения {name} (см. .env.example)")
    return value


# =====================================================================
# S3 (Yandex Object Storage)
# =====================================================================
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "https://storage.yandexcloud.net")
S3_REGION = os.environ.get("S3_REGION", "ru-central1")
S3_BUCKET = os.environ.get("S3_BUCKET", "mastitis-bucket")
S3_PREFIX = os.environ.get("S3_PREFIX", "test-grab")


def s3_credentials() -> tuple[str, str]:
    return _required("S3_KEY_ID"), _required("S3_SECRET")


# =====================================================================
# Камеры
# =====================================================================
def ip_cam_url() -> str:
    return _required("IP_CAM_RTSP_URL")

THERMAL_DEVICE = os.environ.get("THERMAL_DEVICE", "/dev/video0")
THERMAL_WIDTH = int(os.environ.get("THERMAL_WIDTH", "256"))
THERMAL_HEIGHT = int(os.environ.get("THERMAL_HEIGHT", "192"))


# =====================================================================
# Съёмка
# =====================================================================
INTERVAL_SEC = float(os.environ.get("INTERVAL_SEC", "10"))
# Максимум снимков за одно окно (0 = без лимита, снимаем пока окно активно)
SHOTS_PER_WINDOW = int(os.environ.get("SHOTS_PER_WINDOW", "0"))


# =====================================================================
# Временные окна работы (WINDOW_1, WINDOW_2, … = ЧЧ:ММ-ЧЧ:ММ)
# Примеры: WINDOW_1=06:00-08:00, WINDOW_2=18:00-20:00
# =====================================================================
def time_windows():
    """Возвращает список окон, отсортированных. Выход с ошибкой, если окон нет."""
    return parse_windows(dict(os.environ))


# =====================================================================
# Диск для очистки (backup.py) — рабочий диск на Raspberry Pi: /mnt/hdd
# =====================================================================
DISK_MOUNT_POINT = Path(os.environ.get("DISK_MOUNT_POINT", "/mnt/hdd"))
MEDIA_DIRS = [d.strip() for d in os.environ.get("MEDIA_DIRS", "photos,videos").split(",") if d.strip()]
DISK_USAGE_THRESHOLD = float(os.environ.get("DISK_USAGE_THRESHOLD", "80"))   # процент заполнения, при котором начинаем чистить
DISK_USAGE_TARGET = float(os.environ.get("DISK_USAGE_TARGET", "70"))          # до какого процента чистим
MIN_FILE_AGE_DAYS = int(os.environ.get("MIN_FILE_AGE_DAYS", "1"))             # не трогать файлы моложе N дней
LOG_FILE = Path(os.environ.get("LOG_FILE", "logs/backup.log"))


# =====================================================================
# Логи (log_storage.py) — файлы по дням + выгрузка в S3
# =====================================================================
LOG_DIR = Path(os.environ.get("LOG_DIR") or Path(__file__).resolve().parent / "logs")
LOG_S3_PREFIX = os.environ.get("LOG_S3_PREFIX", "logs")


# =====================================================================
# Обратная совместимость (старые имена переменных, можно удалить позже)
# =====================================================================
MORNING_START = os.environ.get("MORNING_START", "06:00")
MORNING_END = os.environ.get("MORNING_END", "08:00")
EVENING_START = os.environ.get("EVENING_START", "18:00")
EVENING_END = os.environ.get("EVENING_END", "20:00")
