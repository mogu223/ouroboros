"""
Ouroboros agent core — thin orchestrator.

Delegates to: loop.py (LLM tool loop), tools/ (tool schemas/execution),
llm.py (LLM calls), memory.py (scratchpad/identity),
context.py (context building), review.py (code collection/metrics).
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import queue
import threading
import time
import traceback
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

from ouroboros.utils import (
    utc_now_iso, read_text, append_jsonl,
    safe_relpath, truncate_for_log,
    get_git_info, sanitize_task_for_event,
)
from ouroboros.llm import LLMClient, add_usage
from ouroboros.tools import ToolRegistry
from ouroboros.tools.registry import ToolContext
from ouroboros.memory import Memory
from ouroboros.context import build_llm_messages
import asyncio
from ouroboros.loop import run_tool_loop


def run_llm_tool_loop(
    llm,
    tools,
    drive_logs,
    task_id,
    messages,
    active_model,
    active_effort,
    *,
    max_iterations=200,
    max_wall_time_sec=0,
    max_retries=8,
    budget_remaining_usd=None,
    event_queue=None,
    task_type='task',
    incoming_messages_queue=None,
    send_progress_message=None,
):
    _ = incoming_messages_queue
    emit_progress = send_progress_message or (lambda _text: None)
    return asyncio.run(
        run_tool_loop(
            llm=llm,
            tools=tools,
            messages=messages,
            drive_logs=drive_logs,
            task_id=str(task_id),
            emit_progress=emit_progress,
            active_model=str(active_model or ''),
            active_effort=str(active_effort or 'medium'),
            budget_remaining_usd=budget_remaining_usd,
            max_iterations=max(1, int(max_iterations)),
            max_wall_time_sec=max(0.0, float(max_wall_time_sec or 0)),
            max_retries=int(max_retries),
            task_type=str(task_type or 'task'),
            event_queue=event_queue,
        )
    )



# ---------------------------------------------------------------------------
# Module-level guard for one-time worker boot logging
# ---------------------------------------------------------------------------
_worker_boot_logged = False
_worker_boot_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Environment + Paths
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Env:
    repo_dir: pathlib.Path
    drive_root: pathlib.Path
    branch_dev: str = "ouroboros"

    def repo_path(self, rel: str) -> pathlib.Path:
        return (self.repo_dir / safe_relpath(rel)).resolve()

    def drive_path(self, rel: str) -> pathlib.Path:
        return (self.drive_root / safe_relpath(rel)).resolve()


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class OuroborosAgent:
    """One agent instance per worker process. Mostly stateless; long-term state lives on Drive."""

    def __init__(self, env: Env, event_queue: Any = None):
        self.env = env
        self._pending_events: List[Dict[str, Any]] = []
        self._event_queue: Any = event_queue
        self._current_chat_id: Optional[int] = None
        self._current_task_type: Optional[str] = None

        # Message injection: owner can send messages while agent is busy
        self._incoming_messages: queue.Queue = queue.Queue()
        self._busy = False
        self._last_progress_ts: float = 0.0
        self._task_started_ts: float = 0.0

        # SSOT modules
        self.llm = LLMClient()
        self.tools = ToolRegistry(repo_dir=env.repo_dir, drive_root=env.drive_root)
        self.memory = Memory(drive_root=env.drive_root, repo_dir=env.repo_dir)

        self._log_worker_boot_once()

    def inject_message(self, text: str) -> None:
        """Thread-safe: inject owner message into the active conversation."""
        self._incoming_messages.put(text)

    def _log_worker_boot_once(self) -> None:
        global _worker_boot_logged
        try:
            with _worker_boot_lock:
                if _worker_boot_logged:
                    return
                _worker_boot_logged = True
            git_branch, git_sha = get_git_info(self.env.repo_dir)
            append_jsonl(self.env.drive_path('logs') / 'events.jsonl', {
                'ts': utc_now_iso(), 'type': 'worker_boot',
                'pid': os.getpid(), 'git_branch': git_branch, 'git_sha': git_sha,
            })
            self._verify_restart(git_sha)
            self._verify_system_state(git_sha)
        except Exception:
            log.warning("Worker boot logging failed", exc_info=True)
            return

    def _verify_restart(self, git_sha: str) -> None:
        """Best-effort restart verification."""
        try:
            pending_path = self.env.drive_path('state') / 'pending_restart_verify.json'
            claim_path = pending_path.with_name(f"pending_restart_verify.claimed.{os.getpid()}.json")
            try:
                os.rename(str(pending_path), str(claim_path))
            except (FileNotFoundError, Exception):
                return
            try:
                claim_data = json.loads(read_text(claim_path))
                expected_sha = str(claim_data.get("expected_sha", "")).strip()
                ok = bool(expected_sha and expected_sha == git_sha)
                append_jsonl(self.env.drive_path('logs') / 'events.jsonl', {
                    'ts': utc_now_iso(), 'type': 'restart_verify',
                    'pid': os.getpid(), 'ok': ok,
                    'expected_sha': expected_sha, 'observed_sha': git_sha,
                })
            except Exception:
                log.debug("Failed to log restart verify event", exc_info=True)
                pass
            try:
                claim_path.unlink()
            except Exception:
                log.debug("Failed to delete restart verify claim file", exc_info=True)
                pass
        except Exception:
            log.debug("Restart verification failed", exc_info=True)
            pass

    def _check_uncommitted_changes(self) -> Tuple[dict, int]:
        """Check for uncommitted changes and attempt auto-rescue commit & push."""
        import re
        import subprocess
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(self.env.repo_dir),
                capture_output=True, text=True, timeout=10, check=True
            )
            dirty_files = [l.strip() for l in result.stdout.strip().split('\\n') if l.strip()]
            if dirty_files:
                # Auto-rescue: commit and push
                auto_committed = False
                try:
                    # Only stage tracked files (not secrets/notebooks)
                    subprocess.run(["git", "add", "-u"], cwd=str(self.env.repo_dir), timeout=10, check=True)
                    subprocess.run(
                        ["git", "commit", "-m", "auto-rescue: uncommitted changes detected on startup"],
                        cwd=str(self.env.repo_dir), timeout=30, check=True
                    )
                    # Validate branch name
                    if not re.match(r'^[a-zA-Z0-9_/-]+$', self.env.branch_dev):\
                        raise ValueError(f"Invalid branch name: {self.env.branch_dev}")
                    # Pull with rebase before push
                    subprocess.run(
                        ["git", "pull", "--rebase", "origin", self.env.branch_dev],
                        cwd=str(self.env.repo_dir), timeout=60, check=True
                    )
                    # Push
                    try:
                        subprocess.run(
                            ["git", "push", "origin", self.env.branch_dev],
                            cwd=str(self.env.repo_dir), timeout=60, check=True
                        )
                        auto_committed = True
                        log.warning(f"Auto-rescued {len(dirty_files)} uncommitted files on startup")
                    except subprocess.CalledProcessError:
                        # If push fails, undo the commit
                        subprocess.run(
                            ["git", "reset", "HEAD~1"],
                            cwd=str(self.env.repo_dir), timeout=10, check=True
                        )
                        raise
                except Exception as e:
                    log.warning(f"Failed to auto-rescue uncommitted changes: {e}", exc_info=True)
                return {
                    "status": "warning", "files": dirty_files[:20],
                    "auto_committed": auto_committed,
                }, 1
            else:
                return {"status": "ok"}, 0
        except Exception as e:
            return {"status": "error", "error": str(e)}, 0

    def _check_version_sync(self) -> Tuple[dict, int]:
        """Check VERSION file sync with git tags and pyproject.toml."""
        import subprocess
        import re
        try:
            version_file = read_text(self.env.repo_path("VERSION")).strip()
            issue_count = 0
            result_data = {"version_file": version_file}

            # Check pyproject.toml version
            pyproject_path = self.env.repo_path("pyproject.toml")
            pyproject_content = read_text(pyproject_path)
            # 修正正则表达式中的转义和括号匹配问题
            match = re.search(r"^version\s*=\s*['\"]([^'\"]+)['\"]", pyproject_content, re.MULTILINE) # <--- 修正点: 修正正则表达式
            if match:
                pyproject_version = match.group(1)
                result_data["pyproject_version"] = pyproject_version
                if version_file != pyproject_version:
                    result_data["status"] = "warning"
                    issue_count += 1

            # Check README.md version (Bible P7: VERSION == README version)
            try:
                readme_content = read_text(self.env.repo_path("README.md"))
                readme_match = re.search(r'\\*\\*Version:\\*\\*\\s*(\\d+\\.\\d+\\.\\d+)', readme_content)
                if readme_match:
                    readme_version = readme_match.group(1)
                    result_data["readme_version"] = readme_version
                    if version_file != readme_version:
                        result_data["status"] = "warning"
                        issue_count += 1
            except Exception:
                log.debug("Failed to check README.md version", exc_info=True)

            # Check git tags
            result = subprocess.run(
                ["git", "describe", "--tags", "--abbrev=0"],
                cwd=str(self.env.repo_dir),
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                result_data["status"] = "warning"
                result_data["message"] = "no_tags"
                return result_data, issue_count
            else:
                latest_tag = result.stdout.strip().lstrip('v')
                result_data["latest_tag"] = latest_tag
                if version_file != latest_tag:
                    result_data["status"] = "warning"
                    issue_count += 1

            if issue_count == 0:
                result_data["status"] = "ok"

            return result_data, issue_count
        except Exception as e:
            return {"status": "error", "error": str(e)}, 0

    def _check_budget(self) -> Tuple[dict, int]:
        """Check budget remaining with warning thresholds."""
        try:
            state_path = self.env.drive_path("state") / "state.json"
            state_data = json.loads(read_text(state_path))
            total_budget_str = os.environ.get("TOTAL_BUDGET", "")

            # Handle unset or zero budget gracefully
            if not total_budget_str or float(total_budget_str) == 0:
                return {"status": "unconfigured"}, 0
            else:
                total_budget = float(total_budget_str)
                spent = float(state_data.get("spent_usd", 0)) # <--- 修正点: 删除多余的反斜杠
                remaining = max(0, total_budget - spent)

                if remaining < 10:
                    status = "emergency"
                    issues = 1
                elif remaining < 50:
                    status = "critical"
                    issues = 1
                elif remaining < 100:
                    status = "warning"
                    issues = 0
                else:
                    status = "ok"
                    issues = 0

                return {
                    "status": status,
                    "remaining_usd": round(remaining, 2),
                    "total_usd": total_budget,
                    "spent_usd": round(spent, 2),
                }, issues
        except Exception as e:
            return {"status": "error", "error": str(e)}, 0

    def _verify_system_state(self, git_sha: str) -> None:
        """Bible Principle 1: verify system state on every startup.

        Checks:
        - Uncommitted changes (auto-rescue commit & push)
        - VERSION file sync with git tags
        - Budget remaining (warning thresholds)
        """
        checks = {}
        issues = 0
        drive_logs = self.env.drive_path("logs")

        # 1. Uncommitted changes
        checks["uncommitted_changes"], issue_count = self._check_uncommitted_changes()
        issues += issue_count

        # 2. VERSION vs git tag
        checks["version_sync"], issue_count = self._check_version_sync()
        issues += issue_count

        # 3. Budget check
        checks["budget"], issue_count = self._check_budget()
        issues += issue_count

        # Log verification result
        event = {
            "ts": utc_now_iso(),
            "type": "startup_verification",
            "checks": checks,
            "issues_count": issues,
            "git_sha": git_sha,
        }
        append_jsonl(drive_logs / "events.jsonl", event)

        if issues > 0:
            log.warning(f"Startup verification found {issues} issue(s): {checks}")

    # =====================================================================
    # Main entry point
    # =====================================================================

    def _prepare_task_context(self, task: Dict[str, Any]) -> Tuple[ToolContext, List[Dict[str, Any]], Dict[str, Any]]:
        """Set up ToolContext, build messages, return (ctx, messages, cap_info)."""
        drive_logs = self.env.drive_path("logs")
        sanitized_task = sanitize_task_for_event(task, drive_logs)
        append_jsonl(drive_logs / "events.jsonl", {"ts": utc_now_iso(), "type": "task_received", "task": sanitized_task})\

        # Set tool context for this task
        ctx = ToolContext(
            repo_dir=self.env.repo_dir,
            drive_root=self.env.drive_root,
            branch_dev=self.env.branch_dev,
            pending_events=self._pending_events,
            current_chat_id=self._current_chat_id,
            current_task_type=self._current_task_type,
            emit_progress_fn=self._emit_progress,
            task_depth=int(task.get("depth", 0)),
            is_direct_chat=bool(task.get("_is_direct_chat")),\
        )
        self.tools.set_context(ctx)

        # Typing indicator via event queue (no direct Telegram API)
        self._emit_typing_start()

        # --- Build context (delegated to context.py) ---
        messages, cap_info = build_llm_messages(
            env=self.env,
            memory=self.memory,
            task=task,
            review_context_builder=self._build_review_context,
        )

        if cap_info.get("trimmed_sections"):
            try:
                append_jsonl(drive_logs / "events.jsonl", {
                    "ts": utc_now_iso(), "type": "context_soft_cap_trim",
                    "task_id": task.get("id"), **cap_info,
                })
            except Exception:
                log.warning("Failed to log context soft cap trim event", exc_info=True)
        
        return ctx, messages, cap_info

    def process_task(self, task: Dict[str, Any]) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        """Process a task (from telegram bot or internal scheduling)."""
        drive_logs = self.env.drive_path("logs")
        self._task_started_ts = time.time()
        self._current_chat_id = task.get("chat_id")
        self._current_task_type = task.get("type")

        # Load latest state from Drive (needed for budget, etc.)
        state = json.loads(read_text(self.env.drive_path("state") / "state.json"))

        final_response_content = ""
        accumulated_usage: Dict[str, Any] = {}
        llm_trace: Dict[str, Any] = {"assistant_notes": []}

        try:
            ctx, messages, cap_info = self._prepare_task_context(task)

            is_direct_chat = bool(task.get("_is_direct_chat"))
            max_retries_cfg = int(self.memory.get_config("llm_max_retries", default=8) or 8)
            max_iterations_cfg = int(self.memory.get_config("task_max_iterations", default=200) or 200)
            max_wall_time_cfg = float(self.memory.get_config("task_max_wall_time_sec", default=0) or 0)

            if is_direct_chat:
                max_retries_cfg = int(self.memory.get_config("direct_llm_max_retries", default=min(max_retries_cfg, 4)) or 4)
                max_iterations_cfg = int(self.memory.get_config("direct_max_iterations", default=12) or 12)
                max_wall_time_cfg = float(self.memory.get_config("direct_max_wall_time_sec", default=75) or 75)

            max_retries_cfg = max(1, max_retries_cfg)
            max_iterations_cfg = max(1, max_iterations_cfg)
            max_wall_time_cfg = max(0.0, max_wall_time_cfg)
            
            # --- Main LLM tool loop (delegated to loop.py) ---
            final_response_content, accumulated_usage, llm_trace = run_llm_tool_loop( # <--- 修改点2: 调用run_llm_tool_loop
                self.llm,
                self.tools,
                self.env.drive_path("logs"),
                task.get("id"),
                messages,
                state.get("active_model"),
                state.get("active_effort"),
                max_iterations=max_iterations_cfg,
                max_wall_time_sec=max_wall_time_cfg,
                max_retries=max_retries_cfg,
                budget_remaining_usd=state.get("remaining_usd"),
                event_queue=self._event_queue,
                task_type=task.get("type", "task"),
                incoming_messages_queue=self._incoming_messages,
                send_progress_message=self._send_progress_message,
                # tool_schemas=self.tools.get_all_tool_schemas(), # <--- 修改点3: 删除这一行
            )
            return final_response_content, accumulated_usage, llm_trace
        except Exception as e:
            log.exception("Error processing task")
            final_response_content = f"⚠️ SYSTEM_ERROR: {type(e).__name__}: {e}"
            return final_response_content, accumulated_usage, llm_trace
        finally:
            self._current_chat_id = None
            self._current_task_type = None
            # Emit all pending events before exiting (e.g., to record progress message costs)
            self._emit_typing_end()
            self._flush_pending_events()

    def _send_progress_message(self, text: str) -> None:
        """Internal helper for emitting progress messages."""
        now = time.time()
        # Avoid spamming progress messages to owner if called too frequently
        # But always send on task start/end (managed by callers) and if there's been a long pause
        if now - self._last_progress_ts > 10 or now - self._task_started_ts < 2:
            self._last_progress_ts = now
            self._pending_events.append({
                "ts": utc_now_iso(),
                "type": "progress",
                "text": text,
                "chat_id": self._current_chat_id,
            })
            if self._event_queue:
                self._flush_pending_events()

    def _emit_typing_start(self) -> None:
        """Tell the supervisor to send 'typing...' status."""
        if self._current_chat_id and self._event_queue:
            self._pending_events.append({
                "ts": utc_now_iso(), "type": "typing_start",
                "chat_id": self._current_chat_id,
            })
            self._flush_pending_events()

    def _emit_typing_end(self) -> None:
        """Tell the supervisor to stop 'typing...' status."""
        if self._current_chat_id and self._event_queue:
            self._pending_events.append({
                "ts": utc_now_iso(), "type": "typing_end",
                "chat_id": self._current_chat_id,
            })
            self._flush_pending_events()

    def _flush_pending_events(self) -> None:
        """Move all pending events to the queue for supervisor to pick up."""
        while self._pending_events:
            self._event_queue.put(self._pending_events.pop(0))

    def _build_review_context(self, prompt: str, files: List[str]) -> str:
        """Constructs a review context from a list of files."""
        context_str = f"审查任务: {prompt}\n\n"
        for f in files:
            try:
                content = read_text(self.env.repo_path(f))
                context_str += f"--- 文件: {f} ---\n{content}\n"
            except FileNotFoundError:
                context_str += f"--- 文件: {f} (未找到) ---\n"
        return context_str


    def _extract_patch_payload(self, raw_text: str) -> Optional[str]:
        """Extract an apply_patch payload from LLM output."""
        text = str(raw_text or "").strip()
        if not text:
            return None

        def _extract_patch(s: str) -> Optional[str]:
            begin = s.find("*** Begin Patch")
            end = s.rfind("*** End Patch")
            if begin >= 0 and end >= begin:
                end += len("*** End Patch")
                patch = s[begin:end].strip()
                if patch and any(marker in patch for marker in (
                    "*** Update File:",
                    "*** Add File:",
                    "*** Delete File:",
                )):
                    return patch + "\n"
            return None

        # Direct patch text
        patch = _extract_patch(text)
        if patch:
            return patch

        # Markdown fenced code block
        if text.startswith("```"):
            lines = text.splitlines()
            if len(lines) >= 3 and lines[-1].strip().startswith("```"):
                inner = "\n".join(lines[1:-1]).strip()
                patch = _extract_patch(inner)
                if patch:
                    return patch

        # Strict JSON output: {"patch": "*** Begin Patch ..."}
        parsed = None
        try:
            parsed = json.loads(text)
        except Exception:
            left = text.find("{")
            right = text.rfind("}")
            if left >= 0 and right > left:
                try:
                    parsed = json.loads(text[left:right + 1])
                except Exception:
                    parsed = None

        if isinstance(parsed, dict):
            for key in ("patch", "apply_patch"):
                candidate = str(parsed.get(key) or "").strip()
                patch = _extract_patch(candidate)
                if patch:
                    return patch

        return None



    def _codex_code_edit_handler(
        self,
        ctx: ToolContext,
        prompt: str,
        cwd: str = '.',
        model: str = '',
        effort: str = 'high',
    ) -> str:
        """
        Generate and apply a code patch using Codex-style workflow via OpenAI-compatible API.
        """
        _ = ctx

        from ouroboros.tools.codex_cli_prompts import GENERATE_CODEX_PATCH_PROMPT

        state = json.loads(read_text(self.env.drive_path('state') / 'state.json'))

        chosen_model = (
            str(model or '').strip()
            or os.environ.get('OUROBOROS_CODEX_MODEL', '').strip()
            or os.environ.get('OUROBOROS_MODEL_CODE', '').strip()
            or str(state.get('active_model') or '').strip()
            or self.llm.default_model()
        )
        chosen_effort = (
            str(effort or '').strip()
            or os.environ.get('OUROBOROS_CODEX_REASONING_EFFORT', '').strip()
            or str(state.get('active_effort') or '').strip()
            or 'high'
        )

        messages = [
            {'role': 'system', 'content': GENERATE_CODEX_PATCH_PROMPT},
            {'role': 'user', 'content': str(prompt or '')},
        ]

        try:
            response_msg, usage = self.llm.chat(
                messages=messages,
                model=chosen_model,
                tools=None,
                reasoning_effort=chosen_effort,
                max_tokens=12000,
            )
        except Exception as e:
            return f'⚠️ Codex API 调用失败: {type(e).__name__}: {e}'

        response_text = str((response_msg or {}).get('content') or '')
        patch_payload = self._extract_patch_payload(response_text)
        if not patch_payload:
            preview = response_text[:500] if response_text else '(empty)'
            return (
                '⚠️ Codex 未返回可应用的补丁。'
                "请让它返回 JSON {'patch': '*** Begin Patch...*** End Patch'}。"
                f'\nmodel={chosen_model}\npreview={preview}'
            )

        work_dir = self.env.repo_dir
        if cwd and str(cwd).strip() not in ('', '.', './'):
            candidate = (self.env.repo_dir / str(cwd)).resolve()
            if candidate.exists() and candidate.is_dir():
                work_dir = candidate

        try:
            res = subprocess.run(
                ['apply_patch'],
                input=patch_payload,
                text=True,
                capture_output=True,
                cwd=str(work_dir),
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            return '⚠️ Codex 补丁应用超时（180s）。'
        except Exception as e:
            return f'⚠️ Codex 补丁应用失败: {type(e).__name__}: {e}'

        out = (res.stdout or '')
        if res.stderr:
            out += '\n--- STDERR ---\n' + res.stderr

        # Emit usage event for budget/accounting pipeline
        if usage:
            usage_event = {
                'ts': utc_now_iso(),
                'type': 'llm_usage',
                'task_id': 'codex_code_edit',
                'category': 'codex_code_edit',
                'model': chosen_model,
                'usage': {
                    'prompt_tokens': usage.get('prompt_tokens', 0),
                    'completion_tokens': usage.get('completion_tokens', 0),
                    'cost': usage.get('cost', 0),
                },
            }
            if self._event_queue is not None:
                try:
                    self._event_queue.put_nowait(usage_event)
                except Exception:
                    self._pending_events.append(usage_event)
            else:
                self._pending_events.append(usage_event)

        if res.returncode != 0:
            return (
                f'⚠️ Codex 补丁应用失败 (exit_code={res.returncode})。'
                f'\nmodel={chosen_model}\n{out[:4000]}'
            )

        cost = float((usage or {}).get('cost') or 0.0)
        return (
            f'Codex 补丁已应用。model={chosen_model}, effort={chosen_effort}, cost=${cost:.6f}'
            + (f'\n{out[:3000]}' if out.strip() else '')
        )

    def _init_tools(self) -> None:
        """Inject handlers for specific tools that need agent's internal state."""
        self.tools.override_handler("codex_code_edit", self._codex_code_edit_handler)
        # Backward-compatible alias: old prompts may still call claude_code_edit
        self.tools.override_handler("claude_code_edit", self._codex_code_edit_handler)

    def run(self, task: Dict[str, Any]) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        """Entry point for worker tasks."""
        self._init_tools() # Inject special tool handlers
        return self.process_task(task)

# --- supervisor compatibility patch ---
def _compat_handle_task(self, task):
    self._busy = True
    self._pending_events = []
    start_ts = time.time()

    text = ''
    usage = {}
    llm_trace = {}
    try:
        text, usage, llm_trace = self.run(task)
    except Exception as e:
        log.exception('handle_task failed')
        text = f'⚠️ SYSTEM_ERROR: {type(e).__name__}: {e}'
        usage = {}
        llm_trace = {'error': repr(e)}
    finally:
        self._busy = False

    chat_id_raw = task.get('chat_id')
    try:
        chat_id = int(chat_id_raw) if chat_id_raw is not None else 0
    except Exception:
        chat_id = 0

    if chat_id and str(text or '').strip():
        self._pending_events.append({
            'ts': utc_now_iso(),
            'type': 'send_message',
            'chat_id': chat_id,
            'text': str(text),
            'format': 'markdown',
            'is_progress': False,
        })

    if usage:
        self._pending_events.append({
            'ts': utc_now_iso(),
            'type': 'llm_usage',
            'task_id': str(task.get('id') or ''),
            'category': str(task.get('type') or 'task'),
            'model': str((llm_trace or {}).get('model_used') or ''),
            'usage': usage,
        })

    self._pending_events.append({
        'ts': utc_now_iso(),
        'type': 'task_done',
        'task_id': str(task.get('id') or ''),
        'task_type': str(task.get('type') or 'task'),
        'cost_usd': float((usage or {}).get('cost') or 0.0),
        'total_rounds': int((usage or {}).get('rounds') or 0),
        'duration_sec': round(time.time() - start_ts, 3),
    })

    return list(self._pending_events)


def _compat_send_progress_message(self, text):
    now = time.time()
    if now - self._last_progress_ts > 10 or now - self._task_started_ts < 2:
        self._last_progress_ts = now
        self._pending_events.append({
            'ts': utc_now_iso(),
            'type': 'send_message',
            'text': f'💬 {text}',
            'chat_id': self._current_chat_id,
            'format': 'markdown',
            'is_progress': True,
        })
        if self._event_queue:
            self._flush_pending_events()


OuroborosAgent.handle_task = _compat_handle_task
OuroborosAgent._send_progress_message = _compat_send_progress_message


def make_agent(repo_dir: str, drive_root: str, event_queue=None):
    env = Env(repo_dir=pathlib.Path(repo_dir), drive_root=pathlib.Path(drive_root))
    return OuroborosAgent(env, event_queue=event_queue)
# --- end compatibility patch ---

def _compat_emit_progress(self, text):
    return self._send_progress_message(text)


OuroborosAgent._emit_progress = _compat_emit_progress
