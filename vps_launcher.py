import logging, os, sys, pathlib, time, threading, types, builtins, typing, random

# --- [0] 解决 Agent 进化产生的类型定义问题 ---
for n in ['Any','Dict','List','Optional','Set','Tuple','Union','Iterable','Callable']:
    try: setattr(builtins, n, getattr(typing, n))
    except: pass

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
log = logging.getLogger("Ouroboros")
base_dir = pathlib.Path("/opt/ouroboros").resolve()
sys.path.insert(0, str(base_dir))
data_dir = base_dir / "data"

# --- [1] 解析 API 池 ---
raw_pool = os.environ.get("API_POOL", "")
API_POOL = []
if raw_pool:
    for e in raw_pool.split(","):
        if "|" in e:
            u, k = e.split("|", 1)
            API_POOL.append({"url": u.strip(), "key": k.strip()})

try:
    from supervisor.state import init as s_init, init_state, load_state, save_state, append_jsonl
    from supervisor.telegram import init as t_init, TelegramClient, send_with_budget
    from supervisor.workers import init as w_init, spawn_workers, get_event_q, WORKERS, PENDING, RUNNING, handle_chat_direct
    from supervisor.queue import restore_pending_from_snapshot

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    budget = float(os.environ.get("TOTAL_BUDGET", "500000"))

    # --- [关键：必须先初始化 TG 实例] ---
    s_init(data_dir, budget)
    init_state()
    TG = TelegramClient(token)
    
    # --- [关键：手动定义分发器，绕过丢失的 events 模块] ---
    def manual_dispatch(evt, ctx):
        etype = evt.get("type")
        if etype in ["chat_reply", "task_result"]:
            content = evt.get("content") or evt.get("result", {}).get("content")
            cid = evt.get("chat_id") or evt.get("result", {}).get("chat_id")
            if content and cid:
                log.info("📤 正在发送回 Telegram...")
                try: ctx.send_with_budget(cid, content)
                except Exception as e: log.error("❌ 发送异常: " + str(e))
        elif etype == "typing_start":
            try: ctx.TG.send_chat_action(evt.get("chat_id"), "typing")
            except: pass

    # --- [定义上下文，现在 TG 已经存在，不会再报 NameError] ---
    _ctx = types.SimpleNamespace(
        DRIVE_ROOT=data_dir, REPO_DIR=base_dir, TG=TG, WORKERS=WORKERS, 
        PENDING=PENDING, RUNNING=RUNNING, MAX_WORKERS=2, 
        send_with_budget=send_with_budget, load_state=load_state, 
        save_state=save_state, append_jsonl=append_jsonl
    )

    t_init(drive_root=data_dir, total_budget_limit=budget, budget_report_every=5, tg_client=TG)
    w_init(repo_dir=base_dir, drive_root=data_dir, max_workers=2, soft_timeout=600, hard_timeout=1800, total_budget_limit=budget)

    def smart_chat(cid, txt, img):
        if API_POOL:
            cfg = random.choice(API_POOL)
            os.environ["OPENAI_BASE_URL"] = cfg["url"]
            os.environ["OPENAI_API_KEY"] = cfg["key"]
            log.info("🔄 节点: " + cfg["url"])
        if handle_chat_direct:
            handle_chat_direct(cid, txt + "\n(请直接用中文回复，不要用工具)", img)

    log.info("🚀 系统就绪，开始监听消息...")
    spawn_workers(2)
    restore_pending_from_snapshot()
    offset = int(load_state().get("tg_offset") or 0)

    # --- [死循环：保证消息必达] ---
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
            
            st = load_state()
            st["tg_offset"] = offset
            save_state(st)
            time.sleep(0.5)
        except Exception as ee:
            log.error("⚠️ 循环内异常: " + str(ee))
            time.sleep(1)

except Exception as e:
    log.error("❌ 严重启动失败: " + str(e))
    sys.exit(1)
