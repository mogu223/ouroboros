# ----------------------------
# 4.1) Initialize Discord Bridge (if configured)
# ----------------------------
DISCORD_BRIDGE = None
DISCORD_ENABLED = False

try:
    # Try multiple config locations
    discord_config_paths = [
        pathlib.Path("/opt/ouroboros/.env.discord"),
        REPO_DIR / ".env.discord",
        DRIVE_ROOT / ".env.discord",
    ]
    
    discord_config_path = None
    for path in discord_config_paths:
        if path.exists():
            discord_config_path = path
            break
    
    if discord_config_path:
        # Parse config file
        discord_token = None
        discord_owner_id = None
        with open(discord_config_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith("DISCORD_BOT_TOKEN="):
                    discord_token = line.split("=", 1)[1].strip()
                elif line.startswith("DISCORD_OWNER_ID="):
                    discord_owner_id = line.split("=", 1)[1].strip()
        
        if discord_token:
            log.info("Discord configuration found, initializing bridge...")
            
            # Set environment variable for discord_bridge module
            os.environ["DISCORD_BOT_TOKEN"] = discord_token
            if discord_owner_id:
                os.environ["DISCORD_OWNER_ID"] = discord_owner_id
            
            # Import and initialize Discord bridge
            from ouroboros.channels.discord_bridge import DiscordBridge
            
            # Start Discord bot in background thread
            def start_discord_bot():
                try:
                    bridge = DiscordBridge()
                    bridge.run()
                except Exception as e:
                    log.error(f"Discord bot error: {e}", exc_info=True)
            
            discord_thread = threading.Thread(target=start_discord_bot, daemon=True, name="DiscordBridge")
            discord_thread.start()
            DISCORD_ENABLED = True
            log.info("✅ Discord bridge started in background thread")
        else:
            log.info("Discord token not found in config, Discord bridge disabled")
    else:
        log.info("Discord config file not found, Discord bridge disabled")
except Exception as e:
    log.warning(f"Failed to initialize Discord bridge: {e}")
    DISCORD_ENABLED = False