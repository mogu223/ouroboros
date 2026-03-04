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
_pending_responses: Dict[str, asyncio.Future] = {}
_event_loop: Optional[asyncio.AbstractEventLoop] = None

def load_config():
    """从配置文件加载 Token 和白名单"""
    config = {
        "token": os.getenv("DISCORD_BOT_TOKEN"),
        "allowed_users": set()
    }
    
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    if key == "DISCORD_BOT_TOKEN":
                        config["token"] = value
                    elif key == "DISCORD_OWNER_ID":
                        config["allowed_users"].add(int(value))
                    elif key == "DISCORD_ALLOWED_USERS":
                        for uid in value.split(","):
                            uid = uid.strip()
                            if uid:
                                config["allowed_users"].add(int(uid))
    
    return config

# 加载配置
CONFIG = load_config()
DISCORD_TOKEN = CONFIG["token"]
ALLOWED_USER_IDS = CONFIG["allowed_users"]


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
    global _discord_bot
    
    if _discord_bot is None:
        logger.warning("Discord bot not initialized")
        return False
    
    if not text:
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
        
        # 如果已经在事件循环中，创建任务；否则运行新循环
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(send(), loop)
            else:
                loop.run_until_complete(send())
        except RuntimeError:
            # 没有事件循环，创建新的
            asyncio.run(send())
        
        return True
    except Exception as e:
        logger.error(f"❌ Error sending Discord message: {e}")
        return False


class DiscordBridge:
    """Discord 桥接类"""
    
    def __init__(self):
        global _discord_bot
        
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
            logger.info(f"🟢 Discord bot logged in as {_discord_bot.user}")
            logger.info(f"📡 Connected to {len(_discord_bot.guilds)} guilds")
            if ALLOWED_USER_IDS:
                logger.info(f"🔐 Whitelist mode: {len(ALLOWED_USER_IDS)} allowed user(s)")
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
            
            # 白名单检查
            if ALLOWED_USER_IDS and message.author.id not in ALLOWED_USER_IDS:
                logger.warning(f"🚫 Blocked message from unauthorized user: {message.author} ({message.author.id})")
                return
            
            # 处理消息
            await self._handle_message(message)
    
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
                # 使用 Discord 用户 ID 作为 chat_id（带前缀）
                chat_id = f"discord_{user_id}"
                handle_chat_direct(chat_id=chat_id, text=content, image_data=None)
            except Exception as e:
                logger.error(f"❌ Error in handle_chat_direct: {e}")
        
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
            status_text = f"""🟢 **大喷菇 Discord Bot**
            
📡 状态：在线
⏱️ 延迟：{round(_discord_bot.latency * 1000)}ms
🏠 服务器数：{len(_discord_bot.guilds)}
🔐 模式：{"白名单" if ALLOWED_USER_IDS else "公开"}
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
        if not DISCORD_TOKEN:
            raise ValueError("DISCORD_BOT_TOKEN not set")
        
        logger.info("🚀 Starting Discord bridge...")
        _discord_bot.run(DISCORD_TOKEN)
    
    async def start_async(self):
        """异步启动"""
        if not DISCORD_TOKEN:
            raise ValueError("DISCORD_BOT_TOKEN not set")
        
        logger.info("🚀 Starting Discord bridge (async mode)...")
        await _discord_bot.start(DISCORD_TOKEN)
    
    async def stop(self):
        """停止机器人"""
        await _discord_bot.close()


def create_bridge() -> DiscordBridge:
    """创建 Discord 桥接实例"""
    return DiscordBridge()


def get_bot() -> Optional[commands.Bot]:
    """获取 bot 实例"""
    return _discord_bot


if __name__ == "__main__":
    print(f"Discord Bridge - Standalone Mode")
    print(f"Token loaded: {'Yes' if DISCORD_TOKEN else 'No'}")
    print(f"Allowed users: {ALLOWED_USER_IDS if ALLOWED_USER_IDS else 'All'}")
    
    bridge = create_bridge()
    bridge.run()
