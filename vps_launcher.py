import logging, os, sys, pathlib, time, threading, types, builtins, typing, random
for n in ['Any','Dict','List','Optional','Set','Tuple','Union','Iterable','Callable']:
    try: setattr(builtins, n, getattr(typing, n))
    except: pass
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
log = logging.getLogger("Ouroboros")
base_dir = pathlib.Path("/opt/ouroboros").resolve()
sys.path.insert(0, str(base_dir))
data_dir = pathlib.Path("/content/drive/MyDrive/Ouroboros").resolve()
os.environ["OUROBOROS_DRIVE_ROOT"] = str(data_dir)
from supervisor.state import load_state, save_state
from supervisor.telegram import TelegramClient, init as t_init
from supervisor.workers import spawn_workers, get_event_q, handle_chat_direct
from supervisor.queue import restore_pending_from_snapshot
token = os.environ.get("TELEGRAM_BOT_TOKEN")
budget = float(os.environ.get("TOTAL_BUDGET", "500000"))
TG = TelegramClient(token)
def dispatch(evt, ctx):
    from supervisor.events import dispatch_event
    dispatch_event(evt, ctx)

# Discord Bridge 初始化
DISCORD_ENABLED = False
try:
    discord_config_path = pathlib.Path("/opt/ouroboros/.env.discord")
    if discord_config_path.exists():
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
            os.environ["DISCORD_BOT_TOKEN"] = discord_token
            if discord_owner_id:
                os.environ["DISCORD_OWNER_ID"] = discord_owner_id
            
            from ouroboros.channels.discord_bridge import DiscordBridge
            
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

from supervisor.telegram import send_with_budget
from supervisor.state import append_jsonl
_ctx = types.SimpleNamespace(
    DRIVE_ROOT=data_dir, 
    REPO_DIR=base_dir, 
    TG=TG, 
    MAX_WORKERS=2, 
    send_with_budget=send_with_budget, 
    load_state=load_state, 
    save_state=save_state,
    append_jsonl=append_jsonl
)
# s_init removed
t_init(drive_root=data_dir, total_budget_limit=budget, budget_report_every=5, tg_client=TG)
def smart_chat(cid, txt, img):
    if os.environ.get("API_POOL"):
        c = random.choice(os.environ.get("API_POOL").split(",")).split("|")
        os.environ["OPENAI_BASE_URL"], os.environ["OPENAI_API_KEY"] = c[0], c[1]
    handle_chat_direct(cid, txt + "\n(用中文回复)", img)
spawn_workers(2); restore_pending_from_snapshot()
offset = int(load_state().get("tg_offset") or 0)
log.info("🚀 启动成功，监听中...")
while True:
    try:
        eq = get_event_q()
        while not eq.empty(): dispatch(eq.get_nowait(), _ctx)
        upds = TG.get_updates(offset=offset, timeout=5)
        for u in upds:
            offset = int(u["update_id"]) + 1
            m = u.get("message", {})
            if m.get("text"): threading.Thread(target=smart_chat, args=(m['chat']['id'], m['text'], None), daemon=True).start()
        st = load_state(); st["tg_offset"] = offset; save_state(st); time.sleep(0.5)
    except Exception as e: log.error(f"ERR: {e}"); time.sleep(1)
