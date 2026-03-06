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
from ouroboros.loop import run_llm_tool_loop # <--- 修改点1: 导入run_llm_tool_loop


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
            
            # --- Main LLM tool loop (delegated to loop.py) ---
            final_response_content, accumulated_usage, llm_trace = run_llm_tool_loop( # <--- 修改点2: 调用run_llm_tool_loop
                self.llm,
                self.tools,
                self.env.drive_path("logs"),
                task.get("id"),
                messages,
                state.get("active_model"),
                state.get("active_effort"),
                max_retries=self.memory.get_config("llm_max_retries", default=8),
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

    def _claude_code_edit_handler(self, ctx: ToolContext, prompt: str, cwd: str = ".") -> str:
        """
        Claude Code CLI calls are handled via `apply_patch` (shim in /usr/local/bin).
        This tool implements the logic to invoke the Claude Code CLI locally.
        It uses the current LLM to generate the Claude Code CLI command.
        """
        _ = ctx # ctx is not used for now, might be in the future

        from ouroboros.tools.claude_cli_prompts import GENERATE_CODE_CLI_CMD_PROMPT
        
        # 1. Use the current LLM to generate the Claude Code CLI command
        messages = [
            {"role": "system", "content": GENERATE_CODE_CLI_CMD_PROMPT},
            {"role": "user", "content": prompt},
        ]

        # Use current active model/effort (set by owner, or default from state.json)
        state = json.loads(read_text(self.env.drive_path("state") / "state.json"))
        active_model = state.get("active_model")
        active_effort = state.get("active_effort")

        # This is where the core LLM loop is called to generate the command
        # This will need to be fixed as well, similar to process_task
        cli_command_output, usage, llm_trace = run_llm_tool_loop( # <--- 修改点4: 调用run_llm_tool_loop
            self.llm,
            self.tools,
            self.env.drive_path("logs"),
            "claude_code_cli_gen", # task_id for this sub-task
            messages,
            active_model,
            active_effort,
            max_retries=self.memory.get_config("llm_max_retries", default=8),
            budget_remaining_usd=state.get("remaining_usd"),
            event_queue=self._event_queue,
            task_type="subtask",
            # tool_schemas=self.tools.get_all_tool_schemas(), # <--- 修改点5: 删除这一行
            # No incoming_messages_queue for sub-tasks like this
            send_progress_message=lambda x: None, # No progress for this internal step
        )
        
        if not cli_command_output:
            return "⚠️ Failed to generate Claude Code CLI command."

        # Parse the output to extract the command (expecting a JSON object)
        try:
            parsed_output = json.loads(cli_command_output)
            command_args = parsed_output.get("command_args")
            if not isinstance(command_args, list) or not all(isinstance(arg, str) for arg in command_args):
                raise ValueError("Expected 'command_args' to be a list of strings.")
            
            # The tool output might contain assistant notes or other things, 
            # we only care about the command.
            if not command_args:
                return f"⚠️ Generated Claude Code CLI command was empty or invalid: {cli_command_output}"
            
        except json.JSONDecodeError:
            return f"⚠️ Failed to parse Claude Code CLI command output as JSON: {cli_command_output}"
        except ValueError as e:
            return f"⚠️ Invalid Claude Code CLI command format: {e}. Output: {cli_command_output}"

        # 2. Execute the generated Claude Code CLI command via shell
        full_command = ["apply_patch"] + command_args # apply_patch is a shim

        try:
            # Use run_shell to execute the command. This command is designed to output
            # the diff / changes made directly to stdout, which will be captured as result.
            run_shell_result = self.tools.execute("run_shell", {"cmd": full_command, "cwd": cwd})
            
            # Check for error in run_shell_result (conventionally starts with ⚠️)
            if run_shell_result.strip().startswith("⚠️"):
                return run_shell_result # Propagate the error

            # The Claude Code CLI (via apply_patch shim) directly modifies files.
            # We assume success if no error was returned by run_shell.
            return f"Claude Code CLI executed successfully. Changes applied to files:\n{run_shell_result}"

        except Exception as e:
            return f"⚠️ Error executing Claude Code CLI via apply_patch: {type(e).__name__}: {e}"


    def _init_tools(self) -> None:
        """Inject handlers for specific tools that need agent's internal state."""
        # For claude_code_edit, we need to inject a handler that uses the agent's LLM loop
        # to generate the Claude CLI command.
        self.tools.override_handler("claude_code_edit", self._claude_code_edit_handler)

    def run(self, task: Dict[str, Any]) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        """Entry point for worker tasks."""
        self._init_tools() # Inject special tool handlers
        return self.process_task(task)
