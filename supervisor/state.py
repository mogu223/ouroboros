"""
Supervisor — State management.

Loads/saves state.json, budget tracking, state utilities.
"""

from __future__ import annotations

import datetime
import json
import logging
import pathlib
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

log = logging.getLogger(__name__)

# Module-level config (set by supervisor init)
DRIVE_ROOT: pathlib.Path = pathlib.Path("/content/drive/MyDrive/Ouroboros")
STATE_PATH: pathlib.Path = DRIVE_ROOT / "state" / "state.json"
TOTAL_BUDGET_LIMIT: float = 500_000.0  # Default budget cap
EVOLUTION_BUDGET_RESERVE: float = 5.0  # Reserve for evolution tasks

# Queue snapshot path
QUEUE_SNAPSHOT_PATH: pathlib.Path = DRIVE_ROOT / "state" / "queue_snapshot.json"

# Legacy failure threshold used for evolution backoff notices.
EVOLUTION_FAILURE_THRESHOLD: int = 100  # Evolution stays enabled; threshold only triggers backoff.


def ensure_state_defaults(st: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure all required state fields have default values."""
    st.setdefault("created_at", datetime.datetime.now(datetime.timezone.utc).isoformat())
    st.setdefault("owner_id", None)
    st.setdefault("owner_chat_id", None)
    st.setdefault("tg_offset", 0)
    # Discord integration
    st.setdefault("discord_owner_id", None)
    st.setdefault("discord_offset", None)
    st.setdefault("discord_enabled", False)
    st.setdefault("spent_usd", 0.0)
    st.setdefault("spent_calls", 0)
    st.setdefault("spent_tokens_prompt", 0)
    st.setdefault("spent_tokens_completion", 0)
    st.setdefault("spent_tokens_cached", 0)
    st.setdefault("session_id", uuid.uuid4().hex)
    st.setdefault("current_branch", None)
    st.setdefault("current_sha", None)
    st.setdefault("last_owner_message_at", "")
    st.setdefault("last_evolution_task_at", "")
    st.setdefault("budget_messages_since_report", 0)
    st.setdefault("evolution_mode_enabled", False)
    st.setdefault("evolution_cycle", 0)
    st.setdefault("session_total_snapshot", None)
    st.setdefault("session_spent_snapshot", None)
    st.setdefault("budget_drift_pct", None)
    st.setdefault("budget_drift_alert", False)
    st.setdefault("evolution_consecutive_failures", 0)
    st.setdefault("evolution_backoff_until", "")
    st.setdefault("evolution_backoff_reason", "")
    # Remove legacy keys
    for legacy_key in ("approvals", "idle_cursor", "idle_stats", "last_idle_task_at",
                        "last_auto_review_at", "last_review_task_id", "session_daily_snapshot"):
        st.pop(legacy_key, None)
    return st


def load_state() -> Dict[str, Any]:
    """Load state from disk."""
    try:
        if STATE_PATH.exists():
            st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            return ensure_state_defaults(st)
    except Exception as e:
        log.warning("Failed to load state: %s", e)
    
    # Return fresh state with defaults
    return ensure_state_defaults({})


def save_state(st: Dict[str, Any]) -> None:
    """Save state to disk."""
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(st, indent=2), encoding="utf-8")
    except Exception as e:
        log.error("Failed to save state: %s", e)


def append_jsonl(path: pathlib.Path, entry: Dict[str, Any]) -> None:
    """Append an entry to a JSONL file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("Failed to append to %s: %s", path, e)


def atomic_write_text(path: pathlib.Path, content: str) -> None:
    """Write text atomically (write to temp, then rename)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.rename(path)
    except Exception as e:
        log.error("Failed to write %s: %s", path, e)


def budget_remaining(st: Optional[Dict[str, Any]] = None) -> float:
    """Calculate remaining budget.

    TOTAL_BUDGET_LIMIT <= 0 means no hard cap (infinite budget).
    Accepts optional preloaded state for call sites that already have it.
    """
    state_obj = st if isinstance(st, dict) else load_state()
    spent = float(state_obj.get("spent_usd") or 0.0)
    if float(TOTAL_BUDGET_LIMIT or 0.0) <= 0:
        return float("inf")
    return max(0.0, float(TOTAL_BUDGET_LIMIT) - spent)


def budget_pct(st: Optional[Dict[str, Any]] = None) -> float:
    """Calculate budget usage percentage (0.0 - 1.0).

    With TOTAL_BUDGET_LIMIT <= 0 (no cap), always returns 0.0.
    """
    if float(TOTAL_BUDGET_LIMIT or 0.0) <= 0:
        return 0.0
    state_obj = st if isinstance(st, dict) else load_state()
    spent = float(state_obj.get("spent_usd") or 0.0)
    return min(1.0, spent / float(TOTAL_BUDGET_LIMIT))
