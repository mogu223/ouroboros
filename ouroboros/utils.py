"""
Ouroboros — Shared utilities.

Single source for helper functions used across all modules.
Does not import anything from ouroboros.* (zero dependency level).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import pathlib
import subprocess
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

# Default timezone: Shanghai (UTC+8)
# Can be overridden via TZ environment variable
_TZ_NAME = os.environ.get("TZ", "Asia/Shanghai")
_TZ_OFFSET = _dt.timedelta(hours=8)  # Shanghai is UTC+8
_TZ = _dt.timezone(_TZ_OFFSET, _TZ_NAME)


def utc_now_iso() -> str:
    """Return current time in configured timezone (default: Shanghai UTC+8)."""
    return _dt.datetime.now(tz=_TZ).isoformat()


def utc_now() -> _dt.datetime:
    """Return current datetime in configured timezone (default: Shanghai UTC+8)."""
    return _dt.datetime.now(tz=_TZ)

