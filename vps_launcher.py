import logging, os, sys, pathlib, time, threading, types, builtins, typing, random

# --- [0] 类型补丁 ---
for n in ['Any','Dict','List','Optional','Set','Tuple','Union','Iterable','Callable']:
    try: setattr(builtins, n, getattr(typing, n))
    except: pass

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
log = logging.getLogger("OuroborosFinal")
base_dir = pathlib.Path("/opt/ouroboros").resolve()
sys.path.insert(0, str(base_dir))
data_dir = base_dir / "data"

# --- [1] API 资源池解析 ---
raw_pool = os.environ.get("API_POOL", "")
API_POOL = []
if raw_pool:
    for e in raw_pool.split(","):
        if "|" in e:
            u, k = e.split("|", 1)
            API_POOL.append({"url": u.strip(), "key": k.strip()})
log.info(f"✅ 已加载 API 池: {len(API_POOL)} 组配置")

# --- [2] 自动清理 Git 锁 ---
lock_path = base_dir / ".git" / "index.lock"
if lock_path.exists(): lock_path.unlink()

# --- [3] 初始化组件与逻辑闭环 ---
try:
    from supervisor.state import init as s_init, init_state, load_state, save_state, append_jsonl
    from supervisor.telegram import init as t_init, TelegramClient, send_with_budget
    from supervisor.workers import init as w_init, spawn_workers, get_event_q, WORKERS, PENDING, RUNNING, handle_chat_direct
    from supervisor.queue import restore_pending_from_snapshot
    
    # 动态尝试加载 dispatch_event，失败则启用保底逻辑
    dispatch_event_func = None
    try:
        from supervisor.events import dispatch_event
        dispatch_event_func = dispatch_event
    except ImportError:
        log.warning("⚠️ 模块缺失 dispatch_event，启用内置保底发送器")

    def universal_dispatch(evt, ctx):
        etype = evt.get("type")
        # 核心：捕获所有回复事件并强行发往 Telegram
        if etype in ["chat_reply", "task_result", "agent_output"]:
            log.info(f"📩 抓取到回复: {etype}")
            content = evt.get("content") or evt.get("result", {}).get("content")
            cid = evt.get("chat_id") or evt.get("result", {}).get("chat_id")
            if content and cid:
                log.info(f"📤 正在回传 Telegram ({cid})")
                try: ctx.send_with_budget(cid, content)
                except: pass
        elif dispatch_event_func:
            try: dispatch_event_func(evt, ctx)
            except: pass

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    budget = float(os.environ.get("TOTAL_BUDGET", "500000"))
    s_init(data_dir, budget); init_state()
    TG = TelegramClient(token)
    t_init(drive_root=data_dir, total_budget_limit=budget, budget_report_every=5, tg_client=TG)
    w_init(repo_dir=base_dir, drive_root=data_dir, max_workers=2, soft_timeout=600, hard_timeout=1800, total_budget_limit=budget)

    _ctx = types.SimpleNamespace(
        DRIVE_ROOT=data_dir, REPO_DIR=base_dir, TG=TG, WORKERS=WORKERS, PENDING=PENDING, RUNNING=RUNNING,
        MAX_WORKERS=2, send_with_budget=send_with_budget, load_state=load_state, save_state=save_state,
        append_jsonl=append_jsonl
    )
except Exception as e:
    log.error(f"❌ 初始化失败: {e}"); sys.exit(1)

def smart_chat(cid, txt, img):
    if API_POOL:
        cfg = random.choice(API_POOL)
        os.environ["OPENAI_BASE_URL"], os.environ["OPENAI_API_KEY"] = cfg["url"], cfg["key"]
        log.info(f"🔄 路由切换: {cfg['url']}")
    if handle_chat_direct:
        handle_chat_direct(cid, f"{txt}\n(请直接用中文回复，不使用工具)", img)

log.info("🚀 终极版启动，正在监听...")
spawn_workers(2); restore_pending_from_snapshot()
offset = int(load_state().get("tg_offset") or 0)

# --- [4] 核心主循环 (永不截断版) ---
while True:
    try:
        eq = get_event_q()
        while not eq.empty():
            universal_dispatch(eq.get_nowait(), _ctx)
        
        updates = TG.get_updates(offset=offset, timeout=5)
        for upd in updates:
            offset = int(upd["update_id"]) + 1
            msg = upd.get("message") or {}
            if msg.get("text"):
                threading.Thread(target=smart_chat, args=(msg['chat']['id'], msg['text'], None), daemon=True).start()
        
        st = load_state(); st["tg_offset"] = offset; save_state(st)
        time.sleep(0.5)
    except Exception as e:
        log.error(f"⚠️ 运行时异常: {e}"); time.sleep(1)
