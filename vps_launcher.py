import logging, os, sys, pathlib, time, threading, types, builtins, typing, random
for n in ['Any','Dict','List','Optional','Set','Tuple','Union','Iterable','Callable']:
    try: setattr(builtins, n, getattr(typing, n))
    except: pass
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
log = logging.getLogger("Ouroboros")
base_dir = pathlib.Path("/opt/ouroboros").resolve()
sys.path.insert(0, str(base_dir))
data_dir = base_dir / "data"
try:
    from supervisor.state import init as s_init, init_state, load_state, save_state, append_jsonl
    from supervisor.telegram import init as t_init, TelegramClient, send_with_budget
    from supervisor.workers import init as w_init, spawn_workers, get_event_q, WORKERS, PENDING, RUNNING, handle_chat_direct
    from supervisor.queue import restore_pending_from_snapshot
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    budget = float(os.environ.get("TOTAL_BUDGET", "500000"))
    TG = TelegramClient(token)
    def manual_dispatch(evt, ctx):
        etype = evt.get("type")
        if etype in ["chat_reply", "task_result"]:
            content = evt.get("content") or evt.get("result", {}).get("content")
            cid = evt.get("chat_id") or evt.get("result", {}).get("chat_id")
            if content and cid:
                log.info("📤 发送回 Telegram...")
                try: ctx.send_with_budget(cid, content)
                except: pass
    _ctx = types.SimpleNamespace(DRIVE_ROOT=data_dir, REPO_DIR=base_dir, TG=TG, WORKERS=WORKERS, PENDING=PENDING, RUNNING=RUNNING, MAX_WORKERS=2, send_with_budget=send_with_budget, load_state=load_state, save_state=save_state, append_jsonl=append_jsonl)
    s_init(data_dir, budget); init_state()
    t_init(drive_root=data_dir, total_budget_limit=budget, budget_report_every=5, tg_client=TG)
    w_init(repo_dir=base_dir, drive_root=data_dir, max_workers=2, soft_timeout=600, hard_timeout=1800, total_budget_limit=budget)
except Exception as e:
    log.error("❌ 失败: " + str(e)); sys.exit(1)
def smart_chat(cid, txt, img):
    if os.environ.get("API_POOL"):
        cfg = random.choice(os.environ.get("API_POOL").split(",")).split("|")
        os.environ["OPENAI_BASE_URL"], os.environ["OPENAI_API_KEY"] = cfg[0], cfg[1]
    if handle_chat_direct:
        handle_chat_direct(cid, txt + "\n(直接用中文简短回答)", img)
spawn_workers(2); restore_pending_from_snapshot()
offset = int(load_state().get("tg_offset") or 0)
log.info("🚀 终极版启动...")
while True:
    try:
        eq = get_event_q()
        while not eq.empty(): manual_dispatch(eq.get_nowait(), _ctx)
        updates = TG.get_updates(offset=offset, timeout=5)
        for upd in updates:
            offset = int(upd["update_id"]) + 1
            msg = upd.get("message") or {}
            if msg.get("text"):
                threading.Thread(target=smart_chat, args=(msg['chat']['id'], msg['text'], None), daemon=True).start()
        st = load_state(); st["tg_offset"] = offset; save_state(st); time.sleep(0.5)
    except Exception as e:
        log.error("⚠️ 异常: " + str(e)); time.sleep(1)
