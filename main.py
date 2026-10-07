"""Серия синхронных снимков с термальной и IP-камеры -> папка в S3."""
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import config
from ip_cam import IPCamera
from s3_connection import S3Storage
from thermal import ThermalCamera

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("main")


def capture_both(thermal: ThermalCamera, ip_cam: IPCamera, pool: ThreadPoolExecutor):
    """Снимает обе камеры одновременно. Возвращает (термо-снимок, кадр IP-камеры)."""
    f_thermal = pool.submit(thermal.capture)
    f_ip = pool.submit(ip_cam.capture)
    return f_thermal.result(), f_ip.result()


def build_files(shot, ip_frame) -> dict[str, tuple[bytes, str]]:
    return {
        "thermal.npy": (shot.temp_npy(), "application/octet-stream"),
        "thermal.png": (shot.heatmap_png(), "image/png"),
        "ip_cam.jpg": (IPCamera.encode_jpg(ip_frame), "image/jpeg"),
    }


def upload_job(s3: S3Storage, folder: str, files, idx: int, total: int, t_min: float, t_max: float) -> bool:
    try:
        uri = s3.upload_shot(folder, files)
        log.info("%d/%d T=%.1f..%.1f -> %s", idx, total, t_min, t_max, uri)
        return True
    except Exception:
        log.exception("%d/%d не удалось загрузить %s", idx, total, folder)
        return False


def main() -> None:
    s3 = S3Storage()
    s3.check()

    with ThermalCamera(config.THERMAL_DEVICE, config.THERMAL_WIDTH, config.THERMAL_HEIGHT) as thermal, \
            IPCamera(config.ip_cam_url()) as ip_cam, \
            ThreadPoolExecutor(max_workers=2, thread_name_prefix="capture") as capture_pool, \
            ThreadPoolExecutor(max_workers=2, thread_name_prefix="upload") as upload_pool:

        uploads = []
        next_shot = time.monotonic()

        for i in range(1, config.SHOTS + 1):
            # держим ровный интервал между снимками независимо от времени съёмки/загрузки
            time.sleep(max(0.0, next_shot - time.monotonic()))
            next_shot += config.INTERVAL_SEC

            folder = time.strftime("%Y-%m-%d_%H-%M-%S")
            try:
                shot, ip_frame = capture_both(thermal, ip_cam, capture_pool)
                files = build_files(shot, ip_frame)
            except Exception:
                log.exception("%d/%d снимок пропущен (нужны оба кадра)", i, config.SHOTS)
                continue

            # загрузка идёт в фоне, чтобы не сбивать интервал между снимками
            uploads.append(
                upload_pool.submit(
                    upload_job, s3, folder, files, i, config.SHOTS,
                    float(shot.temp.min()), float(shot.temp.max()),
                )
            )

        ok = sum(f.result() for f in uploads)
        log.info("готово: загружено %d из %d", ok, config.SHOTS)


if __name__ == "__main__":
    main()
