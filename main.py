"""Пакеты синхронных снимков с термальной и IP-камеры -> S3, только во временных окнах из .env."""
import logging
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime

from cameras.ip_cam import IPCamera
from cameras.thermal import ThermalCamera
from core import config
from core.log_storage import LogUploader, setup_logging
from core.time_windows import active_until, next_start
from storage.local_drive import LocalDrive
from storage.s3 import S3Storage

log = logging.getLogger("main")

# вне окна спим кусками, чтобы заметить скачок системных часов (у Pi нет RTC, время приходит по NTP)
IDLE_CHECK_SEC = 60
# пауза перед повторной попыткой открыть камеры, если они не открылись
CAMERA_RETRY_SEC = 30


def capture_both(thermal: ThermalCamera, ip_cam: IPCamera, pool: ThreadPoolExecutor):
    """Снимает обе камеры одновременно. Возвращает (термо-снимок, кадр IP-камеры)."""
    f_thermal = pool.submit(thermal.capture)
    f_ip = pool.submit(ip_cam.capture)
    return f_thermal.result(), f_ip.result()


def build_files(shot, ip_frame, stamp: str) -> dict[str, tuple[bytes, str]]:
    """stamp (дата_время снимка) добавляется к имени каждого файла: thermal_<stamp>.npy и т.д."""
    return {
        f"thermal_{stamp}.npy": (shot.temp_npy(), "application/octet-stream"),
        f"thermal_{stamp}.png": (shot.heatmap_png(), "image/png"),
        f"ip_cam_{stamp}.jpg": (IPCamera.encode_jpg(ip_frame), "image/jpeg"),
    }


def upload_job(s3: S3Storage, drive: LocalDrive | None, folder: str, files, idx: int,
               t_min: float, t_max: float) -> bool:
    """drive передаётся, только если пакет сохранён на диск: тогда после загрузки ставится метка,
    а при ошибке пакет будет догружен с диска вне окна."""
    try:
        uri = s3.upload_shot(folder, files)
        log.info("#%d T=%.1f..%.1f -> %s", idx, t_min, t_max, uri)
    except Exception:
        log.exception("#%d не удалось загрузить %s", idx, folder)
        if drive is not None:
            drive.has_backlog = True
        return False
    if drive is not None:
        try:
            drive.mark_uploaded(folder)
        except Exception:
            log.exception("не удалось поставить метку загрузки для %s", folder)
    return True


def save_local(drive: LocalDrive | None, folder: str, files) -> bool:
    """True - пакет сохранён на диск."""
    if drive is None:
        return False
    try:
        return drive.save_shot(folder, files) is not None
    except Exception:
        log.exception("не удалось сохранить %s на диск", folder)
        return False


def run_window(s3: S3Storage, drive: LocalDrive | None, upload_pool: ThreadPoolExecutor, until: datetime) -> None:
    """Снимает пакеты каждые INTERVAL_SEC секунд до момента until.
    Камеры открываются только на время окна."""
    log.info("окно работы до %s, интервал %.0f с", f"{until:%H:%M}", config.INTERVAL_SEC)
    shots = 0
    uploads = []

    if drive is not None and not drive.is_mounted():
        log.error("диск %s не смонтирован - в это окно пакеты сохраняются только в S3", drive.mount)
        drive = None
    if drive is not None:
        drive.remove_incomplete()

    while datetime.now() < until:
        try:
            with ThermalCamera(config.THERMAL_DEVICE, config.THERMAL_WIDTH, config.THERMAL_HEIGHT) as thermal, \
                    IPCamera(config.ip_cam_url()) as ip_cam, \
                    ThreadPoolExecutor(max_workers=2, thread_name_prefix="capture") as capture_pool:

                next_shot = time.monotonic()
                while datetime.now() < until:
                    # держим ровный интервал между снимками независимо от времени съёмки/загрузки
                    time.sleep(max(0.0, next_shot - time.monotonic()))
                    next_shot += config.INTERVAL_SEC
                    if datetime.now() >= until:
                        break

                    shots += 1
                    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
                    folder = f"{stamp[:10]}/{stamp}"  # папка дня / папка пакета - и в S3, и на диске
                    try:
                        shot, ip_frame = capture_both(thermal, ip_cam, capture_pool)
                        files = build_files(shot, ip_frame, stamp)
                    except Exception:
                        log.exception("#%d снимок пропущен (нужны оба кадра)", shots)
                        continue

                    saved = save_local(drive, folder, files)
                    # загрузка идёт в фоне, чтобы не сбивать интервал между снимками
                    uploads.append(
                        upload_pool.submit(
                            upload_job, s3, drive if saved else None, folder, files, shots,
                            float(shot.temp.min()), float(shot.temp.max()),
                        )
                    )
        except Exception:
            log.exception("не удалось открыть камеры, повтор через %d с", CAMERA_RETRY_SEC)
            time.sleep(max(0.0, min(CAMERA_RETRY_SEC, (until - datetime.now()).total_seconds())))

    ok = sum(f.result() for f in uploads)
    log.info("окно завершено: загружено %d пакетов из %d", ok, shots)


class Terminated(BaseException):
    """Скрипт остановлен сигналом SIGTERM (например, systemctl stop)."""


def _on_sigterm(signum, frame):
    raise Terminated()


def run(s3: S3Storage, log_uploader: LogUploader) -> None:
    windows = config.time_windows()
    drive = config.local_drive()
    log.info("окна работы: %s", ", ".join(map(str, windows)))

    # drive запускает фоновую очистку диска; закрывается после пула загрузок (порядок with - обратный)
    with drive or nullcontext(), \
            ThreadPoolExecutor(max_workers=2, thread_name_prefix="upload") as upload_pool:
        while True:
            now = datetime.now()
            until = active_until(windows, now)
            if until is not None:
                run_window(s3, drive, upload_pool, until)
                log_uploader.upload_current()  # после окна лог в облаке актуален
                continue

            start = next_start(windows, now)
            log.info("вне окна работы, следующее начало в %s", f"{start:%Y-%m-%d %H:%M}")
            while (left := (start - datetime.now()).total_seconds()) > 0:
                if drive is not None:
                    # вне окна догружаем в S3 пакеты с диска, которые не ушли сразу (не было сети)
                    drive.upload_pending(s3, until=start)
                    left = (start - datetime.now()).total_seconds()
                time.sleep(max(0.0, min(left, IDLE_CHECK_SEC)))
                if active_until(windows, datetime.now()) is not None:
                    break  # часы перевели (NTP) - окно уже началось


def main() -> None:
    log_handler = setup_logging(config.LOG_DIR)
    signal.signal(signal.SIGTERM, _on_sigterm)

    log_uploader = None
    reason = "неизвестно"
    try:
        s3 = S3Storage()
        s3.check()
        # выгружает логи прошлых дней (сразу и после каждой полуночи)
        log_uploader = LogUploader(s3, log_handler, config.LOG_S3_PREFIX)
        log_uploader.start()
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
            log_uploader.stop()  # финальная выгрузка лога текущего дня


if __name__ == "__main__":
    main()
