"""
Ouroboros — LLM tool loop.

Core loop: send messages to LLM, execute tool calls, repeat until final response.
Extracted from agent.py to keep the agent thin.
"""

from __future__ import annotations

import json
import os
import pathlib
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Tuple

import logging

from ouroboros.llm import LLMClient, normalize_reasoning_effort, add_usage
from ouroboros.tools.registry import ToolRegistry
from ouroboros.context import compact_tool_history, compact_tool_history_llm
from ouroboros.utils import utc_now_iso, append_jsonl, truncate_for_log, sanitize_tool_args_for_log, sanitize_tool_result_for_log, estimate_tokens
from ouroboros.resilience import get_circuit_breaker, get_iteration_guardian, get_global_api_health
from ouroboros.config import get_fallback_models
from ouroboros.reasoning import enhanced_reasoning, get_strategy_for_task, parse_json_from_text


log = logging.getLogger(__name__)

# Pricing from OpenRouter API (2026-02-17). Update periodically via /api/v1/models.
_MODEL_PRICING_STATIC = {
    "anthropic/claude-opus-4.6": (5.0, 0.5, 25.0),
    "anthropic/claude-opus-4": (15.0, 1.5, 75.0),
    "anthropic/claude-sonnet-4": (3.0, 0.30, 15.0),
    "anthropic/claude-sonnet-4.6": (3.0, 0.30, 15.0),
    "anthropic/claude-sonnet-4.5": (3.0, 0.30, 15.0),
    "openai/o3": (2.0, 0.50, 8.0),
    "openai/o3-pro": (20.0, 1.0, 80.0),
    "openai/o4-mini": (1.10, 0.275, 4.40),
    "openai/gpt-4.1": (2.0, 0.50, 8.0),
    "openai/gpt-5.2": (1.75, 0.175, 14.0),
    "openai/gpt-5.2-codex": (1.75, 0.175, 14.0),
    "glm-5": (1.25, 0.125, 10.0),
    "google/gemini-3-pro-preview": (2.0, 0.20, 12.0),
    "x-ai/grok-3-mini": (0.30, 0.03, 0.50),
    "qwen/qwen3.5-plus-02-15": (0.40, 0.04, 2.40),
    "qwen3.5-plus": (0.40, 0.04, 2.40),
    "kimi-k2.5": (0.50, 0.05, 3.0),
    "MiniMax-M2.5": (0.60, 0.06, 3.5),
    "gemini-2.5-flash-lite": (0.10, 0.01, 0.50),
}

_pricing_fetched = False
_cached_pricing = None
_pricing_lock = threading.Lock()

def _get_pricing() -> Dict[str, Tuple[float, float, float]]:
    """
    Lazy-load pricing. On first call, attempts to fetch from OpenRouter API.
    Falls back to static pricing if fetch fails.
    Thread-safe via module-level lock.
    """
    global _pricing_fetched, _cached_pricing

    # Fast path: already fetched (read without lock for performance)
    if _pricing_fetched:
        return _cached_pricing or _MODEL_PRICING_STATIC

    # Slow path: fetch pricing (lock required)
    with _pricing_lock:
        # Double-check after acquiring lock (another thread may have fetched)
        if _pricing_fetched:
            return _cached_pricing or _MODEL_PRICING_STATIC

        _pricing_fetched = True
        _cached_pricing = dict(_MODEL_PRICING_STATIC)

        base_url = str(os.environ.get("OPENAI_BASE_URL", "") or "").lower()
        if "openrouter" in base_url:
            try:
                from ouroboros.llm import fetch_openrouter_pricing
                _live = fetch_openrouter_pricing()
                if _live and len(_live) > 5:
                    _cached_pricing.update(_live)
            except Exception as e:
                import logging as _log
                _log.getLogger(__name__).warning("Failed to sync pricing from OpenRouter: %s", e)
                # Reset flag so we retry next time
                _pricing_fetched = False

        return _cached_pricing

def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int,
                   cached_tokens: int = 0, cache_write_tokens: int = 0) -> float:
    """Estimate cost from token counts using known pricing. Returns 0 if model unknown."""
    model_pricing = _get_pricing()
    # Try exact match first
    pricing = model_pricing.get(model)
    if not pricing:
        # Try longest prefix match
        best_match = None
        best_length = 0
        for key, val in model_pricing.items():
            if model and model.startswith(key):
                if len(key) > best_length:
                    best_match = val
                    best_length = len(key)
        pricing = best_match
    if not pricing:
        return 0.0
    input_price, cached_price, output_price = pricing
    # Non-cached input tokens = prompt_tokens - cached_tokens
    regular_input = max(0, prompt_tokens - cached_tokens)
    cost = (
        regular_input * input_price / 1_000_000
        + cached_tokens * cached_price / 1_000_000
        + completion_tokens * output_price / 1_000_000
    )
    return round(cost, 6)

READ_ONLY_PARALLEL_TOOLS = frozenset({
    "repo_read", "repo_list",
    "drive_read", "drive_list",
    "web_search", "codebase_digest", "chat_history",
})

# Stateful browser tools require thread-affinity (Playwright sync uses greenlet)
STATEFUL_BROWSER_TOOLS = frozenset({"browse_page", "browser_action"})


def _truncate_tool_result(result: Any) -> str:
    """
    Hard-cap tool result string to 15000 characters.
    If truncated, append a note with the original length.
    """
    result_str = str(result)
    if len(result_str) <= 15000:
        return result_str
    original_len = len(result_str)
    return result_str[:15000] + f"\n... (truncated from {original_len} chars)"


def _execute_single_tool(
    tools: ToolRegistry,
    tc: Dict[str, Any],
    drive_logs: pathlib.Path,
    task_id: str = "",
) -> Dict[str, Any]:
    """
    Execute a single tool call and return all needed info.

    Returns dict with: tool_call_id, fn_name, result, is_error, args_for_log, is_code_tool
    """
    fn_name = tc["function"]["name"]
    tool_call_id = tc["id"]
    is_code_tool = fn_name in tools.CODE_TOOLS

    # Parse arguments
    try:
        args = json.loads(tc["function"]["arguments"] or "{}")
    except (json.JSONDecodeError, ValueError) as e:
        result = f"⚠️ TOOL_ARG_ERROR: Could not parse arguments for '{fn_name}': {e}"
        return {
            "tool_call_id": tool_call_id,
            "fn_name": fn_name,
            "result": result,
            "is_error": True,
            "args_for_log": {},
            "is_code_tool": is_code_tool,
        }

    args_for_log = sanitize_tool_args_for_log(fn_name, args if isinstance(args, dict) else {})

    # Execute tool
    tool_ok = True
    try:
        result = tools.execute(fn_name, args)
    except Exception as e:
        tool_ok = False
        result = f"⚠️ TOOL_ERROR ({fn_name}): {type(e).__name__}: {e}"
        append_jsonl(drive_logs / "events.jsonl", {
            "ts": utc_now_iso(), "type": "tool_error", "task_id": task_id,
            "tool": fn_name, "args": args_for_log, "error": repr(e),
        })

    # Log tool execution (sanitize secrets from result before persisting)
    append_jsonl(drive_logs / "tools.jsonl", {
        "ts": utc_now_iso(), "tool": fn_name, "args": args_for_log,
        "result_preview": sanitize_tool_result_for_log(truncate_for_log(result, 2000)),
    })

    is_error = (not tool_ok) or str(result).startswith("⚠️")

    return {
        "tool_call_id": tool_call_id,
        "fn_name": fn_name,
        "result": result,
        "is_error": is_error,
        "args_for_log": args_for_log,
        "is_code_tool": is_code_tool,
    }


class _StatefulToolExecutor:
    """
    Thread-sticky executor for stateful tools (browser, etc).

    Playwright sync API uses greenlet internally which has strict thread-affinity:
    once a greenlet starts in a thread, all subsequent calls must happen in the same thread.
    This executor ensures browse_page/browser_action always run in the same thread.

    On timeout: we shutdown the executor and create a fresh one to reset state.
    """
    def __init__(self):
        self._executor: Optional[ThreadPoolExecutor] = None

    def submit(self, fn, *args, **kwargs):
        """Submit work to the sticky thread. Creates executor on first call."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stateful_tool")
        return self._executor.submit(fn, *args, **kwargs)

    def reset(self):
        """Shutdown current executor and create a fresh one. Used after timeout/error."""
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def shutdown(self, wait=True, cancel_futures=False):
        """Final cleanup."""
        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
            self._executor = None


def _make_timeout_result(
    fn_name: str,
    tool_call_id: str,
    is_code_tool: bool,
    tc: Dict[str, Any],
    drive_logs: pathlib.Path,
    timeout_sec: int,
    task_id: str = "",
    reset_msg: str = "",
) -> Dict[str, Any]:
    """
    Create a timeout error result dictionary and log the timeout event.

    Args:
        reset_msg: Optional additional message (e.g., "Browser state has been reset. ")

    Returns: Dict with tool_call_id, fn_name, result, is_error, args_for_log, is_code_tool
    """
    args_for_log = {}
    try:
        args = json.loads(tc["function"]["arguments"] or "{}")
        args_for_log = sanitize_tool_args_for_log(fn_name, args if isinstance(args, dict) else {})
    except Exception:
        pass

    result = (
        f"⚠️ TOOL_TIMEOUT ({fn_name}): exceeded {timeout_sec}s limit. "
        f"The tool is still running in background but control is returned to you. "
        f"{reset_msg}Try a different approach or inform the owner{' about the issue' if not reset_msg else ''}."
    )

    append_jsonl(drive_logs / "events.jsonl", {
        "ts": utc_now_iso(), "type": "tool_timeout",
        "tool": fn_name, "args": args_for_log,
        "timeout_sec": timeout_sec,
    })
    append_jsonl(drive_logs / "tools.jsonl", {
        "ts": utc_now_iso(), "tool": fn_name,
        "args": args_for_log, "result_preview": result,
    })

    return {
        "tool_call_id": tool_call_id,
        "fn_name": fn_name,
        "result": result,
        "is_error": True,
        "args_for_log": args_for_log,
        "is_code_tool": is_code_tool,
    }


def _execute_with_timeout(
    tools: ToolRegistry,
    tc: Dict[str, Any],
    drive_logs: pathlib.Path,
    timeout_sec: int,
    task_id: str = "",
    stateful_executor: Optional[_StatefulToolExecutor] = None,
) -> Dict[str, Any]:
    """
    Execute a tool call with a hard timeout.

    On timeout: returns TOOL_TIMEOUT error so the LLM regains control.
    For stateful tools (browser): resets the sticky executor to recover state.
    For regular tools: the hung worker thread leaks as daemon — watchdog handles recovery.
    """
    fn_name = tc["function"]["name"]
    tool_call_id = tc["id"]
    is_code_tool = fn_name in tools.CODE_TOOLS
    use_stateful = stateful_executor and fn_name in STATEFUL_BROWSER_TOOLS

    # Two distinct paths: stateful (thread-sticky) vs regular (per-call)
    if use_stateful:
        # Stateful executor: submit + wait, reset on timeout
        future = stateful_executor.submit(_execute_single_tool, tools, tc, drive_logs, task_id)
        try:
            return future.result(timeout=timeout_sec)
        except TimeoutError:
            stateful_executor.reset()
            reset_msg = "Browser state has been reset. "
            return _make_timeout_result(
                fn_name, tool_call_id, is_code_tool, tc, drive_logs,
                timeout_sec, task_id, reset_msg
            )
    else:
        # Regular executor: explicit lifecycle to avoid shutdown(wait=True) deadlock
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(_execute_single_tool, tools, tc, drive_logs, task_id)
            try:
                return future.result(timeout=timeout_sec)
            except TimeoutError:
                return _make_timeout_result(
                    fn_name, tool_call_id, is_code_tool, tc, drive_logs,
                    timeout_sec, task_id, reset_msg=""
                )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)


def _handle_tool_calls(
    tool_calls: List[Dict[str, Any]],
    tools: ToolRegistry,
    drive_logs: pathlib.Path,
    task_id: str,
    stateful_executor: _StatefulToolExecutor,
    messages: List[Dict[str, Any]],
    llm_trace: Dict[str, Any],
    emit_progress: Callable[[str], None],
) -> int:
    """
    Execute tool calls and append results to messages.

    Returns: Number of errors encountered
    """
    # Parallelize only for a strict read-only whitelist; all calls wrapped with timeout.
    can_parallel = (
        len(tool_calls) > 1 and
        all(
            tc.get("function", {}).get("name") in READ_ONLY_PARALLEL_TOOLS
            for tc in tool_calls
        )
    )

    if not can_parallel:
        results = [
            _execute_with_timeout(tools, tc, drive_logs,
                                  tools.get_timeout(tc["function"]["name"]), task_id,
                                  stateful_executor)
            for tc in tool_calls
        ]
    else:
        max_workers = min(len(tool_calls), 8)
        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            future_to_index = {
                executor.submit(
                    _execute_with_timeout, tools, tc, drive_logs,
                    tools.get_timeout(tc["function"]["name"]), task_id,
                    stateful_executor,
                ): idx
                for idx, tc in enumerate(tool_calls)
            }
            results = [None] * len(tool_calls)
            for future in as_completed(future_to_index):
                idx = future_to_index[future]
                results[idx] = future.result()
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    # Process results in original order
    return _process_tool_results(results, messages, llm_trace, emit_progress)


def _handle_text_response(
    content: Optional[str],
    llm_trace: Dict[str, Any],
    accumulated_usage: Dict[str, Any],
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """
    Handle LLM response without tool calls (final response).

    Returns: (final_text, accumulated_usage, llm_trace)
    """
    if content and content.strip():
        llm_trace["assistant_notes"].append(content.strip()[:320])
    return (content or ""), accumulated_usage, llm_trace


def _check_budget_limits(
    budget_remaining_usd: Optional[float],
    accumulated_usage: Dict[str, Any],
    round_idx: int,
    messages: List[Dict[str, Any]],
    llm: LLMClient,
    active_model: str,
    active_effort: str,
    max_retries: int,
    drive_logs: pathlib.Path,
    task_id: str,
    event_queue: Optional[queue.Queue],
    llm_trace: Dict[str, Any],
    task_type: str = "task",
) -> Optional[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
    """
    Check if the current task has exceeded budget limits.

    If budget exceeded:
    - Writes a final budget alert to logs.
    - Sends a BUDGET_EXCEEDED event if queue is present.
    - Returns a final error message tuple to be returned by run_tool_loop.
    - Returns None if budget is okay.
    """
    if budget_remaining_usd is None or budget_remaining_usd >= 0:
        return None

    final_cost = accumulated_usage.get("cost_usd", 0.0)
    budget_overrun = abs(budget_remaining_usd)

    log.warning(
        "Task %s budget exceeded. Final cost: $%.4f, overrun: $%.4f",
        task_id, final_cost, budget_overrun
    )

    error_message = (
        f"⚠️ TASK BUDGET EXCEEDED. The allocated budget has been surpassed. "
        f"Final cost: ${final_cost:.4f}. Please review cost control settings "
        "or increase the budget for this type of task."
    )

    # Persist the final (aborted) trace
    append_jsonl(drive_logs / "events.jsonl", {
        "ts": utc_now_iso(),
        "type": "llm_trace",
        "task_id": task_id,
        "trace": llm_trace,
    })
    if event_queue:
        event_queue.put_nowait({
            "type": "BUDGET_EXCEEDED",
            "task_id": task_id,
            "final_cost_usd": final_cost,
        })

    return error_message, accumulated_usage, llm_trace


def _process_tool_results(
    results: List[Dict[str, Any]],
    messages: List[Dict[str, Any]],
    llm_trace: Dict[str, Any],
    emit_progress: Callable[[str], None],
) -> int:
    """
    Append tool results to messages and emit progress.

    Returns:
        int: Number of errors encountered.
    """
    errors_found = 0
    tool_results_for_llm = []

    for r in results:
        if r["is_error"]:
            errors_found += 1

        # Truncate result before appending to messages list (hard cap)
        result_str = _truncate_tool_result(r["result"])
        tool_results_for_llm.append({
            "tool_call_id": r["tool_call_id"],
            "role": "tool",
            "name": r["fn_name"],
            "content": result_str,
        })

        # Emit progress to owner
        progress = f"Tool: {r['fn_name']}({r['args_for_log']}) -> {truncate_for_log(result_str, 200)}"
        emit_progress(progress)
        llm_trace["tool_calls"].append({
            "name": r["fn_name"],
            "args": r["args_for_log"],
            "result_preview": truncate_for_log(result_str, 500),
            "is_error": r["is_error"],
            "is_code_tool": r["is_code_tool"],
        })

    # Append all tool results as a single message
    if tool_results_for_llm:
        messages.append({
            "role": "tool",
            "content": tool_results_for_llm,
        })

    return errors_found


def run_tool_loop(
    messages: List[Dict[str, Any]],
    tools: ToolRegistry,
    llm: LLMClient,
    drive_logs: pathlib.Path,
    task_id: str,
    emit_progress: Callable[[str], None],
    event_queue: Optional[queue.Queue] = None,
    budget_remaining_usd: Optional[float] = None,
    max_rounds: int = 15,
    max_retries: int = 3,
    task_type: str = "task",
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """
    Core LLM tool loop.

    Args:
        messages: Conversation history (mutated by this function).
        tools: Tool registry for execution.
        llm: LLM client.
        drive_logs: Path to drive logs directory.
        task_id: ID of the current task.
        emit_progress: Function to send progress updates to the owner.
        event_queue: Queue to send events to the supervisor.
        budget_remaining_usd: Remaining budget for this task.
        max_rounds: Max iterations of the loop.
        max_retries: Max retries for LLM API calls.
        task_type: The type of task being run ('task' or 'evolution').

    Returns:
        Tuple of (final_response_text, accumulated_usage, llm_trace)
    """
    round_idx = 0
    accumulated_usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "cost_usd": 0.0, "llm_calls": 0, "cache_hits": 0, "cache_writes": 0}
    llm_trace = {"rounds": [], "tool_calls": [], "assistant_notes": []}
    stateful_executor = _StatefulToolExecutor()
    consecutive_empty_response_count = 0

    try:
        while round_idx < max_rounds:
            # --- Check budget before calling LLM ---
            budget_check_result = _check_budget_limits(
                budget_remaining_usd, accumulated_usage, round_idx, messages, llm,
                llm.model, llm.effort, max_retries, drive_logs, task_id,
                event_queue, llm_trace, task_type
            )
            if budget_check_result:
                return budget_check_result

            # --- Call LLM ---
            response_message, usage, trace = llm.chat(
                messages,
                tools.get_schemas(),
                max_retries=max_retries,
            )

            # --- Empty Response Circuit Breaker ---
            if response_message is None or not response_message.content and not response_message.tool_calls:
                consecutive_empty_response_count += 1
                log.warning("Empty LLM response #%d for task %s", consecutive_empty_response_count, task_id)
                if consecutive_empty_response_count >= 3:
                    raise RuntimeError(f"LLM returned empty responses {consecutive_empty_response_count} consecutive times. Aborting task.")
                
                # Append a placeholder to history to record the event and avoid tight loops.
                messages.append({"role": "assistant", "content": "(Received empty response, retrying...)"})
                llm_trace["assistant_notes"].append("(Received empty response, retrying...)")
                round_idx += 1
                # Give the model provider a moment to recover
                time.sleep(1) 
                continue
            else:
                consecutive_empty_response_count = 0


            # --- Update usage and trace ---
            add_usage(accumulated_usage, usage)
            if budget_remaining_usd is not None:
                budget_remaining_usd -= usage.get("cost_usd", 0.0)
            llm_trace["rounds"].append(trace)

            # --- Append assistant response to history ---
            # Use model_dump to get a clean dict representation
            messages.append(response_message.model_dump(exclude_none=True))

            # --- Handle tool calls or final response ---
            if response_message.tool_calls:
                emit_progress("Thinking...")
                llm_trace["assistant_notes"].append(response_message.content or "(tool use)")
                
                _handle_tool_calls(
                    response_message.tool_calls, tools, drive_logs, task_id,
                    stateful_executor, messages, llm_trace, emit_progress
                )
            else:
                # No tool calls, this is the final response
                return _handle_text_response(response_message.content, llm_trace, accumulated_usage)

            round_idx += 1

        # --- Loop finished (max_rounds reached) ---
        final_text = "⚠️ MAX_ROUNDS_REACHED. The task took too many steps. Please simplify the request or increase max_rounds."
        llm_trace["assistant_notes"].append(final_text)
        return final_text, accumulated_usage, llm_trace

    finally:
        # --- Final logging and cleanup ---
        stateful_executor.shutdown(wait=False)
        final_cost = accumulated_usage.get('cost_usd', 0.0)
        log.info(
            "Task %s finished. Rounds: %d, LLM Calls: %d, Cost: $%.4f",
            task_id, round_idx, accumulated_usage.get('llm_calls', 0), final_cost
        )
        append_jsonl(drive_logs / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "llm_trace",
            "task_id": task_id,
            "trace": llm_trace,
        })
        if event_queue:
            event_queue.put_nowait({
                "type": "TASK_COMPLETED" if "final_text" in locals() else "TASK_FAILED",
                "task_id": task_id,
                "final_cost_usd": final_cost
            })
