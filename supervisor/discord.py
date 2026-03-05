"""
Supervisor — Discord client + formatting.

DiscordClient, message splitting, markdown formatting, send_with_budget.
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
TOTAL_BUDGET_LIMIT: float = 0.0
BUDGET_REPORT_EVERY_MESSAGES: int = 10
_DC: Optional["DiscordClient"] = None


def init(drive_root, total_budget_limit: float, budget_report_every: int,
         dc_client: "DiscordClient") -> None:
    global DRIVE_ROOT, TOTAL_BUDGET_LIMIT, BUDGET_REPORT_EVERY_MESSAGES, _DC
    DRIVE_ROOT = drive_root
    TOTAL_BUDGET_LIMIT = total_budget_limit
    BUDGET_REPORT_EVERY_MESSAGES = budget_report_every
    _DC = dc_client


def get_dc() -> "DiscordClient":
    assert _DC is not None, "discord.init() not called"
    return _DC


# ---------------------------------------------------------------------------
# DiscordClient
# ---------------------------------------------------------------------------

class DiscordClient:
    def __init__(self, token: str):
        self.base = "https://discord.com/api/v10"
        self.token = token
        self.headers = {"Authorization": f"Bot {token}"}
        self._user_id: Optional[str] = None

    def get_current_user(self) -> Optional[Dict[str, Any]]:
        """Get bot's own user info."""
        try:
            r = requests.get(f"{self.base}/users/@me", headers=self.headers, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("Failed to get bot user info: %s", e)
            return None

    @property
    def user_id(self) -> Optional[str]:
        """Get bot's user ID (cached)."""
        if self._user_id is None:
            user = self.get_current_user()
            if user:
                self._user_id = user.get("id")
        return self._user_id

    def get_dms(self) -> List[Dict[str, Any]]:
        """Get list of DM channels for the bot."""
        try:
            r = requests.get(f"{self.base}/users/@me/channels", headers=self.headers, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("Failed to get DM channels: %s", e)
            return []

    def send_message(self, channel_id: str, text: str) -> Tuple[bool, str]:
        """Send a message to a Discord channel (DM or guild channel)."""
        last_err = "unknown"
        for attempt in range(3):
            try:
                payload = {"content": text}
                r = requests.post(
                    f"{self.base}/channels/{channel_id}/messages",
                    headers=self.headers,
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

    def send_message_embed(self, channel_id: str, title: str, description: str,
                           color: int = 0x5865F2) -> Tuple[bool, str]:
        """Send an embed message to Discord."""
        try:
            payload = {
                "embeds": [{
                    "title": title,
                    "description": description,
                    "color": color,
                }]
            }
            r = requests.post(
                f"{self.base}/channels/{channel_id}/messages",
                headers=self.headers,
                json=payload,
                timeout=30,
            )
            r.raise_for_status()
            return True, "ok"
        except Exception as e:
            return False, repr(e)

    def send_file(self, channel_id: str, file_bytes: bytes, filename: str,
                  caption: str = "") -> Tuple[bool, str]:
        """Send a file to a Discord channel."""
        last_err = "unknown"
        for attempt in range(3):
            try:
                files = {"file": (filename, file_bytes)}
                data = {}
                if caption:
                    data["content"] = caption[:2000]
                r = requests.post(
                    f"{self.base}/channels/{channel_id}/messages",
                    headers=self.headers,
                    data=data,
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

    def get_channel_messages(self, channel_id: str, limit: int = 1,
                             before: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get recent messages from a channel."""
        try:
            params = {"limit": limit}
            if before:
                params["before"] = before
            r = requests.get(
                f"{self.base}/channels/{channel_id}/messages",
                headers=self.headers,
                params=params,
                timeout=10,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("Failed to get messages from channel %s: %s", channel_id, e)
            return []

    def create_dm(self, user_id: str) -> Optional[str]:
        """Create a DM channel with a user. Returns channel_id or None."""
        try:
            r = requests.post(
                f"{self.base}/users/@me/channels",
                headers=self.headers,
                json={"recipient_id": user_id},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            return data.get("id")
        except Exception as e:
            log.warning("Failed to create DM with user %s: %s", user_id, e)
            return None


# ---------------------------------------------------------------------------
# Message splitting + formatting
# ---------------------------------------------------------------------------

def split_discord(text: str, limit: int = 2000) -> List[str]:
    """Split text into chunks that fit Discord's 2000 character limit."""
    chunks: List[str] = []
    s = text
    while len(s) > limit:
        # Try to split at newline
        cut = s.rfind("\n", 0, limit)
        if cut < 100:  # No good newline, split at limit
            cut = limit
        chunks.append(s[:cut])
        s = s[cut:]
    chunks.append(s)
    return chunks


def _sanitize_discord_text(text: str) -> str:
    """Sanitize text for Discord."""
    if text is None:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Discord doesn't allow null characters
    return "".join(c for c in text if ord(c) >= 32 or c in ("\n", "\t"))


def _markdown_to_discord(md: str) -> str:
    """Convert Markdown to Discord markdown format.

    Discord supports: **bold**, *italic*, __underline__, ~~strikethrough~~,
    `inline code`, ```code blocks```, [links](url), > quotes.
    """
    if not md:
        return ""

    # Most markdown is already compatible, just need to handle some edge cases

    # Headers -> bold (Discord doesn't have headers)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"**\1**", md, flags=re.MULTILINE)

    # Convert HTML tags that might be in the text
    text = re.sub(r"<b>(.+?)</b>", r"**\1**", text)
    text = re.sub(r"<i>(.+?)</i>", r"*\1*", text)
    text = re.sub(r"<s>(.+?)</s>", r"~~\1~~", text)
    text = re.sub(r"<code>(.+?)</code>", r"`\1`", text)
    text = re.sub(r"<pre>([\s\S]+?)</pre>", r"```\n\1\n```", text)
    text = re.sub(r"<a href=\"([^\"]+)\">(.+?)</a>", r"[\2](\1)", text)

    # Clean up any HTML entities
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&amp;", "&").replace("&quot;", '"')

    return text


def _chunk_markdown_for_discord(md: str, max_chars: int = 1900) -> List[str]:
    """Chunk markdown text for Discord, respecting code blocks."""
    md = md or ""
    max_chars = max(256, min(2000, int(max_chars)))
    lines = md.splitlines(keepends=True)
    chunks: List[str] = []
    cur = ""
    in_fence = False
    fence_close = "```\n"

    def _flush() -> None:
        nonlocal cur
        if cur and cur.strip():
            chunks.append(cur)
        cur = ""

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence

        reserve = len(fence_close) if in_fence else 0
        if len(cur) + len(line) > max_chars - reserve:
            if in_fence and cur:
                cur += fence_close
            _flush()
            cur = "```\n" if in_fence else ""
        cur += line

    if in_fence:
        cur += fence_close
    _flush()
    return chunks or [md]


def _send_markdown_discord(channel_id: str, text: str) -> Tuple[bool, str]:
    """Send markdown text to Discord, with chunking."""
    dc = get_dc()
    chunks = _chunk_markdown_for_discord(text or "", max_chars=1900)
    chunks = [c for c in chunks if isinstance(c, str) and c.strip()]
    if not chunks:
        return False, "empty_chunks"

    last_err = "ok"
    for chunk in chunks:
        discord_text = _markdown_to_discord(chunk)
        ok, err = dc.send_message(channel_id, _sanitize_discord_text(discord_text))
        if not ok:
            return False, err
        last_err = err
    return True, last_err


# ---------------------------------------------------------------------------
# Budget + logging
# ---------------------------------------------------------------------------

def _format_budget_line(st: Dict[str, Any]) -> str:
    spent = float(st.get("spent_usd") or 0.0)
    total = float(TOTAL_BUDGET_LIMIT or 0.0)
    pct = (spent / total * 100.0) if total > 0 else 0.0
    sha = (st.get("current_sha") or "")[:8]
    branch = st.get("current_branch") or "?"
    return f"—\nBudget: ${spent:.4f} / ${total:.2f} ({pct:.2f}%) | {branch}@{sha}"


def budget_line(force: bool = False) -> str:
    try:
        st = load_state()
        every = max(1, int(BUDGET_REPORT_EVERY_MESSAGES))
        if force:
            st["budget_messages_since_report"] = 0
            save_state(st)
            return _format_budget_line(st)

        counter = int(st.get("budget_messages_since_report") or 0) + 1
        if counter < every:
            st["budget_messages_since_report"] = counter
            save_state(st)
            return ""

        st["budget_messages_since_report"] = 0
        save_state(st)
        return _format_budget_line(st)
    except Exception:
        log.debug("Failed to format budget line", exc_info=True)
        return ""


def send_with_budget(channel_id: str, text: str, force_budget: bool = False) -> bool:
    """Send a message to Discord with optional budget line."""
    bl = budget_line(force=force_budget)
    full_text = f"{text}\n{bl}" if bl else text
    ok, err = _send_markdown_discord(channel_id, full_text)
    if not ok:
        log.warning("Discord send failed: %s", err)
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "discord_send_error",
                "channel_id": channel_id,
                "error": err,
            },
        )
    return ok


# ---------------------------------------------------------------------------
# Discord message polling
# ---------------------------------------------------------------------------

def poll_discord_messages(owner_id: str, offset_msg_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Poll Discord for new DM messages from owner.

    Returns list of message dicts with: id, author_id, content, timestamp, attachments
    """
    dc = get_dc()
    messages = []

    # Get or create DM channel with owner
    dm_channel_id = _get_owner_dm_channel(owner_id)
    if not dm_channel_id:
        log.warning("Could not get DM channel for owner %s", owner_id)
        return messages

    # Get recent messages
    recent = dc.get_channel_messages(dm_channel_id, limit=10, before=None)
    if not recent:
        return messages

    # Filter for owner messages and apply offset
    for msg in recent:
        msg_id = msg.get("id")
        author = msg.get("author", {})
        author_id = author.get("id")

        # Only process owner messages (not bot's own messages)
        if author_id != owner_id:
            continue

        # Skip if we've already processed this message
        if offset_msg_id and msg_id <= offset_msg_id:
            continue

        # Extract content
        content = msg.get("content", "")

        # Extract attachments
        attachments = []
        for att in msg.get("attachments", []):
            attachments.append({
                "id": att.get("id"),
                "url": att.get("url"),
                "filename": att.get("filename"),
                "content_type": att.get("content_type"),
            })

        messages.append({
            "id": msg_id,
            "author_id": author_id,
            "channel_id": dm_channel_id,
            "content": content,
            "timestamp": msg.get("timestamp"),
            "attachments": attachments,
        })

    # Sort by ID (oldest first)
    messages.sort(key=lambda m: m["id"])
    return messages


# Cache for owner DM channel
_owner_dm_channel_cache: Dict[str, str] = {}


def _get_owner_dm_channel(owner_id: str) -> Optional[str]:
    """Get cached DM channel for owner, or fetch/create it."""
    if owner_id in _owner_dm_channel_cache:
        return _owner_dm_channel_cache[owner_id]

    dc = get_dc()

    # Try to find existing DM channel
    dms = dc.get_dms()
    for dm in dms:
        # Check if this DM is with the owner
        recipients = dm.get("recipients", [])
        for recipient in recipients:
            if recipient.get("id") == owner_id:
                channel_id = dm.get("id")
                _owner_dm_channel_cache[owner_id] = channel_id
                return channel_id

    # Create new DM channel
    channel_id = dc.create_dm(owner_id)
    if channel_id:
        _owner_dm_channel_cache[owner_id] = channel_id
    return channel_id


def download_attachment(attachment_url: str) -> Optional[Tuple[bytes, str]]:
    """Download an attachment from Discord. Returns (bytes, content_type) or None."""
    try:
        r = requests.get(attachment_url, timeout=30)
        r.raise_for_status()
        content_type = r.headers.get("content-type", "application/octet-stream")
        return r.content, content_type
    except Exception as e:
        log.warning("Failed to download attachment: %s", e)
        return None
