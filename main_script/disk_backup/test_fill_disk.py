#!/usr/bin/env python3
"""Compatibility alias for test_backup.py."""
from test_backup import *  # noqa: F401,F403
from test_backup import main

if __name__ == "__main__":
    raise SystemExit(main())
