#!/usr/bin/env python3
"""
Test Discord connection
"""
import os
import sys

# Load config
config_path = "/opt/ouroboros/.env.discord"
if not os.path.exists(config_path):
    print(f"❌ Config file not found: {config_path}")
    sys.exit(1)

# Parse config
discord_token = None
discord_owner_id = None
with open(config_path, 'r') as f:
    for line in f:
        line = line.strip()
        if line.startswith("DISCORD_BOT_TOKEN="):
            discord_token = line.split("=", 1)[1].strip()
        elif line.startswith("DISCORD_OWNER_ID="):
            discord_owner_id = line.split("=", 1)[1].strip()

if not discord_token:
    print("❌ DISCORD_BOT_TOKEN not found")
    sys.exit(1)

print(f"✅ Token loaded: {discord_token[:20]}...")
print(f"✅ Owner ID: {discord_owner_id}")

# Test discord.py
try:
    import discord
    from discord.ext import commands
    print(f"✅ discord.py version: {discord.__version__}")
except ImportError as e:
    print(f"❌ discord.py not installed: {e}")
    sys.exit(1)

# Create bot
intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True
intents.guild_messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"✅✅✅ SUCCESS! Bot logged in as: {bot.user}")
    print(f"   ID: {bot.user.id}")
    print(f"   Connected to {len(bot.guilds)} guild(s)")
    await bot.close()

@bot.event
async def on_connect():
    print("✅ Connected to Discord gateway...")

try:
    print("🚀 Attempting to connect to Discord...")
    bot.run(discord_token)
    print("✅✅✅ Discord connection test PASSED!")
except Exception as e:
    print(f"❌❌❌ Discord connection test FAILED: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
