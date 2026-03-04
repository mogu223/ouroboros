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
log.info(f"✅ 已加载 API 池: {len(API_POOL)} 组配置")

# --- [2] 动态寻找失踪的函数 (解决 ImportError) ---
def find_func(func_name):
    # 搜索所有可能的模块
    for mod_name in ['supervisor.events', 'supervisor.workers', 'supervisor.queue', 'supervisor.telegram']:
        try:
            mod = __import__(mod_name, fromlist=[func_name])
            f = getattr(mod, func_name, None)
            if f:
                log.info(f"🔍 找到函数 {func_name} -> {mod_name}")
                return f
        except: continue
    return None

try:
    from supervisor.state import init as s_init, init_state, load_state, save_state, append_jsonl, update_budget_from_usage
    from supervisor.telegram import init as t_init, TelegramClient, send_with_budget
    from supervisor.workers import init as w_init, spawn_workers, get_event_q, WORKERS, PENDING, RUNNING, handle_chat_direct
    from supervisor.queue import restore_pending_from_snapshot, enqueue_task, cancel_task_by_id, queue_review_task, persist_queue_snapshot, sort_pending
    from supervisor.git_ops import safe_restart
    
    # 动态抓取核心分发函数
    dispatch_event = find_func('dispatch_event') or (lambda e, c: log.warning(f"Event dropped: {e}"))
    
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
        update_budget_from_usage=update_budget_from_usage, append_jsonl=append_jsonl, enqueue_task=enqueue_task,
        cancel_task_by_id=cancel_task_by_id, queue_review_task=queue_review_task, persist_queue_snapshot=persist_queue_snapshot,
        safe_restart=safe_restart, kill_workers=lambda: None, spawn_workers=spawn_workers, sort_pending=sort_pending
    )
except Exception as e:
    log.error(f"❌ 初始化失败: {e}")
    sys.exit(1)

def smart_chat(cid, txt, img):
    if API_POOL:
        cfg = random.choice(API_POOL)
        os.environ["OPENAI_BASE_URL"], os.environ["OPENAI_API_KEY"] = cfg["url"], cfg["key"]
        log.info(f"🔄 路由: {cfg['url']}")
    if handle_chat_direct: handle_chat_direct(cid, txt + "\n(Reply in Chinese)", img)

log.info("🚀 Ouroboros 生产环境已就绪...")
spawn_workers(2)
restore_pending_from_snapshot()
offset = int(load_state().get("tg_offset") or 0)

while True:
    try:
        eq = get_event_q()
        while not eq.empty(): dispatch_event(eq.get_nowait(), _ctx)
        updates = TG.get_updates(offset=offset, timeout=5)
        for upd in updates:
            offset = int(upd["update_id"]) + 1
            msg = upd.get("message") or {}
            if msg.get("text"):
                threading.Thread(target=smart_chat, args=(msg['chat']['id'], msg['text'], None), daemon=True).start()
        st = load_state(); st["tg_offset"] = offset; save_state(st)
        time.sleep(0.5)
    except Exception as e:
        log.error(f"⚠️ 异常: {e}"); time.sleep(1)
