"""
Ouroboros — Scheduler Tool.

Simple time-based reminders that trigger via background consciousness.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ouroboros.utils import utc_now_iso, read_text, write_text

log = logging.getLogger(__name__)

# Path will be set by ToolContext
_DRIVE_ROOT = None


def _get_reminders_path() -> Any:
    """Get path to reminders file."""
    global _DRIVE_ROOT
    if _DRIVE_ROOT is None:
        return None
    return _DRIVE_ROOT / "state" / "reminders.json"


def _load_reminders() -> List[Dict[str, Any]]:
    """Load reminders from file."""
    path = _get_reminders_path()
    if path is None or not path.exists():
        return []
    try:
        data = json.loads(read_text(path))
        return data if isinstance(data, list) else []
    except Exception:
        log.debug("Failed to load reminders", exc_info=True)
        return []


def _save_reminders(reminders: List[Dict[str, Any]]) -> None:
    """Save reminders to file."""
    path = _get_reminders_path()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(path, json.dumps(reminders, ensure_ascii=False, indent=2))


def schedule_reminder(
    trigger_at: str,
    message: str,
    context: Optional[str] = None,
) -> str:
    """
    Schedule a reminder for a future time.

    Args:
        trigger_at: ISO datetime string (e.g., "2026-03-04T11:00:00") 
                    or relative time (e.g., "+2h", "+30m", "tomorrow 09:00")
        message: The reminder message to send when triggered
        context: Optional context/instructions for the agent when triggered

    Returns:
        Confirmation message with reminder ID
    """
    # Parse trigger_at
    trigger_dt = _parse_trigger_time(trigger_at)
    if trigger_dt is None:
        return f"❌ Invalid trigger time: {trigger_at}. Use ISO format or relative time like '+2h', 'tomorrow 09:00'."

    reminder = {
        "id": f"rem_{uuid.uuid4().hex[:8]}",
        "trigger_at": trigger_dt.isoformat(),
        "message": message,
        "context": context or "",
        "created_at": utc_now_iso(),
        "status": "pending",
    }

    reminders = _load_reminders()
    reminders.append(reminder)
    _save_reminders(reminders)

    return f"✅ Reminder scheduled for {trigger_dt.strftime('%Y-%m-%d %H:%M')} UTC\nID: {reminder['id']}\nMessage: {message}"


def _parse_trigger_time(trigger_at: str) -> Optional[datetime]:
    """Parse trigger_at string into datetime."""
    trigger_at = trigger_at.strip()

    # Try ISO format first
    try:
        # Handle both with and without timezone
        if trigger_at.endswith("Z"):
            trigger_at = trigger_at[:-1] + "+00:00"
        dt = datetime.fromisoformat(trigger_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass

    # Relative time: +Nh, +Nm, +Ns
    if trigger_at.startswith("+"):
        try:
            unit = trigger_at[-1].lower()
            amount = int(trigger_at[1:-1])
            now = datetime.now(timezone.utc)
            if unit == "h":
                from datetime import timedelta
                return now + timedelta(hours=amount)
            elif unit == "m":
                from datetime import timedelta
                return now + timedelta(minutes=amount)
            elif unit == "s":
                from datetime import timedelta
                return now + timedelta(seconds=amount)
        except (ValueError, IndexError):
            pass

    # "tomorrow HH:MM"
    if trigger_at.lower().startswith("tomorrow"):
        try:
            time_part = trigger_at[8:].strip()
            hour, minute = map(int, time_part.split(":"))
            now = datetime.now(timezone.utc)
            tomorrow = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            from datetime import timedelta
            tomorrow += timedelta(days=1)
            return tomorrow
        except (ValueError, IndexError):
            pass

    return None


def check_due_reminders() -> List[Dict[str, Any]]:
    """
    Check for reminders that are due and return them.
    Called by background consciousness on each wakeup.
    """
    reminders = _load_reminders()
    now = datetime.now(timezone.utc)
    due = []

    for r in reminders:
        if r.get("status") != "pending":
            continue
        try:
            trigger_dt = datetime.fromisoformat(r["trigger_at"])
            if trigger_dt.tzinfo is None:
                trigger_dt = trigger_dt.replace(tzinfo=timezone.utc)
            if trigger_dt <= now:
                due.append(r)
        except Exception:
            log.debug(f"Failed to parse reminder trigger_at: {r}", exc_info=True)

    return due


def mark_reminder_sent(reminder_id: str) -> None:
    """Mark a reminder as sent/completed."""
    reminders = _load_reminders()
    for r in reminders:
        if r.get("id") == reminder_id:
            r["status"] = "sent"
            r["sent_at"] = utc_now_iso()
            break
    _save_reminders(reminders)


def list_reminders(include_sent: bool = False) -> str:
    """List all pending reminders."""
    reminders = _load_reminders()
    pending = [r for r in reminders if r.get("status") == "pending" or include_sent]

    if not pending:
        return "(no pending reminders)"

    lines = ["📅 Reminders:"]
    now = datetime.now(timezone.utc)
    for r in pending:
        status = r.get("status", "pending")
        trigger_dt = datetime.fromisoformat(r["trigger_at"])
        if trigger_dt.tzinfo is None:
            trigger_dt = trigger_dt.replace(tzinfo=timezone.utc)
        delta = trigger_dt - now
        delta_str = _format_delta(delta)
        lines.append(f"  [{status}] {r['id']}: {trigger_dt.strftime('%m-%d %H:%M')} UTC ({delta_str})")
        lines.append(f"    → {r['message'][:80]}")

    return "\n".join(lines)


def cancel_reminder(reminder_id: str) -> str:
    """Cancel a pending reminder."""
    reminders = _load_reminders()
    for r in reminders:
        if r.get("id") == reminder_id:
            r["status"] = "cancelled"
            _save_reminders(reminders)
            return f"✅ Reminder {reminder_id} cancelled."
    return f"❌ Reminder {reminder_id} not found."


def _format_delta(delta) -> str:
    """Format a timedelta as human-readable string."""
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return "OVERDUE"
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    if hours > 24:
        days = hours // 24
        return f"in {days}d {hours % 24}h"
    elif hours > 0:
        return f"in {hours}h {minutes}m"
    else:
        return f"in {minutes}m"


# Tool schemas for registry
TOOLS = [
    {
        "name": "schedule_reminder",
        "description": "Schedule a reminder for a future time. The reminder will trigger and send a message to you when the time comes. Use this for time-based notifications.",
        "parameters": {
            "type": "object",
            "properties": {
                "trigger_at": {
                    "type": "string",
                    "description": "When to trigger. ISO datetime (e.g., '2026-03-04T11:00:00') or relative ('+2h', '+30m', 'tomorrow 09:00'). Timezone is UTC."
                },
                "message": {
                    "type": "string",
                    "description": "The reminder message to send when triggered"
                },
                "context": {
                    "type": "string",
                    "description": "Optional context/instructions for yourself when the reminder triggers"
                }
            },
            "required": ["trigger_at", "message"]
        }
    },
    {
        "name": "list_reminders",
        "description": "List all pending reminders you have scheduled.",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "cancel_reminder",
        "description": "Cancel a pending reminder by ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "reminder_id": {
                    "type": "string",
                    "description": "The ID of the reminder to cancel"
                }
            },
            "required": ["reminder_id"]
        }
    }
]


def get_tools():
    """Return tool schemas for registry."""
    return TOOLS


def init(drive_root: Any) -> None:
    """Initialize scheduler with drive root path."""
    global _DRIVE_ROOT
    _DRIVE_ROOT = drive_root