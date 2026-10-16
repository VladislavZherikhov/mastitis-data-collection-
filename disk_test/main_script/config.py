"""Единая точка конфигурации. Секреты берутся из файла .env рядом с этим файлом или из окружения."""
import os
from pathlib import Path


def _load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# .env ищем рядом с config.py, а не в текущей папке запуска
_load_env(Path(__file__).resolve().parent / ".env")


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
SHOTS = int(os.environ.get("SHOTS", 5))
INTERVAL_SEC = float(os.environ.get("INTERVAL_SEC", 3))
