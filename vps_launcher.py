from __future__ import annotations

import datetime
import logging
import os
import pathlib
import queue
import subprocess
import sys
import time
import types
import uuid
from typing import Any, Dict, Optional
from urllib.parse import quote

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
log = logging.getLogger('Ouroboros')


def _safe_int_env(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, str(default)) or default).strip())
    except Exception:
        return int(default)


def _safe_float_env(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name, str(default)) or default).strip())
    except Exception:
        return float(default)


REPO_DIR = pathlib.Path(os.environ.get('OUROBOROS_REPO_DIR', '/opt/ouroboros')).resolve()
DRIVE_ROOT = pathlib.Path(os.environ.get('OUROBOROS_DRIVE_ROOT', '/var/lib/ouroboros')).resolve()

os.environ.setdefault('OUROBOROS_REPO_DIR', str(REPO_DIR))
os.environ.setdefault('OUROBOROS_DRIVE_ROOT', str(DRIVE_ROOT))
os.environ.setdefault('OUROBOROS_LAUNCHER_FILE', 'vps_launcher.py')

sys.path.insert(0, str(REPO_DIR))

from supervisor import state as sup_state
from supervisor import queue as sup_queue
from supervisor import workers as sup_workers
from supervisor import git_ops as sup_git
from supervisor.state import load_state, save_state, append_jsonl
from supervisor.telegram import TelegramClient, init as telegram_init, send_with_budget
from supervisor.events import dispatch_event
from supervisor.queue import _queue_lock
from supervisor.launcher_discord import init_discord_bridge
from ouroboros.consciousness import BackgroundConsciousness

TELEGRAM_TOKEN = str(os.environ.get('TELEGRAM_BOT_TOKEN', '') or '').strip()
if not TELEGRAM_TOKEN:
    raise RuntimeError('Missing TELEGRAM_BOT_TOKEN')

TOTAL_BUDGET = _safe_float_env('TOTAL_BUDGET', 0.0)
MAX_WORKERS = max(1, _safe_int_env('OUROBOROS_MAX_WORKERS', 3))
POLL_TIMEOUT = max(3, _safe_int_env('OUROBOROS_TELEGRAM_POLL_TIMEOUT', _safe_int_env('TELEGRAM_GETUPDATES_TIMEOUT', 15)))
MAIN_LOOP_SLEEP = max(0.05, _safe_float_env('OUROBOROS_MAIN_LOOP_SLEEP_SEC', 0.2))
SOFT_TIMEOUT_SEC = max(60, _safe_int_env('OUROBOROS_TASK_SOFT_TIMEOUT_SEC', 900))
HARD_TIMEOUT_SEC = max(SOFT_TIMEOUT_SEC + 60, _safe_int_env('OUROBOROS_TASK_HARD_TIMEOUT_SEC', 2400))

# Evolution task settings
# Shorter timeout for evolution tasks to prevent hanging (30 seconds wall-time)
EVOLUTION_WALL_TIME_SEC = max(30, _safe_int_env('OUROBOROS_EVOLUTION_WALL_TIME_SEC', 45))
# Idle time threshold before triggering evolution (seconds of empty queue)
IDLE_TIME_BEFORE_EVOLUTION_SEC = max(10, _safe_int_env('OUROBOROS_IDLE_TIME_BEFORE_EVOLUTION_SEC', 30))
# Enable idle-time evolution (auto-spawn small evolution tasks when idle)
IDLE_EVOLUTION_ENABLED = _safe_int_env('OUROBOROS_IDLE_EVOLUTION_ENABLED', 1) == 1

BRANCH_DEV = str(os.environ.get('OUROBOROS_BRANCH_DEV', 'ouroboros') or 'ouroboros').strip()
BRANCH_STABLE = str(os.environ.get('OUROBOROS_BRANCH_STABLE', 'ouroboros-stable') or 'ouroboros-stable').strip()


def _build_remote_url() -> str:
    explicit = str(os.environ.get('OUROBOROS_REMOTE_URL', '') or '').strip()
    if explicit:
        return explicit

    gh_user = str(os.environ.get('GITHUB_USER', '') or '').strip()
    gh_repo = str(os.environ.get('GITHUB_REPO', '') or '').strip()
    gh_token = str(os.environ.get('GITHUB_TOKEN', '') or '').strip()

    if gh_user and gh_repo and gh_token:
        token_q = quote(gh_token, safe='')
        return f'https://{token_q}:x-oauth-basic@github.com/{gh_user}/{gh_repo}.git'
    if gh_user and gh_repo:
        return f'https://github.com/{gh_user}/{gh_repo}.git'
    return ''


def _init_paths() -> None:
    for sub in ('state', 'logs', 'memory', 'index', 'locks', 'archive', 'task_results'):
        (DRIVE_ROOT / sub).mkdir(parents=True, exist_ok=True)


def _init_state_module() -> None:
    sup_state.DRIVE_ROOT = DRIVE_ROOT
    sup_state.STATE_PATH = DRIVE_ROOT / 'state' / 'state.json'
    sup_state.QUEUE_SNAPSHOT_PATH = DRIVE_ROOT / 'state' / 'queue_snapshot.json'
    sup_state.TOTAL_BUDGET_LIMIT = float(TOTAL_BUDGET)
    sup_state.EVOLUTION_BUDGET_RESERVE = max(0.0, _safe_float_env('OUROBOROS_EVOLUTION_BUDGET_RESERVE', 5.0))

    state_obj = load_state()
    save_state(state_obj)


def _update_budget_from_usage(usage: Dict[str, Any]) -> None:
    if not isinstance(usage, dict):
        return
    st = load_state()
    st['spent_calls'] = int(st.get('spent_calls') or 0) + 1
    st['spent_tokens_prompt'] = int(st.get('spent_tokens_prompt') or 0) + int(usage.get('prompt_tokens') or 0)
    st['spent_tokens_completion'] = int(st.get('spent_tokens_completion') or 0) + int(usage.get('completion_tokens') or 0)
    st['spent_tokens_cached'] = int(st.get('spent_tokens_cached') or 0) + int(usage.get('cached_tokens') or 0)
    st['spent_usd'] = float(st.get('spent_usd') or 0.0) + float(usage.get('cost') or 0.0)
    save_state(st)


def _refresh_current_sha() -> None:
    try:
        result = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=str(REPO_DIR),
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        sha = str(result.stdout or '').strip()
        if not sha:
            return
        st = load_state()
        st['current_sha'] = sha
        save_state(st)
    except Exception:
        log.warning('Failed to refresh current_sha from local git HEAD', exc_info=True)


def _owner_chat_id() -> Optional[int]:
    st = load_state()
    owner = st.get('owner_chat_id')
    if owner is None:
        return None
    try:
        return int(owner)
    except Exception:
        return None


def _ensure_owner(chat_id: int, tg: TelegramClient) -> bool:
    st = load_state()
    owner_chat = st.get('owner_chat_id')

    if owner_chat is None:
        st['owner_chat_id'] = int(chat_id)
        st['owner_id'] = int(chat_id)
        st['last_owner_message_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save_state(st)
        send_with_budget(int(chat_id), '✅ Owner registered. Ouroboros is online.')
        return True

    try:
        owner_chat_int = int(owner_chat)
    except Exception:
        owner_chat_int = int(chat_id)

    if int(chat_id) != owner_chat_int:
        tg.send_message(chat_id, '⛔ Unauthorized chat. This bot is owner-locked.')
        return False

    st['last_owner_message_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save_state(st)
    return True


def _set_evolution_mode(enabled: bool) -> None:
    st = load_state()
    st['evolution_mode_enabled'] = bool(enabled)
    save_state(st)

    if not enabled:
        with _queue_lock:
            sup_workers.PENDING[:] = [t for t in sup_workers.PENDING if str(t.get('type') or '') != 'evolution']
            sup_queue.sort_pending()
            sup_queue.persist_queue_snapshot(reason='evolve_off_fast_cmd')


def _handle_fast_command(chat_id: int, text: str) -> bool:
    cmd = str(text or '').strip().lower()

    if cmd == '/status':
        st = load_state()
        worker_total = len(sup_workers.WORKERS)
        worker_alive = sum(1 for w in sup_workers.WORKERS.values() if w.proc.is_alive())
        pending = len(sup_workers.PENDING)
        running = len(sup_workers.RUNNING)
        remaining = sup_state.budget_remaining(st)
        budget_str = 'unlimited' if remaining == float('inf') else f'${remaining:.2f}'
        msg = (
            f'🟢 service=online\n'
            f'workers={worker_alive}/{worker_total}\n'
            f'pending={pending}, running={running}\n'
            f'budget_remaining={budget_str}\n'
            f'evolution={"on" if bool(st.get("evolution_mode_enabled")) else "off"}'
        )
        send_with_budget(chat_id, msg)
        return True

    if cmd in ('/evolve off', '/evolve stop'):
        _set_evolution_mode(False)
        send_with_budget(chat_id, '🧬 Evolution: OFF')
        return True

    if cmd in ('/evolve on', '/evolve start'):
        _set_evolution_mode(True)
        send_with_budget(chat_id, '🧬 Evolution: ON')
        return True

    return False


def _extract_message(update: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    msg = update.get('message')
    if isinstance(msg, dict):
        return msg
    msg = update.get('edited_message')
    if isinstance(msg, dict):
        return msg
    return None


def _build_task_from_update(update: Dict[str, Any], tg: TelegramClient) -> Optional[Dict[str, Any]]:
    msg = _extract_message(update)
    if not msg:
        return None

    chat = msg.get('chat') or {}
    chat_id_raw = chat.get('id')
    try:
        chat_id = int(chat_id_raw)
    except Exception:
        return None

    text = str(msg.get('text') or '').strip()

    image_b64 = None
    image_mime = ''
    caption = str(msg.get('caption') or '').strip()

    photos = msg.get('photo')
    if isinstance(photos, list) and photos:
        file_id = str((photos[-1] or {}).get('file_id') or '').strip()
        if file_id:
            b64, mime = tg.download_file_base64(file_id)
            if b64:
                image_b64 = b64
                image_mime = mime or 'image/jpeg'
                if not text:
                    text = caption
                if not text:
                    text = '(image attached)'

    if not text:
        return None

    task: Dict[str, Any] = {
        'id': uuid.uuid4().hex[:8],
        'type': 'task',
        'chat_id': int(chat_id),
        'text': text,
        '_is_direct_chat': True,
    }

    if image_b64:
        task['image_base64'] = image_b64
        task['image_mime'] = image_mime
        if caption:
            task['image_caption'] = caption

    return task


def _preempt_background_task_for_owner_message() -> None:
    if not sup_workers.WORKERS:
        return
    if len(sup_workers.RUNNING) < len(sup_workers.WORKERS):
        return

    for running_task_id, meta in list(sup_workers.RUNNING.items()):
        running_task = meta.get('task') if isinstance(meta, dict) else {}
        task_type = str((running_task or {}).get('type') or '').strip().lower()
        task_text = str((running_task or {}).get('text') or '').strip().upper()

        is_background_like = (
            task_type in ('evolution', 'review')
            or task_text.startswith('EVOLUTION #')
            or task_text.startswith('REVIEW:')
        )
        if not is_background_like:
            continue

        if sup_queue.cancel_task_by_id(str(running_task_id)):
            append_jsonl(
                DRIVE_ROOT / 'logs' / 'supervisor.jsonl',
                {
                    'ts': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    'type': 'owner_message_preempt_background',
                    'task_id': str(running_task_id),
                    'task_type': task_type,
                },
            )
            return


def _make_context(tg: TelegramClient, consciousness: BackgroundConsciousness) -> Any:
    return types.SimpleNamespace(
        DRIVE_ROOT=DRIVE_ROOT,
        REPO_DIR=REPO_DIR,
        BRANCH_DEV=BRANCH_DEV,
        BRANCH_STABLE=BRANCH_STABLE,
        TG=tg,
        RUNNING=sup_workers.RUNNING,
        WORKERS=sup_workers.WORKERS,
        PENDING=sup_workers.PENDING,
        consciousness=consciousness,
        send_with_budget=send_with_budget,
        load_state=load_state,
        save_state=save_state,
        append_jsonl=append_jsonl,
        update_budget_from_usage=_update_budget_from_usage,
        safe_restart=sup_git.safe_restart,
        kill_workers=sup_workers.kill_workers,
        persist_queue_snapshot=sup_queue.persist_queue_snapshot,
        queue_review_task=sup_queue.queue_review_task,
        enqueue_task=sup_queue.enqueue_task,
        cancel_task_by_id=sup_queue.cancel_task_by_id,
        sort_pending=sup_queue.sort_pending,
    )


def main() -> None:
    _init_paths()
    _init_state_module()

    remote_url = _build_remote_url()

    sup_queue.init(DRIVE_ROOT, SOFT_TIMEOUT_SEC, HARD_TIMEOUT_SEC)
    sup_workers.init(
        repo_dir=REPO_DIR,
        drive_root=DRIVE_ROOT,
        max_workers=MAX_WORKERS,
        soft_timeout=SOFT_TIMEOUT_SEC,
        hard_timeout=HARD_TIMEOUT_SEC,
        total_budget_limit=TOTAL_BUDGET,
        branch_dev=BRANCH_DEV,
        branch_stable=BRANCH_STABLE,
    )
    sup_git.init(
        repo_dir=REPO_DIR,
        drive_root=DRIVE_ROOT,
        remote_url=remote_url,
        branch_dev=BRANCH_DEV,
        branch_stable=BRANCH_STABLE,
    )
    _refresh_current_sha()

    tg = TelegramClient(TELEGRAM_TOKEN)
    telegram_init(
        drive_root=DRIVE_ROOT,
        total_budget_limit=TOTAL_BUDGET,
        budget_report_every=max(1, _safe_int_env('OUROBOROS_BUDGET_REPORT_EVERY_MESSAGES', 10)),
        tg_client=tg,
    )

    sup_workers.spawn_workers(MAX_WORKERS)
    event_q = sup_workers.get_event_q()
    consciousness = BackgroundConsciousness(
        drive_root=DRIVE_ROOT,
        repo_dir=REPO_DIR,
        event_queue=event_q,
        owner_chat_id_fn=_owner_chat_id,
    )
    ctx = _make_context(tg, consciousness)
    restored = sup_queue.restore_pending_from_snapshot()

    # Initialize Discord Bridge
    discord_enabled = init_discord_bridge(REPO_DIR, DRIVE_ROOT)

    st = load_state()
    offset = int(st.get('tg_offset') or 0)

    append_jsonl(
        DRIVE_ROOT / 'logs' / 'supervisor.jsonl',
        {
            'ts': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'type': 'launcher_start',
            'launcher': 'vps_launcher.py',
            'repo_dir': str(REPO_DIR),
            'drive_root': str(DRIVE_ROOT),
            'max_workers': MAX_WORKERS,
            'poll_timeout': POLL_TIMEOUT,
            'restored_pending': restored,
            'discord_enabled': discord_enabled,
        },
    )

    log.info('🚀 VPS launcher started: workers=%d, poll_timeout=%ds, discord=%s', MAX_WORKERS, POLL_TIMEOUT, 'enabled' if discord_enabled else 'disabled')
    last_heartbeat = 0.0
    idle_start_time = None  # Track when queue became idle
    last_evolution_trigger_time = 0.0  # Track last evolution trigger

    while True:
        try:
            drained = 0
            while drained < 256:
                try:
                    evt = event_q.get_nowait()
                except queue.Empty:
                    break
                dispatch_event(evt, ctx)
                drained += 1

            updates = tg.get_updates(offset=offset, timeout=POLL_TIMEOUT)
            for upd in updates:
                try:
                    update_id = int(upd.get('update_id') or 0)
                except Exception:
                    continue
                if update_id >= offset:
                    offset = update_id + 1

                task = _build_task_from_update(upd, tg)
                if not task:
                    continue

                chat_id = int(task['chat_id'])
                if not _ensure_owner(chat_id, tg):
                    continue

                if _handle_fast_command(chat_id, str(task.get('text') or '')):
                    continue

                task['priority'] = -10
                _preempt_background_task_for_owner_message()
                sup_queue.enqueue_task(task, front=True)
                sup_queue.persist_queue_snapshot(reason='owner_message_enqueued_front')

            st = load_state()
            st['tg_offset'] = int(offset)
            save_state(st)

            if not sup_workers.WORKERS:
                sup_workers.spawn_workers(MAX_WORKERS)

            # Drive worker scheduling: keep pool healthy and dispatch queued tasks
            sup_workers.ensure_workers_healthy()
            sup_queue.enforce_task_timeouts()
            sup_workers.assign_tasks()

            # Idle-time evolution: trigger small evolution tasks when system is idle
            now = time.time()
            is_queue_empty = (not sup_workers.PENDING) and (not sup_workers.RUNNING)

            if is_queue_empty:
                if idle_start_time is None:
                    idle_start_time = now
                elif IDLE_EVOLUTION_ENABLED:
                    idle_duration = now - idle_start_time
                    # Trigger evolution if idle for enough time and not recently triggered
                    if (idle_duration >= IDLE_TIME_BEFORE_EVOLUTION_SEC and
                        now - last_evolution_trigger_time >= 300):  # At least 5 min between evolution
                        sup_queue.enqueue_evolution_task_if_needed(idle_check=True)
                        last_evolution_trigger_time = now
                        idle_start_time = None  # Reset idle timer after triggering
            else:
                idle_start_time = None  # Reset idle timer when queue has tasks

            if now - last_heartbeat >= 30:
                consciousness.heartbeat()
                last_heartbeat = now

            time.sleep(MAIN_LOOP_SLEEP)

        except KeyboardInterrupt:
            log.info('Shutting down...')
            break
        except Exception as e:
            log.exception('Main loop error: %s', e)
            time.sleep(1)


if __name__ == '__main__':
    main()
