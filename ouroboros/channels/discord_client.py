"""
Discord client for Ouroboros.

Provides Discord bot integration similar to Telegram:
- Long polling for messages
- Message formatting (Markdown -> Discord)
- Whitelist security
- Bridge to Ouroboros agent loop
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any, Dict, List, Optional, Set

log = logging.getLogger(__name__)

# Try to import discord.py
try:
    import discord
    from discord.ext import commands
    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False
    discord = None
    commands = None
    log.warning("discord.py not installed. Discord channel disabled.")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Environment variables
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_OWNER_ID = os.environ.get("DISCORD_OWNER_ID", "")  # Discord user ID (string)
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")  # Server ID (optional)
DISCORD_COMMAND_PREFIX = os.environ.get("DISCORD_COMMAND_PREFIX", "!")

# Whitelist of allowed Discord user IDs (set by owner)
_allowed_users: Set[int] = set()

# Reference to message handler (set by init)
_message_handler = None


# ---------------------------------------------------------------------------
# Discord Client
# ---------------------------------------------------------------------------

class DiscordClient:
    """Discord bot client for Ouroboros."""
    
    def __init__(self, token: str, owner_id: Optional[int] = None, guild_id: Optional[int] = None):
        if not DISCORD_AVAILABLE:
            raise RuntimeError("discord.py not installed. Run: pip install discord.py")
        
        self.token = token
        self.owner_id = owner_id
        self.guild_id = guild_id
        
        # Configure intents
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        intents.guild_messages = True
        
        # Create bot
        self.bot = commands.Bot(
            command_prefix=DISCORD_COMMAND_PREFIX,
            intents=intents,
            description="大喷菇 - Ouroboros AI Agent"
        )
        
        # Setup event handlers
        self._setup_events()
        
        # Running state
        self._running = False
        self._thread: Optional[threading.Thread] = None
    
    def _setup_events(self):
        """Setup Discord event handlers."""
        
        @self.bot.event
        async def on_ready():
            log.info("Discord bot connected: %s (ID: %d)", self.bot.user.name, self.bot.user.id)
            if self.guild_id:
                guild = self.bot.get_guild(self.guild_id)
                if guild:
                    log.info("Connected to guild: %s (ID: %d)", guild.name, guild.id)
        
        @self.bot.event
        async def on_message(message: discord.Message):
            # Ignore own messages
            if message.author == self.bot.user:
                return
            
            # Check if user is allowed
            user_id = message.author.id
            
            # Owner is always allowed
            if self.owner_id and user_id == self.owner_id:
                await self._handle_message(message)
                return
            
            # Check whitelist
            if user_id in _allowed_users:
                await self._handle_message(message)
                return
            
            # Not allowed - send warning (only in DMs to avoid spam)
            if isinstance(message.channel, discord.DMChannel):
                await message.reply(
                    "⚠️ 你不在白名单中。请联系蘑菇添加你的 Discord ID。\n"
                    f"你的 Discord ID: `{user_id}`"
                )
    
    async def _handle_message(self, message: discord.Message):
        """Handle an allowed message."""
        if _message_handler is None:
            await message.reply("⚠️ 系统尚未就绪，请稍后再试。")
            return
        
        # Show typing indicator
        async with message.channel.typing():
            try:
                # Call the message handler
                response = await _message_handler(
                    user_id=str(message.author.id),
                    user_name=message.author.display_name,
                    text=message.content,
                    channel_id=str(message.channel.id),
                    guild_id=str(message.guild.id) if message.guild else None,
                )
                
                if response:
                    # Split and send response
                    await self._send_long_message(message.channel, response)
            
            except Exception as e:
                log.error("Error handling Discord message: %s", e, exc_info=True)
                await message.reply(f"❌ 处理消息时出错: {e}")
    
    async def _send_long_message(self, channel, text: str, max_len: int = 1900):
        """Send a message, splitting if needed (Discord limit is 2000 chars)."""
        if len(text) <= max_len:
            await channel.send(text)
            return
        
        # Split by newlines first
        lines = text.split("\n")
        chunks = []
        current = ""
        
        for line in lines:
            if len(current) + len(line) + 1 > max_len:
                if current:
                    chunks.append(current)
                current = line
            else:
                if current:
                    current += "\n" + line
                else:
                    current = line
        
        if current:
            chunks.append(current)
        
        # Send chunks
        for chunk in chunks:
            if chunk.strip():
                await channel.send(chunk)
    
    def send_message(self, channel_id: int, text: str) -> bool:
        """Send a message to a Discord channel (sync wrapper)."""
        if not self._running:
            return False
        
        async def _send():
            try:
                channel = self.bot.get_channel(channel_id)
                if channel:
                    await self._send_long_message(channel, text)
                    return True
                return False
            except Exception as e:
                log.error("Failed to send Discord message: %s", e, exc_info=True)
                return False
        
        # Schedule in the bot's event loop
        asyncio.run_coroutine_threadsafe(_send(), self.bot.loop)
        return True
    
    def start(self):
        """Start the Discord bot in a background thread."""
        if self._running:
            return
        
        def _run_bot():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                self._running = True
                loop.run_until_complete(self.bot.start(self.token))
            except Exception as e:
                log.error("Discord bot error: %s", e, exc_info=True)
            finally:
                self._running = False
                loop.close()
        
        self._thread = threading.Thread(target=_run_bot, daemon=True, name="DiscordBot")
        self._thread.start()
        log.info("Discord bot started in background thread")
    
    def stop(self):
        """Stop the Discord bot."""
        if not self._running:
            return
        
        async def _stop():
            await self.bot.close()
        
        asyncio.run_coroutine_threadsafe(_stop(), self.bot.loop)
        self._running = False
        log.info("Discord bot stopped")


# ---------------------------------------------------------------------------
# Message Handler Registration
# ---------------------------------------------------------------------------

def set_message_handler(handler):
    """Set the async message handler function.
    
    Handler signature:
        async def handler(user_id: str, user_name: str, text: str, 
                         channel_id: str, guild_id: Optional[str]) -> str:
            # Process message and return response
            return "Response text"
    """
    global _message_handler
    _message_handler = handler


def add_allowed_user(user_id: int):
    """Add a Discord user ID to the whitelist."""
    _allowed_users.add(user_id)
    log.info("Added Discord user %d to whitelist", user_id)


def remove_allowed_user(user_id: int):
    """Remove a Discord user ID from the whitelist."""
    _allowed_users.discard(user_id)
    log.info("Removed Discord user %d from whitelist", user_id)


def get_allowed_users() -> Set[int]:
    """Get the set of allowed Discord user IDs."""
    return _allowed_users.copy()


# ---------------------------------------------------------------------------
# Factory Function
# ---------------------------------------------------------------------------

def create_discord_client() -> Optional[DiscordClient]:
    """Create a Discord client from environment variables.
    
    Returns None if Discord is not configured or unavailable.
    """
    if not DISCORD_AVAILABLE:
        log.warning("discord.py not available, skipping Discord client creation")
        return None
    
    if not DISCORD_BOT_TOKEN:
        log.info("DISCORD_BOT_TOKEN not set, Discord channel disabled")
        return None
    
    owner_id = int(DISCORD_OWNER_ID) if DISCORD_OWNER_ID else None
    guild_id = int(DISCORD_GUILD_ID) if DISCORD_GUILD_ID else None
    
    client = DiscordClient(
        token=DISCORD_BOT_TOKEN,
        owner_id=owner_id,
        guild_id=guild_id,
    )
    
    # Add owner to whitelist by default
    if owner_id:
        add_allowed_user(owner_id)
    
    return client


# ---------------------------------------------------------------------------
# Discord Tools for Ouroboros
# ---------------------------------------------------------------------------

def get_discord_tools():
    """Return Discord-related tools for Ouroboros."""
    from ouroboros.tools.registry import ToolEntry
    
    tools = []
    
    if not DISCORD_AVAILABLE:
        return tools
    
    # Tool: send_discord_message
    async def send_discord_message(channel_id: str, text: str) -> dict:
        """Send a message to a Discord channel.
        
        Args:
            channel_id: Discord channel ID (string)
            text: Message text to send
            
        Returns:
            {"success": bool, "error": str or None}
        """
        # This is a placeholder - actual implementation needs the client instance
        return {"success": False, "error": "Discord client not initialized"}
    
    # Tool: add_discord_whitelist
    def add_discord_whitelist(user_id: str) -> dict:
        """Add a Discord user to the whitelist.
        
        Args:
            user_id: Discord user ID (string)
            
        Returns:
            {"success": bool, "error": str or None}
        """
        try:
            add_allowed_user(int(user_id))
            return {"success": True, "error": None}
        except ValueError:
            return {"success": False, "error": "Invalid user ID"}
    
    # Tool: list_discord_whitelist
    def list_discord_whitelist() -> dict:
        """List all Discord users in the whitelist.
        
        Returns:
            {"users": [user_id1, user_id2, ...]}
        """
        return {"users": list(get_allowed_users())}
    
    return tools