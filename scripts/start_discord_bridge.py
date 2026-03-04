#!/usr/bin/env python3
"""
Standalone Discord Bridge Launcher

Usage:
    python scripts/start_discord_bridge.py
"""

import os
import sys
import asyncio

# 添加项目路径
sys.path.insert(0, "/opt/ouroboros")

# 从 Drive 加载 Token
def load_discord_token():
    """从 Google Drive 加载 Discord Token"""
    token_path = "/opt/ouroboros/data/secrets/discord_token.env"
    
    if not os.path.exists(token_path):
        print(f"❌ Token file not found: {token_path}")
        return None
    
    with open(token_path, "r") as f:
        for line in f:
            if line.startswith("DISCORD_BOT_TOKEN="):
                token = line.strip().split("=", 1)[1]
                return token
    
    print("❌ DISCORD_BOT_TOKEN not found in file")
    return None

async def ouroboros_callback(message_data):
    """
    处理 Discord 消息的回调函数
    
    这里可以集成到 Ouroboros 主循环
    目前只是简单回显
    """
    content = message_data["content"]
    username = message_data["username"]
    
    print(f"📨 [{username}]: {content}")
    
    # TODO: 集成到 Ouroboros 主循环
    # 目前返回简单响应
    return f"🤖 大喷菇收到消息：{content[:50]}..."

def main():
    print("🚀 Starting Discord Bridge...")
    
    # 加载 Token
    token = load_discord_token()
    if not token:
        sys.exit(1)
    
    os.environ["DISCORD_BOT_TOKEN"] = token
    print("✅ Token loaded")
    
    # 导入并运行
    from ouroboros.channels.discord_bridge import create_bridge
    
    bridge = create_bridge(callback=ouroboros_callback)
    
    print("🟢 Discord bridge started")
    print("Press Ctrl+C to stop")
    
    try:
        bridge.run()
    except KeyboardInterrupt:
        print("\n👋 Stopping Discord bridge...")
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
