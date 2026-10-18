#!/usr/bin/env python3
"""Backup/maintenance helper for a disk mounted on Raspberry Pi.

The script monitors disk usage. When usage reaches the configured threshold
(default 80 %) it removes the oldest photo/video files until usage is back
to the configured target (default 70 %).

Environment variables:
    DISK_MOUNT_POINT   Mount point of the disk (default: /mnt/hdd)
    LOG_FILE           Log file path (default: ./logs/backup.log in project root)
    MEDIA_DIRS         Comma separated directories, relative to mount point
                       or absolute (default: photos,videos)
    DISK_USAGE_THRESHOLD  Trigger threshold in percent (default: 80)
    DISK_USAGE_TARGET     Target usage after cleanup in percent (default: 70)
    MIN_FILE_AGE_DAYS  Do not touch files newer than this many days (default: 1)
    LOG_FILE           Log file path (default: <script_dir>/logs/backup.log)
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

MEDIA_EXTENSIONS = {
    # Images
    ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff",
    ".heic", ".heif", ".webp", ".raw", ".cr2", ".nef", ".arw",
    # Videos
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm",
    ".m4v", ".mpg", ".mpeg", ".3gp", ".h264", ".mts", ".m2ts",
}


def format_bytes(num: float) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(num)
    for unit in units:
        if abs(size) < 1024.0 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} {units[-1]}"


@dataclass
class LimitSpec:
    """A disk limit expressed either in percent or in bytes."""

    mode: str  # "percent" or "bytes"
    value: float
    source: str = ""

    def describe(self) -> str:
        if self.mode == "percent":
            return f"{self.value:g}%"
        return format_bytes(self.value)


@dataclass
class Config:
    mount: Path
    media_dirs: list[Path]
    threshold: LimitSpec
    target: LimitSpec
    min_age_days: int
    log_file: Path
    dry_run: bool = False


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


_PERCENT_UNITS = {"%", "PERCENT", "PCT", "PERCENTS"}
_SIZE_UNITS = {
    "B": 1,
    "BYTE": 1,
    "BYTES": 1,
    "KB": 1024,
    "KIB": 1024,
    "K": 1024,
    "MB": 1024 ** 2,
    "MIB": 1024 ** 2,
    "M": 1024 ** 2,
    "GB": 1024 ** 3,
    "GIB": 1024 ** 3,
    "G": 1024 ** 3,
    "TB": 1024 ** 4,
    "TIB": 1024 ** 4,
    "T": 1024 ** 4,
    "PB": 1024 ** 5,
    "PIB": 1024 ** 5,
    "P": 1024 ** 5,
}


def _split_number_unit(raw: str) -> tuple[str, str]:
    s = str(raw).strip()
    match = re.match(r"^([0-9]*\.?[0-9]+)\s*([A-Za-z%]*)$", s)
    if not match:
        raise ValueError(f"Cannot parse limit value: {raw!r}")
    return match.group(1), match.group(2)


def parse_limit(raw, default_mode: str, source: str = "") -> LimitSpec | None:
    """Parse a limit like 80, 80%, 500MB, 2 GiB.

    A bare number uses ``default_mode`` ("percent" or "bytes").
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None

    number_str, unit_str = _split_number_unit(s)
    value = float(number_str)
    unit = unit_str.strip().upper()

    if unit in _PERCENT_UNITS:
        return LimitSpec("percent", value, source)
    if unit in _SIZE_UNITS:
        return LimitSpec("bytes", value * _SIZE_UNITS[unit], source)
    if unit == "":
        if default_mode == "percent":
            return LimitSpec("percent", value, source)
        return LimitSpec("bytes", value, source)

    raise ValueError(f"Unknown unit in limit value: {raw!r}")


def limit_reached(usage: object, limit: LimitSpec) -> bool:
    """Return True when disk usage has crossed the cleanup threshold."""
    if limit.mode == "percent":
        used_pct = usage.used / usage.total * 100.0
        return used_pct >= limit.value
    return usage.free < limit.value


def target_free_bytes(usage: object, limit: LimitSpec) -> int:
    """Free bytes that should remain after cleanup."""
    if limit.mode == "percent":
        return int(usage.total * (100.0 - limit.value) / 100.0)
    return int(limit.value)


def target_reached(usage: object, limit: LimitSpec) -> bool:
    """Return True when the post-cleanup target has been reached."""
    if limit.mode == "percent":
        used_pct = usage.used / usage.total * 100.0
        return used_pct <= limit.value
    return usage.free >= limit.value


def validate_limits(threshold: LimitSpec, target: LimitSpec) -> None:
    if threshold.mode == "percent" and target.mode == "percent":
        if not (1 <= target.value < threshold.value <= 100):
            raise ValueError(
                "Config error: require 1 <= target percent < threshold percent <= 100"
            )
        return
    if threshold.mode == "bytes" and target.mode == "bytes":
        if not (threshold.value > 0 and target.value > threshold.value):
            raise ValueError(
                "Config error: target free bytes must be greater than threshold free bytes"
            )
        return
    # Mixed modes: best-effort validation only.
    if threshold.mode == "percent" and target.mode == "bytes":
        if target.value <= 0:
            raise ValueError("Config error: target free bytes must be positive")
        return
    if threshold.mode == "bytes" and target.mode == "percent":
        if not (1 <= target.value <= 100):
            raise ValueError("Config error: target percent must be between 1 and 100")
        return


def build_config(args: argparse.Namespace | None = None) -> Config:
    if args is None:
        args = argparse.Namespace(
            mount=None, dirs=None, threshold=None, target=None,
            min_age=None, log_file=None, dry_run=False,
        )

    mount = Path(getattr(args, "mount", None) or os.getenv("DISK_MOUNT_POINT", "/mnt/hdd"))

    dirs_raw = getattr(args, "dirs", None)
    if dirs_raw is None:
        dirs_raw = os.getenv("MEDIA_DIRS", "photos,videos")
    media_dirs: list[Path] = []
    for item in str(dirs_raw).split(","):
        item = item.strip()
        if not item:
            continue
        p = Path(item)
        if not p.is_absolute():
            p = mount / p
        media_dirs.append(p)

    min_age = getattr(args, "min_age", None)
    if min_age is None:
        min_age = _env_int("MIN_FILE_AGE_DAYS", 1)

    log_raw = getattr(args, "log_file", None)
    if log_raw is None:
        log_raw = os.getenv("LOG_FILE")
    if not log_raw:
        log_raw = str(DEFAULT_LOG_DIR / "backup.log")
    log_file = Path(log_raw)

    dry_run = bool(getattr(args, "dry_run", False))

    free_threshold_raw = getattr(args, "free_threshold", None)
    free_target_raw = getattr(args, "free_target", None)

    def first_limit(candidates):
        for raw, default_mode, source in candidates:
            if raw is None:
                continue
            if isinstance(raw, str) and raw.strip() == "":
                continue
            parsed = parse_limit(raw, default_mode, source)
            if parsed is not None:
                return parsed
        return None

    threshold_limit = first_limit([
        (free_threshold_raw, "bytes", "--free-threshold"),
        (os.getenv("DISK_FREE_THRESHOLD"), "bytes", "env DISK_FREE_THRESHOLD"),
        (getattr(args, "threshold", None), "percent", "--threshold"),
        (os.getenv("DISK_USAGE_THRESHOLD"), "percent", "env DISK_USAGE_THRESHOLD"),
    ])
    if threshold_limit is None:
        threshold_limit = LimitSpec("percent", 80.0, "default")

    target_limit = first_limit([
        (free_target_raw, "bytes", "--free-target"),
        (os.getenv("DISK_FREE_TARGET"), "bytes", "env DISK_FREE_TARGET"),
        (getattr(args, "target", None), "percent", "--target"),
        (os.getenv("DISK_USAGE_TARGET"), "percent", "env DISK_USAGE_TARGET"),
    ])
    if target_limit is None:
        target_limit = LimitSpec("percent", 70.0, "default")

    validate_limits(threshold_limit, target_limit)

    return Config(
        mount=mount,
        media_dirs=media_dirs,
        threshold=threshold_limit,
        target=target_limit,
        min_age_days=min_age,
        log_file=log_file,
        dry_run=dry_run,
    )


def setup_logger(log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("disk_backup")
    logger.setLevel(logging.INFO)
    # Avoid duplicate handlers when called several times.
    logger.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    return logger


def collect_media(media_dirs: Iterable[Path]) -> list[tuple[Path, float, int]]:
    """Return (path, mtime, size) for every supported media file."""
    result: list[tuple[Path, float, int]] = []
    for directory in media_dirs:
        if not directory.exists():
            continue
        try:
            for path in directory.rglob("*"):
                if not path.is_file():
                    continue
                if path.suffix.lower() not in MEDIA_EXTENSIONS:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                result.append((path, stat.st_mtime, stat.st_size))
        except OSError as exc:
            # Per-directory failure must not abort the whole run.
            continue
    return result


def cleanup(config: Config, logger: logging.Logger | None = None) -> dict:
    """Run one cleanup pass. Returns a small statistics dictionary."""
    if logger is None:
        logger = setup_logger(config.log_file)

    usage = shutil.disk_usage(config.mount)
    used_pct = usage.used / usage.total * 100.0
    logger.info(
        "Disk check: mount=%s used=%d/%d bytes (%.2f%%), free=%d bytes, "
        "threshold=%s, target=%s",
        config.mount, usage.used, usage.total, used_pct, usage.free,
        config.threshold.describe(), config.target.describe(),
    )

    if not limit_reached(usage, config.threshold):
        logger.info("Usage below threshold (%s); nothing to do.",
                    config.threshold.describe())
        return {
            "threshold_reached": False,
            "deleted_files": 0,
            "freed_bytes": 0,
            "used_percent_before": used_pct,
            "used_percent_after": used_pct,
        }

    logger.warning("Threshold reached: %s", config.threshold.describe())

    # Amount of free space we want to reach after cleanup.
    free_target = target_free_bytes(usage, config.target)
    needed_bytes = max(0, free_target - usage.free)
    logger.info("Need to free at least %d bytes to reach target %s",
                needed_bytes, config.target.describe())

    now = time.time()
    min_mtime = now - config.min_age_days * 86400.0

    candidates = sorted(
        (item for item in collect_media(config.media_dirs) if item[1] <= min_mtime),
        key=lambda item: item[1],
    )
    logger.info("Found %d eligible media files older than %d day(s)",
                len(candidates), config.min_age_days)

    deleted_files = 0
    freed_bytes = 0
    errors = 0
    for path, mtime, size in candidates:
        if freed_bytes >= needed_bytes and needed_bytes > 0:
            break
        try:
            if config.dry_run:
                logger.info("[DRY-RUN] would delete %s (size=%d, mtime=%s)",
                            path, size, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)))
            else:
                path.unlink()
                logger.info("Deleted %s (size=%d)", path, size)
            deleted_files += 1
            freed_bytes += size
        except OSError as exc:
            errors += 1
            logger.error("Failed to delete %s: %s", path, exc)

    usage_after = shutil.disk_usage(config.mount)
    used_pct_after = usage_after.used / usage_after.total * 100.0
    logger.info(
        "Cleanup finished: deleted=%d files, freed=%d bytes, errors=%d, "
        "used now=%.2f%%, free now=%d bytes",
        deleted_files, freed_bytes, errors, used_pct_after, usage_after.free,
    )

    if not config.dry_run and freed_bytes < needed_bytes:
        logger.warning(
            "Could not free enough space: freed %d of %d needed bytes. "
            "Consider lowering MIN_FILE_AGE_DAYS or checking media directories.",
            freed_bytes, needed_bytes,
        )

    return {
        "threshold_reached": True,
        "deleted_files": deleted_files,
        "freed_bytes": freed_bytes,
        "errors": errors,
        "used_percent_before": used_pct,
        "used_percent_after": used_pct_after,
        "needed_bytes": needed_bytes,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete old photos/videos when disk usage reaches a threshold.",
    )
    parser.add_argument("--mount", help="Disk mount point")
    parser.add_argument("--dirs", help="Comma separated media directories")
    parser.add_argument("--threshold", help="Trigger threshold, e.g. 80 or 80%")
    parser.add_argument("--target", help="Target usage after cleanup, e.g. 70 or 70%")
    parser.add_argument("--free-threshold", dest="free_threshold",
                        help="Trigger threshold as free space, e.g. 2GB, 500MB")
    parser.add_argument("--free-target", dest="free_target",
                        help="Target free space after cleanup, e.g. 5GB, 2GB")
    parser.add_argument("--min-age", dest="min_age", type=int,
                        help="Minimum file age in days before deletion")
    parser.add_argument("--log-file", dest="log_file", help="Log file path")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="Log actions without deleting files")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = build_config(args)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    logger = setup_logger(config.log_file)
    logger.info("Starting disk backup/cleanup pass")
    stats = cleanup(config, logger)
    logger.info("Finished: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
