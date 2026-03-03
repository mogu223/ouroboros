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
        "ts": utc_now_iso(), "tool": fn_name, "task_id": task_id,
        "args": args_for_log,
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
        # Hard stop — budget exceeded 50% of remaining budget on this task
        # Inform LLM so it can wrap up quickly
        warn_msg = f"[BUDGET_WARN] Task cost ${task_cost:.3f} exceeds 50% of remaining budget ${budget_remaining_usd:.3f}. Please wrap up now."
        messages.append({"role": "system", "content": warn_msg})

    if budget_pct > 0.8:
        # Critical — force completion
        finish_reason = f"⚠️ Task cost ${task_cost:.3f} exceeded 80% of remaining budget ${budget_remaining_usd:.3f}. Task terminated."
        messages.append({"role": "system", "content": f"[BUDGET_STOP] {finish_reason}"})
        try:
            final_msg, final_cost = _call_llm_with_retry(
                llm, messages, active_model, None, active_effort,
                max_retries, drive_logs, task_id, round_idx, event_queue, accumulated_usage, task_type
            )
            if final_msg:
                return (final_msg.get("content") or finish_reason), accumulated_usage, llm_trace
        except Exception:
            log.warning("Failed to get final response after budget stop", exc_info=True)
        return finish_reason, accumulated_usage, llm_trace

    return None


def _maybe_inject_self_check(
    round_idx: int,
    max_rounds: int,
    messages: List[Dict[str, Any]],
    accumulated_usage: Dict[str, Any],
    emit_progress: Callable[[str], None],
):
    """
    Inject a self-check reminder every 50 rounds if the task is taking long.
    LLM-first: agent decides what to do, this is just a nudge.
    """
    if round_idx > 0 and round_idx % 50 == 0:
        task_cost = accumulated_usage.get("cost", 0)
        check_msg = (
            f"[SELF_CHECK] Round {round_idx}/{max_rounds}. "
            f"Task cost so far: ${task_cost:.3f}. "
            f"If you're stuck, consider: (1) using schedule_task for parallel work, "
            f"(2) switching to a more capable model via switch_model, "
            f"(3) simplifying the approach, or (4) asking the owner for guidance."
        )
        messages.append({"role": "system", "content": check_msg})
        emit_progress(f"📍 Round {round_idx}, task cost: ${task_cost:.3f}")


def _drain_incoming_messages(
    messages: List[Dict[str, Any]],
    incoming_messages: Optional[List[Dict[str, Any]]],
    drive_root: Optional[pathlib.Path],
    task_id: str,
    event_queue: Optional[queue.Queue],
    seen_set: set,
):
    """
    Drain owner messages from in-process queue and Drive mailbox.

    Messages are appended to the messages list as [Owner message during task]: ...
    """
    # In-process queue (from telegram bot)
    if incoming_messages:
        while incoming_messages:
            msg = incoming_messages.pop(0)
            owner_text = msg.get("text", "")
            if owner_text:
                messages.append({
                    "role": "user",
                    "content": f"[Owner message during task]: {owner_text}"
                })

    # Drive mailbox (for background tasks / long-running tasks)
    if drive_root:
        mailbox_path = drive_root / "mailbox"
        if mailbox_path.exists():
            for entry in list(mailbox_path.iterdir()):
                if entry.suffix == ".json" and entry.name not in seen_set:
                    try:
                        data = json.loads(entry.read_text(encoding="utf-8"))
                        owner_text = data.get("text", "")
                        if owner_text:
                            messages.append({
                                "role": "user",
                                "content": f"[Owner message during task]: {owner_text}"
                            })
                        seen_set.add(entry.name)
                    except Exception as e:
                        log.warning(f"Failed to read mailbox entry {entry}: {e}")


def _setup_dynamic_tools(
    tools: ToolRegistry,
    tool_schemas: List[Dict[str, Any]],
    messages: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Set up dynamic tool discovery.

    Returns: (tool_schemas, enabled_extra_tools)
    """
    enabled_extra_tools = []

    # Check if LLM previously requested extra tools
    ctx = tools._ctx
    if ctx and hasattr(ctx, 'enabled_tools') and ctx.enabled_tools:
        for tool_name in ctx.enabled_tools:
            schema = tools.schema(tool_name)
            if schema:
                tool_schemas.append(schema)
                enabled_extra_tools.append(tool_name)

    # Inject tool discovery hint if there are more tools available
    all_tools = tools.list_tools()
    core_tools = tools.list_core_tools()
    extra_tools = [t for t in all_tools if t not in core_tools]
    if extra_tools and not enabled_extra_tools:
        hint = (
            f"[TOOL_HINT] {len(extra_tools)} additional tools available. "
            f"Use list_available_tools to see them, enable_tools to activate them."
        )
        messages.append({"role": "system", "content": hint})

    return tool_schemas, enabled_extra_tools


def _process_tool_results(
    results: List[Dict[str, Any]],
    messages: List[Dict[str, Any]],
    llm_trace: Dict[str, Any],
    emit_progress: Callable[[str], None],
) -> int:
    """
    Process tool execution results and append to messages.

    Returns: Number of errors encountered
    """
    error_count = 0
    for r in results:
        tool_call_id = r["tool_call_id"]
        fn_name = r["fn_name"]
        result = r["result"]
        is_error = r["is_error"]
        if is_error:
            error_count += 1
        result_str = _truncate_tool_result(result)
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": fn_name,
            "content": result_str,
        })
        llm_trace["tool_calls"].append({
            "name": fn_name,
            "args": r.get("args_for_log", {}),
            "result_preview": truncate_for_log(result, 500),
            "is_error": is_error,
        })
        # Emit progress for code tools (owner sees activity)
        if r["is_code_tool"] and not is_error:
            emit_progress(f"✓ {fn_name}")
    return error_count


def _emit_llm_usage_event(
    event_queue: Optional[queue.Queue],
    task_id: str,
    model: str,
    usage: Dict[str, Any],
    cost: float,
    category: str = "task",
):
    """Emit an LLM usage event to the event queue (real-time budget tracking)."""
    if not event_queue:
        return
    try:
        event_queue.put_nowait({
            "type": "llm_usage",
            "ts": utc_now_iso(),
            "task_id": task_id,
            "model": model,
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "cached_tokens": int(usage.get("cached_tokens") or 0),
            "cache_write_tokens": int(usage.get("cache_write_tokens") or 0),
            "cost": cost,
            "cost_estimated": not bool(usage.get("cost")),
            "usage": usage,
            "category": category,
        })
    except Exception:
        log.debug("Failed to put llm_usage event to queue", exc_info=True)


def _call_llm_with_retry(
    llm: LLMClient,
    messages: List[Dict[str, Any]],
    model: str,
    tools: Optional[List[Dict[str, Any]]],
    effort: str,
    max_retries: int,
    drive_logs: pathlib.Path,
    task_id: str,
    round_idx: int,
    event_queue: Optional[queue.Queue],
    accumulated_usage: Dict[str, Any],
    task_type: str = "",
) -> Tuple[Optional[Dict[str, Any]], float]:
    """
    Call LLM with retry logic, usage tracking, and event emission.

    Returns:
        (response_message, cost) on success
        (None, 0.0) on failure after max_retries
    """
    msg = None
    last_error: Optional[Exception] = None

    for attempt in range(max_retries):
        try:
            kwargs = {"messages": messages, "model": model, "reasoning_effort": effort}
            if tools:
                kwargs["tools"] = tools
            resp_msg, usage = llm.chat(**kwargs)
            msg = resp_msg
            add_usage(accumulated_usage, usage)

            # Calculate cost and emit Event for EVERY attempt (including retries)
            cost = float(usage.get("cost") or 0)
            if not cost:
                cost = _estimate_cost(
                    model,
                    int(usage.get("prompt_tokens") or 0),
                    int(usage.get("completion_tokens") or 0),
                    int(usage.get("cached_tokens") or 0),
                    int(usage.get("cache_write_tokens") or 0),
                )

            # Emit real-time usage event with category based on task_type
            category = task_type if task_type in ("evolution", "consciousness", "review", "summarize") else "task"
            _emit_llm_usage_event(event_queue, task_id, model, usage, cost, category)

            # Empty response = retry-worthy (model sometimes returns empty content with no tool_calls)
            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content")
            if not tool_calls and (not content or not content.strip()):
                log.warning("LLM returned empty response (no content, no tool_calls), attempt %d/%d", attempt + 1, max_retries)

                # Log raw empty response for debugging
                append_jsonl(drive_logs / "events.jsonl", {
                    "ts": utc_now_iso(), "type": "llm_empty_response",
                    "task_id": task_id,
                    "round": round_idx, "attempt": attempt + 1,
                    "model": model,
                    "raw_content": repr(content)[:500] if content else None,
                    "raw_tool_calls": repr(tool_calls)[:500] if tool_calls else None,
                    "finish_reason": msg.get("finish_reason") or msg.get("stop_reason"),
                })

                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                # Last attempt — return None to trigger "could not get response"
                return None, cost

            # Count only successful rounds
            accumulated_usage["rounds"] = accumulated_usage.get("rounds", 0) + 1

            # Log per-round metrics
            _round_event = {
                "ts": utc_now_iso(), "type": "llm_round",
                "task_id": task_id,
                "round": round_idx, "model": model,
                "reasoning_effort": effort,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "cached_tokens": usage.get("cached_tokens"),
                "cost": cost,
                "attempt": attempt + 1,
            }
            append_jsonl(drive_logs / "events.jsonl", _round_event)

            return msg, cost

        except Exception as e:
            last_error = e
            log.warning("LLM call failed (attempt %d/%d): %s", attempt + 1, max_retries, e)

            # Log error
            append_jsonl(drive_logs / "events.jsonl", {
                "ts": utc_now_iso(), "type": "llm_error",
                "task_id": task_id,
                "round": round_idx, "attempt": attempt + 1,
                "model": model,
                "error": str(e)[:500],
            })

            # Record failure in circuit breaker
            cb = get_circuit_breaker()
            cb.record_failure(model, str(e))

            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue

    # All retries exhausted
    return None, 0.0


def run_tool_loop(
    llm: LLMClient,
    messages: List[Dict[str, Any]],
    tools: ToolRegistry,
    drive_logs: pathlib.Path,
    incoming_messages: Optional[List[Dict[str, Any]]] = None,
    emit_progress: Callable[[str], None] = lambda _: None,
    task_type: str = "",
    task_id: str = "",
    budget_remaining_usd: Optional[float] = None,
    event_queue: Optional[queue.Queue] = None,
    initial_effort: str = "medium",
    drive_root: Optional[pathlib.Path] = None,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """
    Core LLM-with-tools loop.

    Sends messages to LLM, executes tool calls, retries on errors.
    LLM controls model/effort via switch_model tool (LLM-first, Bible P3).

    Args:
        budget_remaining_usd: If set, forces completion when task cost exceeds 50% of this budget
        initial_effort: Initial reasoning effort level (default "medium")

    Returns: (final_text, accumulated_usage, llm_trace)
    """
    # LLM-first: single default model, LLM switches via tool if needed
    active_model = llm.default_model()
    active_effort = initial_effort

    llm_trace: Dict[str, Any] = {"assistant_notes": [], "tool_calls": []}
    accumulated_usage: Dict[str, Any] = {}
    max_retries = 3
    # Wire module-level registry ref so tool_discovery handlers work outside run_llm_loop too
    from ouroboros.tools import tool_discovery as _td
    _td.set_registry(tools)

    # Selective tool schemas: core set + meta-tools for discovery.
    tool_schemas = tools.schemas(core_only=True)
    tool_schemas, _enabled_extra_tools = _setup_dynamic_tools(tools, tool_schemas, messages)

    # Set budget tracking on tool context for real-time usage events
    tools._ctx.event_queue = event_queue
    tools._ctx.task_id = task_id
    # Thread-sticky executor for browser tools (Playwright sync requires greenlet thread-affinity)
    stateful_executor = _StatefulToolExecutor()
    # Dedup set for per-task owner messages from Drive mailbox
    _owner_msg_seen: set = set()
    try:
        MAX_ROUNDS = max(1, int(os.environ.get("OUROBOROS_MAX_ROUNDS", "200")))
    except (ValueError, TypeError):
        MAX_ROUNDS = 200
        log.warning("Invalid OUROBOROS_MAX_ROUNDS, defaulting to 200")
    round_idx = 0
    
    # Get GlobalApiHealth instance for cooldown tracking
    global_health = get_global_api_health()
    
    try:
        while True:
            round_idx += 1

            # Check global API health - if all models failed recently, wait for cooldown
            if global_health.is_globally_blocked():
                remaining = global_health.get_remaining_cooldown()
                return (
                    f"⚠️ All LLM APIs are temporarily unavailable. "
                    f"Global cooldown: {remaining:.0f}s remaining. "
                    f"Please wait and try again later.",
                    accumulated_usage,
                    llm_trace
                )

            # Hard limit on rounds to prevent runaway tasks
            if round_idx > MAX_ROUNDS:
                finish_reason = f"⚠️ Task exceeded MAX_ROUNDS ({MAX_ROUNDS}). Consider decomposing into subtasks via schedule_task."
                messages.append({"role": "system", "content": f"[ROUND_LIMIT] {finish_reason}"})
                try:
                    final_msg, final_cost = _call_llm_with_retry(
                        llm, messages, active_model, None, active_effort,
                        max_retries, drive_logs, task_id, round_idx, event_queue, accumulated_usage, task_type
                    )
                    if final_msg:
                        return (final_msg.get("content") or finish_reason), accumulated_usage, llm_trace
                    return finish_reason, accumulated_usage, llm_trace
                except Exception:
                    log.warning("Failed to get final response after round limit", exc_info=True)
                    return finish_reason, accumulated_usage, llm_trace

            # Soft self-check reminder every 50 rounds (LLM-first: agent decides, not code)
            _maybe_inject_self_check(round_idx, MAX_ROUNDS, messages, accumulated_usage, emit_progress)

            # Apply LLM-driven model/effort switch (via switch_model tool)
            ctx = tools._ctx
            if ctx.active_model_override:
                active_model = ctx.active_model_override
                ctx.active_model_override = None
            if ctx.active_effort_override:
                active_effort = normalize_reasoning_effort(ctx.active_effort_override, default=active_effort)
                ctx.active_effort_override = None

            # Inject owner messages (in-process queue + Drive mailbox)
            _drain_incoming_messages(messages, incoming_messages, drive_root, task_id, event_queue, _owner_msg_seen)

            # Compact old tool history when needed
            # Check for LLM-requested compaction first (via compact_context tool)
            pending_compaction = getattr(tools._ctx, '_pending_compaction', None)
            if pending_compaction is not None:
                messages = compact_tool_history_llm(messages, keep_recent=pending_compaction)
                tools._ctx._pending_compaction = None
            elif round_idx > 8:
                messages = compact_tool_history(messages, keep_recent=6)
            elif round_idx > 3:
                # Light compaction: only if messages list is very long (>60 items)
                if len(messages) > 60:
                    messages = compact_tool_history(messages, keep_recent=6)

            # --- LLM call with retry ---
            msg, cost = _call_llm_with_retry(
                llm, messages, active_model, tool_schemas, active_effort,
                max_retries, drive_logs, task_id, round_idx, event_queue, accumulated_usage, task_type
            )

            # Fallback logic: try ALL available fallback models before giving up
            if msg is None:
                cb = get_circuit_breaker()
                fallback_list_raw = os.environ.get(
                    "OUROBOROS_MODEL_FALLBACK_LIST",
                    ""
                )
                fallback_candidates = [m.strip() for m in fallback_list_raw.split(",") if m.strip()]
                
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
                        log.info("Skipping blocked fallback model: %s", fallback_model)
                        continue
                    
                    # Emit progress message so user sees fallback happening
                    fallback_progress = f"⚡ Fallback: {active_model} → {fallback_model}"
                    emit_progress(fallback_progress)
                    
                    msg, fallback_cost = _call_llm_with_retry(
                        llm, messages, fallback_model, tool_schemas, active_effort,
                        max_retries, drive_logs, task_id, round_idx, event_queue, accumulated_usage, task_type
                    )
                    
                    if msg is not None:
                        # Fallback succeeded! Reset circuit breaker for this model
                        cb.reset(fallback_model)
                        # Continue processing with this msg
                        break
                    else:
                        all_errors.append(f"{fallback_model}: failed")
                else:
                    # All fallback models failed - trigger global cooldown
                    global_health.record_global_failure(all_errors)
                    return (
                        f"⚠️ All LLM models are currently unavailable. "
                        f"Tried: {active_model} → {', '.join(fallback_candidates)}. "
                        f"System is in cooldown mode. Please wait a moment and try again.",
                    ), accumulated_usage, llm_trace

                # If we got here with msg=None, something went wrong
                if msg is None:
                    global_health.record_global_failure([f"All {len(fallback_candidates) + 1} models failed"])
                    return (
                        f"⚠️ Failed to get a response from any model. "
                        f"Please try again in a few seconds.",
                    ), accumulated_usage, llm_trace

            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content")
            # No tool calls — final response
            if not tool_calls:
                return _handle_text_response(content, llm_trace, accumulated_usage)

            # Process tool calls
            messages.append({"role": "assistant", "content": content or "", "tool_calls": tool_calls})

            if content and content.strip():
                emit_progress(content.strip())
                llm_trace["assistant_notes"].append(content.strip()[:320])

            error_count = _handle_tool_calls(
                tool_calls,
                tools,
                drive_logs,
                task_id,
                stateful_executor,
                messages,
                llm_trace,
                emit_progress,
            )

            # Check for request_stop tool result
            for tc in tool_calls:
                if tc.get("function", {}).get("name") == "request_stop":
                    # LLM requested to stop — look for the tool result in messages
                    for i in range(len(messages) - 1, max(0, len(messages) - 5), -1):
                        m = messages[i]
                        if m.get("role") == "tool" and m.get("name") == "request_stop":
                            result = m.get("content", "")
                            # Try to parse as JSON
                            try:
                                result_data = json.loads(result)
                                if isinstance(result_data, dict) and "text" in result_data:
                                    return result_data["text"], accumulated_usage, llm_trace
                            except (json.JSONDecodeError, ValueError):
                                pass
                            return result, accumulated_usage, llm_trace
                    return "Stop requested.", accumulated_usage, llm_trace

            # Tool errors exceed threshold — inject warning, don't abort
            if error_count > 0:
                warn_msg = f"[TOOL_ERROR] {error_count} tool call(s) failed in this round. Consider alternatives or inform the owner."
                messages.append({"role": "system", "content": warn_msg})

    finally:
        # Cleanup stateful executor
        stateful_executor.shutdown(wait=False, cancel_futures=True)