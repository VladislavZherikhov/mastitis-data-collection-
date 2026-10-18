#!/usr/bin/env python3
"""Тест системы : проверка конфигурации, очистки диска и временных окон.

Можно задать:
  --mount          точка монтирования тестового диска
  --dirs           каталоги со снимками (через запятую)
  --threshold-pct  порог заполнения диска в процентах (например 80)
  --threshold-mb   порог заполнения диска в МБ (альтернатива процентам)
  --target-pct     цель после очистки в процентах (например 70)
  --target-mb      цель после очистки в МБ
  --min-age        минимальный возраст файла для удаления (дней)
  --window         временное окно в формате ЧЧ:ММ-ЧЧ:ММ (можно несколько)
  --interval       интервал между снимками (сек)
  --shots          сколько снимков сделать в тесте
  --dry-run        только проход без удаления файлов
  --keep-files     не удалять тестовую папку после теста

Тест не требует камер и S3 — проверяет конфиг, окна и очистку диска.
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

# Подключаем модули проекта
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "disk_backup") not in sys.path:
    sys.path.insert(0, str(ROOT / "disk_backup"))

import config                      # noqa: E402
from time_windows import (  # noqa: E402
    TimeWindow, parse_windows, active_until, next_start,
)
import backup                      # noqa: E402 (disk_backup/backup.py)
from backup import Config as DiskConfig, LimitSpec  # noqa: E402

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("test_system")

FAILURES = 0


def check(condition: bool, msg: str) -> None:
    global FAILURES
    if condition:
        log.info("PASS: %s", msg)
    else:
        FAILURES += 1
        log.error("FAIL: %s", msg)


# =====================================================================
# 1. Тест конфигурации
# =====================================================================
def test_config(env_overrides: dict[str, str]) -> None:
    log.info("--- Тест 1: конфигурация ---")
    for k, v in env_overrides.items():
        os.environ[k] = v

    # Перезагружаем config, чтобы подхватить новые переменные
    import importlib
    importlib.reload(config)

    check(config.S3_BUCKET == os.environ.get("S3_BUCKET", "mastitis-bucket"), "S3_BUCKET читается из окружения")
    check(config.INTERVAL_SEC > 0, "INTERVAL_SEC положительный")
    check(config.DISK_MOUNT_POINT is not None, "DISK_MOUNT_POINT задан")
    check(len(config.MEDIA_DIRS) > 0, "MEDIA_DIRS не пуст")
    check(0 < config.DISK_USAGE_TARGET < config.DISK_USAGE_THRESHOLD <= 100,
          "цель меньше порога, порог <= 100%")
    check(config.MIN_FILE_AGE_DAYS >= 0, "MIN_FILE_AGE_DAYS >= 0")
    check(config.LOG_DIR is not None, "LOG_DIR задан")

    # Окна
    try:
        windows = config.time_windows()
        check(len(windows) > 0, f"хотя бы одно окно задано ({len(windows)})")
        for w in windows:
            check(w.start != w.end, f"окно {w.name}: начало != конец")
    except SystemExit as e:
        check(False, f"окна заданы корректно (ошибка: {e.code})")


# =====================================================================
# 2. Тест временных окон
# =====================================================================
def test_time_windows(window_specs: list[str]) -> None:
    log.info("--- Тест 2: временные окна ---")
    try:
        windows = parse_windows({f"WINDOW_{i+1}": s for i, s in enumerate(window_specs)})
        check(len(windows) == len(window_specs), f"распарсено {len(windows)} окон")
    except SystemExit as e:
        check(False, f"окна парсятся без ошибки (ошибка: {e.code})")
        return

    now = datetime.now()
    # active_until не должен падать вне окон
    until = active_until(windows, now)
    check(until is None or until > now, "active_until возвращает корректное время или None")

    # next_start не должен падать
    start = next_start(windows, now)
    check(start > now, "next_start возвращает будущее время")

    # Проверка окна через полночь (22:00-02:00)
    midnight_windows = parse_windows({"WINDOW_1": "22:00-02:00"})
    w = midnight_windows[0]
    check(w.start.hour == 22 and w.end.hour == 2, "окно через полночь парсится корректно")


# =====================================================================
# 3. Тест очистки диска (проценты и МБ)
# =====================================================================
def test_disk_cleanup(mount: Path, dirs: list[str], threshold_pct: float | None,
                      threshold_mb: float | None, target_pct: float | None,
                      target_mb: float | None, min_age: int, dry_run: bool) -> None:
    log.info("--- Тест 3: очистка диска (mount=%s) ---", mount)

    if not mount.exists():
        check(False, f"точка монтирования {mount} существует")
        return

    media_dirs = [mount / d for d in dirs]
    for d in media_dirs:
        d.mkdir(parents=True, exist_ok=True)

    # Создаём тестовые файлы "старые" (старше min_age дней)
    old_mtime = time.time() - (max(min_age, 1) + 5) * 86400
    test_dir = media_dirs[0] / ".test_cleanup"
    test_dir.mkdir(exist_ok=True)

    created = []
    for i in range(5):
        p = test_dir / f"old_file_{i:03d}.jpg"
        p.write_bytes(b"\x00" * (1024 * 1024))  # 1 МБ каждый
        os.utime(p, (old_mtime, old_mtime))
        created.append(p)

    # Порог и цель
    threshold: LimitSpec | None = None
    target: LimitSpec | None = None

    if threshold_mb is not None:
        threshold = LimitSpec("bytes", threshold_mb * 1024 * 1024, "test")
    elif threshold_pct is not None:
        threshold = LimitSpec("percent", threshold_pct, "test")

    if target_mb is not None:
        target = LimitSpec("bytes", target_mb * 1024 * 1024, "test")
    elif target_pct is not None:
        target = LimitSpec("percent", target_pct, "test")

    if threshold is None:
        threshold = LimitSpec("percent", 80.0, "test_default")
    if target is None:
        target = LimitSpec("percent", 70.0, "test_default")

    try:
        cfg = DiskConfig(
            mount=mount,
            media_dirs=media_dirs,
            threshold=threshold,
            target=target,
            min_age_days=min_age,
            log_file=Path("logs/test_backup.log"),
            dry_run=dry_run,
        )
        stats = backup.cleanup(cfg)
        check("deleted_files" in stats, "cleanup вернул статистику")
        check(stats["deleted_files"] >= 0, "deleted_files >= 0")

        if dry_run:
            check(all(p.exists() for p in created), "в dry-run файлы не удалены")
        else:
            # В реальном режиме файлы должны быть удалены (если был достигнут порог)
            remaining = [p for p in created if p.exists()]
            log.info("Осталось тестовых файлов: %d", len(remaining))

    except Exception as exc:
        check(False, f"cleanup не упал с исключением (ошибка: {exc})")

    # Убираем тестовую папку
    if not dry_run and test_dir.exists():
        shutil.rmtree(test_dir, ignore_errors=True)


# =====================================================================
# 4. Интеграционный тест: конфиг + окна + диск
# =====================================================================
def test_integration(mount: Path, dirs: list[str], window_specs: list[str],
                     threshold_pct: float, min_age: int) -> None:
    log.info("--- Тест 4: интеграция (окна + диск) ---")

    # Устанавливаем переменные окружения для теста
    os.environ["DISK_MOUNT_POINT"] = str(mount)
    os.environ["MEDIA_DIRS"] = ",".join(dirs)
    os.environ["DISK_USAGE_THRESHOLD"] = str(threshold_pct)
    os.environ["DISK_USAGE_TARGET"] = "70"
    os.environ["MIN_FILE_AGE_DAYS"] = str(min_age)
    for i, s in enumerate(window_specs, 1):
        os.environ[f"WINDOW_{i}"] = s

    import importlib
    importlib.reload(config)

    try:
        windows = config.time_windows()
        check(len(windows) > 0, f"интеграция: окна загружены ({len(windows)})")
    except SystemExit as e:
        check(False, f"интеграция: окна без ошибки (ошибка: {e.code})")
        return

    # Дисковая очистка в интеграции
    try:
        media_dirs = [config.DISK_MOUNT_POINT / d for d in config.MEDIA_DIRS]
        stats = backup.cleanup(DiskConfig(
            mount=config.DISK_MOUNT_POINT,
            media_dirs=media_dirs,
            threshold=LimitSpec("percent", config.DISK_USAGE_THRESHOLD, "env"),
            target=LimitSpec("percent", config.DISK_USAGE_TARGET, "env"),
            min_age_days=config.MIN_FILE_AGE_DAYS,
            log_file=config.LOG_FILE,
        ))
        check("deleted_files" in stats, "интеграционный cleanup отработал")
    except Exception as exc:
        check(False, f"интеграционный cleanup не упал (ошибка: {exc})")


# =====================================================================
# main
# =====================================================================
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Тест системы  (конфиг, окна, диск)")
    p.add_argument("--mount", default=None, help="Точка монтирования тестового диска")
    p.add_argument("--dirs", default="photos,videos", help="Каталоги со снимками (через запятую)")
    p.add_argument("--threshold-pct", type=float, default=None, help="Порог заполнения диска в %")
    p.add_argument("--threshold-mb", type=float, default=None, help="Порог заполнения диска в МБ")
    p.add_argument("--target-pct", type=float, default=None, help="Цель после очистки в %")
    p.add_argument("--target-mb", type=float, default=None, help="Цель после очистки в МБ")
    p.add_argument("--min-age", type=int, default=1, help="Минимальный возраст файла (дней)")
    p.add_argument("--window", action="append", default=None,
                   help="Временное окно ЧЧ:ММ-ЧЧ:ММ (можно несколько раз)")
    p.add_argument("--interval", type=float, default=10, help="Интервал между снимками (сек)")
    p.add_argument("--shots", type=int, default=0, help="Снимков за окно (0 = без лимита)")
    p.add_argument("--dry-run", action="store_true", help="Только проход без удаления файлов")
    p.add_argument("--keep-files", action="store_true", help="Не удалять тестовую папку после теста")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    log.info("=" * 60)
    log.info("ТЕСТ СИСТЕМЫ «СЕВА»")
    log.info("=" * 60)

    window_specs = args.window or ["06:00-08:00", "18:00-20:00"]

    # Переменные окружения для config
    env_overrides = {
        "INTERVAL_SEC": str(args.interval),
        "SHOTS_PER_WINDOW": str(args.shots),
        "MIN_FILE_AGE_DAYS": str(args.min_age),
        "S3_BUCKET": "mastitis-bucket",
        "S3_PREFIX": "test-grab",
    }

    test_config(env_overrides)
    test_time_windows(window_specs)

    # Диск для теста очистки
    if args.mount:
        test_disk_cleanup(
            mount=Path(args.mount),
            dirs=[d.strip() for d in args.dirs.split(",") if d.strip()],
            threshold_pct=args.threshold_pct,
            threshold_mb=args.threshold_mb,
            target_pct=args.target_pct,
            target_mb=args.target_mb,
            min_age=args.min_age,
            dry_run=args.dry_run,
        )
        test_integration(
            mount=Path(args.mount),
            dirs=[d.strip() for d in args.dirs.split(",") if d.strip()],
            window_specs=window_specs,
            threshold_pct=args.threshold_pct if args.threshold_pct is not None else 80.0,
            min_age=args.min_age,
        )
    else:
        log.warning("--mount не указан — тест диска пропущен")

    log.info("=" * 60)
    if FAILURES == 0:
        log.info("РЕЗУЛЬТАТ: ВСЕ ТЕСТЫ ПРОЙДЕНЫ")
    else:
        log.error("РЕЗУЛЬТАТ: %d тест(ов) не прошли", FAILURES)
    log.info("=" * 60)

    return 0 if FAILURES == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
