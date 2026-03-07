"""
Discord Bridge initialization module for vps_launcher.

Extracted from vps_launcher.py to comply with Principle 5 (Minimalism).
Each function is under 80 lines.
"""

from __future__ import annotations

import logging
import os
import pathlib
import threading
from typing import Optional

log = logging.getLogger('Ouroboros')


def _find_discord_config(
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
) -> Optional[pathlib.Path]:
    """Find Discord configuration file in standard locations."""
    config_paths = [
        pathlib.Path("/opt/ouroboros/.env.discord"),
        repo_dir / ".env.discord",
        drive_root / ".env.discord",
    ]
    
    for path in config_paths:
        if path.exists():
            return path
    return None


def _parse_discord_config(config_path: pathlib.Path) -> tuple[Optional[str], Optional[str]]:
    """Parse Discord token and owner ID from config file."""
    token: Optional[str] = None
    owner_id: Optional[str] = None
    
    with open(config_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line.startswith("DISCORD_BOT_TOKEN="):
                token = line.split("=", 1)[1].strip()
            elif line.startswith("DISCORD_OWNER_ID="):
                owner_id = line.split("=", 1)[1].strip()
    
    return token, owner_id


def _start_discord_thread(token: str, owner_id: Optional[str]) -> bool:
    """Start Discord bot in background thread."""
    os.environ["DISCORD_BOT_TOKEN"] = token
    if owner_id:
        os.environ["DISCORD_OWNER_ID"] = owner_id
    
    try:
        from ouroboros.channels.discord_bridge import DiscordBridge
        
        def run_bot():
            try:
                bridge = DiscordBridge()
                bridge.run()
            except Exception as e:
                log.error(f"Discord bot error: {e}", exc_info=True)
        
        thread = threading.Thread(target=run_bot, daemon=True, name="DiscordBridge")
        thread.start()
        log.info("✅ Discord bridge started in background thread")
        return True
        
    except Exception as e:
        log.warning(f"Failed to start Discord bridge: {e}")
        return False


def init_discord_bridge(
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
) -> bool:
    """
    Initialize Discord Bridge if configured.
    
    Returns True if bridge was started successfully, False otherwise.
    """
    try:
        config_path = _find_discord_config(repo_dir, drive_root)
        if not config_path:
            log.info("Discord config file not found, Discord bridge disabled")
            return False
        
        token, owner_id = _parse_discord_config(config_path)
        
        if not token:
            log.info("Discord token not found in config, Discord bridge disabled")
            return False
        
        log.info("Discord configuration found, initializing bridge...")
        return _start_discord_thread(token, owner_id)
        
    except Exception as e:
        log.warning(f"Failed to initialize Discord bridge: {e}")
        return False
