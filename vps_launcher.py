import logging
import os, sys, pathlib, time, threading, types, builtins, typing, random

# --- [0] 类型补丁：防止 Agent 报错 ---
for name in ['Any', 'Dict', 'List', 'Optional', 'Set', 'Tuple', 'Union', 'Iterable', 'Callable']:
    try: setattr(builtins, name, getattr(typing, name))
    except: pass

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger("OuroborosFinal")
base_dir = pathlib.Path("/opt/ouroboros").resolve()
sys.path.insert(0, str(base_dir))
data_dir = base_dir / "data"

# --- [1] API 资源池深度解析 ---
raw_pool = os.environ.get("API_POOL", "")
API_POOL = []
if raw_pool:
    for entry in raw_pool.split(","):
        if "|" in entry:
            u, k = entry.split("|", 1)
            API_POOL.append({"url": u.strip(), "key": k.strip()})
log.info(f"✅ 已成功加载 API 池: {len(API_POOL)} 组配置")

# --- [2] 强制 Git 锁清理逻辑 ---
lock_path = base_dir / ".git" / "index.lock"
if lock_path.exists():
    lock_path.unlink()

# --- [3] 组件初始化 ---
try:
    from supervisor.state import init as s_init, init_state, load_state, save_state, append_jsonl
    from supervisor.telegram import init as t_init, TelegramClient, send_with_budget
    from supervisor.workers import init as w_init, spawn_workers, get_event_q, WORKERS, PENDING, RUNNING, handle_chat_direct
    from supervisor.queue import restore_pending_from_snapshot
    
    # 动态抓取分发器 (如果找不到就自己接管)
    try:
        from supervisor.events import dispatch_event
    except ImportError:
        def dispatch_event(evt, ctx):
            etype = evt.get('type')
            if etype in ['chat_reply', 'task_result']:
                content = evt.get('content') or evt.get('result', {}).get('content')
                cid = evt.get('chat_id') or evt.get('result', {}).get('chat_id')
                if content and cid:
                    log.info(f"📤 拦截并回传消息: {str(content)[:20]}...")
                    ctx.send_with_budget(cid, content)
    
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    budget_limit = float(os.environ.get("TOTAL_BUDGET", "500000"))
    s_init(data_dir, budget_limit)
    init_state()
    TG = TelegramClient(token)
    t_init(drive_root=data_dir, total_budget_limit=budget_limit, budget_report_every=5, tg_client=TG)
    w_init(repo_dir=base_dir, drive_root=data_dir, max_workers=2, soft_timeout=600, hard_timeout=1800, total_budget_limit=budget_limit)

    _ctx = types.SimpleNamespace(
        DRIVE_ROOT=data_dir, REPO_DIR=base_dir, TG=TG, WORKERS=WORKERS, PENDING=PENDING, RUNNING=RUNNING,
        MAX_WORKERS=2, send_with_budget=send_with_budget, load_state=load_state, save_state=save_state,
        append_jsonl=append_jsonl
    )
except Exception as e:
    log.error(f"❌ 初始化崩溃: {e}")
    sys.exit(1)

def smart_chat(cid, txt, img):
    if API_POOL:
        cfg = random.choice(API_POOL)
        os.environ["OPENAI_BASE_URL"], os.environ["OPENAI_API_KEY"] = cfg["url"], cfg["key"]
        log.info(f"🔄 路由切换: {cfg['url']}")
    if handle_chat_direct:
        # 强制增加后缀，避开损坏的工具
        handle_chat_direct(cid, txt + "\n(请简短地用中文回复我)", img)

log.info("🚀 Ouroboros 终极逻辑已闭环，启动监听...")
spawn_workers(2)
restore_pending_from_snapshot()
offset = int(load_state().get("tg_offset") or 0)

# --- [4] 核心死循环 (之前就是断在这里) ---
while True:
    try:
        # 处理内部队列事件
        eq = get_event_q()
        while not eq.empty():
            dispatch_event(eq.get_nowait(), _ctx)
        
        # 轮询 Telegram 消息
        updates = TG.get_updates(offset=offset, timeout=5)
        for upd in updates:
            offset = int(upd["update_id"]) + 1
            msg = upd.get("message") or {}
            if msg.get("text"):
                threading.Thread(target=smart_chat, args=(msg['chat']['id'], msg['text'], None), daemon=True).start()
        
        st = load_state()
        st["tg_offset"] = offset
        save_state(st)
        time.sleep(0.5)
    except Exception as e:
        log.error(f"⚠️ 运行时异常: {e}")
        time.sleep(1)
