"""Web search tool with Responses API primary path and HTTP fallback."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List
from urllib.parse import quote_plus

import requests

from ouroboros.config import get_openai_api_key, get_openai_base_url
from ouroboros.tools.registry import ToolContext, ToolEntry

_DEFAULT_FALLBACK_TEMPLATE = "https://r.jina.ai/http://https://www.bing.com/search?q={query}"


def _extract_response_text(payload: Dict[str, Any]) -> str:
    text_parts: List[str] = []
    for item in payload.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for block in item.get("content", []) or []:
            if block.get("type") in ("output_text", "text"):
                txt = str(block.get("text") or "").strip()
                if txt:
                    text_parts.append(txt)
    return "\n\n".join(text_parts).strip()


def _extract_sources(text: str, limit: int = 8) -> List[Dict[str, str]]:
    sources: List[Dict[str, str]] = []
    seen = set()
    for title, url in re.findall(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", text or ""):
        clean_title = " ".join(str(title).split())[:200]
        clean_url = str(url).strip()
        if not clean_title or not clean_url or clean_url in seen:
            continue
        seen.add(clean_url)
        sources.append({"title": clean_title, "url": clean_url})
        if len(sources) >= limit:
            break
    return sources


def _fallback_web_search(query: str) -> Dict[str, Any]:
    template = str(os.environ.get("OUROBOROS_WEBSEARCH_FALLBACK_URL", _DEFAULT_FALLBACK_TEMPLATE) or "").strip()
    if not template:
        template = _DEFAULT_FALLBACK_TEMPLATE
    url = template.format(query=quote_plus(query))
    resp = requests.get(
        url,
        timeout=25,
        headers={"User-Agent": "Mozilla/5.0 (Ouroboros web_search fallback)"},
    )
    resp.raise_for_status()
    body = str(resp.text or "").strip()
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    answer = "\n".join(lines[:40]).strip()[:6000]
    return {
        "answer": answer or "(no answer)",
        "sources": _extract_sources(body),
        "fallback": "bing-via-r.jina.ai",
    }


def _web_search(ctx: ToolContext, query: str) -> str:
    _ = ctx
    api_key = get_openai_api_key() or os.environ.get("OPENAI_API_KEY", "")
    base_url = get_openai_base_url()
    upstream_error = ""

    if api_key:
        try:
            from openai import OpenAI

            client = OpenAI(api_key=api_key, base_url=base_url, timeout=30.0)
            resp = client.responses.create(
                model=os.environ.get("OUROBOROS_WEBSEARCH_MODEL", "gpt-5"),
                tools=[{"type": "web_search"}],
                tool_choice="auto",
                input=query,
            )
            payload = resp.model_dump()
            answer = _extract_response_text(payload)
            if answer:
                return json.dumps(
                    {
                        "answer": answer,
                        "sources": _extract_sources(answer),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            upstream_error = "empty responses payload"
        except Exception as e:
            upstream_error = repr(e)
    else:
        upstream_error = "OPENAI_API_KEY not set"

    try:
        fallback = _fallback_web_search(query)
        if upstream_error:
            fallback["upstream_error"] = upstream_error
        return json.dumps(fallback, ensure_ascii=False, indent=2)
    except Exception as e:
        payload: Dict[str, Any] = {"error": repr(e)}
        if upstream_error:
            payload["upstream_error"] = upstream_error
        return json.dumps(payload, ensure_ascii=False)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("web_search", {
            "name": "web_search",
            "description": "Search the web via OpenAI Responses API, with HTTP fallback when the upstream gateway does not support /responses.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string"},
            }, "required": ["query"]},
        }, _web_search),
    ]
