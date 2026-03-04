import logging
import os, sys, pathlib, time, threading, types

# --- [1] 环境伪装 ---
mock_colab = types.ModuleType("google.colab")
mock_colab.userdata = types.SimpleNamespace(get=lambda x: os.environ.get(x))
sys.modules["google.colab"] = mock_colab

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger("OuroborosVPS")
base_dir = pathlib.Path("/opt/ouroboros").resolve()
sys.path.insert(0, str(base_dir))
data_dir = base_dir / "data"

# --- [2] 动态抓取官方真实对象 (解决不回话的关键) ---
try:
    from supervisor.state import init as s_init, init_state, load_state, save_state, append_jsonl, update_budget_from_usage
    from supervisor.telegram import init as t_init, TelegramClient, send_with_budget
    from supervisor.workers import init as w_init, spawn_workers, get_event_q, WORKERS, PENDING, RUNNING, handle_chat_direct
    from supervisor.queue import restore_pending_from_snapshot, enqueue_task, cancel_task_by_id, queue_review_task, persist_queue_snapshot, sort_pending
    from supervisor.git_ops import safe_restart
    from supervisor.events import dispatch_event
    
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    budget_limit = float(os.environ.get("TOTAL_BUDGET", "500000"))
    
    s_init(data_dir, budget_limit)
    init_state()
    TG = TelegramClient(token)
    t_init(drive_root=data_dir, total_budget_limit=budget_limit, budget_report_every=5, tg_client=TG)
    w_init(repo_dir=base_dir, drive_root=data_dir, max_workers=2, soft_timeout=600, hard_timeout=1800, total_budget_limit=budget_limit)

    # 构造完美上下文环境 (Event Context)
    _event_ctx = types.SimpleNamespace(
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
    sys.exit(1)

# --- [3] 启动循环 ---
log.info("🚀 Ouroboros 生产环境已就绪...")
spawn_workers(2)
restore_pending_from_snapshot()

offset = int(load_state().get("tg_offset") or 0)
while True:
    try:
        # 1. 核心：处理所有的内部事件 (包括发送消息)
        eq = get_event_q()
        while not eq.empty():
            dispatch_event(eq.get_nowait(), _event_ctx)

        # 2. 轮询消息
        updates = TG.get_updates(offset=offset, timeout=5)
        for upd in updates:
            offset = int(upd["update_id"]) + 1
            msg = upd.get("message") or {}
            if msg.get("text"):
                # 使用真实函数处理对话
                threading.Thread(target=handle_chat_direct, args=(msg['chat']['id'], msg['text'], None), daemon=True).start()
        
        st = load_state()
        st["tg_offset"] = offset
        save_state(st)
        time.sleep(0.5)
    except Exception as e:
        log.error(f"⚠️ 运行时异常: {e}")
        time.sleep(1)
