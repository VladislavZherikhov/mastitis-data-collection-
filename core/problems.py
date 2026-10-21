"""Учёт повторяющихся проблем, чтобы не заспамить лог.

Пример: пропал интернет, и каждые 10 с не загружается пакет. Вместо 360 одинаковых строк в час:
- первое появление проблемы - сразу в лог;
- повторы - одной строкой раз в repeat_sec, с числом повторов;
- когда проблема ушла - одна строка «восстановлено», с длительностью.
Список активных проблем попадает в status.json.
"""
import logging
import threading
import time
from dataclasses import dataclass, field

from core.logmarks import IMPORTANT


@dataclass
class _Active:
    message: str
    since: float = field(default_factory=time.monotonic)
    last_logged: float = field(default_factory=time.monotonic)
    repeats: int = 0          # повторов с последней записи в лог
    total: int = 1            # всего случаев


class Problems:
    def __init__(self, logger: logging.Logger, repeat_sec: float = 600):
        self.log = logger
        self.repeat_sec = repeat_sec
        self._lock = threading.Lock()
        self._active: dict[str, _Active] = {}

    def report(self, key: str, message: str, *, level: int = logging.ERROR, exc_info: bool = False) -> None:
        """Проблема случилась (ещё раз). key - что именно сломалось (например "s3:network")."""
        now = time.monotonic()
        with self._lock:
            item = self._active.get(key)
            if item is None:
                self._active[key] = _Active(message)
                first = True
            else:
                item.message = message
                item.repeats += 1
                item.total += 1
                if now - item.last_logged < self.repeat_sec:
                    return
                repeats, item.repeats, item.last_logged = item.repeats, 0, now
                first = False
        if first:
            self.log.log(level, message, exc_info=exc_info)
        else:
            self.log.log(level, "%s (повторилось ещё %d раз за %d мин)", message, repeats, self.repeat_sec // 60)

    def resolved(self, key_or_prefix: str, message: str | None = None) -> None:
        """Проблема ушла. Можно передать префикс: resolved("s3:") закрывает все проблемы S3."""
        with self._lock:
            keys = [k for k in self._active if k == key_or_prefix or k.startswith(key_or_prefix)]
            items = [(k, self._active.pop(k)) for k in keys]
        for key, item in items:
            minutes = (time.monotonic() - item.since) / 60
            self.log.info("восстановлено: %s (проблема длилась %.0f мин, случаев: %d)",
                          message or item.message, minutes, item.total, extra=IMPORTANT)

    def is_active(self, key: str) -> bool:
        with self._lock:
            return key in self._active

    def active(self) -> list[str]:
        """Для status.json."""
        now = time.monotonic()
        with self._lock:
            return [f"{item.message} (уже {(now - item.since) / 60:.0f} мин)" for item in self._active.values()]
