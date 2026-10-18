"""Логи: файл на каждый день (logs/2026-10-17.log) + выгрузка в S3.

- текущий день пишется в logs/<дата>.log, после полуночи начинается новый файл;
- файлы прошлых дней выгружаются в S3 и удаляются с диска только после успешной загрузки
  (при старте, после полуночи и повторно, если не получилось);
- текущий файл можно выгрузить в любой момент (конец окна работы, остановка) - он перезапишется в S3.
"""
import logging
import socket
import threading
from datetime import date, datetime
from pathlib import Path

from s3_connection import S3Storage

log = logging.getLogger("logs")

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


class DailyFileHandler(logging.FileHandler):
    """Пишет в <log_dir>/<YYYY-MM-DD>.log, при смене даты переключается на новый файл."""

    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._day = date.today()
        super().__init__(self._path(self._day), encoding="utf-8", delay=True)

    def _path(self, day: date) -> Path:
        return self.log_dir / f"{day.isoformat()}.log"

    @property
    def current_path(self) -> Path:
        return Path(self.baseFilename)

    def switch_day_if_needed(self) -> None:
        today = date.today()
        if today == self._day:
            return
        with self.lock:
            if self.stream is not None:
                self.stream.close()
                self.stream = None  # новый файл откроется при следующей записи (delay=True)
            self._day = today
            self.baseFilename = str(self._path(today))

    def emit(self, record: logging.LogRecord) -> None:
        self.switch_day_if_needed()
        super().emit(record)


def setup_logging(log_dir: Path) -> DailyFileHandler:
    """Логи идут и в консоль (для journalctl), и в файл текущего дня."""
    file_handler = DailyFileHandler(log_dir)
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT,
                        handlers=[logging.StreamHandler(), file_handler])
    return file_handler


class LogUploader:
    """Выгружает файлы логов в s3://<bucket>/<prefix>/<hostname>/<дата>.log."""

    def __init__(self, s3: S3Storage, handler: DailyFileHandler, prefix: str,
                 check_sec: float = 60, retry_sec: float = 600):
        self.s3 = s3
        self.handler = handler
        self.base = f"{prefix.strip('/')}/{socket.gethostname()}"
        self.check_sec = check_sec  # как часто проверять смену даты
        self.retry_sec = retry_sec  # пауза перед повтором, если выгрузка прошлых дней не удалась
        self._lock = threading.Lock()  # чтобы выгрузки из разных потоков не пересекались
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- выгрузка ---
    def _upload(self, path: Path) -> None:
        self.s3.upload_bytes(f"{self.base}/{path.name}", path.read_bytes(), "text/plain; charset=utf-8")

    def upload_current(self) -> None:
        """Выгружает файл текущего дня (перезаписывает его в S3). С диска не удаляется."""
        with self._lock:
            path = self.handler.current_path
            if not path.is_file():
                return
            try:
                self._upload(path)
            except Exception:
                log.exception("не удалось выгрузить лог %s", path.name)

    def upload_past_days(self) -> bool:
        """Выгружает и удаляет файлы прошлых дней. True - если все выгрузились."""
        self.handler.switch_day_if_needed()  # чтобы вчерашний файл был закрыт, даже если после полуночи не было записей
        today = date.today()
        ok = True
        with self._lock:
            for path in sorted(self.handler.log_dir.glob("*.log")):
                try:
                    day = date.fromisoformat(path.stem)
                except ValueError:
                    continue  # посторонний файл
                if day >= today:
                    continue
                try:
                    self._upload(path)
                    path.unlink()
                except Exception:
                    ok = False
                    log.exception("не удалось выгрузить лог %s, повтор позже", path.name)
        return ok

    # --- фоновый поток: смена даты и повторы ---
    def start(self) -> None:
        self._thread = threading.Thread(target=self._worker, name="log-uploader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Останавливает поток и делает финальную выгрузку текущего файла."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.upload_current()

    def _worker(self) -> None:
        ok = self.upload_past_days()
        day = date.today()
        last_try = datetime.now()
        while not self._stop.wait(self.check_sec):
            now = datetime.now()
            if now.date() != day or (not ok and (now - last_try).total_seconds() >= self.retry_sec):
                day = now.date()
                last_try = now
                ok = self.upload_past_days()
