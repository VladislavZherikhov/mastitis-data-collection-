"""Единая точка конфигурации. Секреты берутся из файла .env в корне проекта или из окружения."""
import os
from pathlib import Path

from core.time_windows import parse_windows


def _load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# корень проекта (где лежат main.py и .env), а не текущая папка запуска
PROJECT_DIR = Path(__file__).resolve().parent.parent
_load_env(PROJECT_DIR / ".env")


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Не задана переменная окружения {name} (см. .env.example)")
    return value


# --- S3 (Yandex Object Storage) ---
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "https://storage.yandexcloud.net")
S3_REGION = os.environ.get("S3_REGION", "ru-central1")
S3_BUCKET = os.environ.get("S3_BUCKET", "mastitis-bucket")
S3_PREFIX = os.environ.get("S3_PREFIX", "test-grab")


def s3_credentials() -> tuple[str, str]:
    return _required("S3_KEY_ID"), _required("S3_SECRET")


# --- IP-камера ---
def ip_cam_url() -> str:
    return _required("IP_CAM_RTSP_URL")


# --- Термальная камера ---
THERMAL_DEVICE = os.environ.get("THERMAL_DEVICE", "/dev/video0")
THERMAL_WIDTH = 256
THERMAL_HEIGHT = 192

# --- Режим съёмки ---
# пауза между пакетами снимков внутри окна работы
INTERVAL_SEC = float(os.environ.get("INTERVAL_SEC", 10))


def time_windows():
    """Окна работы из WINDOW_1, WINDOW_2, ... (формат ЧЧ:ММ-ЧЧ:ММ)."""
    return parse_windows(dict(os.environ))


# --- Локальный диск (storage/local_drive.py) ---
# точка монтирования диска; пусто - не сохранять пакеты на диск
DRIVE_MOUNT = os.environ.get("DRIVE_MOUNT", "/mnt/hdd").strip()
# пакеты лежат в <DRIVE_MOUNT>/<S3_PREFIX>/<дата_время>/ - как в облаке
DRIVE_MIN_AGE_DAYS = float(os.environ.get("DRIVE_MIN_AGE_DAYS", 1))
# аварийный минимум свободного места: при меньшем пакеты пишутся только в S3
DRIVE_MIN_FREE = os.environ.get("DRIVE_MIN_FREE", "1GB")


def local_drive():
    """LocalDrive или None, если сохранение на диск выключено (DRIVE_MOUNT пустой)."""
    if not DRIVE_MOUNT:
        return None
    from storage.local_drive import LocalDrive, limits_from_env, parse_size
    threshold, target = limits_from_env(dict(os.environ))
    mount = Path(DRIVE_MOUNT)
    return LocalDrive(mount, mount / S3_PREFIX.strip("/"), threshold, target, DRIVE_MIN_AGE_DAYS,
                      min_free=parse_size(DRIVE_MIN_FREE, "DRIVE_MIN_FREE"))


# --- Логи ---
# файлы логов по дням на Pi; по умолчанию папка logs в корне проекта
LOG_DIR = Path(os.environ.get("LOG_DIR") or PROJECT_DIR / "logs")
# папка логов в бакете: <LOG_S3_PREFIX>/<hostname>/<дата>.log
LOG_S3_PREFIX = os.environ.get("LOG_S3_PREFIX", "logs")
