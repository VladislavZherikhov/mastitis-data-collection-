#!/usr/bin/env python3
"""Test helper for backup.py.

The script generates screenshot-like image files to fill the monitored disk.
Generated files are marked as "old" (mtime in the past) so that backup.py
considers them eligible for deletion.  After filling, it runs one cleanup
pass, checks the result and removes any leftovers.  Everything is logged.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import backup  # noqa: E402

DEFAULT_LOG_FILE = BASE_DIR / "logs" / "test_backup.log"


def setup_logger(log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("disk_backup_test")
    logger.setLevel(logging.INFO)
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


def make_screenshot(path: Path, size_bytes: int, logger: logging.Logger) -> None:
    """Create a PNG image of approximately size_bytes bytes."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "Pillow is required for screenshot generation. "
            "Install it with: pip install Pillow"
        ) from exc

    # Aim for the requested size using a square RGB image.
    dimension = max(64, int((size_bytes / 3) ** 0.5))
    data = os.urandom(dimension * dimension * 3)
    image = Image.frombytes("RGB", (dimension, dimension), data)
    image.save(path, format="PNG", optimize=False)
    logger.debug("Generated %s (%d bytes)", path, path.stat().st_size)


def fill_disk(config: backup.Config, logger: logging.Logger,
             max_files: int, shot_size_mb: int,
             trigger: backup.LimitSpec | None = None) -> tuple[Path, int]:
    """Generate old screenshot files until the cleanup threshold is reached."""
    if trigger is None:
        trigger = config.threshold

    test_dir = config.media_dirs[0] / ".backup_test"
    test_dir.mkdir(parents=True, exist_ok=True)

    # Clean leftovers from a previous interrupted run.
    for old in test_dir.glob("*.png"):
        try:
            old.unlink()
        except OSError:
            pass

    shot_size = shot_size_mb * 1024 * 1024
    old_mtime = time.time() - 30 * 86400  # 30 days ago -> eligible for cleanup
    created = 0

    for i in range(max_files):
        usage = shutil.disk_usage(config.mount)
        if created > 0 and backup.limit_reached(usage, trigger):
            logger.info("Reached fill trigger %s", trigger.describe())
            break
        if usage.free < 1 * 1024 * 1024 * 1024:
            logger.warning("Less than 1 GB free; stopping generation early")
            break

        path = test_dir / f"screenshot_{int(time.time() * 1000)}_{i:04d}.png"
        logger.info("Creating screenshot %d/%d: %s", i + 1, max_files, path)
        make_screenshot(path, shot_size, logger)
        os.utime(path, (old_mtime, old_mtime))
        created += 1

        usage = shutil.disk_usage(config.mount)
        logger.info(
            "After file %d: used=%.2f%% free=%s", created,
            usage.used / usage.total * 100.0, backup.format_bytes(usage.free),
        )

    return test_dir, created


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fill a disk with test screenshots and check backup.py.",
    )
    parser.add_argument("--max-files", type=int, default=20,
                        help="Maximum number of screenshots to generate (default: 20)")
    parser.add_argument("--shot-size-mb", type=int, default=50,
                        help="Approximate size of one screenshot in MB (default: 50)")
    parser.add_argument("--fill-to", type=int, default=None,
                        help="Fill disk up to this usage percent (default: threshold + 2)")
    parser.add_argument("--keep-files", action="store_true",
                        help="Do not delete the generated test directory afterwards")
    # Passthrough options for backup.py configuration:
    parser.add_argument("--mount", help="Disk mount point")
    parser.add_argument("--dirs", help="Comma separated media directories")
    parser.add_argument("--threshold", help="Trigger threshold, e.g. 80 or 80%")
    parser.add_argument("--target", help="Target usage after cleanup, e.g. 70 or 70%")
    parser.add_argument("--free-threshold", dest="free_threshold",
                        help="Trigger threshold as free space, e.g. 2GB")
    parser.add_argument("--free-target", dest="free_target",
                        help="Target free space after cleanup, e.g. 5GB")
    parser.add_argument("--min-age", dest="min_age", type=int,
                        help="Minimum file age in days before deletion")
    parser.add_argument("--log-file", dest="log_file", help="Log file path for backup.py")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = backup.build_config(args)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    log_file = (BASE_DIR / "logs" / "test_backup.log")
    logger = setup_logger(log_file)
    logger.info("Starting disk fill/cleanup test")

    trigger = backup.LimitSpec("percent", args.fill_to) if args.fill_to is not None else config.threshold

    before = shutil.disk_usage(config.mount)
    logger.info("Before test: used=%.2f%% free=%s",
                before.used / before.total * 100.0, backup.format_bytes(before.free))

    test_dir, created = fill_disk(
        config, logger, max_files=args.max_files,
        shot_size_mb=args.shot_size_mb, trigger=trigger,
    )
    logger.info("Generated %d screenshot file(s) in %s", created, test_dir)

    filled_usage = shutil.disk_usage(config.mount)
    filled_pct = filled_usage.used / filled_usage.total * 100.0
    logger.info("After fill: used=%.2f%% free=%s", filled_pct,
                backup.format_bytes(filled_usage.free))

    filled_reached = backup.limit_reached(filled_usage, config.threshold)
    if not filled_reached:
        logger.warning(
            "Disk did not reach threshold (%s); "
            "cleanup pass will report 'nothing to do'.",
            config.threshold.describe(),
        )

    # Run the real cleanup routine.
    backup_logger = backup.setup_logger(config.log_file)
    logger.info("Running cleanup pass")
    stats = backup.cleanup(config, backup_logger)
    logger.info("Cleanup stats: %s", stats)

    after = shutil.disk_usage(config.mount)
    after_pct = after.used / after.total * 100.0
    logger.info("After cleanup: used=%.2f%% free=%s", after_pct,
                backup.format_bytes(after.free))

    remaining_test_files = list(test_dir.glob("*.png")) if test_dir.exists() else []
    logger.info("Remaining generated test screenshots: %d", len(remaining_test_files))

    target_ok = backup.target_reached(after, config.target)
    success = True
    if filled_reached:
        if stats.get("deleted_files", 0) <= 0:
            logger.error("FAIL: threshold was reached but no files were deleted")
            success = False
        if not target_ok:
            logger.error("FAIL: cleanup target not reached (%s)",
                         config.target.describe())
            success = False

    if success:
        logger.info("RESULT: PASS - disk cleanup works correctly")
    else:
        logger.error("RESULT: FAIL - see messages above")

    if not args.keep_files and test_dir.exists():
        logger.info("Removing test directory %s", test_dir)
        shutil.rmtree(test_dir, ignore_errors=True)

    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
