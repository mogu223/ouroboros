"""
Resource monitoring and protection for Ouroboros.

Memory thresholds, heavy task detection, and resource-aware task scheduling.
"""

from __future__ import annotations
import logging
import pathlib
import re
from dataclasses import dataclass
from typing import Dict, Optional, Set

log = logging.getLogger(__name__)


@dataclass
class MemoryInfo:
    """Memory status snapshot."""
    total_mb: float
    available_mb: float
    swap_total_mb: float
    swap_free_mb: float
    used_pct: float  # (total - available) / total * 100


def get_memory_info() -> MemoryInfo:
    """Read memory info from /proc/meminfo (Linux only)."""
    try:
        data = pathlib.Path("/proc/meminfo").read_text(encoding="utf-8")
        values = {}
        for line in data.splitlines():
            if ":" in line:
                key, val = line.split(":", 1)
                # Extract number (kB)
                m = re.search(r"(\d+)", val)
                if m:
                    values[key.strip()] = int(m.group(1))  # in kB
        
        total_kb = values.get("MemTotal", 0)
        available_kb = values.get("MemAvailable", values.get("MemFree", 0))
        swap_total_kb = values.get("SwapTotal", 0)
        swap_free_kb = values.get("SwapFree", 0)
        
        total_mb = total_kb / 1024
        available_mb = available_kb / 1024
        swap_total_mb = swap_total_kb / 1024
        swap_free_mb = swap_free_kb / 1024
        
        used_pct = ((total_kb - available_kb) / total_kb * 100) if total_kb > 0 else 0
        
        return MemoryInfo(
            total_mb=total_mb,
            available_mb=available_mb,
            swap_total_mb=swap_total_mb,
            swap_free_mb=swap_free_mb,
            used_pct=used_pct,
        )
    except Exception as e:
        log.warning(f"Failed to read memory info: {e}")
        # Return safe defaults
        return MemoryInfo(
            total_mb=1600,  # Assume 1.6GB
            available_mb=500,
            swap_total_mb=4000,
            swap_free_mb=4000,
            used_pct=70,
        )


# ---------------------------------------------------------------------------
# Heavy task detection
# ---------------------------------------------------------------------------

# Tasks that require extra memory (browser, vision, multi-model)
HEAVY_TASK_TOOLS: Set[str] = {
    "browse_page",
    "browser_action", 
    "multi_model_review",
    "analyze_screenshot",
    "vlm_query",
}

# Estimated memory overhead for heavy tools (MB)
HEAVY_TOOL_MEMORY_OVERHEAD: Dict[str, int] = {
    "browse_page": 500,  # Chromium headless
    "browser_action": 500,
    "multi_model_review": 300,  # Multiple parallel LLM calls
    "analyze_screenshot": 200,
    "vlm_query": 100,
}


def is_heavy_task(task: dict) -> bool:
    """Check if a task involves memory-heavy tool calls."""
    # Check tool_calls in task (if pre-planned)
    tool_calls = task.get("tool_calls", [])
    for tc in tool_calls:
        tool_name = tc.get("name", "")
        if tool_name in HEAVY_TASK_TOOLS:
            return True
    
    # Check if task text suggests heavy operations
    text = str(task.get("text", "")).lower()
    heavy_keywords = ["browse", "browser", "screenshot", "multi_model", "vision"]
    for kw in heavy_keywords:
        if kw in text:
            return True
    
    return False


def get_task_memory_estimate(task: dict) -> int:
    """Estimate memory requirement for a task (MB)."""
    base = 100  # Base overhead for any task
    
    tool_calls = task.get("tool_calls", [])
    for tc in tool_calls:
        tool_name = tc.get("name", "")
        if tool_name in HEAVY_TOOL_MEMORY_OVERHEAD:
            base += HEAVY_TOOL_MEMORY_OVERHEAD[tool_name]
    
    # If task looks heavy but no specific tools detected, add buffer
    if is_heavy_task(task):
        base += 300
    
    return base


# ---------------------------------------------------------------------------
# Memory thresholds and checks
# ---------------------------------------------------------------------------

# Minimum available memory to accept any task (MB)
MIN_AVAILABLE_MEMORY_MB = 300

# Minimum available memory to accept heavy task (MB)
MIN_AVAILABLE_FOR_HEAVY_MB = 600


def check_memory_for_task(task: dict) -> tuple[bool, str]:
    """
    Check if there's enough memory to run a task.
    
    Returns:
        (ok, reason): True if task can proceed, False with reason otherwise.
    """
    mem = get_memory_info()
    
    # Estimate task memory need
    task_need_mb = get_task_memory_estimate(task)
    is_heavy = is_heavy_task(task)
    
    min_required = MIN_AVAILABLE_FOR_HEAVY_MB if is_heavy else MIN_AVAILABLE_MEMORY_MB
    
    if mem.available_mb < min_required:
        reason = (
            f"Insufficient memory: {mem.available_mb:.0f}MB available, "
            f"need {min_required}MB for {'heavy' if is_heavy else 'normal'} task"
        )
        return False, reason
    
    # Additional check: available + swap should cover task
    effective_available = mem.available_mb + mem.swap_free_mb * 0.5  # Swap is slower
    if effective_available < task_need_mb:
        reason = (
            f"Memory + swap insufficient: {effective_available:.0f}MB effective, "
            f"task needs ~{task_need_mb}MB"
        )
        return False, reason
    
    return True, f"Memory OK: {mem.available_mb:.0f}MB available, {mem.used_pct:.0f}% used"


def get_memory_status_text() -> str:
    """Get human-readable memory status for logging/debugging."""
    mem = get_memory_info()
    return (
        f"Memory: {mem.available_mb:.0f}MB / {mem.total_mb:.0f}MB available "
        f"({mem.used_pct:.0f}% used), Swap: {mem.swap_free_mb:.0f}MB / {mem.swap_total_mb:.0f}MB free"
    )