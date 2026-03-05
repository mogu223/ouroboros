"""
Supervisor — Event handlers.

Handles events from workers: send_message, progress, tool calls, etc.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Dict

log = logging.getLogger(__name__)


def _handle_send_message(evt: Dict[str, Any], ctx: Any) -> None:
    """Handle send_message event for both Telegram and Discord.
    
    Discord chat_ids are prefixed with 'discord_' followed by the user ID.
    """
    try:
        chat_id_raw = evt.get("chat_id")
        text = str(evt.get("text") or "")
        log_text = str(evt.get("log_text") if evt.get("log_text") is not None else "")
        fmt = str(evt.get("format") or "")
        is_progress = bool(evt.get("is_progress"))
        
        # Check if this is a Discord message
        if isinstance(chat_id_raw, str) and chat_id_raw.startswith("discord_"):
            # Discord message
            try:
                user_id = int(chat_id_raw.replace("discord_", ""))
                from ouroboros.channels.discord_bridge import send_discord_message
                ok = send_discord_message(user_id, text)
                if not ok:
                    log.warning("Failed to send Discord message to %s", user_id)
            except Exception as discord_err:
                log.error("Discord send error: %s", discord_err)
                # Fallback: log the error
                ctx.append_jsonl(
                    ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
                    {
                        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "type": "discord_send_error",
                        "user_id": chat_id_raw,
                        "error": repr(discord_err),
                    },
                )
        else:
            # Telegram message (original behavior)
            ctx.send_with_budget(
                int(chat_id_raw),
                text,
                log_text=(str(log_text) if isinstance(log_text, str) else None),
                fmt=fmt,
                is_progress=is_progress,
            )
    except Exception as e:
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "send_message_event_error",
                "error": repr(e),
            },
        )
