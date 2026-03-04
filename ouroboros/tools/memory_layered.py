"""Memory tools: layered chat history (L0/L1/L2), memory points management."""

from __future__ import annotations

import json
import logging
import pathlib
from typing import Any, Dict, List, Literal, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import utc_now_iso, read_text, write_text, append_jsonl

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Layered Chat History
# ---------------------------------------------------------------------------

def _chat_history_layered(
    ctx: ToolContext,
    l0: bool = True,
    l1: bool = True,
    l2_query: Optional[str] = None,
    l2_limit: int = 5
) -> str:
    """
    Retrieve chat history in layers (L0 summary, L1 points, L2 full content).
    
    - L0: Recent message summaries (~500 tokens) - always loaded
    - L1: Key decision points (~2000 tokens) - loaded on demand
    - L2: Full history with semantic search - loaded by query
    
    Args:
        l0: Include L0 summary (recent 10 messages)
        l1: Include L1 points (key decisions, facts, todos)
        l2_query: Optional search query for L2 full history
        l2_limit: Max number of L2 results to return
    """
    from ouroboros.memory import Memory
    mem = Memory(drive_root=ctx.drive_root)
    
    result_parts = []
    
    # L0: Recent message summaries
    if l0:
        recent = mem.chat_history(count=10, offset=0)
        result_parts.append(f"## L0: Recent Messages (Last 10)\n\n{recent}\n")
    
    # L1: Key decision points from scratchpad
    if l1:
        scratchpad_path = mem.scratchpad_path()
        if scratchpad_path.exists():
            scratchpad = read_text(scratchpad_path)
            # Extract key sections if they exist
            result_parts.append(f"## L1: Current Context (Scratchpad)\n\n{scratchpad[:3000]}...\n")
    
    # L2: Full history search
    if l2_query:
        chat_path = mem.logs_path("chat.jsonl")
        if chat_path.exists():
            try:
                raw_lines = chat_path.read_text(encoding="utf-8").strip().split("\n")
                entries = []
                search_lower = l2_query.lower()
                
                for line in raw_lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        text = str(entry.get("text", "")).lower()
                        if search_lower in text:
                            entries.append(entry)
                    except Exception:
                        continue
                
                # Return most recent matches
                entries = entries[-l2_limit:]
                
                if entries:
                    l2_lines = []
                    for e in entries:
                        ts = str(e.get("ts", ""))[:16]
                        direction = "→" if str(e.get("direction", "")).lower() in ("out", "outgoing") else "←"
                        text = str(e.get("text", ""))
                        l2_lines.append(f"{direction} [{ts}] {text}")
                    
                    result_parts.append(f"## L2: Search Results for '{l2_query}' ({len(entries)} matches)\n\n" + "\n".join(l2_lines) + "\n")
                else:
                    result_parts.append(f"## L2: Search Results for '{l2_query}'\n\n(no matches found)\n")
            except Exception as e:
                result_parts.append(f"## L2: Search Error\n\n⚠️ {e}\n")
    
    return "\n".join(result_parts)


# ---------------------------------------------------------------------------
# Memory Points Management (L1)
# ---------------------------------------------------------------------------

def _update_memory_point(
    ctx: ToolContext,
    type: Literal["decision", "fact", "todo", "preference", "constraint"],
    content: str,
    action: Literal["add", "update", "remove"] = "add",
    id: Optional[str] = None
) -> str:
    """
    Update L1 memory points (decisions, facts, todos, preferences, constraints).
    
    Args:
        type: Type of memory point
        content: Content of the memory point
        action: add, update, or remove
        id: Optional ID for update/remove operations
    """
    from ouroboros.memory import Memory
    mem = Memory(drive_root=ctx.drive_root)
    
    # Load or initialize memory points
    points_path = mem._memory_path("memory_points.json")
    if points_path.exists():
        try:
            points = json.loads(read_text(points_path))
        except Exception:
            points = {"decisions": [], "facts": [], "todos": [], "preferences": [], "constraints": []}
    else:
        points = {"decisions": [], "facts": [], "todos": [], "preferences": [], "constraints": []}
    
    # Ensure type exists
    if type not in points:
        points[type] = []
    
    timestamp = utc_now_iso()
    
    if action == "add":
        point_id = f"{type}_{len(points[type]) + 1}_{timestamp[:10]}"
        points[type].append({
            "id": point_id,
            "content": content,
            "created_at": timestamp,
            "updated_at": timestamp
        })
        result = f"✅ Added {type}: {content[:100]}..."
        
    elif action == "update":
        if not id:
            return "⚠️ ID required for update action"
        
        found = False
        for i, point in enumerate(points[type]):
            if point.get("id") == id:
                points[type][i]["content"] = content
                points[type][i]["updated_at"] = timestamp
                found = True
                result = f"✅ Updated {type} {id}: {content[:100]}..."
                break
        
        if not found:
            return f"⚠️ {type} with ID {id} not found"
            
    elif action == "remove":
        if not id:
            return "⚠️ ID required for remove action"
        
        original_len = len(points[type])
        points[type] = [p for p in points[type] if p.get("id") != id]
        
        if len(points[type]) < original_len:
            result = f"✅ Removed {type} {id}"
        else:
            return f"⚠️ {type} with ID {id} not found"
    
    else:
        return f"⚠️ Unknown action: {action}"
    
    # Save updated points
    points_path.parent.mkdir(parents=True, exist_ok=True)
    write_text(points_path, json.dumps(points, indent=2, ensure_ascii=False))
    
    return result


# ---------------------------------------------------------------------------
# Get Memory Points
# ---------------------------------------------------------------------------

def _get_memory_points(ctx: ToolContext, type: Optional[str] = None) -> str:
    """
    Retrieve L1 memory points.
    
    Args:
        type: Optional filter by type (decision, fact, todo, preference, constraint)
    """
    from ouroboros.memory import Memory
    mem = Memory(drive_root=ctx.drive_root)
    
    points_path = mem._memory_path("memory_points.json")
    if not points_path.exists():
        return "(no memory points stored)"
    
    try:
        points = json.loads(read_text(points_path))
    except Exception:
        return "⚠️ Error reading memory points"
    
    if type and type in points:
        # Filter by type
        filtered = {type: points[type]}
    else:
        filtered = points
    
    # Format output
    lines = ["## L1 Memory Points\n"]
    
    for point_type, items in filtered.items():
        if items:
            lines.append(f"### {point_type.capitalize()}s ({len(items)})")
            for item in items[-10:]:  # Last 10 per type
                lines.append(f"- [{item.get('id', '?')}] {item.get('content', '')}")
            if len(items) > 10:
                lines.append(f"... and {len(items) - 10} more")
            lines.append("")
    
    if not any(filtered.values()):
        return "(no memory points found)"
    
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool Registration
# ---------------------------------------------------------------------------

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("chat_history_layered", {
            "name": "chat_history_layered",
            "description": (
                "Retrieve chat history in layers (L0 summary, L1 points, L2 full content). "
                "L0: Recent 10 messages (~500 tokens). "
                "L1: Key decision points from scratchpad (~2000 tokens). "
                "L2: Full history with semantic search (on demand). "
                "Use this instead of chat_history for token-efficient context loading."
            ),
            "parameters": {"type": "object", "properties": {
                "l0": {"type": "boolean", "default": True, "description": "Include L0 summary (recent 10 messages)"},
                "l1": {"type": "boolean", "default": True, "description": "Include L1 points (scratchpad context)"},
                "l2_query": {"type": "string", "default": None, "description": "Search query for L2 full history"},
                "l2_limit": {"type": "integer", "default": 5, "description": "Max L2 results to return"},
            }, "required": []},
        }, _chat_history_layered),
        
        ToolEntry("update_memory_point", {
            "name": "update_memory_point",
            "description": (
                "Update L1 memory points (decisions, facts, todos, preferences, constraints). "
                "Use to persistently store key information for future context."
            ),
            "parameters": {"type": "object", "properties": {
                "type": {"type": "string", "enum": ["decision", "fact", "todo", "preference", "constraint"], "description": "Type of memory point"},
                "content": {"type": "string", "description": "Content of the memory point"},
                "action": {"type": "string", "enum": ["add", "update", "remove"], "default": "add", "description": "Action to perform"},
                "id": {"type": "string", "default": None, "description": "ID for update/remove operations"},
            }, "required": ["type", "content"]},
        }, _update_memory_point),
        
        ToolEntry("get_memory_points", {
            "name": "get_memory_points",
            "description": "Retrieve L1 memory points. Optionally filter by type.",
            "parameters": {"type": "object", "properties": {
                "type": {"type": "string", "enum": ["decision", "fact", "todo", "preference", "constraint"], "default": None, "description": "Filter by type"},
            }, "required": []},
        }, _get_memory_points),
    ]
