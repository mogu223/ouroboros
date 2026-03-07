"""
Supervisor — Unified communication layer for multi-platform support.

Handles message routing between Telegram, Discord, and future platforms.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple, Union

log = logging.getLogger(__name__)


def send_message(chat_id: Union[int, str], text: str, parse_mode: str = "") -> Tuple[bool, str]:
    """
    Send message to any platform based on chat_id format.
    
    chat_id formats:
    - int or "tg_{id}": Telegram
    - "discord_{id}": Discord
    """
    # Determine platform from chat_id
    platform = _get_platform(chat_id)
    
    if platform == "telegram":
        return _send_telegram(chat_id, text, parse_mode)
    elif platform == "discord":
        return _send_discord(chat_id, text)
    else:
        log.warning("Unknown platform for chat_id: %s", chat_id)
        return False, "unknown_platform"


def send_chat_action(chat_id: Union[int, str], action: str = "typing") -> bool:
    """Send chat action (typing indicator) to any platform."""
    platform = _get_platform(chat_id)
    
    if platform == "telegram":
        try:
            from supervisor.telegram import get_tg
            tg_chat_id = _extract_id(chat_id)
            return get_tg().send_chat_action(tg_chat_id, action)
        except Exception as e:
            log.warning("Failed to send Telegram chat action", exc_info=True)
            return False
    elif platform == "discord":
        # Discord doesn't have a direct typing API exposed in our bridge
        # The bridge already sends "收到" message as acknowledgment
        return True
    else:
        return False


def send_photo(chat_id: Union[int, str], photo_bytes: bytes, caption: str = "") -> Tuple[bool, str]:
    """Send photo to any platform."""
    platform = _get_platform(chat_id)
    
    if platform == "telegram":
        try:
            from supervisor.telegram import get_tg
            tg_chat_id = _extract_id(chat_id)
            return get_tg().send_photo(tg_chat_id, photo_bytes, caption)
        except Exception as e:
            log.warning("Failed to send Telegram photo", exc_info=True)
            return False, str(e)
    elif platform == "discord":
        try:
            from ouroboros.channels.discord_bridge import send_discord_message
            # Discord bridge doesn't support photo sending yet, send as text
            user_id = _extract_id(chat_id, prefix="discord_")
            if user_id:
                # For now, just notify that photo was taken
                text = f"📸 Photo: {caption}" if caption else "📸 Photo attached"
                return send_discord_message(user_id, text), "ok"
            return False, "invalid_discord_id"
        except Exception as e:
            log.warning("Failed to send Discord photo", exc_info=True)
            return False, str(e)
    else:
        return False, "unknown_platform"


def _get_platform(chat_id: Union[int, str]) -> str:
    """Determine platform from chat_id."""
    # Discord chat_id uses negative prefix: -1000000000000 - user_id
    if isinstance(chat_id, int):
        if chat_id < -1000000000000:
            return "discord"
        return "telegram"
    
    chat_id_str = str(chat_id)
    if chat_id_str.startswith("discord_"):
        return "discord"
    elif chat_id_str.startswith("tg_"):
        return "telegram"
    else:
        # Try to parse as int (legacy Telegram format or Discord negative ID)
        try:
            id_val = int(chat_id_str)
            if id_val < -1000000000000:
                return "discord"
            return "telegram"
        except ValueError:
            return "unknown"


def _extract_id(chat_id: Union[int, str], prefix: str = None) -> Optional[int]:
    """Extract numeric ID from chat_id."""
    # Handle Discord negative ID format: -1000000000000 - user_id
    if isinstance(chat_id, int):
        if chat_id < -1000000000000:
            # Convert back: user_id = -1000000000000 - chat_id
            return -1000000000000 - chat_id
        return chat_id
    
    chat_id_str = str(chat_id)
    if prefix:
        if chat_id_str.startswith(prefix):
            try:
                return int(chat_id_str[len(prefix):])
            except ValueError:
                return None
        return None
    else:
        # Try without prefix
        if chat_id_str.startswith("tg_"):
            chat_id_str = chat_id_str[3:]
        elif chat_id_str.startswith("discord_"):
            chat_id_str = chat_id_str[8:]
        
        try:
            id_val = int(chat_id_str)
            # Handle negative Discord ID
            if id_val < -1000000000000:
                return -1000000000000 - id_val
            return id_val
        except ValueError:
            return None


def _send_telegram(chat_id: Union[int, str], text: str, parse_mode: str = "") -> Tuple[bool, str]:
    """Send message via Telegram."""
    try:
        from supervisor.telegram import get_tg
        tg_chat_id = _extract_id(chat_id)
        if tg_chat_id is None:
            return False, "invalid_telegram_id"
        return get_tg().send_message(tg_chat_id, text, parse_mode)
    except Exception as e:
        log.warning("Failed to send Telegram message", exc_info=True)
        return False, str(e)


def _send_discord(chat_id: str, text: str) -> Tuple[bool, str]:
    """Send message via Discord."""
    try:
        from ouroboros.channels.discord_bridge import send_discord_message
        user_id = _extract_id(chat_id, prefix="discord_")
        if user_id is None:
            return False, "invalid_discord_id"
        success = send_discord_message(user_id, text)
        return success, "ok" if success else "discord_send_failed"
    except Exception as e:
        log.warning("Failed to send Discord message", exc_info=True)
        return False, str(e)

