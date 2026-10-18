"""Локальный диск: копия каждого пакета снимков + очистка старых пакетов при заполнении.

Структура на диске такая же, как в S3 (папка дня -> папка пакета -> 3 файла):
    <DRIVE_MOUNT>/<S3_PREFIX>/2026-10-18/2026-10-18_06-00-10/thermal_2026-10-18_06-00-10.npy ...

- пакет пишется во временную папку .tmp_<дата_время> и переименовывается, когда записаны все файлы,
  поэтому на диске не бывает недописанных пакетов;
- после успешной загрузки в S3 в папке пакета появляется метка .uploaded; пакеты без метки
  догружаются в S3 вне окон работы;
- очистка (логика из disk_backup/backup.py Севы) идёт в фоновом потоке: когда диск заполнен до
  порога, удаляются самые старые пакеты (папки целиком), пока заполненность не опустится до цели.
  Сначала удаляются уже выгруженные в S3 (прошлый день, где всё выгружено, - целиком), потом -
  не выгруженные старше DRIVE_MIN_AGE_DAYS;
- если свободного места меньше DRIVE_MIN_FREE, пакеты на диск не пишутся (только в S3).
Подробности - в drive_doc.md.
"""
import logging
import mimetypes
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger("drive")

DAY_FORMAT = "%Y-%m-%d"                # папка дня
FOLDER_FORMAT = "%Y-%m-%d_%H-%M-%S"    # папка пакета (и метка времени в именах файлов)
TMP_PREFIX = ".tmp_"
UPLOADED_MARK = ".uploaded"

_SIZE_UNITS = {
    "B": 1, "K": 1024, "KB": 1024, "KIB": 1024,
    "M": 1024 ** 2, "MB": 1024 ** 2, "MIB": 1024 ** 2,
    "G": 1024 ** 3, "GB": 1024 ** 3, "GIB": 1024 ** 3,
    "T": 1024 ** 4, "TB": 1024 ** 4, "TIB": 1024 ** 4,
}


def format_bytes(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(num) < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TiB"


def parse_size(raw: str, name: str) -> int:
    """'500MB', '2GB', '2 GiB', '1048576' (байты) -> байты."""
    m = re.match(r"^\s*([0-9]*\.?[0-9]+)\s*([A-Za-z]*)\s*$", raw)
    unit = m[2].upper() if m else ""
    if not m or (unit and unit not in _SIZE_UNITS):
        raise SystemExit(f"{name}='{raw}': ожидается размер, например 500MB или 2GB")
    return int(float(m[1]) * _SIZE_UNITS.get(unit, 1))


def parse_percent(raw: str, name: str) -> float:
    try:
        value = float(raw.strip().rstrip("%"))
    except ValueError:
        raise SystemExit(f"{name}='{raw}': ожидается процент, например 80") from None
    if not 0 < value <= 100:
        raise SystemExit(f"{name}='{raw}': процент должен быть от 0 до 100")
    return value


@dataclass(frozen=True)
class Limit:
    """Порог/цель очистки: по проценту занятого места или по объёму свободного."""
    mode: str  # "used_percent" или "free_bytes"
    value: float

    def __str__(self) -> str:
        if self.mode == "used_percent":
            return f"занято {self.value:g}%"
        return f"свободно {format_bytes(self.value)}"


def limits_from_env(env: dict[str, str]) -> tuple[Limit, Limit]:
    """Порог и цель очистки. Если задан DRIVE_FREE_* (объём свободного места), он важнее процентов."""
    def pick(free_key: str, pct_key: str, default_pct: float) -> Limit:
        if env.get(free_key, "").strip():
            return Limit("free_bytes", parse_size(env[free_key], free_key))
        if env.get(pct_key, "").strip():
            return Limit("used_percent", parse_percent(env[pct_key], pct_key))
        return Limit("used_percent", default_pct)

    threshold = pick("DRIVE_FREE_THRESHOLD", "DRIVE_USAGE_THRESHOLD", 80)
    target = pick("DRIVE_FREE_TARGET", "DRIVE_USAGE_TARGET", 70)

    if threshold.mode == target.mode == "used_percent" and not target.value < threshold.value:
        raise SystemExit("DRIVE_USAGE_TARGET должен быть меньше DRIVE_USAGE_THRESHOLD")
    if threshold.mode == target.mode == "free_bytes" and not target.value > threshold.value:
        raise SystemExit("DRIVE_FREE_TARGET должен быть больше DRIVE_FREE_THRESHOLD")
    return threshold, target


class LocalDrive:
    """Сохранение пакетов, догрузка в S3 и фоновая очистка.

    Потоки:
    - съёмка (main) вызывает save_shot(): пишет пакет и только проверяет место (микросекунды);
      если порог достигнут - будит поток очистки и сразу возвращается к съёмке;
    - поток очистки (drive-cleanup) удаляет старые пакеты, не мешая съёмке;
    - потоки загрузки вызывают mark_uploaded(), основной цикл вне окон - upload_pending().
    Операции с одной папкой пакета (удаление, чтение для догрузки, метка) защищены общей
    блокировкой, поэтому очистка не удалит папку посреди чтения или записи метки.
    """

    CLEANUP_BATCH = 200       # после стольких удалённых папок - короткая пауза, чтобы не забивать диск
    CLEANUP_BATCH_PAUSE = 0.2

    def __init__(self, mount: Path, data_dir: Path, threshold: Limit, target: Limit,
                 min_age_days: float, min_free: int):
        self.mount = mount
        self.data_dir = data_dir  # куда складываются пакеты (внутри mount)
        self.threshold = threshold
        self.target = target
        self.min_age_days = min_age_days
        self.min_age = timedelta(days=min_age_days)
        self.min_free = min_free  # аварийный минимум: при меньшем свободном месте пакеты на диск не пишутся
        self.retry_sec = 600  # пауза перед повтором неудачной очистки / догрузки в S3

        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_cleanup_try = 0.0
        self._removed_since_pause = 0  # счётчик для пауз при массовом удалении
        self._low_space = False  # сейчас пакеты не пишутся из-за нехватки места (чтобы логировать один раз)

        # есть ли пакеты, не дошедшие до S3. При старте неизвестно - поэтому True (один раз просканируем)
        self.has_backlog = True
        self._next_backlog_try = 0.0

    # --- жизненный цикл фонового потока очистки ---
    def start(self) -> None:
        self._thread = threading.Thread(target=self._cleanup_worker, name="drive-cleanup", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Поток дочищает текущую папку и выходит."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def is_mounted(self) -> bool:
        """Диск должен быть смонтирован - иначе /mnt/hdd это просто папка на SD-карте,
        и запись туда забьёт системную карту."""
        return os.path.ismount(self.mount)

    def remove_incomplete(self) -> None:
        """Удаляет недописанные пакеты (<день>/.tmp_*), оставшиеся после сбоя питания или падения скрипта."""
        if not self.data_dir.is_dir():
            return
        for p in self.data_dir.glob(f"*/{TMP_PREFIX}*"):
            shutil.rmtree(p, ignore_errors=True)
            log.warning("удалён недописанный пакет %s", p.relative_to(self.data_dir).as_posix())

    # --- сохранение ---
    def save_shot(self, folder: str, files: dict[str, tuple[bytes, str]]) -> Path | None:
        """Сохраняет пакет в <data_dir>/<folder>/ (folder = "<день>/<дата_время>").
        None - если не сохранён из-за нехватки места.

        Сначала пишет во временную папку и только потом переименовывает: переименование
        происходит целиком, поэтому пакет на диске либо полный, либо его нет."""
        size = sum(len(data) for data, _ in files.values())
        if not self._has_room(size):
            return None

        target = self.data_dir / folder
        tmp = target.with_name(TMP_PREFIX + target.name)  # рядом, в той же папке дня
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            for name, (data, _content_type) in files.items():
                with open(tmp / name, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())  # данные физически на диске до переименования
            tmp.rename(target)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

        self.request_cleanup_if_needed()
        return target

    def _has_room(self, size: int) -> bool:
        """Аварийный минимум: если после записи свободного места останется меньше min_free -
        пакет на диск не пишем (он всё равно уйдёт в S3) и будим очистку."""
        free = shutil.disk_usage(self.mount).free
        if free - size >= self.min_free:
            if self._low_space:
                self._low_space = False
                log.info("место на диске освободилось (%s), пакеты снова сохраняются на диск", self.usage_str())
            return True

        self._wake.set()
        if not self._low_space:
            self._low_space = True
            log.error("на диске осталось меньше %s (%s) - пакеты сохраняются только в S3, пока очистка "
                      "не освободит место", format_bytes(self.min_free), self.usage_str())
        return False

    def mark_uploaded(self, folder: str) -> None:
        """Ставит метку «пакет есть в S3». Если пакета на диске нет (не сохранился или удалён) - ничего не делает."""
        path = self.data_dir / folder
        with self._lock:
            if path.is_dir():
                (path / UPLOADED_MARK).touch()

    # --- догрузка в S3 того, что не ушло сразу ---
    def upload_pending(self, s3, until: datetime) -> None:
        """Догружает в S3 пакеты без метки .uploaded (от старых к новым) до момента until.
        При первой же ошибке останавливается и пробует снова через retry_sec."""
        if not self.has_backlog or time.monotonic() < self._next_backlog_try or not self.is_mounted():
            return

        uploaded = 0
        for _created, path in self._packages_oldest_first():
            if datetime.now() >= until or self._stop.is_set():
                break  # начинается окно съёмки (или остановка) - доделаем потом
            # блокировка только на чтение с диска: сама загрузка может долго висеть при плохой сети,
            # и очистка не должна её ждать
            with self._lock:
                if not path.is_dir() or self._is_uploaded(path):
                    continue
                files = {
                    f.name: (f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")
                    for f in path.iterdir() if f.is_file() and not f.name.startswith(".")
                }
            folder = path.relative_to(self.data_dir).as_posix()  # "<день>/<дата_время>"
            try:
                s3.upload_shot(folder, files)
            except Exception:
                log.exception("догрузка в S3: не удалось выгрузить %s, повтор через %d мин",
                              folder, self.retry_sec // 60)
                self._next_backlog_try = time.monotonic() + self.retry_sec
                break
            self.mark_uploaded(folder)
            uploaded += 1
        else:
            self.has_backlog = False  # прошли все пакеты без ошибок

        if uploaded:
            log.info("догрузка в S3: выгружено пакетов с диска %d", uploaded)

    # --- очистка ---
    def _reached(self, limit: Limit, *, above: bool) -> bool:
        """above=True: дошли ли до порога (пора чистить); False: опустились ли до цели (хватит)."""
        usage = shutil.disk_usage(self.mount)
        if limit.mode == "used_percent":
            used_pct = usage.used / usage.total * 100
            return used_pct >= limit.value if above else used_pct <= limit.value
        return usage.free < limit.value if above else usage.free >= limit.value

    @staticmethod
    def _is_uploaded(package: Path) -> bool:
        return (package / UPLOADED_MARK).exists()

    @staticmethod
    def _dated_dirs(parent: Path, fmt: str) -> list[tuple[datetime, Path]]:
        """Подпапки с именем в формате fmt, от старых к новым. Чужие и .tmp_ папки не трогаем."""
        result = []
        if not parent.is_dir():
            return result
        for p in parent.iterdir():
            if not p.is_dir():
                continue
            try:
                result.append((datetime.strptime(p.name, fmt), p))
            except ValueError:
                continue
        return sorted(result)

    def _days_oldest_first(self) -> list[tuple[date, Path]]:
        return [(d.date(), p) for d, p in self._dated_dirs(self.data_dir, DAY_FORMAT)]

    def _packages_in(self, day_path: Path) -> list[tuple[datetime, Path]]:
        return self._dated_dirs(day_path, FOLDER_FORMAT)

    def _packages_oldest_first(self) -> list[tuple[datetime, Path]]:
        """Все пакеты всех дней, от старых к новым."""
        return [pkg for _, day_path in self._days_oldest_first() for pkg in self._packages_in(day_path)]

    def usage_str(self) -> str:
        u = shutil.disk_usage(self.mount)
        return f"занято {u.used / u.total * 100:.1f}%, свободно {format_bytes(u.free)}"

    def request_cleanup_if_needed(self) -> None:
        """Быстрая проверка из цикла съёмки: если порог достигнут - будит поток очистки."""
        if time.monotonic() >= self._next_cleanup_try and self._reached(self.threshold, above=True):
            self._wake.set()

    def _cleanup_worker(self) -> None:
        while True:
            self._wake.wait()
            if self._stop.is_set():
                return
            self._wake.clear()
            try:
                if self.is_mounted() and self._reached(self.threshold, above=True):
                    self._cleanup()
            except Exception:
                log.exception("ошибка очистки диска")
                self._next_cleanup_try = time.monotonic() + self.retry_sec

    def _cleanup(self) -> None:
        log.warning("диск заполнен (%s, порог: %s), удаляю старые пакеты до цели: %s",
                    self.usage_str(), self.threshold, self.target)
        self._removed_since_pause = 0
        deleted = {"выгруженных": 0, "не выгруженных в S3": 0}
        today = date.today()

        def done() -> bool:
            return self._stop.is_set() or self._reached(self.target, above=False)

        # 1) выгруженные в S3 - от старых дней к новым. Прошлый день, где всё выгружено,
        #    удаляется целиком одной операцией; иначе - выгруженные пакеты по одному
        for day, day_path in self._days_oldest_first():
            if done():
                break
            packages = self._packages_in(day_path)
            if (day < today and packages and all(self._is_uploaded(p) for _, p in packages)
                    and not any(day_path.glob(TMP_PREFIX + "*"))):
                if self._remove(day_path):
                    deleted["выгруженных"] += len(packages)
                continue
            for _created, path in packages:
                if done():
                    break
                if self._is_uploaded(path) and self._remove(path):
                    deleted["выгруженных"] += 1
            self._remove_if_empty(day_path)

        # 2) только если не хватило - не выгруженные в S3, старше min_age, по одному
        newest_allowed = datetime.now() - self.min_age
        for created, path in self._packages_oldest_first():
            if done() or created > newest_allowed:
                break
            if not self._is_uploaded(path) and self._remove(path):
                deleted["не выгруженных в S3"] += 1
                self._remove_if_empty(path.parent)

        summary = ", ".join(f"{k}: {v}" for k, v in deleted.items())
        if deleted["не выгруженных в S3"]:
            log.warning("очистка диска удалила пакеты, которых нет в S3 (%d) - они потеряны",
                        deleted["не выгруженных в S3"])
        if self._stop.is_set():
            log.info("очистка диска прервана остановкой скрипта: удалено пакетов (%s), %s", summary, self.usage_str())
        elif self._reached(self.target, above=False):
            log.info("очистка диска: удалено пакетов (%s), теперь %s", summary, self.usage_str())
        else:
            # чтобы не повторять безуспешную очистку на каждом снимке
            self._next_cleanup_try = time.monotonic() + self.retry_sec
            log.error("очистка диска: удалено пакетов (%s), но цель не достигнута (%s). "
                      "Остались только не выгруженные пакеты моложе %g дн. или место занято чем-то другим",
                      summary, self.usage_str(), self.min_age_days)

    def _remove(self, path: Path) -> bool:
        """Удаляет папку пакета или дня целиком (под блокировкой). True - если удалена."""
        with self._lock:
            try:
                shutil.rmtree(path)
            except FileNotFoundError:
                return False
            except OSError:
                log.exception("не удалось удалить %s", path)
                return False
        self._removed_since_pause += 1
        if self._removed_since_pause >= self.CLEANUP_BATCH:
            self._removed_since_pause = 0
            self._stop.wait(self.CLEANUP_BATCH_PAUSE)  # даём диску передышку для записи новых пакетов
        return True

    @staticmethod
    def _remove_if_empty(day_path: Path) -> None:
        try:
            day_path.rmdir()  # удалится, только если папка дня пустая
        except OSError:
            pass
