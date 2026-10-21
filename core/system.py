"""Системные проверки Raspberry Pi: время, питание/температура, защита от второго экземпляра."""
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from core.logmarks import IMPORTANT

log = logging.getLogger("system")

# время раньше этого года - точно не синхронизировано (у Pi нет часов с батарейкой)
MIN_SANE_YEAR = 2025


# --- второй экземпляр ---
def single_instance_lock(path: Path):
    """Не даёт запустить второй экземпляр скрипта (например, вручную, когда уже работает сервис).
    Возвращает открытый файл блокировки - его нужно держать до выхода. На Windows не работает (нет fcntl)."""
    try:
        import fcntl
    except ImportError:
        return None
    f = open(path, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        print(f"Скрипт уже запущен (занят {path}). Второй экземпляр не запускаю.", file=sys.stderr)
        raise SystemExit(3)
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


# --- время ---
def clock_synced() -> bool | None:
    """True/False - синхронизировано ли время по NTP; None - проверить нельзя (не Linux/нет timedatectl)."""
    if datetime.now().year < MIN_SANE_YEAR:
        return False
    if not shutil.which("timedatectl"):
        return None
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out == "yes"


def wait_for_clock_sync(max_wait_sec: float, beat: Callable[[], None] = lambda: None) -> bool:
    """Ждёт синхронизации времени (не дольше max_wait_sec). Без неё неверны окна работы, даты папок,
    а S3 отклоняет запросы при расхождении часов больше чем на 15 минут."""
    synced = clock_synced()
    if synced is not False:
        return True
    log.warning("время на Pi ещё не синхронизировано (сейчас %s), жду до %.0f с...",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), max_wait_sec)
    deadline = time.monotonic() + max_wait_sec
    while time.monotonic() < deadline:
        time.sleep(5)
        beat()
        if clock_synced() is not False:
            log.info("время синхронизировано: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), extra=IMPORTANT)
            return True
    log.error("время так и не синхронизировалось (нет интернета?) - работаю с часами Pi: %s. "
              "Окна и даты папок могут быть неверны, пока время не синхронизируется",
              datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return False


# --- питание и температура ---
_THROTTLE_FLAGS = {
    0: "сейчас недостаточно питания",
    1: "сейчас снижена частота",
    2: "сейчас ограничение частоты",
    3: "сейчас перегрев (мягкий предел)",
    16: "с момента загрузки было недостаточно питания",
    18: "с момента загрузки было ограничение частоты",
    19: "с момента загрузки был перегрев",
}


def health() -> dict:
    """Температура CPU и флаги питания/перегрева (vcgencmd get_throttled). Пусто, если не Pi."""
    data: dict = {}
    try:
        data["cpu_temp_c"] = round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
    except (OSError, ValueError):
        pass
    if shutil.which("vcgencmd"):
        try:
            out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5).stdout
            value = int(out.strip().split("=")[1], 16)
            data["throttled"] = hex(value)
            data["throttled_flags"] = [text for bit, text in _THROTTLE_FLAGS.items() if value & (1 << bit)]
            data["undervoltage_now"] = bool(value & 1)
        except (OSError, subprocess.SubprocessError, IndexError, ValueError):
            pass
    return data
