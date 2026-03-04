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
import threading
import uuid

logger = logging.getLogger(__name__)

# 配置文件路径
CONFIG_FILE = Path("/opt/ouroboros/.env.discord")

# Supervisor 引用（运行时注入）
_supervisor_refs = {
    "handle_chat": None,
    "send_message": None,
}

def inject_supervisor_refs(handle_chat_fn, send_message_fn):
    """注入 supervisor 的函数引用"""
    _supervisor_refs["handle_chat"] = handle_chat_fn
    _supervisor_refs["send_message"] = send_message_fn

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


class DiscordBridge:
    """Discord 桥接类"""
    
    def __init__(self):
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
            
            # 白名单检查
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
        
        # 使用 supervisor 的处理函数（如果有）
        handle_chat_fn = _supervisor_refs.get("handle_chat")
        
        if handle_chat_fn:
            try:
                # 构造一个临时的 chat_id（Discord 用户 ID + 特殊前缀）
                temp_chat_id = f"discord_{user_id}"
                
                # 在后台线程中处理（避免阻塞 Discord 事件循环）
                response_container = {"response": None, "error": None}
                
                def process_message():
                    try:
                        # 调用 supervisor 的 handle_chat_direct
                        handle_chat_fn(
                            chat_id=temp_chat_id,
                            text=content,
                            image_data=None
                        )
                        # 等待响应（通过事件队列）
                        # 这里简化处理：直接回复"收到"，实际响应由事件系统异步发送
                        response_container["response"] = None
                    except Exception as e:
                        response_container["error"] = str(e)
                
                # 先回复"收到"
                await message.channel.send("🍄 收到，正在处理...")
                
                # 启动后台处理
                thread = threading.Thread(target=process_message, daemon=True)
                thread.start()
                
            except Exception as e:
                logger.error(f"❌ Error processing message: {e}")
                await message.channel.send(f"⚠️ 处理消息时出错：{e}")
        else:
            # 没有 supervisor 时，简单回显
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
            
📡 状态：在线
⏱️ 延迟：{round(self.bot.latency * 1000)}ms
🏠 服务器数：{len(self.bot.guilds)}
🔐 模式：{"白名单" if ALLOWED_USER_IDS else "公开"}
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
        """异步启动"""
        if not DISCORD_TOKEN:
            raise ValueError("DISCORD_BOT_TOKEN not set")
        
        logger.info("🚀 Starting Discord bridge (async mode)...")
        await self.bot.start(DISCORD_TOKEN)
    
    async def stop(self):
        """停止机器人"""
        await self.bot.close()


def create_bridge() -> DiscordBridge:
    """创建 Discord 桥接实例"""
    return DiscordBridge()


if __name__ == "__main__":
    print(f"Discord Bridge - Standalone Mode")
    print(f"Token loaded: {'Yes' if DISCORD_TOKEN else 'No'}")
    print(f"Allowed users: {ALLOWED_USER_IDS if ALLOWED_USER_IDS else 'All'}")
    
    bridge = create_bridge()
    bridge.run()
