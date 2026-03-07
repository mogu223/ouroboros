"""Codex code editing tools: codex_code_edit (primary), claude_code_edit (compat alias)."""

from __future__ import annotations

import json
import logging
import os
import pathlib
import subprocess
from typing import Any, List

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.apply_patch import install as install_apply_patch

log = logging.getLogger(__name__)


# Global LLM client - will be set by agent during initialization
_llm_client: Any = None


def set_llm_client(llm) -> None:
    """Set the global LLM client for codex operations."""
    global _llm_client
    _llm_client = llm


def _generate_codex_patch(
    prompt: str,
    model: str,
    effort: str,
) -> str:
    """Generate a patch using LLM."""
    from ouroboros.tools.codex_cli_prompts import GENERATE_CODEX_PATCH_PROMPT
    from ouroboros.llm import LLMClient
    
    llm = _llm_client or LLMClient()
    full_prompt = f"{GENERATE_CODEX_PATCH_PROMPT}\n\nUser request:\n{prompt}"
    
    messages = [{"role": "user", "content": full_prompt}]
    
    response_msg, usage = llm.chat(
        messages=messages,
        model=model,
        tools=None,
        effort=effort,
        max_tokens=4096,
    )
    
    return response_msg.get("content", "")


def _parse_patch_from_response(response: str) -> str:
    """Parse patch JSON from LLM response."""
    import re
    
    # Try to find JSON in response
    json_match = re.search(r'\{[^}]*"patch"[^}]*\}', response, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group())
            return data.get("patch", "")
        except json.JSONDecodeError:
            pass
    
    # Fallback: look for patch markers
    if "*** Begin Patch" in response:
        start = response.find("*** Begin Patch")
        end = response.find("*** End Patch")
        if end > start:
            return response[start:end + len("*** End Patch")]
    
    return ""


def _apply_patch(patch: str, cwd: pathlib.Path) -> str:
    """Apply a patch using the apply_patch script."""
    install_apply_patch()
    
    try:
        result = subprocess.run(
            ["/usr/local/bin/apply_patch"],
            input=patch,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=60,
        )
        if result.returncode == 0:
            return "OK: patch applied successfully"
        else:
            return f"⚠️ apply_patch error: {result.stderr}"
    except subprocess.TimeoutExpired:
        return "⚠️ apply_patch timeout"
    except Exception as e:
        return f"⚠️ apply_patch exception: {e}"


def _codex_code_edit_handler(
    ctx: ToolContext,
    prompt: str,
    cwd: str = ".",
    model: str = "",
    effort: str = "high",
) -> str:
    """Handle codex_code_edit tool calls."""
    from ouroboros.llm import DEFAULT_MODEL
    
    # Determine model
    if not model:
        model = os.environ.get("OUROBOROS_CODEX_MODEL", "") or os.environ.get("OUROBOROS_MODEL_CODE", "") or DEFAULT_MODEL
    
    # Ensure cwd is absolute and within repo
    repo_dir = ctx.repo_dir
    work_dir = (repo_dir / cwd).resolve()
    if not str(work_dir).startswith(str(repo_dir)):
        return "⚠️ cwd must be within repo directory"
    
    try:
        # Generate patch
        patch = _generate_codex_patch(prompt, model, effort)
        
        if not patch:
            return "⚠️ Failed to generate patch from LLM response"
        
        # Parse patch
        patch_content = _parse_patch_from_response(patch)
        
        if not patch_content:
            return f"⚠️ Could not parse patch from response: {patch[:500]}"
        
        # Apply patch
        result = _apply_patch(patch_content, work_dir)
        
        return result
        
    except Exception as e:
        log.exception("codex_code_edit failed")
        return f"⚠️ codex_code_edit error: {e}"


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
            _codex_code_edit_handler,
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
            _codex_code_edit_handler,
            is_code_tool=True,
            timeout_sec=240,
        ),
    ]

