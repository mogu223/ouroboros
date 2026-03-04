import logging
import os, sys, pathlib, time, threading, types, builtins, typing

# --- [0] 暴力注入：解决类型标注错误 ---
for name in ['Any', 'Dict', 'List', 'Optional', 'Set', 'Tuple', 'Union', 'Iterable', 'Callable']:
    try:
        setattr(builtins, name, getattr(typing, name))
    except: pass

# --- [1] 环境伪装 ---
mock_colab = types.ModuleType("google.colab")
mock_colab.userdata = types.SimpleNamespace(get=lambda x: os.environ.get(x))
sys.modules["google.colab"] = mock_colab

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger("OuroborosVPS")
base_dir = pathlib.Path("/opt/ouroboros").resolve()
sys.path.insert(0, str(base_dir))
data_dir = base_dir / "data"

# --- [2] 动态探测导入逻辑 ---
def dynamic_import(module_name, func_name):
    """尝试从多个可能的模块中抓取函数"""
    search_paths = [module_name, 'supervisor.workers', 'supervisor.queue', 'supervisor.events']
    for path in search_paths:
        try:
            mod = __import__(path, fromlist=[func_name])
            func = getattr(mod, func_name, None)
            if func:
                log.info(f"🔍 在 {path} 中找到了 {func_name}")
                return func
        except: continue
    return None

try:
    from supervisor.state import init as s_init, init_state, load_state, save_state, append_jsonl, update_budget_from_usage
    from supervisor.telegram import init as t_init, TelegramClient, send_with_budget
    from supervisor.workers import init as w_init, spawn_workers, get_event_q, WORKERS, PENDING, RUNNING, handle_chat_direct
    from supervisor.queue import restore_pending_from_snapshot, enqueue_task, cancel_task_by_id, queue_review_task, persist_queue_snapshot, sort_pending
    from supervisor.git_ops import safe_restart
    
    # 核心：动态探测 dispatch_event
    dispatch_event = dynamic_import('supervisor.events', 'dispatch_event')
    if not dispatch_event:
        # 如果实在找不到，定义一个空函数防止崩溃
        log.warning("⚠️ 警告：找不到 dispatch_event，已使用空函数替代")
        dispatch_event = lambda evt, ctx: log.debug(f"Dropped event: {evt}")

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    budget_limit = float(os.environ.get("TOTAL_BUDGET", "500000"))
    
    s_init(data_dir, budget_limit)
    init_state()
    TG = TelegramClient(token)
    t_init(drive_root=data_dir, total_budget_limit=budget_limit, budget_report_every=5, tg_client=TG)
    w_init(repo_dir=base_dir, drive_root=data_dir, max_workers=2, soft_timeout=600, hard_timeout=1800, total_budget_limit=budget_limit)

    _ctx = types.SimpleNamespace(
        DRIVE_ROOT=data_dir, REPO_DIR=base_dir, TG=TG,
        WORKERS=WORKERS, PENDING=PENDING, RUNNING=RUNNING,
        MAX_WORKERS=2, send_with_budget=send_with_budget,
        load_state=load_state, save_state=save_state,
        update_budget_from_usage=update_budget_from_usage,
        append_jsonl=append_jsonl, enqueue_task=enqueue_task,
        cancel_task_by_id=cancel_task_by_id, queue_review_task=queue_review_task,
        persist_queue_snapshot=persist_queue_snapshot,
        safe_restart=safe_restart, kill_workers=lambda: None,
        spawn_workers=spawn_workers, sort_pending=sort_pending
    )
except Exception as e:
    log.error(f"❌ 启动加载失败: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# --- [3] 运行主循环 ---
log.info("🚀 Ouroboros 动态适配版已启动")
spawn_workers(2)
restore_pending_from_snapshot()

offset = int(load_state().get("tg_offset") or 0)
while True:
    try:
        eq = get_event_q()
        while not eq.empty():
            dispatch_event(eq.get_nowait(), _ctx)

        updates = TG.get_updates(offset=offset, timeout=5)
        for upd in updates:
            offset = int(upd["update_id"]) + 1
            msg = upd.get("message") or {}
            if msg.get("text"):
                threading.Thread(target=handle_chat_direct, args=(msg['chat']['id'], msg['text'], None), daemon=True).start()
        
        st = load_state()
        st["tg_offset"] = offset
        save_state(st)
        time.sleep(0.5)
    except Exception as e:
        log.error(f"⚠️ 运行时异常: {e}")
        time.sleep(1)
