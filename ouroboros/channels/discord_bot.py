"""
Discord Bot Bridge for Ouroboros

Provides Discord channel integration with the following features:
- Message routing between Discord and Ouroboros
- Owner-only access control
- Direct LLM integration with Chinese response enforcement

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
OWNER_STATE = DATA_DIR / "state" / "state.json"

# Bot intents
intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.dm_messages = True


# Load LLM client
try:
    from ouroboros.llm import LLMClient
    llm_client = LLMClient()
    print("[Discord] ✅ LLM client loaded")
except Exception as e:
    print(f"[Discord] ⚠️ Could not load LLM client: {e}")
    llm_client = None


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
        
        # Send typing indicator
        async with message.channel.typing():
            # Call LLM directly
            response = await self._call_llm(content, message.author)
            
            # Send response back to Discord
            if response:
                # Split long responses into multiple messages
                max_length = 1900
                chunks = [response[i:i+max_length] for i in range(0, len(response), max_length)]
                for i, chunk in enumerate(chunks):
                    if i == 0:
                        await message.reply(chunk, mention_author=False)
                    else:
                        await message.channel.send(chunk)
            else:
                await message.reply("⚠️ 处理失败，请稍后再试", mention_author=False)
    
    async def _call_llm(self, user_message: str, user: discord.User) -> Optional[str]:
        """Call LLM and get Chinese response."""
        if llm_client is None:
            return "⚠️ LLM 客户端未初始化"
        
        # Build prompt with Chinese enforcement
        system_prompt = """你是在 Discord 上提供服务的 AI 助手"大喷菇"。

重要要求：
1. **必须用中文回复**（除非用户明确要求其他语言）
2. 回复要简洁、友好、有帮助
3. 如果不确定，诚实说明
4. 不要提及你是 AI 或机器人，自然对话即可

用户消息："""
        
        try:
            print(f"[Discord] 🔄 Calling LLM for user {user}...")
            
            # Call LLM synchronously (run in executor to not block)
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: llm_client.chat(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message}
                    ],
                    model="kimi-k2.5"  # Use stable model
                )
            )
            
            # Extract content from response
            if response and isinstance(response, dict):
                content = response.get("content", "")
                if content:
                    print(f"[Discord] ✅ Got response ({len(content)} chars)")
                    return content.strip()
                else:
                    # Check for reasoning_content (Qwen thinking models)
                    reasoning = response.get("reasoning_content", "")
                    if reasoning:
                        print(f"[Discord] ⚠️ Got reasoning but no content")
                        return "⚠️ 思考中，请稍后..."
            
            print(f"[Discord] ⚠️ Empty response from LLM")
            return None
            
        except Exception as e:
            print(f"[Discord] ❌ LLM call failed: {e}")
            return f"⚠️ 处理出错：{str(e)[:100]}"


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
