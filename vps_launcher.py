import logging
import os, sys, pathlib, time, threading, types

# --- [1] 环境伪装 ---
mock_colab = types.ModuleType("google.colab")
mock_colab.userdata = types.SimpleNamespace(get=lambda x: os.environ.get(x))
mock_colab.drive = types.SimpleNamespace(mount=lambda x: None)
sys.modules["google.colab"] = mock_colab

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger("OuroborosVPS")
base_dir = pathlib.Path("/opt/ouroboros").resolve()
sys.path.insert(0, str(base_dir))
data_dir = base_dir / "data"

# --- [2] 动态导入 ---
try:
    from supervisor.state import init as s_init, init_state, load_state, save_state, append_jsonl
    from supervisor.telegram import init as t_init, TelegramClient
    from supervisor.workers import init as w_init, spawn_workers, get_event_q
    from supervisor.queue import restore_pending_from_snapshot
    from supervisor.events import dispatch_event
    
    import supervisor.workers as workers_mod
    import supervisor.telegram as tg_mod
    
    handle_chat_direct = getattr(workers_mod, 'handle_chat_direct', getattr(tg_mod, 'handle_chat_direct', None))
    assign_tasks = getattr(workers_mod, 'assign_tasks', None)
    send_with_budget = getattr(tg_mod, 'send_with_budget', None)

    budget_limit = float(os.environ.get("TOTAL_BUDGET", "500000"))
    s_init(data_dir, budget_limit)
    init_state()
    TG = TelegramClient(os.environ.get("TELEGRAM_BOT_TOKEN"))
    t_init(drive_root=data_dir, total_budget_limit=budget_limit, budget_report_every=5, tg_client=TG)
    w_init(repo_dir=base_dir, drive_root=data_dir, max_workers=2, soft_timeout=600, hard_timeout=1800, total_budget_limit=budget_limit)
except Exception as e:
    log.error(f"❌ 启动失败：{e}")
    sys.exit(1)

# --- [3] Discord 桥接集成 ---
discord_bridge = None
discord_thread = None

def start_discord_bridge():
    """启动 Discord 桥接"""
    global discord_bridge, discord_thread
    
    try:
        from ouroboros.channels.discord_bridge import create_bridge
        import asyncio
        
        # 创建 callback：Discord 消息 → Ouroboros 处理 → 返回回复
        async def discord_callback(message_data):
            """处理 Discord 消息，返回回复"""
            content = message_data.get("content", "")
            user_id = message_data.get("user_id", 0)
            username = message_data.get("username", "unknown")
            
            log.info(f"📨 Discord message from {username}: {content[:50]}...")
            
            # 创建一个临时的 chat_id 用于区分 Discord 用户
            # 格式：-100 + user_id（避免与 Telegram chat_id 冲突）
            discord_chat_id = -1000000000 - user_id
            
            # 调用 handle_chat_direct 处理消息
            # 注意：这是同步调用，需要在线程中运行
            response_container = {"response": None, "error": None}
            
            def process_message():
                try:
                    # 创建一个简单的 agent 来处理消息
                    from ouroboros.agent import make_agent
                    agent = make_agent(
                        repo_dir=str(base_dir),
                        drive_root=str(data_dir),
                        event_queue=get_event_q()
                    )
                    
                    # 创建任务
                    task = {
                        "id": f"discord_{user_id}_{int(time.time())}",
                        "type": "task",
                        "chat_id": discord_chat_id,
                        "text": content,
                        "source": "discord",
                        "username": username
                    }
                    
                    # 处理任务并收集响应
                    events = agent.handle_task(task)
                    for evt in events:
                        if evt.get("type") == "response":
                            response_container["response"] = evt.get("content", "")
                            break
                except Exception as e:
                    log.error(f"❌ Error processing Discord message: {e}")
                    response_container["error"] = str(e)
            
            # 在线程中处理（避免阻塞 Discord event loop）
            thread = threading.Thread(target=process_message, daemon=True)
            thread.start()
            thread.join(timeout=60)  # 最多等 60 秒
            
            if response_container["error"]:
                return f"⚠️ 处理出错：{response_container['error']}"
            
            return response_container["response"] or "🍄 收到，正在思考..."
        
        # 创建 bridge
        discord_bridge = create_bridge(callback=discord_callback)
        
        # 在独立线程中运行（asyncio event loop）
        def run_bridge():
            try:
                log.info("🚀 Starting Discord bridge in background thread...")
                discord_bridge.run()
            except Exception as e:
                log.error(f"❌ Discord bridge crashed: {e}")
        
        discord_thread = threading.Thread(target=run_bridge, daemon=True)
        discord_thread.start()
        log.info("✅ Discord bridge started")
        
    except Exception as e:
        log.error(f"⚠️ Failed to start Discord bridge: {e}")
        import traceback
        log.debug(traceback.format_exc())

# 尝试启动 Discord bridge
try:
    start_discord_bridge()
except Exception as e:
    log.warning(f"Discord bridge not started: {e}")

# --- [4] 运行 ---
log.info("🚀 Ouroboros 终极净化版已就绪...")
spawn_workers(2)
restore_pending_from_snapshot()

offset = int(load_state().get("tg_offset") or 0)
while True:
    try:
        if assign_tasks: assign_tasks()
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
        updates = TG.get_updates(offset=offset, timeout=5)
        for upd in updates:
            offset = int(upd["update_id"]) + 1
            msg = upd.get("message") or {}
            if msg.get("text"):
                chat_id = msg['chat']['id']
                # 强制增加一个唤醒提示，防止模型回空
                txt = msg['text'] + " (Please respond in Chinese)"
                threading.Thread(target=handle_chat_direct, args=(chat_id, txt, None), daemon=True).start()
        st = load_state()
        st["tg_offset"] = offset
        save_state(st)
        time.sleep(0.5)
    except Exception as e:
        log.error(f"⚠️ 循环异常：{e}")
        time.sleep(1)
