import logging
import os, sys, pathlib, time, threading, types, queue

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
            channel_id = message_data.get("channel_id", 0)
            
            log.info(f"📨 Discord message from {username}: {content[:50]}...")
            
            # Discord 用户 ID 转换为内部 chat_id（负数避免与 Telegram 冲突）
            discord_chat_id = -1000000000 - user_id
            
            # 使用队列同步等待响应
            response_queue = queue.Queue()
            
            def process_message():
                try:
                    from ouroboros.agent import make_agent
                    from ouroboros.utils import utc_now_iso
                    
                    # 创建 agent
                    agent = make_agent(
                        repo_dir=str(base_dir),
                        drive_root=str(data_dir),
                        event_queue=None  # 不发送到主事件队列
                    )
                    
                    # 创建任务
                    task = {
                        "id": f"discord_{user_id}_{int(time.time())}",
                        "type": "task",
                        "chat_id": discord_chat_id,
                        "text": content + " (Please respond in Chinese)",
                        "source": "discord",
                        "username": username,
                        "_is_direct_chat": True,
                    }
                    
                    # 直接调用 agent（不走 event_queue）
                    events = agent.handle_task(task)
                    
                    # 从事件中提取最终响应
                    final_response = None
                    for evt in events:
                        evt_type = evt.get("type", "")
                        if evt_type == "progress":
                            # progress 事件包含中间输出
                            prog_text = evt.get("text", "")
                            if prog_text and not prog_text.startswith("Progress:"):
                                final_response = prog_text
                        elif evt_type == "response":
                            final_response = evt.get("content", "")
                    
                    response_queue.put(final_response or "🍄 消息已处理。")
                    
                except Exception as e:
                    log.error(f"❌ Error processing Discord message: {e}")
                    import traceback
                    log.debug(traceback.format_exc())
                    response_queue.put(f"⚠️ 处理出错：{type(e).__name__}: {e}")
            
            # 在线程中处理
            thread = threading.Thread(target=process_message, daemon=True)
            thread.start()
            thread.join(timeout=120)  # 最多等 2 分钟
            
            try:
                response = response_queue.get_nowait()
                return response
            except queue.Empty:
                return "⏱️ 响应超时，请稍后再试。"
        
        # 创建 bridge
        discord_bridge = create_bridge(callback=discord_callback)
        
        # 在独立线程中运行
        def run_bridge():
            try:
                log.info("🚀 Starting Discord bridge in background thread...")
                discord_bridge.run()
            except Exception as e:
                log.error(f"❌ Discord bridge crashed: {e}")
                import traceback
                log.debug(traceback.format_exc())
        
        discord_thread = threading.Thread(target=run_bridge, daemon=True)
        discord_thread.start()
        log.info("✅ Discord bridge started")
        
    except Exception as e:
        log.warning(f"⚠️ Discord bridge not started: {e}")
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
                txt = msg['text'] + " (Please respond in Chinese)"
                threading.Thread(target=handle_chat_direct, args=(chat_id, txt, None), daemon=True).start()
        st = load_state()
        st["tg_offset"] = offset
        save_state(st)
        time.sleep(0.5)
    except Exception as e:
        log.error(f"⚠️ 循环异常：{e}")
        time.sleep(1)