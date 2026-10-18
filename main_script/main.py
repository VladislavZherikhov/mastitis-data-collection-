"""Снимки с термальной + IP-камеры -> S3 -> очистка локального диска.

Работает только во временных окнах (WINDOW_1, WINDOW_2, … из .env).
После каждого окна запускается очистка диска (backup.py).
"""
import logging
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import config

# Модуль очистки диска лежит в папке disk_backup/
_DISK_BACKUP_DIR = Path(__file__).resolve().parent / "disk_backup"
if str(_DISK_BACKUP_DIR) not in sys.path:
    sys.path.insert(0, str(_DISK_BACKUP_DIR))

from backup import Config as DiskConfig, LimitSpec
import backup as backup_mod
from ip_cam import IPCamera
from log_storage import LogUploader, setup_logging
from s3_connection import S3Storage
from thermal import ThermalCamera
from time_windows import active_until, next_start

log = logging.getLogger("main")

# Когда нет активного окна — спим кусками, чтобы заметить скачок системных часов (у Pi нет RTC)
IDLE_CHECK_SEC = 60
# Пауза перед повторной попыткой открыть камеры
CAMERA_RETRY_SEC = 30


# ---------------------------------------------------------------------
# Съёмка и загрузка
# ---------------------------------------------------------------------
def capture_both(thermal: ThermalCamera, ip_cam: IPCamera, pool: ThreadPoolExecutor):
    """Снимает обе камеры одновременно."""
    f_thermal = pool.submit(thermal.capture)
    f_ip = pool.submit(ip_cam.capture)
    return f_thermal.result(), f_ip.result()


def build_files(shot, ip_frame, stamp: str) -> dict[str, tuple[bytes, str]]:
    return {
        f"thermal_{stamp}.npy": (shot.temp_npy(), "application/octet-stream"),
        f"thermal_{stamp}.png": (shot.heatmap_png(), "image/png"),
        f"ip_cam_{stamp}.jpg": (IPCamera.encode_jpg(ip_frame), "image/jpeg"),
    }


def upload_job(s3: S3Storage, folder: str, files, idx: int, t_min: float, t_max: float) -> bool:
    try:
        uri = s3.upload_shot(folder, files)
        log.info("#%d T=%.1f..%.1f -> %s", idx, t_min, t_max, uri)
        return True
    except Exception:
        log.exception("#%d не удалось загрузить %s", idx, folder)
        return False


# ---------------------------------------------------------------------
# Очистка диска (единая система )
# ---------------------------------------------------------------------
def run_disk_cleanup() -> None:
    """Запускает backup.cleanup() после окна съёмки."""
    try:
        media_dirs = [config.DISK_MOUNT_POINT / d for d in config.MEDIA_DIRS]
        stats = backup_mod.cleanup(
            DiskConfig(
                mount=config.DISK_MOUNT_POINT,
                media_dirs=media_dirs,
                threshold=LimitSpec("percent", config.DISK_USAGE_THRESHOLD, "env DISK_USAGE_THRESHOLD"),
                target=LimitSpec("percent", config.DISK_USAGE_TARGET, "env DISK_USAGE_TARGET"),
                min_age_days=config.MIN_FILE_AGE_DAYS,
                log_file=config.LOG_FILE,
            )
        )
        log.info("Очистка диска завершена: %s", stats)
    except Exception:
        log.exception("Ошибка при очистке диска")



# ---------------------------------------------------------------------
# Одно окно работы
# ---------------------------------------------------------------------
def run_window(s3: S3Storage, upload_pool: ThreadPoolExecutor, until: datetime) -> None:
    """Снимает пакеты каждые INTERVAL_SEC до момента until. Камеры открываются на время окна."""
    log.info("Окно работы до %s, интервал %.0f с", f"{until:%H:%M}", config.INTERVAL_SEC)
    shots = 0
    uploads = []

    while datetime.now() < until:
        try:
            with ThermalCamera(config.THERMAL_DEVICE, config.THERMAL_WIDTH, config.THERMAL_HEIGHT) as thermal, \
                    IPCamera(config.ip_cam_url()) as ip_cam, \
                    ThreadPoolExecutor(max_workers=2, thread_name_prefix="capture") as capture_pool:

                next_shot = time.monotonic()
                while datetime.now() < until:
                    # ровный интервал между снимками
                    time.sleep(max(0.0, next_shot - time.monotonic()))
                    next_shot += config.INTERVAL_SEC
                    if datetime.now() >= until:
                        break

                    # лимит снимков за окно
                    if config.SHOTS_PER_WINDOW > 0 and shots >= config.SHOTS_PER_WINDOW:
                        break

                    shots += 1
                    folder = time.strftime("%Y-%m-%d_%H-%M-%S")
                    try:
                        shot, ip_frame = capture_both(thermal, ip_cam, capture_pool)
                        files = build_files(shot, ip_frame, folder)
                    except Exception:
                        log.exception("#%d снимок пропущен (нужны оба кадра)", shots)
                        continue

                    uploads.append(
                        upload_pool.submit(
                            upload_job, s3, folder, files, shots,
                            float(shot.temp.min()), float(shot.temp.max()),
                        )
                    )
        except Exception as exc:
            # Если IP-камера не ответила после всех попыток внутри IPCamera.start() —
            # пропускаем этот цикл съёмки, пишем в лог и продолжаем работу.
            if "IP-камера" in str(exc) or "подключиться" in str(exc):
                log.error(
                    "IP-камера недоступна после нескольких попыток, пропускаем этот цикл съёмки: %s",
                    exc,
                )
            else:
                log.exception("Не удалось открыть камеры, повтор через %d с", CAMERA_RETRY_SEC)
            remaining = (until - datetime.now()).total_seconds()
            time.sleep(max(0.0, min(CAMERA_RETRY_SEC, remaining)))

    ok = sum(f.result() for f in uploads)
    log.info("Окно завершено: загружено %d пакетов из %d", ok, shots)


# ---------------------------------------------------------------------
# Главный цикл
# ---------------------------------------------------------------------
class Terminated(BaseException):
    """Скрипт остановлен сигналом SIGTERM."""


def _on_sigterm(signum, frame):
    raise Terminated()


def run(s3: S3Storage, log_uploader: LogUploader) -> None:
    windows = config.time_windows()
    log.info("Окна работы: %s", ", ".join(map(str, windows)))

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="upload") as upload_pool:
        while True:
            now = datetime.now()
            until = active_until(windows, now)

            if until is not None:
                run_window(s3, upload_pool, until)
                log_uploader.upload_current()   # после окна лог в облаке актуален
                run_disk_cleanup()              # очистка диска после окна
                continue

            start = next_start(windows, now)
            log.info("Вне окна работы, следующее начало в %s", f"{start:%Y-%m-%d %H:%M}")
            while (left := (start - datetime.now()).total_seconds()) > 0:
                time.sleep(min(left, IDLE_CHECK_SEC))
                if active_until(windows, datetime.now()) is not None:
                    break  # часы перевели (NTP) — окно уже началось


def main() -> None:
    log_handler = setup_logging(config.LOG_DIR)
    signal.signal(signal.SIGTERM, _on_sigterm)

    log_uploader = None
    reason = "неизвестно"
    try:
        s3 = S3Storage()
        s3.check()                               # проверка доступа к бакету
        log_uploader = LogUploader(s3, log_handler, config.LOG_S3_PREFIX)
        log_uploader.start()                     # фоновая выгрузка логов прошлых дней
        run(s3, log_uploader)
    except KeyboardInterrupt:
        reason = "остановлен вручную (Ctrl+C)"
    except Terminated:
        reason = "остановлен системой (SIGTERM)"
    except SystemExit as e:
        reason = f"ошибка конфигурации: {e.code}"
        raise
    except Exception:
        log.exception("скрипт упал")
        reason = "скрипт упал"
        raise
    finally:
        log.info("остановка: %s", reason)
        if log_uploader is not None:
            log_uploader.stop()                  # финальная выгрузка лога текущего дня


if __name__ == "__main__":
    main()
