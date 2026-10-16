#!/usr/bin/env python3
"""Compatibility alias for backup.py."""
from backup import *  # noqa: F401,F403
from backup import main

if __name__ == "__main__":
    raise SystemExit(main())
