#!/usr/bin/env python3
"""
Discord Bot for Ouroboros
Provides a Discord interface for communicating with the Ouroboros AI agent.
"""

import os
import asyncio
import logging
from pathlib import Path

# Load environment from .env.discord
env_file = Path(__file__).parent.parent / ".env.discord"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            if "=" in line:
                key, value = line.strip().split("=", 1)
                os.environ[key] = value

import discord
from discord.ext import commands

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("discord_bot")

# Bot configuration
TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
OWNER_ID = os.environ.get("DISCORD_OWNER_ID")

if not TOKEN:
    logger.error("DISCORD_BOT_TOKEN not set!")
    exit(1)

# Intents
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

# Create bot
bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    description="大喷菇 - Ouroboros AI Agent"
)

# Whitelist (if OWNER_ID is set)
whitelist = set()
if OWNER_ID:
    whitelist.add(int(OWNER_ID))
    logger.info(f"Whitelist enabled. Owner ID: {OWNER_ID}")


def is_authorized(user_id: int) -> bool:
    """Check if user is authorized to use the bot."""
    if not whitelist:  # No whitelist = everyone can use
        return True
    return user_id in whitelist


@bot.event
async def on_ready():
    """Called when bot is ready."""
    logger.info(f"✅ 已登录为 {bot.user} (ID: {bot.user.id})")
    logger.info(f"📊 已加入 {len(bot.guilds)} 个服务器")
    for guild in bot.guilds:
        logger.info(f"  - {guild.name} (ID: {guild.id})")


@bot.event
async def on_message(message: discord.Message):
    """Handle incoming messages."""
    # Ignore bot messages
    if message.author.bot:
        return
    
    # Check authorization
    if not is_authorized(message.author.id):
        logger.warning(f"Unauthorized access attempt from {message.author} (ID: {message.author.id})")
        return
    
    # Process commands first
    await bot.process_commands(message)
    
    # Handle DM or mention
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mention = bot.user.mentioned_in(message)
    
    if is_dm or is_mention:
        # Remove mention from message
        content = message.content
        if is_mention:
            content = content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
        
        if not content:
            await message.reply("你好！我是大喷菇。有什么可以帮你的？")
            return
        
        logger.info(f"收到消息 from {message.author}: {content[:100]}...")
        
        # TODO: Forward to Ouroboros agent
        # For now, echo back
        await message.reply(f"收到：{content}")


@bot.command(name="ping")
async def ping(ctx: commands.Context):
    """Check bot latency."""
    latency = round(bot.latency * 1000)
    await ctx.send(f"🏓 Pong! 延迟: {latency}ms")


@bot.command(name="status")
async def status(ctx: commands.Context):
    """Show bot status."""
    embed = discord.Embed(
        title="🟢 大喷菇状态",
        color=discord.Color.green()
    )
    embed.add_field(name="运行状态", value="在线", inline=True)
    embed.add_field(name="延迟", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="服务器数", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="版本", value="v7.1.0", inline=True)
    await ctx.send(embed=embed)


@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    """Show help message."""
    embed = discord.Embed(
        title="🍄 大喷菇帮助",
        description="我是蘑菇家族的 AI Agent，由 Ouroboros 架构驱动。",
        color=discord.Color.blue()
    )
    embed.add_field(name="!ping", value="检查延迟", inline=False)
    embed.add_field(name="!status", value="查看状态", inline=False)
    embed.add_field(name="直接消息或@提及", value="与我对话", inline=False)
    await ctx.send(embed=embed)


def main():
    """Main entry point."""
    logger.info("🚀 启动 Discord Bot...")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()