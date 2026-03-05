"""
Supervisor — Discord client + message handling.

DiscordClient for sending/receiving messages via Discord Bot API.
"""

from __future__ import annotations

import datetime
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import requests

from supervisor.state import load_state, save_state, append_jsonl

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level config (set via init())
# ---------------------------------------------------------------------------
DRIVE_ROOT = None  # pathlib.Path
_TOTAL_BUDGET_LIMIT: float = 0.0
_BUDGET_REPORT_EVERY_MESSAGES: int = 10
_DISCORD: Optional["DiscordClient"] = None


def init(drive_root, total_budget_limit: float, budget_report_every: int,
         discord_client: "DiscordClient") -> None:
    global DRIVE_ROOT, _TOTAL_BUDGET_LIMIT, _BUDGET_REPORT_EVERY_MESSAGES, _DISCORD
    DRIVE_ROOT = drive_root
    _TOTAL_BUDGET_LIMIT = total_budget_limit
    _BUDGET_REPORT_EVERY_MESSAGES = budget_report_every
    _DISCORD = discord_client


def get_discord() -> "DiscordClient":
    assert _DISCORD is not None, "discord.init() not called"
    return _DISCORD


# ---------------------------------------------------------------------------
# DiscordClient
# ---------------------------------------------------------------------------

class DiscordClient:
    def __init__(self, token: str, owner_id: str):
        self.token = token
        self.owner_id = owner_id
        self.base = "https://discord.com/api/v10"
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
        })
        self._last_message_id: Dict[int, str] = {}  # channel_id -> message_id

    def get_owner_dm_channel(self) -> Optional[int]:
        """Get or create DM channel with owner. Returns channel_id or None."""
        try:
            # Try to get existing DM channel
            r = self._session.get(f"{self.base}/users/@me/channels", timeout=10)
            r.raise_for_status()
            channels = r.json()
            
            for channel in channels:
                if channel.get("type") == 1:  # DM channel
                    # Check if this DM is with the owner
                    recipients = channel.get("recipients", [])
                    for recipient in recipients:
                        if str(recipient.get("id")) == str(self.owner_id):
                            return int(channel["id"])
            
            # Create new DM channel if not found
            r = self._session.post(
                f"{self.base}/users/@me/channels",
                json={"recipient_id": self.owner_id},
                timeout=10,
            )
            r.raise_for_status()
            channel = r.json()
            return int(channel["id"])
        except Exception as e:
            log.warning("Failed to get/create DM channel for owner", exc_info=True)
            return None

    def get_messages(self, channel_id: int, limit: int = 10, after_message_id: str = None) -> List[Dict[str, Any]]:
        """Get recent messages from a channel."""
        try:
            params = {"limit": limit}
            if after_message_id:
                params["after"] = after_message_id
            
            r = self._session.get(
                f"{self.base}/channels/{channel_id}/messages",
                params=params,
                timeout=10,
            )
            r.raise_for_status()
            messages = r.json()
            
            # Filter to only owner's messages
            owner_messages = [
                msg for msg in messages 
                if str(msg.get("author", {}).get("id")) == str(self.owner_id)
            ]
            
            # Return in chronological order (oldest first)
            return list(reversed(owner_messages))
        except Exception as e:
            log.warning("Failed to get messages from channel %d", channel_id, exc_info=True)
            return []

    def send_message(self, channel_id: int, text: str, parse_mode: str = "") -> Tuple[bool, str]:
        """Send a message to a Discord channel."""
        last_err = "unknown"
        for attempt in range(3):
            try:
                # Split long messages
                chunks = self._split_message(text)
                for chunk in chunks:
                    payload = {
                        "content": self._sanitize_discord_text(chunk),
                    }
                    
                    # Simple markdown support (Discord uses its own markdown)
                    if parse_mode == "markdown":
                        # Discord natively supports markdown, no conversion needed
                        pass
                    
                    r = self._session.post(
                        f"{self.base}/channels/{channel_id}/messages",
                        json=payload,
                        timeout=30,
                    )
                    r.raise_for_status()
                    
                return True, "ok"
            except Exception as e:
                last_err = repr(e)
                if attempt < 2:
                    import time
                    time.sleep(0.8 * (attempt + 1))
        return False, last_err

    def send_chat_action(self, channel_id: int, action: str = "typing") -> bool:
        """Send typing indicator to Discord channel."""
        try:
            r = self._session.post(
                f"{self.base}/channels/{channel_id}/typing",
                timeout=5,
            )
            return r.status_code in (200, 204)
        except Exception:
            log.debug("Failed to send typing indicator to channel_id=%d", channel_id, exc_info=True)
            return False

    def send_photo(self, channel_id: int, photo_bytes: bytes, caption: str = "") -> Tuple[bool, str]:
        """Send an image to a Discord channel."""
        last_err = "unknown"
        for attempt in range(3):
            try:
                files = {"file": ("screenshot.png", photo_bytes, "image/png")}
                payload = {}
                if caption:
                    payload["content"] = caption[:2000]  # Discord caption limit
                
                r = self._session.post(
                    f"{self.base}/channels/{channel_id}/messages",
                    json=payload,
                    files=files,
                    timeout=30,
                )
                r.raise_for_status()
                return True, "ok"
            except Exception as e:
                last_err = repr(e)
                if attempt < 2:
                    import time
                    time.sleep(0.8 * (attempt + 1))
        return False, last_err

    def _split_message(self, text: str, limit: int = 2000) -> List[str]:
        """Split message into chunks that fit Discord's 2000 character limit."""
        chunks = []
        while len(text) > limit:
            # Try to split at newline
            split_point = text.rfind("\n", 0, limit)
            if split_point < 100:  # No good split point found
                split_point = limit
            chunks.append(text[:split_point])
            text = text[split_point:].lstrip()
        chunks.append(text)
        return chunks

    def _sanitize_discord_text(self, text: str) -> str:
        """Sanitize text for Discord."""
        if text is None:
            return ""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # Remove invalid Unicode
        return "".join(
            c for c in text
            if (ord(c) >= 32 or c in ("\n", "\t")) and not (0xD800 <= ord(c) <= 0xDFFF)
        )


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def _discord_to_plain_text(md: str) -> str:
    """Convert Discord markdown to plain text (fallback)."""
    if not md:
        return ""
    # Remove code blocks
    text = re.sub(r"```[\s\S]*?```", "", md)
    # Remove inline code
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Remove bold/italic
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    # Remove strikethrough
    text = re.sub(r"~~([^~]+)~~", r"\1", text)
    # Remove links
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return text.strip()


def send_with_budget(channel_id: int, text: str, force: bool = False) -> str:
    """Send message with budget tracking. Returns budget line if applicable."""
    try:
        st = load_state()
        every = max(1, int(_BUDGET_REPORT_EVERY_MESSAGES))
        
        # Check if we should include budget report
        include_budget = force
        if not force:
            counter = int(st.get("budget_messages_since_report") or 0) + 1
            if counter >= every:
                include_budget = True
                st["budget_messages_since_report"] = 0
            else:
                st["budget_messages_since_report"] = counter
            save_state(st)
        
        # Add budget line if needed
        if include_budget:
            spent = float(st.get("spent_usd") or 0.0)
            total = float(_TOTAL_BUDGET_LIMIT or 0.0)
            pct = (spent / total * 100.0) if total > 0 else 0.0
            sha = (st.get("current_sha") or "")[:8]
            branch = st.get("current_branch") or "?"
            budget_line = f"\n\n—\nBudget: ${spent:.4f} / ${total:.2f} ({pct:.2f}%) | {branch}@{sha}"
            text = text + budget_line
        
        # Send message
        discord = get_discord()
        ok, err = discord.send_message(channel_id, text, parse_mode="markdown")
        if not ok:
            log.warning("Failed to send Discord message: %s", err)
        
        return budget_line if include_budget else ""
    except Exception as e:
        log.warning("Error in send_with_budget", exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# Message polling
# ---------------------------------------------------------------------------

def poll_discord_messages(last_message_id: str = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Poll Discord for new messages from owner.
    
    Returns:
        - List of new messages (chronological order)
        - Last message ID for next poll
    """
    discord = get_discord()
    
    # Get owner's DM channel
    channel_id = discord.get_owner_dm_channel()
    if not channel_id:
        return [], last_message_id
    
    # Get recent messages
    messages = discord.get_messages(channel_id, limit=10, after_message_id=last_message_id)
    
    if not messages:
        return [], last_message_id
    
    # Extract message data
    result = []
    new_last_id = last_message_id
    
    for msg in messages:
        msg_id = str(msg.get("id"))
        content = msg.get("content", "")
        timestamp = msg.get("timestamp", "")
        
        # Check for attachments
        attachments = msg.get("attachments", [])
        image_data = None
        
        if attachments:
            # Download first image attachment
            for att in attachments:
                if att.get("content_type", "").startswith("image/"):
                    try:
                        url = att.get("url")
                        if url:
                            r = requests.get(url, timeout=30)
                            r.raise_for_status()
                            import base64
                            image_b64 = base64.b64encode(r.content).decode("ascii")
                            mime = att.get("content_type", "image/png")
                            image_data = (image_b64, mime, "")
                            break
                    except Exception:
                        log.warning("Failed to download Discord attachment", exc_info=True)
        
        result.append({
            "message_id": msg_id,
            "content": content,
            "timestamp": timestamp,
            "image_data": image_data,
            "platform": "discord",
            "channel_id": channel_id,
        })
        
        new_last_id = msg_id
    
    # Return in chronological order (oldest first)
    result.reverse()
    
    return result, new_last_id
