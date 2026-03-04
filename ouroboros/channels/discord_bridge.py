"""
Discord Bridge Module for Ouroboros

Connects Ouroboros to Discord servers via discord.py
"""

import os
import asyncio
import discord
from discord.ext import commands
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# 配置
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ALLOWED_USER_IDS = set()  # 白名单用户 ID，可从环境变量加载

class DiscordBridge:
    """Discord 桥接类"""
    
    def __init__(self, ouroboros_callback=None):
        self.callback = ouroboros_callback  # 回调函数：接收 Discord 消息并转发给 Ouroboros
        self.intents = discord.Intents.default()
        self.intents.message_content = True
        self.intents.dm_messages = True
        
        self.bot = commands.Bot(
            command_prefix="!",
            intents=self.intents,
            help_command=None  # 禁用默认 help 命令
        )
        
        self._setup_events()
        self._setup_commands()
    
    def _setup_events(self):
        """设置事件处理器"""
        
        @self.bot.event
        async def on_ready():
            logger.info(f"Discord bot logged in as {self.bot.user}")
            logger.info(f"Connected to {len(self.bot.guilds)} guilds")
        
        @self.bot.event
        async def on_message(message):
            # 忽略机器人自己的消息
            if message.author == self.bot.user:
                return
            
            # 白名单检查（如果设置了白名单）
            if ALLOWED_USER_IDS and message.author.id not in ALLOWED_USER_IDS:
                logger.warning(f"Blocked message from unauthorized user: {message.author.id}")
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
        
        logger.info(f"Message from {username} ({user_id}) in {channel_type}: {content[:50]}...")
        
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
                    await message.channel.send(response)
            except Exception as e:
                logger.error(f"Error processing message: {e}")
                await message.channel.send("⚠️ 处理消息时出错")
    
    def _setup_commands(self):
        """设置命令"""
        
        @self.bot.command(name="ping")
        async def ping(ctx):
            """检查机器人状态"""
            await ctx.send(f"🟢 Online | Latency: {round(self.bot.latency * 1000)}ms")
        
        @self.bot.command(name="help")
        async def help_cmd(ctx):
            """显示帮助信息"""
            help_text = """
            **大喷菇 Discord Bot**
            
            直接发送消息即可与我对话。
            
            可用命令：
            • `!ping` - 检查状态
            • `!help` - 显示帮助
            
            我会在收到消息后尽快回复。
            """
            await ctx.send(help_text)
    
    def run(self):
        """运行机器人"""
        if not DISCORD_TOKEN:
            raise ValueError("DISCORD_BOT_TOKEN not set in environment")
        
        logger.info("Starting Discord bridge...")
        self.bot.run(DISCORD_TOKEN)
    
    def run_async(self, loop: asyncio.AbstractEventLoop):
        """在现有事件循环中运行"""
        if not DISCORD_TOKEN:
            raise ValueError("DISCORD_BOT_TOKEN not set in environment")
        
        logger.info("Starting Discord bridge (async mode)...")
        return self.bot.start(DISCORD_TOKEN)


# 便捷函数
def create_bridge(callback=None) -> DiscordBridge:
    """创建 Discord 桥接实例"""
    return DiscordBridge(callback=callback)


if __name__ == "__main__":
    #  standalone 测试模式
    async def test_callback(data):
        print(f"Received: {data}")
        return f"Echo: {data['content']}"
    
    bridge = create_bridge(callback=test_callback)
    bridge.run()
