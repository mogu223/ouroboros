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

# 全局状态
_discord_bot: Optional[commands.Bot] = None
_bot_event_loop: Optional[asyncio.AbstractEventLoop] = None
_bot_ready_event = threading.Event()  # 用于等待 bot 就绪
_pending_responses: Dict[str, asyncio.Future] = {}


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
        allowed.add(int(owner_id))
    
    allowed_users = os.getenv("DISCORD_ALLOWED_USERS")
    if allowed_users:
        for uid in allowed_users.split(","):
            uid = uid.strip()
            if uid:
                allowed.add(int(uid))
    
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
                        allowed.add(int(value))
                    elif key == "DISCORD_ALLOWED_USERS":
                        for uid in value.split(","):
                            uid = uid.strip()
                            if uid:
                                allowed.add(int(uid))
    
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
    global _discord_bot, _bot_event_loop
    
    if _discord_bot is None:
        logger.warning("Discord bot not initialized")
        return False
    
    if not text:
        return False
    
    # 等待 bot 就绪（最多 5 秒）
    if not _bot_ready_event.wait(timeout=5.0):
        logger.error("Discord bot not ready after 5 seconds")
        return False
    
    try:
        # 在 bot 的事件循环中运行
        async def send():
            # 尝试通过 DM 发送
            try:
                user = await _discord_bot.fetch_user(user_id)
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
            for guild in _discord_bot.guilds:
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
        if _bot_event_loop is not None:
            # 在 bot 的事件循环中运行协程
            future = asyncio.run_coroutine_threadsafe(send(), _bot_event_loop)
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
        global _discord_bot, _bot_event_loop
        
        self.intents = discord.Intents.default()
        self.intents.message_content = True
        self.intents.dm_messages = True
        self.intents.guild_messages = True
        
        _discord_bot = commands.Bot(
            command_prefix="!",
            intents=self.intents,
            help_command=None
        )
        
        self._setup_events()
        self._setup_commands()
    
    def _setup_events(self):
        """设置事件处理器"""
        
        @_discord_bot.event
        async def on_ready():
            global _bot_event_loop, _discord_bot
            _bot_event_loop = asyncio.get_event_loop()
            _bot_ready_event.set()  # 标记 bot 已就绪
            
            allowed_users = _get_allowed_users()
            
            logger.info(f"🟢 Discord bot logged in as {_discord_bot.user}")
            logger.info(f"📡 Connected to {len(_discord_bot.guilds)} guilds")
            if allowed_users:
                logger.info(f"🔐 Whitelist mode: {len(allowed_users)} allowed user(s)")
            else:
                logger.info(f"🌐 Open mode: all users can interact")
        
        @_discord_bot.event
        async def on_message(message):
            # 忽略机器人自己的消息
            if message.author == _discord_bot.user:
                return
            
            # 忽略其他机器人
            if message.author.bot:
                return
            
            # 白名单检查（运行时动态获取）
            allowed_users = _get_allowed_users()
            if allowed_users and message.author.id not in allowed_users:
                logger.warning(f"🚫 Blocked message from unauthorized user: {message.author} ({message.author.id})")
                return
            
            # 只在私聊或者被 @ 时回复
            is_dm = isinstance(message.channel, discord.DMChannel)
            is_mentioned = _discord_bot.user in message.mentions
            if not is_dm and not is_mentioned:
                return
                
            # 处理消息 (去掉提及机器人的文本)
            content = message.content
            if is_mentioned:
                content = content.replace(f"<@{_discord_bot.user.id}>", "").strip()
                content = content.replace(f"<@!{_discord_bot.user.id}>", "").strip()
            
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
        
        @_discord_bot.command(name="ping")
        async def ping(ctx):
            """检查机器人状态"""
            await ctx.send(f"🟢 Online | Latency: {round(_discord_bot.latency * 1000)}ms")
        
        @_discord_bot.command(name="status")
        async def status(ctx):
            """显示详细状态"""
            allowed_users = _get_allowed_users()
            status_text = f"""🟢 **大喷菇 Discord Bot**
            
📡 状态：在线
⏱️ 延迟：{round(_discord_bot.latency * 1000)}ms
🏠 服务器数：{len(_discord_bot.guilds)}
🔐 模式：{"白名单" if allowed_users else "公开"}
            """
            await ctx.send(status_text)
        
        @_discord_bot.command(name="help")
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
        token = _get_discord_token()
        if not token:
            raise ValueError("DISCORD_BOT_TOKEN not set")
        
        logger.info("🚀 Starting Discord bridge...")
        _discord_bot.run(token)
    
    async def start_async(self):
        """异步启动"""
        token = _get_discord_token()
        if not token:
            raise ValueError("DISCORD_BOT_TOKEN not set")
        
        logger.info("🚀 Starting Discord bridge (async mode)...")
        await _discord_bot.start(token)
    
    async def stop(self):
        """停止机器人"""
        await _discord_bot.close()


def create_bridge() -> DiscordBridge:
    """创建 Discord 桥接实例"""
    return DiscordBridge()


def get_bot() -> Optional[commands.Bot]:
    """获取 bot 实例"""
    return _discord_bot


def is_bot_ready() -> bool:
    """检查 bot 是否已就绪"""
    return _bot_ready_event.is_set() and _discord_bot is not None


if __name__ == "__main__":
    token = _get_discord_token()
    allowed = _get_allowed_users()
    print(f"Discord Bridge - Standalone Mode")
    print(f"Token loaded: {'Yes' if token else 'No'}")
    print(f"Allowed users: {allowed if allowed else 'All'}")
    
    bridge = create_bridge()
    bridge.run()
