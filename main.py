"""Пакеты синхронных снимков с термальной и IP-камеры -> S3, только во временных окнах из .env.

Скрипт рассчитан на работу без присмотра: сбои камер, сети, S3 и диска обрабатываются,
а от зависаний защищает сторож (core/watchdog.py). Все случаи описаны в documentation/cornercases.md.
"""
import faulthandler
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime

from cameras.ip_cam import IPCamera
from cameras.thermal import ThermalCamera
from core import config, system
from core.diagnostics import WindowStats, startup_report
from core.log_storage import LogShipper, setup_logging
from core.logmarks import IMPORTANT
from core.problems import Problems
from core.status import RuntimeStatus
from core.time_windows import active_until, next_start
from core.watchdog import Watchdog
from storage.local_drive import LocalDrive
from storage.s3 import S3Storage, classify_error

log = logging.getLogger("main")

# вне окна спим кусками, чтобы заметить скачок системных часов (у Pi нет RTC, время приходит по NTP)
IDLE_CHECK_SEC = 60
# пауза перед повторной попыткой открыть камеры, если они не открылись
CAMERA_RETRY_SEC = 30
# сколько ждать кадр от камер, прежде чем считать, что камера зависла
CAPTURE_TIMEOUT_SEC = 8
# после стольких неудачных снимков подряд камеры переоткрываются
REOPEN_AFTER_FAILURES = 3
# сколько ждать запись пакета на диск, прежде чем считать, что диск завис
DRIVE_WRITE_TIMEOUT_SEC = 15
# пауза перед новой попыткой писать на диск после ошибки / зависания
DRIVE_RETRY_SEC = 60
DRIVE_HANG_RETRY_SEC = 300
# сколько зависших потоков (камера/диск не вернули управление) терпим, прежде чем перезапустить процесс
MAX_STUCK_THREADS = 3
# коды выхода: 0 - штатная остановка, 1 - падение/перезапуск, 2 - ошибка конфигурации
EXIT_OK, EXIT_RESTART, EXIT_CONFIG = 0, 1, 2

problems = Problems(log)


class RestartRequired(Exception):
    """Восстановиться внутри процесса нельзя (зависшие потоки) - нужен перезапуск процесса (systemd)."""


class Terminated(BaseException):
    """Скрипт остановлен сигналом SIGTERM (например, systemctl stop)."""


def _on_sigterm(signum, frame):
    raise Terminated()


class StuckThreads:
    """Считает потоки, которые зависли в камере/диске и брошены. Их нельзя убить в Python,
    поэтому, если их накопилось много, просим перезапуск процесса."""

    def __init__(self, limit: int):
        self.limit = limit
        self._futures = []

    def add(self, future, what: str) -> None:
        self._futures = [f for f in self._futures if not f.done()] + [future]
        if len(self._futures) >= self.limit:
            raise RestartRequired(f"зависших потоков {len(self._futures)} ({what}) - нужен перезапуск процесса")


stuck = StuckThreads(MAX_STUCK_THREADS)


# ---------------------------------------------------------------------------
# Съёмка
# ---------------------------------------------------------------------------
def build_files(shot, ip_frame, stamp: str) -> dict[str, tuple[bytes, str]]:
    """stamp (дата_время снимка) добавляется к имени каждого файла: thermal_<stamp>.npy и т.д."""
    return {
        f"thermal_{stamp}.npy": (shot.temp_npy(), "application/octet-stream"),
        f"thermal_{stamp}.png": (shot.heatmap_png(), "image/png"),
        f"ip_cam_{stamp}.jpg": (IPCamera.encode_jpg(ip_frame), "image/jpeg"),
    }


@dataclass
class Package:
    """Один пакет снимков и всё, что о нём нужно знать для лога."""
    idx: int
    stamp: str
    folder: str
    files: dict[str, tuple[bytes, str]]
    t_min: float
    t_max: float
    capture_ms: float
    encode_ms: float
    save_ms: float | None = None  # None - на диск не сохранялся

    @property
    def nbytes(self) -> int:
        return sum(len(data) for data, _ in self.files.values())

    def sizes(self) -> str:
        """npy 192 / png 310 / jpg 845 КБ"""
        parts = [f"{name.rsplit('.', 1)[-1]} {len(data) / 1024:.0f}" for name, (data, _) in self.files.items()]
        return " / ".join(parts) + " КБ"


class CameraSession:
    """Обе камеры + пул съёмки. Снимок ограничен по времени: зависшая камера не вешает весь скрипт."""

    def __init__(self):
        self.thermal: ThermalCamera | None = None
        self.ip_cam: IPCamera | None = None
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="capture")
        self._thermal_stuck = False

    def open(self) -> None:
        try:
            self.thermal = ThermalCamera(config.THERMAL_DEVICE, config.THERMAL_WIDTH, config.THERMAL_HEIGHT)
            self.thermal.open()
            self.ip_cam = IPCamera(config.ip_cam_url())
            self.ip_cam.start()
        except BaseException:
            self.close()
            raise

    def capture(self):
        f_thermal = self.pool.submit(self.thermal.capture)
        f_ip = self.pool.submit(self.ip_cam.capture)
        try:
            shot = f_thermal.result(timeout=CAPTURE_TIMEOUT_SEC)
        except FutureTimeout:
            self._thermal_stuck = True
            stuck.add(f_thermal, "термокамера")
            raise TimeoutError(f"термокамера не вернула кадр за {CAPTURE_TIMEOUT_SEC} с (зависла)") from None
        try:
            frame = f_ip.result(timeout=CAPTURE_TIMEOUT_SEC)
        except FutureTimeout:
            stuck.add(f_ip, "IP-камера")
            raise TimeoutError(f"IP-камера не вернула кадр за {CAPTURE_TIMEOUT_SEC} с") from None
        return shot, frame

    def close(self) -> None:
        # не ждём зависшие потоки съёмки
        self.pool.shutdown(wait=False, cancel_futures=True)
        if self.thermal is not None and not self._thermal_stuck:
            # если термокамера висит в read(), освобождать её из другого потока опасно - бросаем
            self.thermal.close()
        if self.ip_cam is not None:
            self.ip_cam.stop()


# ---------------------------------------------------------------------------
# Загрузка в S3 и запись на диск
# ---------------------------------------------------------------------------
class UploadQueue:
    """Загрузка пакетов в фоне с ограничением очереди. Если сеть не успевает за съёмкой,
    новые пакеты не копятся в памяти (иначе Pi упрётся в оперативку): они остаются на диске
    и уйдут догрузкой вне окна."""

    def __init__(self, s3: S3Storage, status: RuntimeStatus, limit: int):
        self.s3 = s3
        self.status = status
        self.stats = WindowStats()  # заменяется в начале каждого окна
        self.limit = limit
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="upload")
        self._lock = threading.Lock()
        self.pending = 0

    def submit(self, pkg: Package, drive: LocalDrive | None) -> bool:
        with self._lock:
            if self.pending >= self.limit:
                return False
            self.pending += 1
        self.pool.submit(self._job, pkg, drive, self.stats)
        return True

    def _job(self, pkg: Package, drive: LocalDrive | None, stats: WindowStats) -> None:
        """drive передаётся, только если пакет сохранён на диск: тогда после загрузки ставится метка,
        а при ошибке пакет будет догружен с диска вне окна."""
        try:
            started = time.monotonic()
            try:
                uri = self.s3.upload_shot(pkg.folder, pkg.files)
            except Exception as e:
                kind, text = classify_error(e)
                where = "пакет есть на диске, уйдёт позже" if drive is not None else "пакета нет на диске - потерян"
                problems.report(f"s3:{kind}", f"не удалось загрузить пакеты в S3: {text} ({where})",
                                level=logging.WARNING if kind == "network" else logging.ERROR,
                                exc_info=kind not in ("network",))
                log.debug("#%d %s не загружен за %.1f с: %s", pkg.idx, pkg.stamp, time.monotonic() - started, text)
                self.status.count("upload_failed")
                if drive is not None:
                    drive.has_backlog = True
                return
            upload_s = time.monotonic() - started
            stats.add(upload_s=upload_s, nbytes=pkg.nbytes)
            problems.resolved("s3:", "загрузка в S3 снова работает")
            self.status.count("uploaded")
            self.status.mark("s3_last_ok")
            log.info("#%d %s: T=%.1f..%.1f °C | %s | съёмка %.0f мс, кодирование %.0f мс, диск %s, "
                     "загрузка %.1f с | в очереди %d -> %s",
                     pkg.idx, pkg.stamp, pkg.t_min, pkg.t_max, pkg.sizes(), pkg.capture_ms, pkg.encode_ms,
                     f"{pkg.save_ms:.0f} мс" if pkg.save_ms is not None else "нет", upload_s, self.pending - 1, uri)
            if drive is not None:
                try:
                    drive.mark_uploaded(pkg.folder)
                except Exception:
                    log.exception("не удалось поставить метку загрузки для %s", pkg.folder)
        except Exception:
            log.exception("неожиданная ошибка загрузки %s", pkg.folder)
        finally:
            with self._lock:
                self.pending -= 1

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)


class DriveWriter:
    """Запись пакетов на диск с таймаутом. Отвалился диск / только чтение / завис - съёмка не страдает:
    пакеты идут только в S3, а запись на диск пробуется снова через паузу."""

    def __init__(self, drive: LocalDrive | None):
        self.drive = drive
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="drive-write")
        self._skip_until = 0.0

    def prepare(self) -> None:
        """В начале окна: убрать недописанные пакеты прошлых сбоев."""
        if self.drive is None or not self.drive.is_mounted():
            return
        try:
            self.drive.remove_incomplete()
        except OSError as e:
            problems.report("drive", f"диск недоступен: {e} - пакеты сохраняются только в S3")

    def save(self, folder: str, files) -> float | None:
        """Сохраняет пакет на диск. Возвращает время записи в мс или None, если не сохранён."""
        if self.drive is None or time.monotonic() < self._skip_until:
            return None
        started = time.monotonic()
        future = self._pool.submit(self.drive.save_shot, folder, files)
        try:
            saved = future.result(timeout=DRIVE_WRITE_TIMEOUT_SEC)
        except FutureTimeout:
            problems.report("drive", f"запись на диск зависла (дольше {DRIVE_WRITE_TIMEOUT_SEC} с) - пакеты "
                                     f"сохраняются только в S3, новая попытка через {DRIVE_HANG_RETRY_SEC // 60} мин")
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="drive-write")
            self._skip_until = time.monotonic() + DRIVE_HANG_RETRY_SEC
            stuck.add(future, "запись на диск")
            return None
        except Exception as e:
            problems.report("drive", f"диск недоступен ({type(e).__name__}: {e}) - пакеты сохраняются только в S3, "
                                     f"новая попытка через {DRIVE_RETRY_SEC} с", exc_info=True)
            self._skip_until = time.monotonic() + DRIVE_RETRY_SEC
            return None
        problems.resolved("drive", "диск снова доступен, пакеты сохраняются на диск")
        if saved is None:
            return None  # не хватило места (это логирует сам диск)
        return (time.monotonic() - started) * 1000

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# Окно работы
# ---------------------------------------------------------------------------
def run_window(uploads: UploadQueue, writer: DriveWriter, status: RuntimeStatus,
               watchdog: Watchdog, until: datetime) -> None:
    """Снимает пакеты каждые INTERVAL_SEC секунд до момента until.
    Камеры открываются только на время окна и переоткрываются, если начали сбоить."""
    status.window_started(until)
    stats = uploads.stats = WindowStats()
    drive = writer.drive
    log.info("окно работы до %s, интервал %.0f с, диск: %s, S3: последняя успешная загрузка %s",
             f"{until:%H:%M}", config.INTERVAL_SEC,
             "выключен" if drive is None else (drive.usage_str() if drive.is_mounted() else "НЕ смонтирован"),
             f"{status.events['s3_last_ok']:%Y-%m-%d %H:%M:%S}" if "s3_last_ok" in status.events else "ещё не было")
    writer.prepare()
    shots = deferred_logged = 0

    finished = False
    try:
        while datetime.now() < until:
            watchdog.beat("открытие камер")
            session = CameraSession()
            try:
                session.open()
            except RestartRequired:
                raise
            except Exception as e:
                session.close()
                problems.report("cameras", f"не удалось открыть камеры: {type(e).__name__}: {e} - повтор каждые "
                                           f"{CAMERA_RETRY_SEC} с", exc_info=True)
                time.sleep(max(0.0, min(CAMERA_RETRY_SEC, (until - datetime.now()).total_seconds())))
                continue
            problems.resolved("cameras", "камеры открыты")

            failures = 0
            try:
                next_shot = time.monotonic()
                while datetime.now() < until:
                    watchdog.beat("съёмка")
                    # держим ровный интервал между снимками независимо от времени съёмки/загрузки
                    time.sleep(max(0.0, next_shot - time.monotonic()))
                    next_shot = max(next_shot + config.INTERVAL_SEC, time.monotonic())
                    if datetime.now() >= until:
                        break

                    shots += 1
                    status.count("shots")
                    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
                    folder = f"{stamp[:10]}/{stamp}"  # папка дня / папка пакета - и в S3, и на диске
                    try:
                        t0 = time.monotonic()
                        shot, ip_frame = session.capture()
                        t1 = time.monotonic()
                        files = build_files(shot, ip_frame, stamp)
                        t2 = time.monotonic()
                    except RestartRequired:
                        raise
                    except Exception as e:
                        failures += 1
                        status.count("skipped")
                        log.debug("#%d %s пропущен (неудача %d подряд): %s: %s", shots, stamp, failures, type(e).__name__, e)
                        problems.report("capture", f"снимок пропущен (нужны оба кадра): {type(e).__name__}: {e}",
                                        exc_info=True)
                        if failures >= REOPEN_AFTER_FAILURES:
                            problems.report("reopen", f"{failures} неудачных снимков подряд - переоткрываю камеры",
                                            level=logging.WARNING)
                            status.mark("cameras_reopened")
                            stats.reopens += 1
                            break
                        continue
                    failures = 0
                    problems.resolved("capture", "снимки снова делаются")
                    problems.resolved("reopen", "камеры работают после переоткрытия")

                    pkg = Package(shots, stamp, folder, files, float(shot.temp.min()), float(shot.temp.max()),
                                  capture_ms=(t1 - t0) * 1000, encode_ms=(t2 - t1) * 1000)
                    pkg.save_ms = writer.save(folder, files)
                    saved = pkg.save_ms is not None
                    stats.add(capture_ms=pkg.capture_ms, save_ms=pkg.save_ms)
                    if saved:
                        status.count("saved_to_drive")
                    log.debug("#%d %s снят: %s, съёмка %.0f мс, кодирование %.0f мс, диск %s", shots, stamp, pkg.sizes(),
                              pkg.capture_ms, pkg.encode_ms, f"{pkg.save_ms:.0f} мс" if saved else "нет")
                    # загрузка идёт в фоне, чтобы не сбивать интервал между снимками
                    if not uploads.submit(pkg, writer.drive if saved else None):
                        status.count("upload_deferred")
                        log.debug("#%d %s: очередь загрузки полна - %s", shots, stamp,
                                  "ждёт на диске" if saved else "не будет загружен")
                        if saved and writer.drive is not None:
                            writer.drive.has_backlog = True
                        if not deferred_logged:
                            deferred_logged = 1
                            log.warning("сеть не успевает за съёмкой (в очереди загрузки %d пакетов) - новые пакеты "
                                        "%s", uploads.pending,
                                        "остаются на диске и уйдут в S3 вне окна" if saved
                                        else "не сохраняются на диск и теряются для облака")
            finally:
                session.close()
        finished = True
    finally:
        # итог пишется и тогда, когда окно прервано (Ctrl+C, SIGTERM, перезапуск, ошибка)
        w = status.window
        log.info("окно %s: снимков %d, пропущено %d, на диск %d, загружено %d, не загрузилось %d, "
                 "отложено %d, ещё в очереди %d", "завершено" if finished else "прервано", shots, w["skipped"],
                 w["saved_to_drive"], w["uploaded"], w["upload_failed"], w["upload_deferred"], uploads.pending,
                 extra=IMPORTANT)
        log.info("итог окна: %s", stats.summary(), extra=IMPORTANT)
        # после этих строк (вне окна) в лог идёт только важное
        status.window_finished()


# ---------------------------------------------------------------------------
# Основной цикл
# ---------------------------------------------------------------------------
def run(s3: S3Storage, status: RuntimeStatus, log_shipper: LogShipper, watchdog: Watchdog) -> None:
    windows = config.time_windows()
    drive = config.local_drive()
    for line in startup_report(config, windows, drive, system.health(), system.clock_synced()):
        log.info("старт: %s", line, extra=IMPORTANT)
    if drive is not None:
        status.add_provider("drive", lambda: {
            "mounted": drive.is_mounted(),
            "usage": drive.usage_str() if drive.is_mounted() else None,
            "has_backlog": drive.has_backlog,
        })

    uploads = UploadQueue(s3, status, config.MAX_PENDING_UPLOADS)
    writer = DriveWriter(drive)
    status.add_provider("upload_queue", lambda: {"pending": uploads.pending, "limit": uploads.limit})

    # drive запускает фоновую очистку диска
    with drive or nullcontext():
        try:
            while True:
                watchdog.beat("основной цикл")
                try:
                    now = datetime.now()
                    until = active_until(windows, now)
                    if until is not None:
                        run_window(uploads, writer, status, watchdog, until)
                        log_shipper.flush_now()  # логи окна - в облако сразу, не ждём интервала
                        continue

                    start = next_start(windows, now)
                    status.idle_until(start)
                    log.info("вне окна работы, следующее начало в %s", f"{start:%Y-%m-%d %H:%M}", extra=IMPORTANT)
                    while (left := (start - datetime.now()).total_seconds()) > 0:
                        watchdog.beat("ожидание окна")
                        if drive is not None:
                            # вне окна догружаем в S3 пакеты с диска, которые не ушли сразу (не было сети)
                            drive.upload_pending(s3, until=start, beat=lambda: watchdog.beat("догрузка с диска"))
                            left = (start - datetime.now()).total_seconds()
                        time.sleep(max(0.0, min(left, IDLE_CHECK_SEC)))
                        if active_until(windows, datetime.now()) is not None:
                            break  # часы перевели (NTP) - окно уже началось
                except (RestartRequired, SystemExit, KeyboardInterrupt, Terminated):
                    raise
                except Exception:
                    # что бы ни случилось - не падаем, а пишем в лог и продолжаем
                    status.window_finished()
                    log.exception("неожиданная ошибка в основном цикле, продолжаю через %d с", CAMERA_RETRY_SEC)
                    time.sleep(CAMERA_RETRY_SEC)
        finally:
            uploads.close()
            writer.close()


def _periodic_health(status: RuntimeStatus) -> None:
    """Раз в минуту (из потока сторожа): питание, перегрев, синхронизация времени."""
    h = system.health()
    status.add_provider("system", lambda: h)
    if h.get("undervoltage_now"):
        problems.report("power", "недостаточно питания Pi (under-voltage) - USB-камера и диск могут отваливаться",
                        level=logging.WARNING)
    else:
        problems.resolved("power", "питание в норме")
    temp = h.get("cpu_temp_c")
    if temp is not None and temp >= 80:
        problems.report("heat", f"перегрев Pi: {temp} °C - частота снижается, всё работает медленнее",
                        level=logging.WARNING)
    elif temp is not None and temp < 75:
        problems.resolved("heat", "температура Pi в норме")
    synced = system.clock_synced()
    status.add_provider("clock_synced", lambda: synced)
    if synced is False:
        problems.report("clock", "время на Pi не синхронизировано - окна и даты папок могут быть неверны",
                        level=logging.WARNING)
    elif synced:
        problems.resolved("clock", "время синхронизировано")


def main() -> int:
    # второй экземпляр (например, ручной запуск при работающем сервисе) сразу выходит
    lock = system.single_instance_lock(config.PROJECT_DIR / ".main.lock")  # noqa: F841 - держим до выхода

    status = RuntimeStatus()
    outbox = setup_logging(config.LOG_DIR, status, config.LOG_LEVEL)
    signal.signal(signal.SIGTERM, _on_sigterm)
    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1)  # kill -USR1 <pid> - вывести, где стоят потоки
    status.add_provider("problems", problems.active)

    watchdog = Watchdog(config.WATCHDOG_TIMEOUT_SEC)
    watchdog.every(60, lambda: _periodic_health(status))
    watchdog.start()

    log_shipper = None
    code = EXIT_OK
    reason = "неизвестно"
    try:
        log.info("скрипт запущен (pid %d)", os.getpid(), extra=IMPORTANT)
        system.wait_for_clock_sync(config.CLOCK_SYNC_WAIT_SEC, beat=lambda: watchdog.beat("ожидание времени"))
        s3 = S3Storage()
        s3.check()  # без сети не падаем - работаем дальше
        # логи и status.json - в S3 каждые LOG_UPLOAD_INTERVAL_SEC (первый раз - сразу)
        log_shipper = LogShipper(s3, outbox, status, config.LOG_S3_PREFIX, config.LOG_UPLOAD_INTERVAL_SEC)
        log_shipper.start()
        run(s3, status, log_shipper, watchdog)
    except KeyboardInterrupt:
        reason = "остановлен вручную (Ctrl+C)"
    except Terminated:
        reason = "остановлен системой (SIGTERM)"
    except SystemExit as e:
        code = EXIT_CONFIG if e.code not in (None, 0) else EXIT_OK
        reason = f"ошибка конфигурации: {e.code}"
        log.error("ошибка конфигурации: %s", e.code)
    except RestartRequired as e:
        code = EXIT_RESTART
        reason = f"нужен перезапуск: {e}"
        log.error("нужен перезапуск процесса: %s", e)
    except Exception:
        code = EXIT_RESTART
        reason = "скрипт упал"
        log.exception("скрипт упал")
    finally:
        # повторный Ctrl+C / SIGTERM во время остановки - выйти сразу, без трейсбеков
        signal.signal(signal.SIGINT, lambda *_: os._exit(130))
        signal.signal(signal.SIGTERM, lambda *_: os._exit(143))
        watchdog.stop()
        log.info("остановка: %s", reason, extra=IMPORTANT)
        status.stopped(reason, code)  # последний status.json покажет, что скрипт остановлен, а не пропал
        if log_shipper is not None:
            log_shipper.stop()  # последняя отправка логов (не дольше 15 с)
    return code


if __name__ == "__main__":
    exit_code = main()
    logging.shutdown()
    sys.stdout.flush()
    # os._exit, а не sys.exit: не ждём потоки, зависшие в сети/камере/диске
    # (всё неотправленное лежит на диске и уйдёт после перезапуска)
    os._exit(exit_code)
