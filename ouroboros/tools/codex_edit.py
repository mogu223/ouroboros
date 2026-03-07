
"""Codex code editing tools: codex_code_edit (primary), claude_code_edit (compat alias)."""

from __future__ import annotations

from typing import List

from ouroboros.tools.registry import ToolContext, ToolEntry


def _unbound_codex_handler(
    ctx: ToolContext,
    prompt: str,
    cwd: str = ".",
    model: str = "",
    effort: str = "high",
) -> str:
    _ = (ctx, prompt, cwd, model, effort)
    return (
        "⚠️ codex_code_edit handler is not bound. "
        "This usually means agent initialization is incomplete."
    )


def get_tools() -> List[ToolEntry]:
    params = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Natural-language coding instruction. The tool will generate and apply an apply_patch payload.",
            },
            "cwd": {
                "type": "string",
                "description": "Optional working directory relative to repo root.",
                "default": ".",
            },
            "model": {
                "type": "string",
                "description": "Optional model override. Defaults to OUROBOROS_CODEX_MODEL or OUROBOROS_MODEL_CODE.",
            },
            "effort": {
                "type": "string",
                "description": "Reasoning effort override (low/medium/high).",
                "default": "high",
            },
        },
        "required": ["prompt"],
    }

    return [
        ToolEntry(
            "codex_code_edit",
            {
                "name": "codex_code_edit",
                "description": "Primary code-editing tool. Uses OpenAI-compatible API to generate an apply_patch payload and applies it to repository files.",
                "parameters": params,
            },
            _unbound_codex_handler,
            is_code_tool=True,
            timeout_sec=240,
        ),
        ToolEntry(
            "claude_code_edit",
            {
                "name": "claude_code_edit",
                "description": "Backward-compatible alias of codex_code_edit. Prefer codex_code_edit in new tasks.",
                "parameters": params,
            },
            _unbound_codex_handler,
            is_code_tool=True,
            timeout_sec=240,
        ),
    ]
