"""Подробные логи: стартовая сводка (версии, настройки, состояние Pi), статистика окна, маскировка паролей."""
import logging
import os
import platform
import re
import socket
import threading
from statistics import mean

log = logging.getLogger("main")


def mask_url(url: str) -> str:
    """rtsp://admin:pass@host/...?user=admin&password=pass -> пароли заменены на ***."""
    url = re.sub(r"(//[^:/@]+:)[^@/]*@", r"\1***@", url)
    return re.sub(r"(?i)(password|pass|pwd)=[^&]*", r"\1=***", url)


def _version(module_name: str) -> str:
    try:
        module = __import__(module_name)
        return str(getattr(module, "__version__", "?"))
    except Exception:
        return "не установлен"


def startup_report(config, windows, drive, health: dict, clock_synced) -> list[str]:
    """Строки для лога при запуске: что за машина, какие версии и какие настройки (без секретов)."""
    lines = [
        f"хост {socket.gethostname()}, pid {os.getpid()}, папка проекта {config.PROJECT_DIR}",
        f"версии: Python {platform.python_version()}, OpenCV {_version('cv2')}, numpy {_version('numpy')}, "
        f"boto3 {_version('boto3')}; ОС {platform.platform()}",
        f"S3: {config.S3_ENDPOINT}, бакет {config.S3_BUCKET}, пакеты в {config.S3_PREFIX}/, "
        f"логи в {config.LOG_S3_PREFIX}/ каждые {config.LOG_UPLOAD_INTERVAL_SEC:.0f} с",
        f"камеры: термо {config.THERMAL_DEVICE} ({config.THERMAL_WIDTH}x{config.THERMAL_HEIGHT}), "
        f"IP {mask_url(os.environ.get('IP_CAM_RTSP_URL', '(не задан)'))}",
        f"съёмка: интервал {config.INTERVAL_SEC:.0f} с, окна {', '.join(map(str, windows))}",
    ]
    if drive is None:
        lines.append("диск: выключен (DRIVE_MOUNT пустой) - пакеты только в S3")
    else:
        mounted = drive.is_mounted()
        lines.append(
            f"диск: {drive.mount} ({'смонтирован, ' + drive.usage_str() if mounted else 'НЕ смонтирован'}), "
            f"пакеты в {drive.data_dir}, очистка с «{drive.threshold}» до «{drive.target}», "
            f"не выгруженные моложе {drive.min_age_days:g} дн. не удаляются, аварийный минимум "
            f"{drive.min_free / 2**30:.1f} ГБ")
    lines.append(
        f"защита: очередь загрузки до {config.MAX_PENDING_UPLOADS} пакетов, сторож {config.WATCHDOG_TIMEOUT_SEC:.0f} с, "
        f"ожидание времени до {config.CLOCK_SYNC_WAIT_SEC:.0f} с; уровень логов {config.LOG_LEVEL}")
    pi = []
    if "cpu_temp_c" in health:
        pi.append(f"температура CPU {health['cpu_temp_c']} °C")
    if "throttled" in health:
        flags = ", ".join(health["throttled_flags"]) or "питание и температура в норме"
        pi.append(f"get_throttled={health['throttled']} ({flags})")
    pi.append({True: "время синхронизировано", False: "время НЕ синхронизировано",
               None: "синхронизацию времени проверить нельзя"}[clock_synced])
    lines.append("Pi: " + "; ".join(pi))
    return lines


class WindowStats:
    """Статистика окна для итоговой строки: время съёмки, загрузки, объём."""

    def __init__(self):
        self._lock = threading.Lock()
        self.capture_ms: list[float] = []
        self.save_ms: list[float] = []
        self.upload_s: list[float] = []
        self.bytes_uploaded = 0
        self.reopens = 0

    def add(self, *, capture_ms: float | None = None, save_ms: float | None = None,
            upload_s: float | None = None, nbytes: int = 0) -> None:
        with self._lock:
            if capture_ms is not None:
                self.capture_ms.append(capture_ms)
            if save_ms is not None:
                self.save_ms.append(save_ms)
            if upload_s is not None:
                self.upload_s.append(upload_s)
                self.bytes_uploaded += nbytes

    def summary(self) -> str:
        with self._lock:
            def stat(values, fmt):
                return f"среднее {fmt(mean(values))}, макс {fmt(max(values))}" if values else "нет данных"
            ms = lambda v: f"{v:.0f} мс"  # noqa: E731
            sec = lambda v: f"{v:.1f} с"  # noqa: E731
            return (f"съёмка: {stat(self.capture_ms, ms)}; запись на диск: {stat(self.save_ms, ms)}; "
                    f"загрузка пакета: {stat(self.upload_s, sec)}; загружено {self.bytes_uploaded / 2**20:.1f} МБ; "
                    f"переоткрытий камер: {self.reopens}")
