"""Логи: отправка в S3 кусками каждые LOG_UPLOAD_INTERVAL_SEC секунд + status.json.

- строки лога пишутся в файл-очередь <LOG_DIR>/outbox.log (только то, что ещё не отправлено);
- раз в интервал очередь «отрезается» в кусок <LOG_DIR>/chunks/<дата_время>.log, куски
  отправляются в s3://<bucket>/<LOG_S3_PREFIX>/<hostname>/<дата>/<ЧЧ-ММ-СС>.log и сразу
  удаляются с Pi. Нет связи - куски копятся и уходят, когда связь вернётся (в том числе после
  перезагрузки);
- в окне работы в лог (и в S3) идёт всё, вне окна - только важное: WARNING и выше
  и записи с пометкой extra=IMPORTANT;
- вместе с кусками обновляется <LOG_S3_PREFIX>/<hostname>/status.json.
Подробности - в documentation/logging.md.
"""
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

from core.logmarks import IMPORTANT
from core.status import RuntimeStatus, StatusErrorHandler
from storage.s3 import S3Storage, classify_error

log = logging.getLogger("logs")

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
CHUNK_FORMAT = "%Y-%m-%d_%H-%M-%S"
# очередь неотправленных логов на Pi не больше этого (при многодневном обрыве удаляются самые старые куски)
MAX_QUEUE_BYTES = 50 * 1024 * 1024

__all__ = ["IMPORTANT", "LogShipper", "OutboxHandler", "setup_logging"]


class WindowFilter(logging.Filter):
    """В окне пропускает всё, вне окна - только WARNING+ и записи с extra=IMPORTANT."""

    def __init__(self, status: RuntimeStatus):
        super().__init__()
        self.status = status

    def filter(self, record: logging.LogRecord) -> bool:
        return (self.status.in_window
                or record.levelno >= logging.WARNING
                or getattr(record, "important", False))


class OutboxHandler(logging.FileHandler):
    """Дописывает строки в outbox.log. cut() атомарно переносит накопленное в кусок для отправки."""

    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.chunks_dir = log_dir / "chunks"
        self.chunks_dir.mkdir(parents=True, exist_ok=True)
        self._last_stamp = ""
        self._same_second = 0
        super().__init__(log_dir / "outbox.log", mode="a", encoding="utf-8", delay=True)

    def cut(self) -> Path | None:
        """Переносит outbox.log в chunks/<дата_время>.log. None - если отправлять нечего."""
        path = Path(self.baseFilename)
        with self.lock:
            if self.stream is not None:
                self.stream.close()
                self.stream = None  # следующая запись откроет новый outbox.log
            if not path.is_file() or path.stat().st_size == 0:
                return None
            # два отрезания в одну секунду (например, конец окна + интервал) не должны дать одно имя:
            # предыдущий кусок мог уже уйти в S3 и удалиться с Pi, и новый перезаписал бы его там
            stamp = datetime.now().strftime(CHUNK_FORMAT)
            self._same_second = self._same_second + 1 if stamp == self._last_stamp else 0
            self._last_stamp = stamp
            name = stamp if not self._same_second else f"{stamp}_{self._same_second}"
            dest = self.chunks_dir / f"{name}.log"
            os.replace(path, dest)
            return dest


def setup_logging(log_dir: Path, status: RuntimeStatus, level: str = "INFO") -> OutboxHandler:
    """Консоль (для journalctl) получает всё; очередь на отправку в S3 - по правилу окна.
    level: INFO - обычный режим, DEBUG - подробно каждый шаг."""
    outbox = OutboxHandler(log_dir)
    outbox.addFilter(WindowFilter(status))
    # ошибка записи лога (например, SD-карта заполнена) не должна сыпать трейсбеками в консоль
    logging.raiseExceptions = False
    numeric = logging.getLevelName(level)
    logging.basicConfig(level=numeric if isinstance(numeric, int) else logging.INFO, format=LOG_FORMAT,
                        handlers=[logging.StreamHandler(), outbox, StatusErrorHandler(status)])
    # сторонние библиотеки на DEBUG пишут слишком много (каждый HTTP-запрос) - им только предупреждения
    for noisy in ("botocore", "boto3", "urllib3", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not isinstance(numeric, int):
        log.warning("неизвестный LOG_LEVEL=%s, использую INFO", level)
    return outbox


class LogShipper:
    """Фоновый поток: раз в interval_sec отправляет накопившиеся логи и status.json в S3."""

    def __init__(self, s3: S3Storage, outbox: OutboxHandler, status: RuntimeStatus,
                 prefix: str, interval_sec: float):
        self.s3 = s3
        self.outbox = outbox
        self.status = status
        self.base = f"{prefix.strip('/')}/{status.host}"
        self.interval_sec = interval_sec
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offline = False  # чтобы писать о пропаже/возврате связи один раз

    def start(self) -> None:
        self._thread = threading.Thread(target=self._worker, name="log-shipper", daemon=True)
        self._thread.start()

    def flush_now(self) -> None:
        """Отправить логи сейчас, не дожидаясь интервала (не блокирует)."""
        self._wake.set()

    def stop(self, timeout: float = 15.0) -> None:
        """Последняя отправка и остановка. Если сеть не успела за timeout - не ждём:
        неотправленное останется в очереди на Pi и уйдёт при следующем запуске."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.warning("логи не успели уйти в S3 при остановке - отправятся при следующем запуске")

    def _worker(self) -> None:
        while not self._stop.is_set():
            self._ship_safe()
            self._wake.wait(self.interval_sec)
            self._wake.clear()
        self._ship_safe()  # последняя отправка после сигнала остановки - всегда

    def _ship_safe(self) -> None:
        try:
            self._ship()
        except Exception:
            # поток отправки логов не должен умереть ни от какой ошибки
            log.exception("неожиданная ошибка отправки логов")

    def _ship(self) -> None:
        self.outbox.cut()
        self._limit_queue()
        sent = 0
        try:
            # от старых к новым; на первой ошибке останавливаемся - остальное уйдёт в следующий раз
            for chunk in sorted(self.outbox.chunks_dir.glob("*.log")):
                self.s3.upload_bytes(self._chunk_key(chunk), chunk.read_bytes(), "text/plain; charset=utf-8")
                chunk.unlink()
                sent += 1
            self.s3.upload_bytes(f"{self.base}/status.json",
                                 json.dumps(self.status.snapshot(), ensure_ascii=False, indent=2).encode(),
                                 "application/json")
        except Exception as e:
            if not self._offline:
                self._offline = True
                # WARNING, чтобы попало в очередь и вне окна; повторять каждые 5 минут не будем
                log.warning("логи не отправлены, копятся на Pi до восстановления: %s", classify_error(e)[1])
            return
        log.debug("логи: отправлено кусков %d, status.json обновлён", sent)
        if self._offline:
            self._offline = False
            log.info("связь с S3 восстановлена, отправлено накопившихся кусков логов: %d", sent, extra=IMPORTANT)

    def _limit_queue(self) -> None:
        """При очень долгом обрыве не даём очереди логов забить SD-карту: удаляем самые старые куски."""
        chunks = sorted(self.outbox.chunks_dir.glob("*.log"))
        sizes = [c.stat().st_size for c in chunks]
        total, dropped = sum(sizes), 0
        for chunk, size in zip(chunks, sizes):
            if total <= MAX_QUEUE_BYTES:
                break
            chunk.unlink(missing_ok=True)
            total -= size
            dropped += 1
        if dropped:
            log.warning("очередь логов на Pi больше %d МБ - удалены самые старые куски: %d",
                        MAX_QUEUE_BYTES // 2**20, dropped)

    def _chunk_key(self, chunk: Path) -> str:
        """chunks/2026-10-21_18-40-00.log -> <base>/2026-10-21/18-40-00.log"""
        day, _, rest = chunk.stem.partition("_")
        return f"{self.base}/{day}/{rest}.log"
