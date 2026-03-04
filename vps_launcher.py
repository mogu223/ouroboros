import logging, os, sys, pathlib, time, threading, types, builtins, typing, random
for n in ['Any','Dict','List','Optional','Set','Tuple','Union','Iterable','Callable']:
    try: setattr(builtins, n, getattr(typing, n))
    except: pass
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
log = logging.getLogger("Ouroboros")
base_dir = pathlib.Path("/opt/ouroboros").resolve()
sys.path.insert(0, str(base_dir))
data_dir = base_dir / "data"
from supervisor.state import init as s_init, init_state, load_state, save_state
from supervisor.telegram import TelegramClient, init as t_init
from supervisor.workers import spawn_workers, get_event_q, handle_chat_direct
from supervisor.queue import restore_pending_from_snapshot
token = os.environ.get("TELEGRAM_BOT_TOKEN")
budget = float(os.environ.get("TOTAL_BUDGET", "500000"))
TG = TelegramClient(token)
def dispatch(evt, ctx):
    msg = evt.get("text") or evt.get("content") or evt.get("result", {}).get("content")
    cid = evt.get("chat_id") or evt.get("result", {}).get("chat_id")
    if msg and cid:
        log.info(f"📤 发送回复到: {cid}")
        ctx.send_with_budget(cid, msg)
_ctx = types.SimpleNamespace(DRIVE_ROOT=data_dir, REPO_DIR=base_dir, TG=TG, MAX_WORKERS=2, send_with_budget=TG.send_message, load_state=load_state, save_state=save_state)
s_init(data_dir, budget); init_state()
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
