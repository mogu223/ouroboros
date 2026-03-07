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
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# 配置文件路径
CONFIG_FILE = Path("/opt/ouroboros/.env.discord")

# 全局状态管理器 - 使用单例模式确保状态一致性
class DiscordState:
    """Discord 状态管理器 - 确保跨模块导入时状态一致"""
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


# 全局状态实例
_state = DiscordState()


def _get_discord_token() -> Optional[str]:
    """从环境变量或配置文件获取 Discord Token"""
    # 优先从环境变量获取
    token = os.getenv("DISCORD_BOT_TOKEN")
    if token:
        return token
    
    # 从配置文件获取
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DISCORD_BOT_TOKEN=") and not line.startswith("#"):
                    return line.split("=", 1)[1].strip()
    
    return None


def _get_allowed_users() -> set:
    """从环境变量或配置文件获取允许的用户列表"""
    allowed = set()
    
    # 从环境变量获取
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
    
    # 从配置文件获取
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


# 存储活跃的对话上下文
active_conversations: Dict[int, dict] = {}


def send_discord_message(user_id: int, text: str) -> bool:
    """
    从主进程发送消息到 Discord 用户
    
    Args:
        user_id: Discord 用户 ID
        text: 消息文本
    
    Returns:
        bool: 是否成功发送
    """
    global _state
    
    bot = _state.bot
    loop = _state.event_loop
    
    if bot is None:
        logger.warning("Discord bot not initialized")
        return False
    
    if not text:
        return False
    
    # 等待 bot 就绪（最多 5 秒）
    if not _state.ready_event.wait(timeout=5.0):
        logger.error("Discord bot not ready after 5 seconds")
        return False
    
    try:
        # 在 bot 的事件循环中运行
        async def send():
            # 尝试通过 DM 发送
            try:
                user = await bot.fetch_user(user_id)
                if user:
                    # Discord 消息限制 2000 字符
                    chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
                    for chunk in chunks:
                        await user.send(chunk)
                    logger.info(f"✅ Sent DM to {user_id}")
                    return True
            except Exception as e:
                logger.warning(f"Cannot DM user {user_id}: {e}")
            
            # 如果 DM 失败，尝试在服务器里找这个用户
            for guild in bot.guilds:
                try:
                    member = await guild.fetch_member(user_id)
                    if member:
                        # 在第一个共同的文本频道发送
                        for channel in guild.text_channels:
                            try:
                                chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
                                for chunk in chunks:
                                    await channel.send(f"<@{user_id}> {chunk}")
                                logger.info(f"✅ Sent message to {user_id} in {guild.name}")
                                return True
                            except Exception:
                                continue
                except Exception:
                    continue
            
            logger.error(f"❌ Could not find user {user_id} in any guild")
            return False
        
        # 使用 bot 线程的事件循环
        if loop is not None:
            # 在 bot 的事件循环中运行协程
            future = asyncio.run_coroutine_threadsafe(send(), loop)
            # 等待结果（最多 10 秒）
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
        logger.error(f"❌ Error sending Discord message: {e}")
        return False


class DiscordBridge:
    """Discord 桥接类"""
    
    def __init__(self):
        global _state
        
        self.intents = discord.Intents.default()
        self.intents.message_content = True
        self.intents.dm_messages = True
        self.intents.guild_messages = True
        
        # 创建 bot 实例并保存到全局状态
        bot = commands.Bot(
            command_prefix="!",
            intents=self.intents,
            help_command=None
        )
        
        _state.bot = bot
        
        self._setup_events()
        self._setup_commands()
    
    def _setup_events(self):
        """设置事件处理器"""
        global _state
        
        bot = _state.bot
        
        @bot.event
        async def on_ready():
            global _state
            
            _state.event_loop = asyncio.get_event_loop()
            _state.ready_event.set()  # 标记 bot 已就绪
            
            allowed_users = _get_allowed_users()
            
            logger.info(f"🟢 Discord bot logged in as {bot.user}")
            logger.info(f"📡 Connected to {len(bot.guilds)} guilds")
            if allowed_users:
                logger.info(f"🔐 Whitelist mode: {len(allowed_users)} allowed user(s)")
            else:
                logger.info(f"🌐 Open mode: all users can interact")
        
        @bot.event
        async def on_message(message):
            # 忽略机器人自己的消息
            if message.author == bot.user:
                return
            
            # 忽略其他机器人
            if message.author.bot:
                return
            
            # 白名单检查（运行动态获取）
            allowed_users = _get_allowed_users()
            if allowed_users and message.author.id not in allowed_users:
                logger.warning(f"🚫 Blocked message from unauthorized user: {message.author} ({message.author.id})")
                return
            
            # 只在私聊或者被 @ 时回复
            is_dm = isinstance(message.channel, discord.DMChannel)
            is_mentioned = bot.user in message.mentions
            if not is_dm and not is_mentioned:
                return
                
            # 处理消息 (去掉提及机器人的文本)
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
            
            # 处理消息
            await self._handle_message(clean_msg)
    
    async def _handle_message(self, message):
        """处理收到的消息"""
        content = message.content
        user_id = message.author.id
        username = message.author.name
        channel_type = "dm" if isinstance(message.channel, discord.DMChannel) else "guild"
        
        logger.info(f"📨 Message from {username} ({user_id}) in {channel_type}: {content[:50]}...")
        
        # 先回复"收到"
        await message.channel.send("🍄 收到，正在处理...")
        
        # 调用主系统的处理函数（通过 supervisor 引用）
        from supervisor.workers import handle_chat_direct
        import threading
        
        # 在后台线程中处理
        def process():
            try:
                # 使用 Discord 用户 ID 作为 chat_id（使用纯数字，前缀用负数表示 Discord）
                # Telegram 使用正数，Discord 使用负数避免冲突
                chat_id = -1000000000000 - user_id
                handle_chat_direct(chat_id=chat_id, text=content, image_data=None)
            except Exception as e:
                logger.error(f"❌ Error in handle_chat_direct: {e}", exc_info=True)
        
        thread = threading.Thread(target=process, daemon=True)
        thread.start()
    
    def _setup_commands(self):
        """设置命令"""
        global _state
        bot = _state.bot
        
        @bot.command(name="ping")
        async def ping(ctx):
            """检查机器人状态"""
            await ctx.send(f"🟢 Online | Latency: {round(bot.latency * 1000)}ms")
        
        @bot.command(name="status")
        async def status(ctx):
            """显示详细状态"""
            allowed_users = _get_allowed_users()
            status_text = f"""
🟢 **大喷菇 Discord Bot**
            
📡 状态：在线
⏱️ 延迟：{round(bot.latency * 1000)}ms
🏠 服务器数：{len(bot.guilds)}
🔐 模式：{"白名单" if allowed_users else "公开"}
            """
            await ctx.send(status_text)
        
        @bot.command(name="help")
        async def help_cmd(ctx):
            """显示帮助信息"""
            help_text = """
🍄 **大喷菇** - 蘑菇家族 AI Agent

直接发送消息即可与我对话。

**可用命令：**
• `!ping` - 检查状态
• `!status` - 显示详细状态
• `!help` - 显示帮助

我是大喷菇，蘑菇家族成员。我的兄弟是香菇 (openclaw)。
            """
            await ctx.send(help_text)
    
    def run(self):
        """运行机器人"""
        global _state
        
        token = _get_discord_token()
        if not token:
            raise ValueError("DISCORD_BOT_TOKEN not set")
        
        logger.info("🚀 Starting Discord bridge...")
        
        bot = _state.bot
        if bot:
            bot.run(token)
        else:
            raise RuntimeError("Discord bot not initialized")
    
    async def start_async(self):
        """异步启动"""
        global _state
        
        token = _get_discord_token()
        if not token:
            raise ValueError("DISCORD_BOT_TOKEN not set")
        
        logger.info("🚀 Starting Discord bridge (async mode)...")
        
        bot = _state.bot
        if bot:
            await bot.start(token)
        else:
            raise RuntimeError("Discord bot not initialized")
    
    async def stop(self):
        """停止机器人"""
        global _state
        
        bot = _state.bot
        if bot:
            await bot.close()


def create_bridge() -> DiscordBridge:
    """创建 Discord 桥接实例"""
    return DiscordBridge()


def get_bot() -> Optional[commands.Bot]:
    """获取 bot 实例"""
    global _state
    return _state.bot


def is_bot_ready() -> bool:
    """检查 bot 是否已就绪"""
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
