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


def read_text(path, encoding="utf-8"):
    import pathlib
    p = pathlib.Path(path)
    if not p.exists(): return ""
    return p.read_text(encoding=encoding)

def append_jsonl(path, data, encoding="utf-8"):
    import pathlib, json
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding=encoding) as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")

def safe_relpath(path, start="."):
    import pathlib
    try:
        return str(pathlib.Path(path).relative_to(start))
    except ValueError:
        return str(path)

def truncate_for_log(text, max_len=1000):
    if not text: return ""
    text = str(text)
    if len(text) <= max_len: return text
    return text[:max_len] + f"... [truncated {len(text) - max_len} chars]"

def get_git_info(repo_path):
    import subprocess
    try:
        cwd = str(repo_path)
        branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd, text=True).strip()
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=cwd, text=True).strip()
        return {"branch": branch, "commit": commit}
    except Exception:
        return {"branch": "unknown", "commit": "unknown"}

def sanitize_task_for_event(task, *args, **kwargs):
    if not task: return ""
    return truncate_for_log(str(task).replace("\n", " ").strip(), 200)

def estimate_tokens(text):
    if not text: return 0
    return len(str(text)) // 4 + 1

def sanitize_tool_args_for_log(args, max_len=500):
    import json
    try:
        s = json.dumps(args, ensure_ascii=False, default=str)
        return s[:max_len] + "..." if len(s) > max_len else s
    except Exception:
        return str(args)[:max_len]

def sanitize_tool_result_for_log(result, max_len=1000):
    import json
    try:
        if isinstance(result, str):
            s = result
        else:
            s = json.dumps(result, ensure_ascii=False, default=str)
        return s[:max_len] + "..." if len(s) > max_len else s
    except Exception:
        return str(result)[:max_len]

def write_text(path, text, encoding="utf-8"):
    import pathlib
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding=encoding)

def short(text, max_len=100):
    if not text: return ""
    text = str(text)
    return text[:max_len] + "..." if len(text) > max_len else text

def clip_text(text, max_len=1000):
    return short(text, max_len)


def run_cmd(cmd, cwd=None, timeout=None):
    import subprocess
    r = subprocess.run(cmd, cwd=cwd, timeout=timeout, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr

