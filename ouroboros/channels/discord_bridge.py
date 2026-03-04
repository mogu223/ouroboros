"""
Discord Bridge Module for Ouroboros

Connects Ouroboros to Discord servers via discord.py
"""

import os
import asyncio
import discord
from discord.ext import commands
from typing import Optional, Callable, Awaitable
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 配置文件路径
CONFIG_FILE = Path("/opt/ouroboros/.env.discord")

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
                        # 支持逗号分隔的多个用户 ID
                        for uid in value.split(","):
                            uid = uid.strip()
                            if uid:
                                config["allowed_users"].add(int(uid))
    
    return config

# 加载配置
CONFIG = load_config()
DISCORD_TOKEN = CONFIG["token"]
ALLOWED_USER_IDS = CONFIG["allowed_users"]


class DiscordBridge:
    """Discord 桥接类"""
    
    def __init__(self, callback: Optional[Callable[[dict], Awaitable[str]]] = None):
        """
        初始化 Discord 桥接
        
        Args:
            callback: 异步回调函数，接收消息数据，返回响应文本
        """
        self.callback = callback
        self.intents = discord.Intents.default()
        self.intents.message_content = True
        self.intents.dm_messages = True
        self.intents.guild_messages = True
        
        self.bot = commands.Bot(
            command_prefix="!",
            intents=self.intents,
            help_command=None
        )
        
        self._setup_events()
        self._setup_commands()
    
    def _setup_events(self):
        """设置事件处理器"""
        
        @self.bot.event
        async def on_ready():
            logger.info(f"🟢 Discord bot logged in as {self.bot.user}")
            logger.info(f"📡 Connected to {len(self.bot.guilds)} guilds")
            if ALLOWED_USER_IDS:
                logger.info(f"🔐 Whitelist mode: {len(ALLOWED_USER_IDS)} allowed user(s)")
            else:
                logger.info(f"🌐 Open mode: all users can interact")
        
        @self.bot.event
        async def on_message(message):
            # 忽略机器人自己的消息
            if message.author == self.bot.user:
                return
            
            # 忽略其他机器人
            if message.author.bot:
                return
            
            # 白名单检查（如果设置了白名单）
            if ALLOWED_USER_IDS and message.author.id not in ALLOWED_USER_IDS:
                logger.warning(f"🚫 Blocked message from unauthorized user: {message.author} ({message.author.id})")
                return
            
            # 处理消息
            await self._handle_message(message)
    
    async def _handle_message(self, message):
        """处理收到的消息"""
        content = message.content
        channel_type = "dm" if isinstance(message.channel, discord.DMChannel) else "guild"
        guild_id = message.guild.id if message.guild else None
        channel_id = message.channel.id
        user_id = message.author.id
        username = message.author.name
        
        logger.info(f"📨 Message from {username} ({user_id}) in {channel_type}: {content[:50]}...")
        
        # 如果有回调，转发给 Ouroboros
        if self.callback:
            try:
                response = await self.callback({
                    "type": "discord_message",
                    "content": content,
                    "channel_type": channel_type,
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                    "user_id": user_id,
                    "username": username,
                    "message_id": message.id
                })
                
                # 发送响应回 Discord
                if response:
                    # Discord 消息限制 2000 字符
                    if len(response) > 2000:
                        for i in range(0, len(response), 1900):
                            await message.channel.send(response[i:i+1900])
                    else:
                        await message.channel.send(response)
            except Exception as e:
                logger.error(f"❌ Error processing message: {e}")
                await message.channel.send("⚠️ 处理消息时出错")
        else:
            # 没有回调时，简单回显
            await message.channel.send(f"🍄 收到：{content[:100]}")
    
    def _setup_commands(self):
        """设置命令"""
        
        @self.bot.command(name="ping")
        async def ping(ctx):
            """检查机器人状态"""
            await ctx.send(f"🟢 Online | Latency: {round(self.bot.latency * 1000)}ms")
        
        @self.bot.command(name="status")
        async def status(ctx):
            """显示详细状态"""
            status_text = f"""🟢 **大喷菇 Discord Bot**
            
📡 状态: 在线
⏱️ 延迟: {round(self.bot.latency * 1000)}ms
🏠 服务器数: {len(self.bot.guilds)}
🔐 模式: {"白名单" if ALLOWED_USER_IDS else "公开"}
            """
            await ctx.send(status_text)
        
        @self.bot.command(name="help")
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
        self.bot.run(DISCORD_TOKEN)
    
    async def start_async(self):
        """异步启动（用于集成到现有事件循环）"""
        if not DISCORD_TOKEN:
            raise ValueError("DISCORD_BOT_TOKEN not set")
        
        logger.info("🚀 Starting Discord bridge (async mode)...")
        await self.bot.start(DISCORD_TOKEN)
    
    async def stop(self):
        """停止机器人"""
        await self.bot.close()


def create_bridge(callback=None) -> DiscordBridge:
    """创建 Discord 桥接实例"""
    return DiscordBridge(callback=callback)


if __name__ == "__main__":
    # standalone 测试模式
    print(f"Discord Bridge - Standalone Mode")
    print(f"Token loaded: {'Yes' if DISCORD_TOKEN else 'No'}")
    print(f"Allowed users: {ALLOWED_USER_IDS if ALLOWED_USER_IDS else 'All'}")
    
    async def test_callback(data):
        print(f"Received: {data}")
        return f"🍄 Echo: {data['content']}"
    
    bridge = create_bridge(callback=test_callback)
    bridge.run()