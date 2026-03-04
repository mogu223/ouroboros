"""
Discord Bridge — Connect Ouroboros to Discord servers.

Features:
- Listen to messages in configured servers
- Route messages to Ouroboros main loop
- Support DM pairing for security
- White-list mode for trusted users only
"""

import os
import asyncio
import discord
from discord.ext import commands
from typing import Optional, Set
import logging

logger = logging.getLogger(__name__)


class DiscordBridge:
    """Bridge between Discord and Ouroboros."""
    
    def __init__(self, token: str, allowed_server_ids: Optional[Set[str]] = None, 
                 allowed_user_ids: Optional[Set[str]] = None):
        self.token = token
        self.allowed_server_ids = allowed_server_ids or set()
        self.allowed_user_ids = allowed_user_ids or set()
        
        # Discord bot setup
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        
        self.bot = commands.Bot(command_prefix='!', intents=intents)
        self.message_queue: asyncio.Queue = asyncio.Queue()
        
        self._setup_events()
    
    def _setup_events(self):
        """Setup Discord event handlers."""
        
        @self.bot.event
        async def on_ready():
            logger.info(f"Discord bot logged in as {self.bot.user}")
            for guild in self.bot.guilds:
                if str(guild.id) in self.allowed_server_ids or not self.allowed_server_ids:
                    logger.info(f"Connected to server: {guild.name} ({guild.id})")
        
        @self.bot.event
        async def on_message(message: discord.Message):
            # Ignore bot's own messages
            if message.author == self.bot.user:
                return
            
            # Check server whitelist
            if self.allowed_server_ids:
                if message.guild and str(message.guild.id) not in self.allowed_server_ids:
                    return
            
            # Check user whitelist
            if self.allowed_user_ids:
                if str(message.author.id) not in self.allowed_user_ids:
                    # Send pairing code request
                    await message.channel.send(
                        f"🔐 你不在白名单中。请联系管理员添加你的用户 ID: `{message.author.id}`"
                    )
                    return
            
            # Queue message for Ouroboros processing
            await self.message_queue.put({
                'type': 'discord_message',
                'author': str(message.author),
                'author_id': str(message.author.id),
                'channel': str(message.channel),
                'channel_id': str(message.channel.id),
                'guild': str(message.guild) if message.guild else 'DM',
                'guild_id': str(message.guild.id) if message.guild else None,
                'content': message.content,
                'message_id': str(message.id),
                'timestamp': message.created_at.isoformat(),
            })
            
            # Acknowledge receipt
            await message.add_reaction('👀')
    
    async def send_message(self, channel_id: str, content: str):
        """Send a message to a Discord channel."""
        channel = self.bot.get_channel(int(channel_id))
        if channel:
            await channel.send(content)
        else:
            logger.warning(f"Channel {channel_id} not found")
    
    async def start(self):
        """Start the Discord bot."""
        logger.info("Starting Discord bridge...")
        await self.bot.start(self.token)
    
    async def stop(self):
        """Stop the Discord bot."""
        await self.bot.close()
        logger.info("Discord bridge stopped")
    
    async def get_pending_messages(self):
        """Get all pending messages from the queue."""
        messages = []
        while not self.message_queue.empty():
            messages.append(await self.message_queue.get())
        return messages


async def main():
    """Main entry point for standalone testing."""
    token = os.getenv('DISCORD_BOT_TOKEN')
    if not token:
        logger.error("DISCORD_BOT_TOKEN not set")
        return
    
    bridge = DiscordBridge(
        token=token,
        allowed_server_ids={'1476089709632553195'},  # Mushroom Family server
        allowed_user_ids=set()  # Empty = allow all in allowed servers
    )
    
    try:
        await bridge.start()
    except KeyboardInterrupt:
        await bridge.stop()


if __name__ == '__main__':
    asyncio.run(main())
