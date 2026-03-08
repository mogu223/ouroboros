"""
Discord Bridge Module for Ouroboros

Connects Ouroboros to Discord servers via discord.py
Integrated with supervisor event system for bidirectional communication.
"""

import os
import asyncio
import discord
from discord.ext import commands
import logging
from pathlib import Path
import threading
import time
from typing import Any, Dict, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ??????
CONFIG_FILE = Path("/opt/ouroboros/.env.discord")

# ??????? - ?????????????
class DiscordState:
    """Discord ????? - ????????????"""
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self._discord_bot: Optional[commands.Bot] = None
            self._bot_event_loop: Optional[asyncio.AbstractEventLoop] = None
            self._bot_ready_event = threading.Event()
            self._pending_responses: Dict[str, asyncio.Future] = {}
            self._initialized = True
    
    @property
    def bot(self) -> Optional[commands.Bot]:
        return self._discord_bot
    
    @bot.setter
    def bot(self, value: Optional[commands.Bot]):
        self._discord_bot = value
    
    @property
    def event_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        return self._bot_event_loop
    
    @event_loop.setter
    def event_loop(self, value: Optional[asyncio.AbstractEventLoop]):
        self._bot_event_loop = value
    
    @property
    def ready_event(self) -> threading.Event:
        return self._bot_ready_event
    
    def is_ready(self) -> bool:
        return self._bot_ready_event.is_set() and self._discord_bot is not None


# ??????
_state = DiscordState()
_AUTO_NOTIFY_CHANNEL_ID: Optional[int] = None


def _read_discord_config_value(key: str) -> Optional[str]:
    """Read a Discord config value from env first, then .env.discord."""
    env_val = str(os.getenv(key, "") or "").strip()
    if env_val:
        return env_val
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, value = line.split("=", 1)
                if k.strip() == key:
                    v = value.strip()
                    return v or None
    return None


def _get_discord_token() -> Optional[str]:
    """???????????? Discord Token"""
    # ?????????
    token = os.getenv("DISCORD_BOT_TOKEN")
    if token:
        return token
    
    # ???????
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DISCORD_BOT_TOKEN=") and not line.startswith("#"):
                    return line.split("=", 1)[1].strip()
    
    return None


def _get_allowed_users() -> set:
    """???????????????????"""
    allowed = set()
    
    # ???????
    owner_id = os.getenv("DISCORD_OWNER_ID")
    if owner_id:
        try:
            allowed.add(int(owner_id))
        except ValueError:
            pass
    
    allowed_users = os.getenv("DISCORD_ALLOWED_USERS")
    if allowed_users:
        for uid in allowed_users.split(","):
            uid = uid.strip()
            if uid:
                try:
                    allowed.add(int(uid))
                except ValueError:
                    pass
    
    # ???????
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    if key == "DISCORD_OWNER_ID":
                        try:
                            allowed.add(int(value))
                        except ValueError:
                            pass
                    elif key == "DISCORD_ALLOWED_USERS":
                        for uid in value.split(","):
                            uid = uid.strip()
                            if uid:
                                try:
                                    allowed.add(int(uid))
                                except ValueError:
                                    pass
    
    return allowed


def _get_notify_channel_id() -> Optional[int]:
    """Resolve notify channel from env/config if provided."""
    raw = _read_discord_config_value("DISCORD_NOTIFY_CHANNEL_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except Exception:
        logger.warning("Invalid DISCORD_NOTIFY_CHANNEL_ID: %s", raw)
        return None


def _get_notify_targets() -> Set[int]:
    """DM targets for notifications: ALLOWED_USERS first, fallback OWNER."""
    targets: Set[int] = set()
    targets.update(_get_allowed_users())
    owner_raw = _read_discord_config_value("DISCORD_OWNER_ID")
    if owner_raw:
        try:
            targets.add(int(owner_raw))
        except Exception:
            logger.warning("Invalid DISCORD_OWNER_ID: %s", owner_raw)
    return targets


def _is_evolution_control_command(text: str) -> bool:
    """Detect evolve control commands from Discord."""
    cmd = str(text or "").strip().lower()
    if not cmd:
        return False
    if cmd.startswith("/evolve") or cmd.startswith("/evolution"):
        return True
    return cmd in {
        "evolve",
        "evolution",
        "toggle evolution",
        "evolve on",
        "evolve off",
    }


# ??????????
active_conversations: Dict[int, dict] = {}


def send_discord_message(user_id: int, text: str) -> bool:
    """
    ????????? Discord ??
    
    Args:
        user_id: Discord ?? ID
        text: ????
    
    Returns:
        bool: ??????
    """
    global _state
    
    bot = _state.bot
    loop = _state.event_loop
    
    if bot is None:
        logger.warning("Discord bot not initialized")
        return False
    
    if not text:
        return False
    
    # ?? bot ????? 5 ??
    if not _state.ready_event.wait(timeout=5.0):
        logger.error("Discord bot not ready after 5 seconds")
        return False
    
    try:
        async def send():
            conv = active_conversations.get(user_id) or {}
            preferred_channel_id = conv.get("channel_id")
            preferred_is_dm = bool(conv.get("is_dm"))

            # 1) ???????????????????
            if preferred_channel_id and not preferred_is_dm:
                try:
                    channel = bot.get_channel(int(preferred_channel_id))
                    if channel is None:
                        channel = await bot.fetch_channel(int(preferred_channel_id))
                    if channel:
                        chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
                        for chunk in chunks:
                            await channel.send(f"<@{user_id}> {chunk}")
                        logger.info(f"? Sent guild reply in channel {preferred_channel_id} to {user_id}")
                        return True
                except Exception as e:
                    logger.warning(f"Guild channel reply failed for {user_id}: {e}")

            # 2) ???? DM
            try:
                user = await bot.fetch_user(user_id)
                if user:
                    # Discord ???? 2000 ??
                    chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
                    for chunk in chunks:
                        await user.send(chunk)
                    logger.info(f"? Sent DM to {user_id}")
                    return True
            except Exception as e:
                logger.warning(f"Cannot DM user {user_id}: {e}")
            
            # 3) ????????????? @ ??
            for guild in bot.guilds:
                try:
                    member = await guild.fetch_member(user_id)
                    if member:
                        # ?????????????
                        for channel in guild.text_channels:
                            try:
                                chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
                                for chunk in chunks:
                                    await channel.send(f"<@{user_id}> {chunk}")
                                logger.info(f"? Fallback sent message to {user_id} in {guild.name}#{channel.name}")
                                return True
                            except Exception:
                                continue
                except Exception:
                    continue
            
            logger.error(f"? Could not find deliverable destination for user {user_id}")
            return False
        
        if loop is not None:
            future = asyncio.run_coroutine_threadsafe(send(), loop)
            try:
                return future.result(timeout=10.0)
            except asyncio.TimeoutError:
                logger.error(f"Timeout sending message to {user_id}")
                return False
            except Exception as e:
                logger.error(f"Error in send coroutine: {e}")
                return False
        else:
            logger.error("Bot event loop not initialized")
            return False
            
    except Exception as e:
        logger.error(f"? Error sending Discord message: {e}")
        return False


def send_discord_channel_message(text: str, channel_id: Optional[int] = None) -> bool:
    """Send a message to a specific channel, or auto-pick the first writable one."""
    global _state, _AUTO_NOTIFY_CHANNEL_ID
    bot = _state.bot
    loop = _state.event_loop
    if bot is None or not text:
        return False
    if not _state.ready_event.wait(timeout=5.0):
        logger.error("Discord bot not ready after 5 seconds (channel send)")
        return False

    try:
        async def send_to_channel() -> bool:
            global _AUTO_NOTIFY_CHANNEL_ID
            target_id = channel_id or _get_notify_channel_id() or _AUTO_NOTIFY_CHANNEL_ID
            target_channel = None

            if target_id:
                try:
                    target_channel = bot.get_channel(int(target_id))
                    if target_channel is None:
                        target_channel = await bot.fetch_channel(int(target_id))
                except Exception as e:
                    logger.warning("Failed to fetch notify channel %s: %s", target_id, e)

            if target_channel is None:
                for guild in bot.guilds:
                    me = guild.me
                    if me is None:
                        try:
                            me = await guild.fetch_member(bot.user.id)
                        except Exception:
                            me = None
                    for channel in guild.text_channels:
                        try:
                            if me and not channel.permissions_for(me).send_messages:
                                continue
                            target_channel = channel
                            _AUTO_NOTIFY_CHANNEL_ID = int(channel.id)
                            logger.info("Auto-selected notify channel: %s (%s)", channel.name, channel.id)
                            break
                        except Exception:
                            continue
                    if target_channel is not None:
                        break

            if target_channel is None:
                logger.warning("No writable Discord text channel found for notification")
                return False

            chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
            for chunk in chunks:
                await target_channel.send(chunk)
            logger.info("Sent Discord channel notice to %s", getattr(target_channel, "id", "unknown"))
            return True

        if loop is None:
            logger.error("Bot event loop not initialized (channel send)")
            return False
        future = asyncio.run_coroutine_threadsafe(send_to_channel(), loop)
        return bool(future.result(timeout=10.0))
    except Exception as e:
        logger.error("Error sending Discord channel message: %s", e)
        return False


def broadcast_discord_notice(text: str) -> Dict[str, Any]:
    """Broadcast notice: DM all targets + send to notify channel."""
    targets = sorted(_get_notify_targets())
    dm_ok = 0
    dm_fail = 0
    for user_id in targets:
        if send_discord_message(user_id, text):
            dm_ok += 1
        else:
            dm_fail += 1
    channel_ok = send_discord_channel_message(text)
    return {
        "dm_targets": len(targets),
        "dm_ok": dm_ok,
        "dm_fail": dm_fail,
        "channel_ok": bool(channel_ok),
    }


class DiscordBridge:
    """Discord ???"""
    
    def __init__(self):
        global _state
        
        self.intents = discord.Intents.default()
        self.intents.message_content = True
        self.intents.dm_messages = True
        self.intents.guild_messages = True
        
        # ?? bot ??????????
        bot = commands.Bot(
            command_prefix="!",
            intents=self.intents,
            help_command=None
        )
        
        _state.bot = bot
        
        self._setup_events()
        self._setup_commands()
    
    def _setup_events(self):
        """???????"""
        global _state
        
        bot = _state.bot
        
        @bot.event
        async def on_ready():
            global _state
            
            _state.event_loop = asyncio.get_event_loop()
            _state.ready_event.set()  # ?? bot ???
            
            allowed_users = _get_allowed_users()
            
            logger.info(f"?? Discord bot logged in as {bot.user}")
            logger.info(f"?? Connected to {len(bot.guilds)} guilds")
            if allowed_users:
                logger.info(f"?? Whitelist mode: {len(allowed_users)} allowed user(s)")
            else:
                logger.info(f"?? Open mode: all users can interact")
        
        @bot.event
        async def on_message(message):
            # ??????????
            if message.author == bot.user:
                return
            
            # ???????
            if message.author.bot:
                return
            
            # ?????????????
            allowed_users = _get_allowed_users()
            if allowed_users and message.author.id not in allowed_users:
                logger.warning(f"?? Blocked message from unauthorized user: {message.author} ({message.author.id})")
                return
            
            # ??????? @ ???
            is_dm = isinstance(message.channel, discord.DMChannel)
            is_mentioned = bot.user in message.mentions
            if not is_dm:
                if not allowed_users and not is_mentioned:
                    return
                
            # ???? (??????????)
            content = message.content
            if is_mentioned:
                content = content.replace(f"<@{bot.user.id}>", "").strip()
                content = content.replace(f"<@!{bot.user.id}>", "").strip()
            
            # Create a mock message object with cleaned content
            class CleanMessage:
                pass
            clean_msg = CleanMessage()
            clean_msg.content = content
            clean_msg.author = message.author
            clean_msg.channel = message.channel
            
            # ????
            await self._handle_message(clean_msg)
    
    async def _handle_message(self, message):
        """???????"""
        content = message.content
        user_id = message.author.id
        username = message.author.name
        channel_type = "dm" if isinstance(message.channel, discord.DMChannel) else "guild"
        
        logger.info(f"?? Message from {username} ({user_id}) in {channel_type}: {content[:50]}...")

        if _is_evolution_control_command(content):
            await message.channel.send("? Evolution control is Telegram-only. ?? Telegram ?? /evolve on|off?")
            logger.info("Blocked Discord evolution control command from %s (%s)", username, user_id)
            return

        active_conversations[user_id] = {
            "channel_id": int(message.channel.id),
            "is_dm": isinstance(message.channel, discord.DMChannel),
            "updated_at": time.time(),
        }
        
        # ???"??"
        await message.channel.send("?? ???????...")
        
        # ????????????? supervisor ???
        from supervisor.workers import handle_chat_direct
        import threading
        
        # ????????
        def process():
            try:
                # ?? Discord ?? ID ?? chat_id?????????????? Discord?
                # Telegram ?????Discord ????????
                chat_id = -1000000000000 - user_id
                handle_chat_direct(
                    chat_id=chat_id,
                    text=content,
                    image_data=None,
                    source_platform="discord",
                )
            except Exception as e:
                logger.error(f"? Error in handle_chat_direct: {e}", exc_info=True)
        
        thread = threading.Thread(target=process, daemon=True)
        thread.start()
    
    def _setup_commands(self):
        """????"""
        global _state
        bot = _state.bot
        
        @bot.command(name="ping")
        async def ping(ctx):
            """???????"""
            await ctx.send(f"?? Online | Latency: {round(bot.latency * 1000)}ms")
        
        @bot.command(name="status")
        async def status(ctx):
            """??????"""
            allowed_users = _get_allowed_users()
            status_text = f"""
?? **??? Discord Bot**
            
?? ?????
?? ???{round(bot.latency * 1000)}ms
?? ?????{len(bot.guilds)}
?? ???{"???" if allowed_users else "??"}
            """
            await ctx.send(status_text)
        
        @bot.command(name="help")
        async def help_cmd(ctx):
            """??????"""
            help_text = """
?? **???** - ???? AI Agent

?????????????

**?????**
? `!ping` - ????
? `!status` - ??????
? `!help` - ????

???????????????????? (openclaw)?
            """
            await ctx.send(help_text)
    
    def run(self):
        """?????"""
        global _state
        
        token = _get_discord_token()
        if not token:
            raise ValueError("DISCORD_BOT_TOKEN not set")
        
        logger.info("?? Starting Discord bridge...")
        
        bot = _state.bot
        if bot:
            bot.run(token)
        else:
            raise RuntimeError("Discord bot not initialized")
    
    async def start_async(self):
        """????"""
        global _state
        
        token = _get_discord_token()
        if not token:
            raise ValueError("DISCORD_BOT_TOKEN not set")
        
        logger.info("?? Starting Discord bridge (async mode)...")
        
        bot = _state.bot
        if bot:
            await bot.start(token)
        else:
            raise RuntimeError("Discord bot not initialized")
    
    async def stop(self):
        """?????"""
        global _state
        
        bot = _state.bot
        if bot:
            await bot.close()


def create_bridge() -> DiscordBridge:
    """?? Discord ????"""
    return DiscordBridge()


def get_bot() -> Optional[commands.Bot]:
    """?? bot ??"""
    global _state
    return _state.bot


def is_bot_ready() -> bool:
    """?? bot ?????"""
    global _state
    return _state.is_ready()


if __name__ == "__main__":
    token = _get_discord_token()
    allowed = _get_allowed_users()
    print(f"Discord Bridge - Standalone Mode")
    print(f"Token loaded: {'Yes' if token else 'No'}")
    print(f"Allowed users: {allowed if allowed else 'All'}")
    
    bridge = create_bridge()
    bridge.run()


