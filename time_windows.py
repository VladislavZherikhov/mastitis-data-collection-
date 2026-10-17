"""Временные окна работы: WINDOW_1=06:00-08:00, WINDOW_2=18:00-20:00, ...

Окно может переходить через полночь (например 22:00-02:00).
"""
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

_KEY_RE = re.compile(r"^WINDOW_(\d+)$")
_VALUE_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")


@dataclass(frozen=True)
class TimeWindow:
    name: str
    start: time
    end: time

    def __str__(self) -> str:
        return f"{self.name}={self.start:%H:%M}-{self.end:%H:%M}"

    def end_if_active(self, now: datetime) -> datetime | None:
        """Если now внутри окна - момент окончания окна, иначе None."""
        t = now.time()
        today_end = datetime.combine(now.date(), self.end)
        if self.start < self.end:
            return today_end if self.start <= t < self.end else None
        # окно через полночь
        if t >= self.start:
            return today_end + timedelta(days=1)
        if t < self.end:
            return today_end
        return None

    def next_start(self, now: datetime) -> datetime:
        start = datetime.combine(now.date(), self.start)
        return start if start > now else start + timedelta(days=1)


def _parse_time(h: str, m: str, raw: str) -> time:
    hour, minute = int(h), int(m)
    if hour == 24 and minute == 0:
        return time(0, 0)  # 24:00 = полночь
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"некорректное время в '{raw}'")
    return time(hour, minute)


def parse_windows(env: dict[str, str]) -> list[TimeWindow]:
    """Собирает все WINDOW_<N> из окружения, отсортированные по N."""
    found = []
    for key, value in env.items():
        m = _KEY_RE.match(key)
        if not m or not value.strip():
            continue
        v = _VALUE_RE.match(value.strip())
        if not v:
            raise SystemExit(f"{key}='{value}': ожидается формат ЧЧ:ММ-ЧЧ:ММ, например 06:00-08:00")
        try:
            start = _parse_time(v[1], v[2], value)
            end = _parse_time(v[3], v[4], value)
        except ValueError as e:
            raise SystemExit(f"{key}: {e}")
        if start == end:
            raise SystemExit(f"{key}='{value}': начало и конец окна совпадают")
        found.append((int(m[1]), TimeWindow(key, start, end)))

    if not found:
        raise SystemExit("Не задано ни одного окна работы WINDOW_1, WINDOW_2, ... (см. .env.example)")
    return [w for _, w in sorted(found)]


def active_until(windows: list[TimeWindow], now: datetime) -> datetime | None:
    """Если сейчас идёт хотя бы одно окно - самый поздний момент окончания среди активных окон."""
    ends = [e for w in windows if (e := w.end_if_active(now)) is not None]
    return max(ends) if ends else None


def next_start(windows: list[TimeWindow], now: datetime) -> datetime:
    return min(w.next_start(now) for w in windows)
