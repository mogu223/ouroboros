
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

from ouroboros.llm import LLMClient, normalize_reasoning_effort, add_usage, EmptyResponseError # EmptyResponseError is new
from ouroboros.tools.registry import ToolRegistry
from ouroboros.context import compact_tool_history, compact_tool_history_llm
from ouroboros.utils import utc_now_iso, append_jsonl, truncate_for_log, sanitize_tool_args_for_log, sanitize_tool_result_for_log, estimate_tokens
from ouroboros.resilience import get_circuit_breaker, get_iteration_guardian, get_global_api_health, CircuitBreaker # CircuitBreaker is new
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


async def _call_llm_with_resilience(
    llm: LLMClient,
    active_model: str,
    active_effort: str,
    fallback_models: List[str],
    messages: List[Dict[str, Any]],
    tools: ToolRegistry,
    llm_trace: Dict[str, Any],
    circuit_breaker: CircuitBreaker,
) -> Tuple[Optional[str], Optional[List[Dict[str, Any]]]]:
    """
    Calls LLM with resilience, using fallback models and circuit breaker.
    Handles EmptyResponseError and empty responses as failures.
    """
    models_to_try = [active_model] + fallback_models
    
    for model_name in models_to_try:
        if not model_name: # Skip if model_name is None or empty string
            continue

        llm_trace["model"] = model_name
        llm_trace["effort"] = active_effort
        
        # Check if the model is available according to the circuit breaker
        if not circuit_breaker.is_available(model_name):
            log.warning(f"Model '{model_name}' is currently blocked by circuit breaker. Trying next available model.")
            llm_trace["assistant_notes"].append(f"Model '{model_name}' blocked by circuit breaker.")
            continue
        
        try:
            # Call LLM (sync API, compatibility with OpenAI-compatible client)
            response_msg, usage_stats = llm.chat(
                messages=messages,
                model=model_name,
                tools=tools.schemas(),
                reasoning_effort=active_effort,
            )

            response_content = None
            response_tool_calls: List[Dict[str, Any]] = []
            if isinstance(response_msg, dict):
                response_content = response_msg.get("content")
                raw_tool_calls = response_msg.get("tool_calls") or []
                if isinstance(raw_tool_calls, dict):
                    raw_tool_calls = [raw_tool_calls]
                if isinstance(raw_tool_calls, list):
                    response_tool_calls = raw_tool_calls

            # Treat empty response or EmptyResponseError as failure
            if response_content is None and not response_tool_calls:
                raise EmptyResponseError(f"Model '{model_name}' returned an empty response.")

            # Record success and return response
            circuit_breaker.record_success(model_name)
            llm_trace["model_used"] = model_name
            add_usage(llm_trace.setdefault("usage", {}), usage_stats)
            return response_content, response_tool_calls

        except EmptyResponseError as e:
            log.error(f"LLM call to '{model_name}' failed: {e}")
            circuit_breaker.record_failure(model_name, str(e))
            llm_trace["assistant_notes"].append(f"LLM call to '{model_name}' failed: {e}")
        except Exception as e:
            log.error(f"Unexpected error during LLM call to '{model_name}': {type(e).__name__}: {e}")
            circuit_breaker.record_failure(model_name, str(e))
            llm_trace["assistant_notes"].append(f"Unexpected LLM error with '{model_name}': {type(e).__name__}: {e}")
            
    # If all models failed or were blocked
    raise RuntimeError("所有模型均已失效或熔断，无法获取 LLM 响应。")


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
    task_id: str = "",
    event_queue: Optional[queue.Queue] = None, # Make event_queue optional with a default None
    llm_trace: Dict[str, Any] = {}, # Make llm_trace optional with a default empty dict
    task_type: str = "task",
) -> Optional[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
    """
    Check if the budget is exceeded or about to be exceeded.
    If so, generates an assistant message to terminate the task.
    Returns (message, usage, trace) tuple if budget exceeded, otherwise None.
    """
    # This function is too long, but for now we won't refactor it further
    # to avoid introducing new bugs.

    # Calculate current cost and remaining budget
    current_cost_estimate = 0.0
    for model_name, usage in accumulated_usage.items():
        current_cost_estimate += _estimate_cost(
            model=model_name,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            cached_tokens=usage["cached_tokens"],
        )

    llm_trace["estimated_total_cost"] = current_cost_estimate

    # Check for hard budget limit
    if budget_remaining_usd is not None and current_cost_estimate >= budget_remaining_usd:
        error_msg = (
            f"❌ 预算已用尽。当前任务已达到 ${budget_remaining_usd:.2f} 的预算限制。 "
            "任务将立即终止。请通知 Owner。"
        )
        llm_trace["error"] = error_msg
        log.critical(error_msg)
        return error_msg, accumulated_usage, llm_trace

    # Warn if approaching budget limit (e.g., 80%)
    if budget_remaining_usd is not None and current_cost_estimate / budget_remaining_usd >= 0.8:
        warning_msg = (
            f"⚠️ 任务接近预算限制。已使用 ${current_cost_estimate:.2f} (80% 以上)。"
            "请注意监控预算。"
        )
        if "budget_warning_sent" not in llm_trace:
            llm_trace["assistant_notes"].append(warning_msg)
            llm_trace["budget_warning_sent"] = True # Only send once per task

    # Check for iteration limit
    if round_idx >= 200:
        error_msg = (
            f"❌ 任务已达到最大迭代次数 ({round_idx} / 200)。"
            "任务将立即终止，以避免无限循环。请检查任务逻辑。"
        )
        llm_trace["error"] = error_msg
        log.critical(error_msg)
        return error_msg, accumulated_usage, llm_trace

    return None


async def _process_tool_results(
    results: List[Dict[str, Any]],
    messages: List[Dict[str, Any]],
    llm_trace: Dict[str, Any],
    emit_progress: Callable[[str], None],
) -> int:
    """
    Process tool execution results, append to messages, and update trace.
    Returns number of errors encountered.
    """
    errors_count = 0
    tool_results_for_context = []

    for result_item in results:
        tool_call_id = result_item["tool_call_id"]
        fn_name = result_item["fn_name"]
        tool_result = _truncate_tool_result(result_item["result"]) # Truncate result here
        is_error = result_item["is_error"]
        args_for_log = result_item["args_for_log"]
        is_code_tool = result_item["is_code_tool"]

        message_content = f"Tool Call: `{fn_name}({json.dumps(args_for_log)})`"
        if is_error:
            errors_count += 1
            progress_msg = f"❌ 工具 '{fn_name}' 执行失败。结果：{tool_result}"
        else:
            progress_msg = f"✅ 工具 '{fn_name}' 执行成功。结果：{tool_result}"

        # Emit progress, but avoid repeating long tool results in progress for brevity
        if len(tool_result) > 200:
            emit_progress(f"{progress_msg[:100]}... (结果已截断，完整内容请看日志)")
        else:
            emit_progress(progress_msg)

        tool_results_for_context.append({
            "tool_call_id": tool_call_id,
            "tool_result": tool_result,
        })
        
        # Log tool details to llm_trace for richer context
        llm_trace["tool_results"].append({
            "ts": utc_now_iso(),
            "tool": fn_name,
            "args": args_for_log,
            "result": tool_result, # Full result before truncation for context, if needed
            "is_error": is_error,
            "is_code_tool": is_code_tool,
        })

    # Group tool results into a single message for the LLM
    if tool_results_for_context:
        messages.append({
            "role": "tool",
            "content": json.dumps(tool_results_for_context)
        })

    return errors_count


async def run_tool_loop(
    llm: LLMClient,
    tools: ToolRegistry,
    messages: List[Dict[str, Any]],
    drive_logs: pathlib.Path,
    task_id: str,
    emit_progress: Callable[[str], None],
    active_model: str,
    active_effort: str,
    budget_remaining_usd: Optional[float] = None,
    max_iterations: int = 200,
    max_wall_time_sec: float = 0.0,
    max_retries: int = 3, # Max retries for LLM calls within _call_llm_with_resilience
    task_type: str = "task",
    event_queue: Optional[queue.Queue] = None,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """
    Main loop: Sends messages to LLM, executes tool calls, repeats until final response.

    Returns: (final_response_text, accumulated_usage, llm_trace)
    """
    accumulated_usage = {}
    llm_trace = {
        "task_id": task_id,
        "model": active_model,
        "effort": active_effort,
        "round": 0,
        "tool_calls": [],
        "tool_results": [],
        "assistant_notes": [],
        "errors": [],
        "model_used": None,
    }

    iteration_guardian = get_iteration_guardian()
    iteration_guardian.enter_iteration() # For debugging recursion/infinite loops

    # Initialize circuit breaker and fallback models
    circuit_breaker = get_circuit_breaker()
    fallback_models = get_fallback_models()
    loop_started_ts = time.time()

    try:
        for round_idx in range(max_iterations):
            llm_trace["round"] = round_idx + 1

            if max_wall_time_sec and (time.time() - loop_started_ts) >= float(max_wall_time_sec):
                timeout_msg = (
                    f"?? ?????? {int(max_wall_time_sec)} ????????"
                    "??????????????"
                )
                llm_trace["error"] = timeout_msg
                llm_trace["assistant_notes"].append("task_wall_time_exceeded")
                log.warning("Task %s exceeded wall-time limit: %ss", task_id, max_wall_time_sec)
                emit_progress(timeout_msg)
                return timeout_msg, accumulated_usage, llm_trace

            # Check iteration guardian (e.g., max depth)
            if iteration_guardian.should_abort():
                error_msg = f"❌ 任务深度过深 ({iteration_guardian.get_depth()})，可能陷入无限递归。任务终止。"
                llm_trace["error"] = error_msg
                log.critical(error_msg)
                emit_progress(error_msg)
                return "", accumulated_usage, llm_trace

            # Check budget limits and max iterations
            budget_status_or_error = _check_budget_limits(
                budget_remaining_usd=budget_remaining_usd,
                accumulated_usage=accumulated_usage,
                round_idx=round_idx,
                messages=messages,
                llm=llm,
                active_model=active_model,
                active_effort=active_effort,
                max_retries=max_retries,
                drive_logs=drive_logs,
                task_id=task_id,
                event_queue=event_queue,
                llm_trace=llm_trace,
                task_type=task_type,
            )
            if budget_status_or_error:
                # _check_budget_limits returns a tuple (error_msg, usage, trace)
                return budget_status_or_error

            emit_progress(f"🔄 LLM Thinking... (Round {round_idx + 1})")

            # --- LLM Call with Resilience ---
            try:
                # Call LLM with resilience, using active and fallback models
                response_content, tool_calls = await _call_llm_with_resilience(
                    llm=llm,
                    active_model=active_model,
                    active_effort=active_effort,
                    fallback_models=fallback_models,
                    messages=messages,
                    tools=tools,
                    llm_trace=llm_trace,
                    circuit_breaker=circuit_breaker,
                )
                # Usage stats are added inside _call_llm_with_resilience
                
            except RuntimeError as e: # Catch the specific exception from _call_llm_with_resilience
                llm_trace["error"] = repr(e)
                llm_trace["assistant_notes"].append(f"所有模型均已失效或熔断，无法获取 LLM 响应。错误：{e}")
                log.critical("所有模型均已失效或熔断，无法获取 LLM 响应。错误：%s", e)
                emit_progress(f"❌ 所有模型均已失效或熔断，任务终止。错误：{e}")
                return "", accumulated_usage, llm_trace # Return empty content and terminate

            except Exception as e:
                # Other unexpected errors during LLM call (e.g., in ToolRegistry.schemas())
                llm_trace["error"] = repr(e)
                llm_trace["assistant_notes"].append(f"LLM 调用发生意外错误: {e}")
                log.error("LLM 调用发生意外错误: %s", e)
                emit_progress(f"❌ LLM 调用发生意外错误，任务终止。错误：{e}")
                return "", accumulated_usage, llm_trace # Return empty content and terminate
            # --- End LLM Call with Resilience ---

            if not response_content and not tool_calls:
                # This should ideally be caught by _call_llm_with_resilience,
                # but as a safeguard, we log it and continue to next iteration
                # if there were no errors from _call_llm_with_resilience
                log.warning("LLM returned an empty response and no tool calls, continuing...")
                llm_trace["assistant_notes"].append("LLM returned empty, continuing.")
                # If _call_llm_with_resilience returned nothing without raising, it means
                # it ran out of models or they are all blocked. The above `except RuntimeError`
                # should handle this. This check is mostly defensive.
                continue

            # Process tool calls if any
            if tool_calls:
                llm_trace["tool_calls"].extend(tool_calls)
                error_count = await _handle_tool_calls(
                    tool_calls=tool_calls,
                    tools=tools,
                    drive_logs=drive_logs,
                    task_id=task_id,
                    stateful_executor=getattr(getattr(tools, "browser", None), "stateful_executor", None),
                    messages=messages,
                    llm_trace=llm_trace,
                    emit_progress=emit_progress,
                )
                if error_count > 0:
                    llm_trace["errors"].append(f"Tool execution encountered {error_count} errors.")
                    emit_progress(f"⚠️ 工具执行过程中遇到 {error_count} 个错误。")

                # Continue to next iteration to get LLM's response to tool results
                continue

            # If no tool calls, it's a final response
            final_response, accumulated_usage, llm_trace = _handle_text_response(
                response_content, llm_trace, accumulated_usage
            )
            emit_progress(f"✅ LLM 回复：{final_response[:200]}...")
            return final_response, accumulated_usage, llm_trace

        loop_limit_msg = f"?? ???????? ({max_iterations})????????????????????"
        llm_trace["error"] = loop_limit_msg
        llm_trace["assistant_notes"].append("max_iterations_reached")
        emit_progress(loop_limit_msg)
        return loop_limit_msg, accumulated_usage, llm_trace

    finally:
        iteration_guardian.exit_iteration() # Ensure iteration guardian state is cleaned up
