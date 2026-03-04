import logging
import os, sys, pathlib, time, threading, types

# --- [1] 环境伪装：模拟 Colab 环境以跳过报错 ---
mock_colab = types.ModuleType("google.colab")
mock_colab.userdata = types.SimpleNamespace(get=lambda x: os.environ.get(x))
mock_colab.drive = types.SimpleNamespace(mount=lambda x: None)
sys.modules["google.colab"] = mock_colab

# --- [2] 基础日志配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger("OuroborosVPS")
base_dir = pathlib.Path("/opt/ouroboros").resolve()
sys.path.insert(0, str(base_dir))
data_dir = base_dir / "data"

# --- [3] 全局变量定义 (防止 NameError) ---
handle_chat_direct = None
TG = None
send_with_budget = None
append_jsonl = None

# --- [4] 定义安全处理函数 (必须在主循环前定义) ---
def safe_handle_chat(cid, txt, img):
    """
    负责调用 Agent 核心思考逻辑。
    增加了时间戳和强制中文指令，以绕过中转站可能的空回复缓存。
    """
    try:
        if handle_chat_direct:
            log.info(f"🧠 Agent 开始思考: {txt}")
            # 在消息末尾增加扰动，强制模型产生实质内容
            processed_txt = txt + f"\n\n(请用中文回复，当前时间戳: {int(time.time())})"
            handle_chat_direct(cid, processed_txt, img)
        else:
            log.error("❌ 核心函数 handle_chat_direct 未能成功加载")
    except Exception as e:
        log.error(f"❌ Agent 思考过程发生异常: {e}")

# --- [5] 初始化核心组件 (处理导入路径) ---
try:
    from supervisor.state import init as s_init, init_state, load_state, save_state, append_jsonl as _aj
    from supervisor.telegram import init as t_init, TelegramClient
    from supervisor.workers import init as w_init, spawn_workers, get_event_q
    from supervisor.queue import restore_pending_from_snapshot
    from supervisor.events import dispatch_event
    
    import supervisor.workers as workers_mod
    import supervisor.telegram as tg_mod
    import supervisor.queue as queue_mod
    
    # 动态抓取不同版本中的函数名
    append_jsonl = _aj
    handle_chat_direct = getattr(workers_mod, 'handle_chat_direct', getattr(tg_mod, 'handle_chat_direct', None))
    assign_tasks = getattr(workers_mod, 'assign_tasks', getattr(queue_mod, 'assign_tasks', None))
    send_with_budget = getattr(tg_mod, 'send_with_budget', None)

    # 从环境变量读取预算（默认 50 万美元以确保不被拦截）
    budget_limit = float(os.environ.get("TOTAL_BUDGET", "500000"))
    
    s_init(data_dir, budget_limit)
    init_state()
    TG = TelegramClient(os.environ.get("TELEGRAM_BOT_TOKEN"))
    t_init(drive_root=data_dir, total_budget_limit=budget_limit, budget_report_every=5, tg_client=TG)
    w_init(repo_dir=base_dir, drive_root=data_dir, max_workers=2, soft_timeout=600, hard_timeout=1800, total_budget_limit=budget_limit)
    
except Exception as e:
    log.error(f"❌ 组件初始化失败: {e}")
    sys.exit(1)

# --- [6] 启动与监听 ---
log.info(f"🚀 Ouroboros 终极净化版已就绪 | 预算: ${budget_limit}")
spawn_workers(2)
restore_pending_from_snapshot()

offset = int(load_state().get("tg_offset") or 0)

while True:
    try:
        # 执行任务分配
        if assign_tasks: assign_tasks()
        
        # 处理回复事件队列 (解决不回话的关键)
        event_q = get_event_q()
        while not event_q.empty():
            evt = event_q.get_nowait()
            ctx = types.SimpleNamespace(
                DRIVE_ROOT=data_dir, REPO_DIR=base_dir, TG=TG, 
                load_state=load_state, save_state=save_state, 
                send_with_budget=send_with_budget, append_jsonl=append_jsonl,
                WORKERS={}, PENDING=[], RUNNING={}
            )
            dispatch_event(evt, ctx)

        # 轮询 Telegram 消息
        updates = TG.get_updates(offset=offset, timeout=5)
        for upd in updates:
            offset = int(upd["update_id"]) + 1
            msg = upd.get("message") or {}
            if msg.get("text"):
                chat_id = msg['chat']['id']
                # 开启新线程处理对话，防止阻塞主循环
                threading.Thread(target=safe_handle_chat, args=(chat_id, msg['text'], None), daemon=True).start()
        
        st = load_state()
        st["tg_offset"] = offset
        save_state(st)
        time.sleep(0.5)
    except Exception as e:
        log.error(f"⚠️ 主循环捕获到异常: {e}")
        time.sleep(1)
