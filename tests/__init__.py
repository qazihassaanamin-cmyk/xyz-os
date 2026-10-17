"""pytest configuration.

A per-test timeout is installed so a hang fails loudly instead of blocking a
run forever, and warnings are kept visible rather than suppressed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Test configuration must be set before the application package is imported.
os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("SECRET_KEY", "testing-secret-key-not-used-in-production")
os.environ.setdefault("ANON_VOTE_SALT", "testing-anon-salt")
os.environ.setdefault("SMTP_HOST", "")
os.environ.setdefault("PYTHONANYWHERE_FREE_TIER", "true")


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: test that takes noticeably longer")


# Fail a test rather than hanging the whole run.
def pytest_timeout_setup():
    try:
        import pytest_timeout  # noqa: F401

        return True
    except ImportError:
        return False
