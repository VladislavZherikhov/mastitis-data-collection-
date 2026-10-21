"""Сторож от зависаний.

Основной цикл регулярно вызывает beat(). Если beat() не было дольше timeout_sec, значит, скрипт
завис без ошибки (камера, диск, сеть). Тогда сторож пишет в лог, где стоит каждый поток,
и завершает процесс с кодом 1 - systemd перезапустит его с чистого листа.

Если скрипт запущен как systemd-сервис с Type=notify и WatchdogSec=..., сторож ещё и сообщает
systemd «я жив» (sd_notify). Тогда systemd сам перезапустит скрипт, даже если зависнет и сторож.

Кроме того, раз в check_sec сторож вызывает периодические проверки (питание, время и т.п.).
"""
import logging
import os
import socket
import sys
import threading
import time
import traceback
from typing import Callable

from core.logmarks import IMPORTANT

log = logging.getLogger("watchdog")

HANG_EXIT_CODE = 1


def sd_notify(message: str) -> None:
    """Сообщение systemd (READY=1, WATCHDOG=1 ...). Без systemd - ничего не делает."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr or not hasattr(socket, "AF_UNIX"):
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(message.encode())
    except OSError:
        pass


def thread_dump() -> str:
    """Где сейчас стоит каждый поток - для диагностики зависания."""
    names = {t.ident: t.name for t in threading.enumerate()}
    parts = []
    for ident, frame in sys._current_frames().items():
        parts.append(f"--- поток {names.get(ident, ident)} ---\n" + "".join(traceback.format_stack(frame)))
    return "\n".join(parts)


class Watchdog:
    def __init__(self, timeout_sec: float, check_sec: float = 15):
        self.timeout_sec = timeout_sec
        self.check_sec = check_sec
        self._last_beat = time.monotonic()
        self._where = "запуск"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._periodic: list[tuple[float, Callable[[], None], list[float]]] = []

    def beat(self, where: str = "") -> None:
        self._last_beat = time.monotonic()
        if where:
            self._where = where

    def every(self, period_sec: float, fn: Callable[[], None]) -> None:
        """Периодическая проверка в потоке сторожа (ошибки в ней не ломают сторожа)."""
        self._periodic.append((period_sec, fn, [0.0]))

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="watchdog", daemon=True)
        self._thread.start()
        sd_notify("READY=1")
        log.info("сторож зависаний запущен: перезапуск, если нет прогресса %.0f с; systemd watchdog: %s",
                 self.timeout_sec, "да" if os.environ.get("NOTIFY_SOCKET") else "нет (запуск не через systemd notify)",
                 extra=IMPORTANT)

    def stop(self) -> None:
        self._stop.set()
        sd_notify("STOPPING=1")

    def _run(self) -> None:
        while not self._stop.wait(self.check_sec):
            silent = time.monotonic() - self._last_beat
            if silent > self.timeout_sec:
                self._hang(silent)
                return
            sd_notify("WATCHDOG=1")
            now = time.monotonic()
            for period, fn, last in self._periodic:
                if now - last[0] >= period:
                    last[0] = now
                    try:
                        fn()
                    except Exception:
                        log.exception("ошибка периодической проверки")

    def _hang(self, silent: float) -> None:
        log.critical("скрипт завис: нет прогресса %.0f с (последний шаг: %s). Перезапуск процесса. "
                     "Где стоят потоки:\n%s", silent, self._where, thread_dump())
        logging.shutdown()  # дописать лог в очередь на диске - уйдёт в S3 после перезапуска
        os._exit(HANG_EXIT_CODE)
