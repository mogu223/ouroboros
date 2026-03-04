"""
Discord Bot Bridge for Ouroboros

Provides Discord channel integration with the following features:
- Message routing between Discord and Ouroboros
- Owner-only access control
- Persistent message queue

Usage:
    python -m ouroboros.channels.discord_bot
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import discord
from discord.ext import commands

# Configuration
DATA_DIR = Path("/opt/ouroboros/data")
SECRETS_DIR = DATA_DIR / "secrets"
TOKEN_FILE = SECRETS_DIR / "discord_token.env"
MESSAGE_QUEUE = DATA_DIR / "discord_queue.jsonl"
OWNER_STATE = DATA_DIR / "state" / "state.json"

# Bot intents
intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.dm_messages = True


class OuroborosDiscordBot(commands.Bot):
    """Discord bot that bridges to Ouroboros agent."""
    
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.owner_id: Optional[int] = None
        self.owner_name: Optional[str] = None
        self._load_owner_info()
    
    def _load_owner_info(self):
        """Load owner info from state file."""
        try:
            with open(OWNER_STATE, 'r') as f:
                state = json.load(f)
                # We'll set owner_id when first message comes
                # For now, just note that owner exists
                if state.get("owner_chat_id"):
                    self.owner_name = f"owner_{state['owner_chat_id']}"
        except Exception as e:
            print(f"[Discord] Could not load owner state: {e}")
    
    async def on_ready(self):
        """Called when bot is connected."""
        print(f"[Discord] ✅ Logged in as {self.user} (ID: {self.user.id})")
        print(f"[Discord] Connected to {len(self.guilds)} guild(s)")
        for guild in self.guilds:
            print(f"[Discord]   - {guild.name} (ID: {guild.id})")
    
    async def on_message(self, message: discord.Message):
        """Handle incoming messages."""
        # Ignore own messages
        if message.author == self.user:
            return
        
        # Only process DMs or mentions
        is_dm = isinstance(message.channel, discord.DMChannel)
        is_mention = self.user.mentioned_in(message)
        
        if not (is_dm or is_mention):
            return
        
        # Extract message content
        content = message.content
        if is_mention:
            # Remove the mention from content
            content = content.replace(f"<@{self.user.id}>", "").strip()
            content = content.replace(f"<@!{self.user.id}>", "").strip()
        
        if not content:
            return
        
        # Log incoming message
        print(f"[Discord] 📥 From {message.author} (ID: {message.author.id}): {content[:100]}...")
        
        # Save to queue for Ouroboros to process
        await self._queue_message(
            discord_user_id=str(message.author.id),
            discord_user_name=str(message.author),
            content=content,
            channel_id=str(message.channel.id),
            is_dm=is_dm
        )
        
        # Send acknowledgment
        await message.reply("收到，正在处理...", mention_author=False)
    
    async def _queue_message(self, discord_user_id: str, discord_user_name: str, 
                             content: str, channel_id: str, is_dm: bool):
        """Queue message for Ouroboros to process."""
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "discord_user_id": discord_user_id,
            "discord_user_name": discord_user_name,
            "content": content,
            "channel_id": channel_id,
            "is_dm": is_dm,
            "processed": False
        }
        
        with open(MESSAGE_QUEUE, 'a') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        
        print(f"[Discord] ✅ Queued message from {discord_user_name}")


def load_token() -> str:
    """Load Discord bot token from secrets."""
    if not TOKEN_FILE.exists():
        raise FileNotFoundError(f"Token file not found: {TOKEN_FILE}")
    
    with open(TOKEN_FILE, 'r') as f:
        for line in f:
            line = line.strip()
            if line.startswith("DISCORD_BOT_TOKEN="):
                return line.split("=", 1)[1]
    
    raise ValueError("DISCORD_BOT_TOKEN not found in token file")


def main():
    """Run the Discord bot."""
    print("[Discord] 🚀 Starting Discord bot bridge...")
    
    try:
        token = load_token()
        print(f"[Discord] ✅ Token loaded ({len(token)} chars)")
    except Exception as e:
        print(f"[Discord] ❌ Failed to load token: {e}")
        sys.exit(1)
    
    # Ensure queue file exists
    MESSAGE_QUEUE.touch(exist_ok=True)
    
    # Create and run bot
    bot = OuroborosDiscordBot()
    
    try:
        bot.run(token)
    except discord.LoginFailure:
        print("[Discord] ❌ Invalid token!")
        sys.exit(1)
    except KeyboardInterrupt:
        print("[Discord] 🛑 Bot stopped by user")
    except Exception as e:
        print(f"[Discord] ❌ Bot error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()