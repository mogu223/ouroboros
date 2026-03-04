import logging
import os, sys, pathlib, time, threading, types, builtins, typing, random

# --- [0] 类型注入 ---
for name in ['Any', 'Dict', 'List', 'Optional', 'Set', 'Tuple', 'Union', 'Iterable', 'Callable']:
    try: setattr(builtins, name, getattr(typing, name))
    except: pass

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger("OuroborosFinal")
base_dir = pathlib.Path("/opt/ouroboros").resolve()
sys.path.insert(0, str(base_dir))
data_dir = base_dir / "data"

# --- [1] API 资源池解析 ---
raw_pool = os.environ.get("API_POOL", "")
API_POOL = []
if raw_pool:
    for entry in raw_pool.split(","):
        if "|" in entry:
            u, k = entry.split("|", 1)
            API_POOL.append({"url": u.strip(), "key": k.strip()})
log.info(f"✅ API 池已就绪: {len(API_POOL)} 组配置")

# --- [2] 初始化核心 ---
try:
    from supervisor.state import init as s_init, init_state, load_state, save_state, append_jsonl
    from supervisor.telegram import init as t_init, TelegramClient, send_with_budget
    from supervisor.workers import init as w_init, spawn_workers, get_event_q, WORKERS, PENDING, RUNNING, handle_chat_direct
    from supervisor.queue import restore_pending_from_snapshot
    
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    budget_limit = float(os.environ.get("TOTAL_BUDGET", "500000"))
    s_init(data_dir, budget_limit)
    init_state()
    TG = TelegramClient(token)
    t_init(drive_root=data_dir, total_budget_limit=budget_limit, budget_report_every=5, tg_client=TG)
    w_init(repo_dir=base_dir, drive_root=data_dir, max_workers=2, soft_timeout=600, hard_timeout=1800, total_budget_limit=budget_limit)

    # --- [关键：手动重建传令逻辑] ---
    def manual_dispatch(evt, ctx):
        """如果底层 dispatch_event 丢了，我们直接处理最关键的回复事件"""
        etype = evt.get('type')
        if etype == 'chat_reply':
            log.info(f"📤 正在通过 Telegram 发送回复...")
            ctx.send_with_budget(evt['chat_id'], evt['content'])
        elif etype == 'typing_start':
            ctx.TG.send_chat_action(evt['chat_id'], 'typing')
        else:
            log.debug(f"ℹ️ 忽略次要事件: {etype}")

    _ctx = types.SimpleNamespace(
        DRIVE_ROOT=data_dir, REPO_DIR=base_dir, TG=TG, WORKERS=WORKERS, PENDING=PENDING, RUNNING=RUNNING,
        MAX_WORKERS=2, send_with_budget=send_with_budget, load_state=load_state, save_state=save_state,
        append_jsonl=append_jsonl
    )
except Exception as e:
    log.error(f"❌ 初始化失败: {e}")
    sys.exit(1)

def smart_chat(cid, txt, img):
    if API_POOL:
        cfg = random.choice(API_POOL)
        os.environ["OPENAI_BASE_URL"], os.environ["OPENAI_API_KEY"] = cfg["url"], cfg["key"]
        log.info(f"🔄 路由切换: {cfg['url']}")
    if handle_chat_direct:
        # 强制增加后缀，诱导模型避开空回复
        handle_chat_direct(cid, txt + "\n(请简短地用中文回复我)", img)

log.info("🚀 Ouroboros 核心逻辑已重组，启动监听...")
spawn_workers(2)
restore_pending_from_snapshot()
offset = int(load_state().get("tg_offset") or 0)

while True:
    try:
        eq = get_event_q()
        while not eq.empty():
            manual_dispatch(eq.get_nowait(), _ctx)
        
        updates = TG.get_updates(offset=offset, timeout=5)
        for upd in updates:
            offset = int(upd["update_id"]) + 1
            msg = upd.get("message") or {}
            if msg.get("text"):
                threading.Thread(target=smart_chat, args=(msg['chat']['id'], msg['text'], None), daemon=True).start()
        
        st = load_state(); st["tg_offset"] = offset; save_state(st)
        time.sleep(0.5)
    except Exception as e:
        log.error(f"⚠️ 运行时异常: {e}")
        time.sleep(1)
