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
from ouroboros.loop import run_llm_tool_loop as loop_run_llm_tool_loop


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
        loop_run_llm_tool_loop(
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
        
        # Set LLM client for codex tool handler
        try:
            from ouroboros.tools.codex_edit import set_llm_client
            set_llm_client(self.llm)
        except ImportError:
            log.debug("codex_edit module not found, handler will use default LLM client")
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
            dirty_files = [l.strip() for l in result.stdout.strip().split('\n') if l.strip()]
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
                    if not re.match(r'^[a-zA-Z0-9_/-]+$', self.env.branch_dev):
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
            match = re.search(r'^version\s*=\s*[\'\"]([^\'\"]+)[\'\"]', pyproject_content, re.MULTILINE)
            if match:
                pyproject_version = match.group(1)
                result_data["pyproject_version"] = pyproject_version
                if version_file != pyproject_version:
                    result_data["status"] = "warning"
                    issue_count += 1

            # Check README.md version (Bible P7: VERSION == README version)
            try:
                readme_content = read_text(self.env.repo_path("README.md"))
                readme_match = re.search(r'\*\*Version:\*\*\s*(\d+\.\d+\.\d+)', readme_content)
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
                spent = float(state_data.get("spent_usd", 0))
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
        append_jsonl(drive_logs / "events.jsonl", {"ts": utc_now_iso(), "type": "task_received", "task": sanitized_task})

        # Set tool context for this task
        ctx = ToolContext(
            repo_dir=self.env.repo_dir,
            drive_root=self.env.drive_root,
            pending_events=self._pending_events,
            current_chat_id=self._current_chat_id,
            current_task_type=self._current_task_type,
            event_queue=self._event_queue,
            task_id=str(task.get("id") or ""),
            task_depth=int(task.get("depth", 0) or 0),
            is_direct_chat=bool(task.get("_is_direct_chat")),
        )
        self.tools.set_context(ctx)

        # Build messages with full context
        messages, cap_info = build_llm_messages(
            env=self.env,
            memory=self.memory,
            task=task,
        )

        return ctx, messages, cap_info

    def run_task(self, task: Dict[str, Any], send_progress_message=None) -> List[Dict[str, Any]]:
        """Run a single task and return supervisor event list."""
        self._busy = True
        self._task_started_ts = time.time()
        self._last_progress_ts = 0.0
        task_id = task.get("id", "unknown")
        task_type = task.get("type", "task")
        self._current_task_type = task_type

        chat_id = task.get("chat_id")
        if chat_id:
            self._current_chat_id = chat_id

        drive_logs = self.env.drive_path("logs")
        started_at = time.time()
        final_text = ""
        usage: Dict[str, Any] = {}
        llm_trace: Dict[str, Any] = {}

        try:
            _ctx, messages, _cap_info = self._prepare_task_context(task)

            emit_progress = send_progress_message or (lambda _text: None)
            emit_progress("Thinking...")

            is_direct_chat = bool(task.get("_is_direct_chat"))
            max_retries_cfg = int(self.memory.get_config("llm_max_retries", default=8) or 8)
            max_iterations_cfg = int(self.memory.get_config("task_max_iterations", default=80) or 80)
            max_wall_time_cfg = float(self.memory.get_config("task_max_wall_time_sec", default=900) or 900)
            if is_direct_chat:
                max_retries_cfg = int(self.memory.get_config("direct_llm_max_retries", default=3) or 3)
                max_iterations_cfg = int(self.memory.get_config("direct_max_iterations", default=8) or 8)
                max_wall_time_cfg = float(self.memory.get_config("direct_max_wall_time_sec", default=45) or 45)

            final_text, usage, llm_trace = run_llm_tool_loop(
                llm=self.llm,
                tools=self.tools,
                drive_logs=drive_logs,
                task_id=task_id,
                messages=messages,
                active_model=task.get("model"),
                active_effort=task.get("effort", "medium"),
                max_iterations=task.get("max_iterations", max_iterations_cfg),
                max_wall_time_sec=task.get("max_wall_time_sec", max_wall_time_cfg),
                max_retries=task.get("max_retries", max_retries_cfg),
                budget_remaining_usd=task.get("budget_remaining_usd"),
                event_queue=self._event_queue,
                task_type=task_type,
                incoming_messages_queue=self._incoming_messages,
                send_progress_message=send_progress_message,
            )
        except Exception as e:
            log.exception("Task failed with exception")
            final_text = f"SYSTEM_ERROR: {type(e).__name__}: {e}"
            usage = {}
            llm_trace = {"error": repr(e)}

        if not usage and isinstance(llm_trace, dict) and isinstance(llm_trace.get("usage"), dict):
            usage = dict(llm_trace.get("usage") or {})

        events: List[Dict[str, Any]] = []
        try:
            chat_id_int = int(chat_id) if chat_id is not None else 0
        except Exception:
            chat_id_int = 0

        if chat_id_int and str(final_text or "").strip():
            events.append({
                "ts": utc_now_iso(),
                "type": "send_message",
                "chat_id": chat_id_int,
                "text": str(final_text),
                "format": "markdown",
                "is_progress": False,
            })

        if isinstance(usage, dict) and usage:
            events.append({
                "ts": utc_now_iso(),
                "type": "llm_usage",
                "task_id": str(task_id),
                "category": str(task_type or "task"),
                "model": str((llm_trace or {}).get("model_used") or task.get("model") or ""),
                "usage": usage,
            })

        events.append({
            "ts": utc_now_iso(),
            "type": "task_done",
            "task_id": str(task_id),
            "task_type": str(task_type or "task"),
            "cost_usd": float((usage or {}).get("cost") or 0.0),
            "total_rounds": int((usage or {}).get("rounds") or 0),
            "duration_sec": round(time.time() - started_at, 3),
        })

        self._busy = False
        self._current_chat_id = None
        self._current_task_type = None
        return events

    def is_busy(self) -> bool:
        return self._busy

    def get_incoming_message(self, timeout: float = 0.0) -> Optional[str]:
        """Non-blocking or blocking get for injected owner messages."""
        try:
            return self._incoming_messages.get(block=timeout > 0, timeout=timeout)
        except queue.Empty:
            return None

    def get_current_chat_id(self) -> Optional[int]:
        return self._current_chat_id

    def get_current_task_type(self) -> Optional[str]:
        return self._current_task_type



# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

def make_agent(repo_dir: str, drive_root: str, event_queue: Any = None) -> OuroborosAgent:
    """Factory function to create an OuroborosAgent instance."""
    env = Env(
        repo_dir=pathlib.Path(repo_dir),
        drive_root=pathlib.Path(drive_root),
    )
    return OuroborosAgent(env=env, event_queue=event_queue)


# Alias for backward compatibility
OuroborosAgent.handle_task = OuroborosAgent.run_task


