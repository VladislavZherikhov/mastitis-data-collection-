"""Состояние скрипта для status.json («я жив»): окно, снимки, загрузки, диск, последняя ошибка.

Обновляется из разных потоков (съёмка, загрузка, логи), поэтому все изменения - под блокировкой.
"""
import logging
import socket
import threading
from datetime import datetime
from typing import Callable

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _fmt(dt: datetime | None) -> str | None:
    return dt.strftime(TIME_FORMAT) if dt else None


class RuntimeStatus:
    def __init__(self):
        self._lock = threading.Lock()
        self.host = socket.gethostname()
        self.started = datetime.now()
        # running - работает; stopped - остановлен (штатно или из-за ошибки, см. stop_reason)
        self.state = "running"
        self.stopped_at: datetime | None = None
        self.stop_reason: str | None = None
        self.exit_code: int | None = None
        self.in_window = False
        self.window_until: datetime | None = None
        self.next_window: datetime | None = None
        self.last_shot: datetime | None = None
        self.last_error: str | None = None
        # счётчики текущего окна (обнуляются в начале окна)
        # upload_deferred - очередь загрузки была полна, пакет уйдёт позже с диска (или потерян, если диска нет)
        self.window = {"shots": 0, "skipped": 0, "saved_to_drive": 0, "uploaded": 0, "upload_failed": 0,
                       "upload_deferred": 0}
        # время последних событий: последняя успешная загрузка в S3, последний перезапуск камер и т.п.
        self.events: dict[str, datetime] = {}
        # дополнительные поля, которые считаются в момент формирования status.json (например, диск)
        self._providers: dict[str, Callable[[], object]] = {}

    # --- окно ---
    def window_started(self, until: datetime) -> None:
        with self._lock:
            self.in_window = True
            self.window_until = until
            self.next_window = None
            self.window = dict.fromkeys(self.window, 0)

    def window_finished(self) -> None:
        with self._lock:
            self.in_window = False
            self.window_until = None

    def stopped(self, reason: str, exit_code: int) -> None:
        with self._lock:
            self.state = "stopped"
            self.in_window = False
            self.window_until = None
            self.stopped_at = datetime.now()
            self.stop_reason = reason
            self.exit_code = exit_code

    def idle_until(self, next_window: datetime) -> None:
        with self._lock:
            self.next_window = next_window

    # --- события ---
    def count(self, key: str, when: datetime | None = None) -> None:
        with self._lock:
            self.window[key] += 1
            if key == "shots":
                self.last_shot = when or datetime.now()

    def mark(self, event: str) -> None:
        """Запомнить время события (например, "s3_last_ok")."""
        with self._lock:
            self.events[event] = datetime.now()

    def error(self, text: str) -> None:
        with self._lock:
            self.last_error = text

    def add_provider(self, name: str, fn: Callable[[], object]) -> None:
        self._providers[name] = fn

    # --- снимок для status.json ---
    def snapshot(self) -> dict:
        with self._lock:
            data = {
                "host": self.host,
                "state": self.state,
                "updated": _fmt(datetime.now()),
                "started": _fmt(self.started),
                "in_window": self.in_window,
                "window_until": _fmt(self.window_until),
                "next_window": _fmt(self.next_window),
                "last_shot": _fmt(self.last_shot),
                "window": dict(self.window),
                "last_error": self.last_error,
                "stopped_at": _fmt(self.stopped_at),
                "stop_reason": self.stop_reason,
                "exit_code": self.exit_code,
                "events": {k: _fmt(v) for k, v in self.events.items()},
            }
        for name, fn in self._providers.items():
            try:
                data[name] = fn()
            except Exception as e:  # статус не должен падать из-за одного поля
                data[name] = f"ошибка: {e}"
        return data


class StatusErrorHandler(logging.Handler):
    """Запоминает последнюю ошибку (ERROR и выше) в status.json."""

    def __init__(self, status: RuntimeStatus):
        super().__init__(level=logging.ERROR)
        self.status = status

    def emit(self, record: logging.LogRecord) -> None:
        try:
            when = datetime.fromtimestamp(record.created).strftime(TIME_FORMAT)
            self.status.error(f"{when} [{record.name}] {record.getMessage()}")
        except Exception:
            self.handleError(record)
