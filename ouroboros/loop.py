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
    Check budget limits and handle budget overrun.

    Returns:
        None if budget is OK (continue loop)
        (final_text, accumulated_usage, llm_trace) if budget exceeded (stop loop)
    """
    if budget_remaining_usd is None:
        return None

    task_cost = accumulated_usage.get("cost", 0)
    budget_pct = task_cost / budget_remaining_usd if budget_remaining_usd > 0 else 1.0

    if budget_pct > 0.5:
        # Hard stop — return budget exceeded message
        # Return in same format as run_tool_loop
        return (
            f"⚠️ BUDGET_EXCEEDED: Task cost ${task_cost:.4f} exceeds 50% of remaining budget ${budget_remaining_usd:.2f}. "
            f"Stopping to prevent runaway costs.",
        ), accumulated_usage, llm_trace

    if budget_pct > 0.25:
        # Warning — add to messages but continue
        warning = (
            f"⚠️ BUDGET_WARNING: Task cost ${task_cost:.4f} is {budget_pct*100:.0f}% of remaining budget ${budget_remaining_usd:.2f}. "
            f"Consider wrapping up soon."
        )
        messages.append({"role": "user", "content": warning})
        # Record warning in trace
        llm_trace["assistant_notes"].append(f"Budget warning: {budget_pct*100:.0f}%")

    return None


def _call_llm_with_retry(
    llm: LLMClient,
    messages: List[Dict[str, Any]],
    model: str,
    tool_schemas: List[Dict[str, Any]],
    effort: str,
    max_retries: int,
    drive_logs: pathlib.Path,
    task_id: str,
    round_idx: int,
    event_queue: Optional[queue.Queue],
    accumulated_usage: Dict[str, Any],
    task_type: str = "task",
) -> Tuple[Optional[Dict[str, Any]], float]:
    """
    Call LLM with retry and exponential backoff.

    Returns: (message, cost) or (None, 0) if all retries failed
    """
    import random
    cb = get_circuit_breaker()
    global_health = get_global_api_health()

    # Check if this specific model is blocked
    if not cb.is_available(model):
        log.warning(f"Model {model} is blocked by circuit breaker")
        return None, 0.0

    last_error = None
    for attempt in range(max_retries):
        try:
            response, usage = llm.call(
                messages=messages,
                model=model,
                tools=tool_schemas,
                effort=effort,
            )
            # Record success
            cb.record_success(model)
            global_health.record_success()

            # Update accumulated usage
            if usage:
                add_usage(accumulated_usage, usage)
                cost = _estimate_cost(
                    model,
                    usage.get("prompt_tokens", 0),
                    usage.get("completion_tokens", 0),
                    usage.get("cached_tokens", 0),
                    usage.get("cache_write_tokens", 0),
                )
                return response, cost
            return response, 0.0

        except Exception as e:
            last_error = e
            log.warning(f"LLM call failed (attempt {attempt+1}/{max_retries}): {e}")

            # Record failure
            cb.record_failure(model)
            global_health.record_failure([str(e)])

            # Exponential backoff with jitter
            if attempt < max_retries - 1:
                base_delay = 2 ** attempt
                jitter = random.uniform(0, 0.5)
                time.sleep(base_delay + jitter)

    # All retries failed
    log.error(f"All {max_retries} LLM retries failed for model {model}")
    return None, 0.0


def _process_tool_results(
    results: List[Dict[str, Any]],
    messages: List[Dict[str, Any]],
    llm_trace: Dict[str, Any],
    emit_progress: Callable[[str], None],
) -> int:
    """
    Process tool results and append to messages.

    Returns: Number of errors encountered
    """
    errors = 0
    for r in results:
        tool_call_id = r["tool_call_id"]
        fn_name = r["fn_name"]
        result = r["result"]
        is_error = r["is_error"]
        is_code_tool = r["is_code_tool"]

        if is_error:
            errors += 1

        # Emit progress for code tools
        if is_code_tool and not is_error:
            emit_progress(f"✓ {fn_name}")
        elif is_code_tool and is_error:
            emit_progress(f"✗ {fn_name}")

        # Record in trace
        llm_trace["tool_calls"].append({
            "name": fn_name,
            "success": not is_error,
        })

        # Append result to messages
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": fn_name,
            "content": _truncate_tool_result(result),
        })

    return errors


def run_tool_loop(
    llm: LLMClient,
    messages: List[Dict[str, Any]],
    tool_schemas: List[Dict[str, Any]],
    tools: ToolRegistry,
    drive_logs: pathlib.Path,
    task_id: str = "",
    event_queue: Optional[queue.Queue] = None,
    max_rounds: int = 200,
    max_retries: int = 3,
    emit_progress: Optional[Callable[[str], None]] = None,
    budget_remaining_usd: Optional[float] = None,
    task_type: str = "task",
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """
    Run the tool loop: send messages to LLM, execute tool calls, repeat until final response.

    Args:
        llm: LLM client
        messages: Conversation history
        tool_schemas: Tool definitions for LLM
        tools: Tool registry for execution
        drive_logs: Path to logs directory
        task_id: Task identifier for logging
        event_queue: Optional queue for progress events
        max_rounds: Maximum rounds before stopping
        max_retries: Maximum retries per LLM call
        emit_progress: Optional callback for progress messages
        budget_remaining_usd: Optional budget limit
        task_type: Task type (task, evolution, etc.)

    Returns:
        (final_text, usage_stats, llm_trace)
    """
    # Initialize state
    active_model = llm.model
    active_effort = llm.effort
    stateful_executor = _StatefulToolExecutor()
    accumulated_usage: Dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
    llm_trace = {
        "rounds": 0,
        "tool_calls": [],
        "assistant_notes": [],
        "models_tried": [active_model],
    }
    global_health = get_global_api_health()

    if emit_progress is None:
        emit_progress = lambda _: None

    try:
        for round_idx in range(max_rounds):
            llm_trace["rounds"] = round_idx + 1

            # Check iteration guardian (prevents infinite loops)
            guardian = get_iteration_guardian()
            if guardian.should_stop(round_idx, task_type):
                return (
                    f"⚠️ ITERATION_LIMIT: Reached {round_idx} rounds. "
                    f"Stopping to prevent infinite loop. Please simplify your request.",
                ), accumulated_usage, llm_trace

            # Call LLM with retry
            msg, cost = _call_llm_with_retry(
                llm, messages, active_model, tool_schemas, active_effort,
                max_retries, drive_logs, task_id, round_idx, event_queue, accumulated_usage, task_type
            )

            # Fallback logic: try ALL available fallback models before giving up
            if msg is None:
                cb = get_circuit_breaker()
                fallback_candidates = get_fallback_models()  # Dynamic config (hot-reload)
                
                
                # Remove active_model from fallback list (no point trying same model)
                fallback_candidates = [m for m in fallback_candidates if m != active_model]
                
                if not fallback_candidates:
                    # No fallback models configured - record global failure and return
                    global_health.record_global_failure([f"Primary model {active_model} failed, no fallbacks configured"])
                    return (
                        f"⚠️ Failed to get a response from model {active_model}. "
                        f"No fallback models are configured. "
                        f"Please check your OUROBOROS_MODEL_FALLBACK_LIST environment variable.",
                    ), accumulated_usage, llm_trace
                
                # Try each fallback model in order
                all_errors = []
                for fallback_model in fallback_candidates:
                    # Skip if this model is currently blocked by circuit breaker
                    if not cb.is_available(fallback_model):
                        log.info(f"Skipping blocked fallback model: {fallback_model}")
                        continue
                    
                    # Record fallback attempt
                    llm_trace["models_tried"].append(fallback_model)
                    emit_progress(f"⚡ Fallback: {active_model} → {fallback_model}")
                    
                    # Update active model for this attempt
                    active_model = fallback_model
                    
                    # Try this fallback model
                    msg, cost = _call_llm_with_retry(
                        llm, messages, active_model, tool_schemas, active_effort,
                        max_retries, drive_logs, task_id, round_idx, event_queue, accumulated_usage, task_type
                    )
                    
                    if msg is not None:
                        # Success! Continue with this model
                        break
                    else:
                        all_errors.append(fallback_model)
                
                # If all fallbacks failed, return error
                if msg is None:
                    # Record global failure
                    global_health.record_global_failure(all_errors)
                    
                    # Check if ALL models are blocked
                    all_blocked = all(not cb.is_available(m) for m in [active_model] + fallback_candidates)
                    if all_blocked:
                        return (
                            f"⚠️ All models are temporarily blocked due to repeated failures. "
                            f"Circuit breaker will reset in a few minutes. Please try again later.",
                        ), accumulated_usage, llm_trace
                    else:
                        return (
                            f"⚠️ Failed to get a response from model {active_model} after 3 attempts. "
                            f"All fallback models match the active one. Try rephrasing your request.",
                        ), accumulated_usage, llm_trace

            # Handle the response
            if msg is None:
                # Should not reach here, but safety check
                return (
                    f"⚠️ No response received from LLM. Please try again.",
                ), accumulated_usage, llm_trace

            # Check for tool calls
            tool_calls = msg.get("tool_calls", [])
            content = msg.get("content")

            # No tool calls = final response
            if not tool_calls:
                return _handle_text_response(content, llm_trace, accumulated_usage)

            # Has tool calls = execute them
            messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            })

            # Execute tool calls
            errors = _handle_tool_calls(
                tool_calls, tools, drive_logs, task_id,
                stateful_executor, messages, llm_trace, emit_progress
            )

            # Check budget
            budget_result = _check_budget_limits(
                budget_remaining_usd, accumulated_usage, round_idx, messages,
                llm, active_model, active_effort, max_retries, drive_logs,
                task_id, event_queue, llm_trace, task_type
            )
            if budget_result is not None:
                return budget_result

        # Max rounds reached
        return (
            f"⚠️ MAX_ROUNDS: Reached {max_rounds} tool call rounds. "
            f"Please simplify your request or break it into smaller tasks.",
        ), accumulated_usage, llm_trace

    finally:
        # Cleanup stateful executor
        stateful_executor.shutdown(wait=False, cancel_futures=True)


# Convenience function for backward compatibility
def run_tool_loop_simple(
    messages: List[Dict[str, Any]],
    tools: ToolRegistry,
    model: str = "qwen3.5-plus",
    effort: str = "medium",
    **kwargs,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """
    Simple wrapper for run_tool_loop that creates LLM client internally.
    For backward compatibility with older code.
    """
    from ouroboros.llm import LLMClient
    llm = LLMClient()  # model and effort passed in call
    drive_logs = pathlib.Path("/tmp/ouroboros_logs")
    drive_logs.mkdir(exist_ok=True)
    
    tool_schemas = tools.get_schemas()
    
    return run_tool_loop(
        llm=llm,
        messages=messages,
        tool_schemas=tool_schemas,
        tools=tools,
        drive_logs=drive_logs,
        **kwargs,
    )