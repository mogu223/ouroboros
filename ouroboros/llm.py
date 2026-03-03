"""
Ouroboros — LLM client.

The only module that communicates with the LLM API (OpenRouter/CLIProxyAPI).
Contract: chat(), default_model(), available_models(), add_usage().

Optimized for CLIProxyAPI compatibility:
- Handles reasoning_content from thinking models (Qwen3)
- Supports model aliases
- Local cost estimation when API doesn't return pricing
- Disables thinking mode by default for stability
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.config import (
    get_config,
    get_default_model,
    get_fallback_models,
    get_all_available_models,
    get_openrouter_api_key,
    get_openai_base_url,
)

log = logging.getLogger(__name__)

DEFAULT_LIGHT_MODEL = "glm-5"

# CLIProxyAPI model aliases mapping (alias -> canonical name)
# Used for pricing lookup and model identification
MODEL_ALIASES = {
    "kimi-k2": "moonshotai/kimi-k2:free",
    "kimi-k2.5": "moonshotai/kimi-k2.5",
    "qwen3.5-plus": "qwen/qwen3.5-plus",
    "glm-5": "zhipu/glm-5",
    "MiniMax-M2.5": "minimax/MiniMax-M2.5",
    "gemini-2.5-flash-lite": "google/gemini-2.5-flash-lite",
}

# Models known to use "thinking" mode by default (Qwen3 series, etc.)
# These may return content in reasoning_content instead of content
THINKING_MODELS = frozenset({
    "qwen3", "qwen3.5", "qwen3-235b", "qwen3.5-plus", "qwen3.5-turbo",
    "deepseek-r1", "deepseek-v3",
})

# Pricing for local cost estimation (input_per_1m, cached_per_1m, output_per_1m)
# Used when API doesn't return cost (CLIProxyAPI case)
LOCAL_PRICING = {
    "glm-5": (1.25, 0.125, 10.0),
    "qwen3.5-plus": (0.40, 0.04, 2.40),
    "kimi-k2.5": (0.50, 0.05, 3.0),
    "MiniMax-M2.5": (0.60, 0.06, 3.5),
    "gemini-2.5-flash-lite": (0.10, 0.01, 0.50),
    # Canonical names
    "qwen/qwen3.5-plus": (0.40, 0.04, 2.40),
    "moonshotai/kimi-k2.5": (0.50, 0.05, 3.0),
    "zhipu/glm-5": (1.25, 0.125, 10.0),
    "minimax/MiniMax-M2.5": (0.60, 0.06, 3.5),
    "google/gemini-2.5-flash-lite": (0.10, 0.01, 0.50),
}


def normalize_reasoning_effort(value: str, default: str = "medium") -> str:
    allowed = {"none", "minimal", "low", "medium", "high", "xhigh"}
    v = str(value or "").strip().lower()
    return v if v in allowed else default


def reasoning_rank(value: str) -> int:
    order = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5}
    return int(order.get(str(value or "").strip().lower(), 3))


def add_usage(total: Dict[str, Any], usage: Dict[str, Any]) -> None:
    """Accumulate usage from one LLM call into a running total."""
    for k in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "cache_write_tokens"):
        total[k] = int(total.get(k) or 0) + int(usage.get(k) or 0)
    if usage.get("cost"):
        total["cost"] = float(total.get("cost") or 0) + float(usage["cost"])


def resolve_model_alias(model: str) -> str:
    """
    Resolve model alias to canonical name.
    Returns original model if not an alias.
    """
    return MODEL_ALIASES.get(model, model)


def is_thinking_model(model: str) -> bool:
    """Check if model is known to use thinking mode by default."""
    model_lower = model.lower()
    return any(tm in model_lower for tm in THINKING_MODELS)


def estimate_cost_local(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
) -> float:
    """
    Estimate cost using local pricing table.
    Returns 0.0 if model not found.
    """
    # Try exact match first
    pricing = LOCAL_PRICING.get(model)
    if not pricing:
        # Try alias resolution
        canonical = resolve_model_alias(model)
        pricing = LOCAL_PRICING.get(canonical)
    if not pricing:
        # Try prefix match
        for key, val in LOCAL_PRICING.items():
            if model.lower().startswith(key.lower()) or key.lower().startswith(model.lower()):
                pricing = val
                break
    if not pricing:
        return 0.0
    
    input_price, cached_price, output_price = pricing
    regular_input = max(0, prompt_tokens - cached_tokens)
    cost = (
        regular_input * input_price / 1_000_000
        + cached_tokens * cached_price / 1_000_000
        + completion_tokens * output_price / 1_000_000
    )
    return round(cost, 6)


def fetch_openrouter_pricing() -> Dict[str, Tuple[float, float, float]]:
    """
    Fetch current pricing from OpenRouter API.

    Returns dict of {model_id: (input_per_1m, cached_per_1m, output_per_1m)}.
    Returns empty dict on failure.
    """
    import logging
    log = logging.getLogger("ouroboros.llm")

    try:
        import requests
    except ImportError:
        log.warning("requests not installed, cannot fetch pricing")
        return {}

    try:
        url = "https://openrouter.ai/api/v1/models"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()

        data = resp.json()
        models = data.get("data", [])

        # Prefixes we care about
        prefixes = ("anthropic/", "openai/", "google/", "meta-llama/", "x-ai/", "qwen/")

        pricing_dict = {}
        for model in models:
            model_id = model.get("id", "")
            if not model_id.startswith(prefixes):
                continue

            pricing = model.get("pricing", {})
            if not pricing or not pricing.get("prompt"):
                continue

            # OpenRouter pricing is in dollars per token (raw values)
            raw_prompt = float(pricing.get("prompt", 0))
            raw_completion = float(pricing.get("completion", 0))
            raw_cached_str = pricing.get("input_cache_read")
            raw_cached = float(raw_cached_str) if raw_cached_str else None

            # Convert to per-million tokens
            prompt_price = round(raw_prompt * 1_000_000, 4)
            completion_price = round(raw_completion * 1_000_000, 4)
            if raw_cached is not None:
                cached_price = round(raw_cached * 1_000_000, 4)
            else:
                cached_price = round(prompt_price * 0.1, 4)  # fallback: 10% of prompt

            # Sanity check: skip obviously wrong prices
            if prompt_price > 1000 or completion_price > 1000:
                log.warning(f"Skipping {model_id}: prices seem wrong (prompt={prompt_price}, completion={completion_price})")
                continue

            pricing_dict[model_id] = (prompt_price, cached_price, completion_price)

        log.info(f"Fetched pricing for {len(pricing_dict)} models from OpenRouter")
        return pricing_dict

    except (requests.RequestException, ValueError, KeyError) as e:
        log.warning(f"Failed to fetch OpenRouter pricing: {e}")
        return {}


class LLMClient:
    """OpenRouter/CLIProxyAPI wrapper. All LLM calls go through this class."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://openrouter.ai/api/v1",
        model: Optional[str] = None,
        effort: str = "medium",
    ):
        from ouroboros.config import get_default_model
        self.model = model or get_default_model()
        self.effort = effort
        # Use dynamic config for API key and base URL
        self._api_key = api_key or get_openrouter_api_key() or os.environ.get("OPENROUTER_API_KEY", "")
        self._base_url = get_openai_base_url() or os.environ.get("OPENAI_BASE_URL", base_url)
        self._client = None
        # Detect if we're using CLIProxyAPI (not OpenRouter directly)
        self._is_cliproxy = "8317" in self._base_url or "cliproxy" in self._base_url.lower()

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(
                base_url=self._base_url,
                api_key=self._api_key,
                default_headers={
                    "HTTP-Referer": "https://colab.research.google.com/",
                    "X-Title": "Ouroboros",
                },
            )
        return self._client

    def _fetch_generation_cost(self, generation_id: str) -> Optional[float]:
        """Fetch cost from OpenRouter Generation API as fallback."""
        # Skip if using CLIProxyAPI (doesn't support this endpoint)
        if self._is_cliproxy:
            return None
        try:
            import requests
            url = f"{self._base_url.rstrip('/')}/generation?id={generation_id}"
            resp = requests.get(url, headers={"Authorization": f"Bearer {self._api_key}"}, timeout=5)
            if resp.status_code == 200:
                data = resp.json().get("data") or {}
                cost = data.get("total_cost") or data.get("usage", {}).get("cost")
                if cost is not None:
                    return float(cost)
            # Generation might not be ready yet — retry once after short delay
            time.sleep(0.5)
            resp = requests.get(url, headers={"Authorization": f"Bearer {self._api_key}"}, timeout=5)
            if resp.status_code == 200:
                data = resp.json().get("data") or {}
                cost = data.get("total_cost") or data.get("usage", {}).get("cost")
                if cost is not None:
                    return float(cost)
        except Exception:
            log.debug("Failed to fetch generation cost from OpenRouter", exc_info=True)
            pass
        return None

    def call(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        effort: str = "medium",
        max_tokens: int = 16384,
        tool_choice: str = "auto",
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Single LLM call. Returns: (response_message_dict, usage_dict with cost).
        
        Optimized for CLIProxyAPI:
        - Disables thinking mode by default for stability
        - Handles reasoning_content from thinking models
        - Estimates cost locally when API doesn't provide it
        """
        client = self._get_client()
        effort = normalize_reasoning_effort(effort)

        # Build extra_body for CLIProxyAPI compatibility
        # Key: disable thinking mode to prevent empty content responses
        extra_body: Dict[str, Any] = {}
        
        if self._is_cliproxy:
            # Disable thinking mode for thinking models (Qwen3, DeepSeek-R1, etc.)
            # This ensures content is in the standard 'content' field, not 'reasoning_content'
            if is_thinking_model(model):
                extra_body["enable_thinking"] = False
                log.debug(f"Disabled thinking mode for model: {model}")
        else:
            # OpenRouter-specific settings
            extra_body["reasoning"] = {"effort": effort, "exclude": True}
            
            # Pin Anthropic models to Anthropic provider for prompt caching
            if model.startswith("anthropic/"):
                extra_body["provider"] = {
                    "order": ["Anthropic"],
                    "allow_fallbacks": False,
                    "require_parameters": True,
                }

        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        
        # Only add extra_body if we have settings
        if extra_body:
            kwargs["extra_body"] = extra_body
            
        if tools:
            # Add cache_control to last tool for Anthropic prompt caching
            # This caches all tool schemas (they never change between calls)
            tools_with_cache = [t for t in tools]  # shallow copy
            if tools_with_cache:
                last_tool = {**tools_with_cache[-1]}  # copy last tool
                last_tool["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
                tools_with_cache[-1] = last_tool
            kwargs["tools"] = tools_with_cache
            kwargs["tool_choice"] = tool_choice

        resp = client.chat.completions.create(**kwargs)
        resp_dict = resp.model_dump()
        usage = resp_dict.get("usage") or {}
        choices = resp_dict.get("choices") or [{}]
        msg = (choices[0] if choices else {}).get("message") or {}

        # === CLIProxyAPI compatibility: handle reasoning_content ===
        # Qwen3 thinking models may return content in reasoning_content instead of content
        content = msg.get("content")
        reasoning_content = msg.get("reasoning_content")
        
        if not content or not content.strip():
            # Empty content - check if reasoning_content has the actual response
            if reasoning_content and reasoning_content.strip():
                log.debug(f"Model {model} returned reasoning_content instead of content, using it")
                msg["content"] = reasoning_content
                # Optionally prefix to indicate it was from reasoning
                # msg["content"] = f"[Reasoning]\n{reasoning_content}"
            else:
                # Both empty - this is a true empty response
                log.warning(f"Model {model} returned empty content and reasoning_content")

        # Extract cached_tokens from prompt_tokens_details if available
        if not usage.get("cached_tokens"):
            prompt_details = usage.get("prompt_tokens_details") or {}
            if isinstance(prompt_details, dict) and prompt_details.get("cached_tokens"):
                usage["cached_tokens"] = int(prompt_details["cached_tokens"])

        # Extract cache_write_tokens from prompt_tokens_details if available
        # OpenRouter: "cache_write_tokens"
        # Native Anthropic: "cache_creation_tokens" or "cache_creation_input_tokens"
        if not usage.get("cache_write_tokens"):
            prompt_details_for_write = usage.get("prompt_tokens_details") or {}
            if isinstance(prompt_details_for_write, dict):
                cache_write = (prompt_details_for_write.get("cache_write_tokens")
                              or prompt_details_for_write.get("cache_creation_tokens")
                              or prompt_details_for_write.get("cache_creation_input_tokens"))
                if cache_write:
                    usage["cache_write_tokens"] = int(cache_write)

        # Ensure cost is present in usage
        if not usage.get("cost"):
            # First try OpenRouter generation API (if not CLIProxyAPI)
            if not self._is_cliproxy:
                gen_id = resp_dict.get("id") or ""
                if gen_id:
                    cost = self._fetch_generation_cost(gen_id)
                    if cost is not None:
                        usage["cost"] = cost
            
            # Fallback: estimate cost locally
            if not usage.get("cost") and usage.get("prompt_tokens"):
                estimated = estimate_cost_local(
                    model,
                    usage.get("prompt_tokens", 0),
                    usage.get("completion_tokens", 0),
                    usage.get("cached_tokens", 0),
                )
                if estimated > 0:
                    usage["cost"] = estimated
                    log.debug(f"Estimated cost locally for {model}: ${estimated:.6f}")

        return msg, usage

    def vision_query(
        self,
        prompt: str,
        images: List[Dict[str, Any]],
        model: str = "glm-5",
        max_tokens: int = 1024,
        effort: str = "low",
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Send a vision query to an LLM. Lightweight — no tools, no loop.

        Args:
            prompt: Text instruction for the model
            images: List of image dicts. Each dict must have either:
                - {"url": "https://..."} — for URL images
                - {"base64": "<b64>", "mime": "image/png"} — for base64 images
            model: VLM-capable model ID
            max_tokens: Max response tokens
            effort: Effort level

        Returns:
            (text_response, usage_dict)
        """
        # Build multipart content
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img in images:
            if "url" in img:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": img["url"]},
                })
            elif "base64" in img:
                mime = img.get("mime", "image/png")
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{img['base64']}"},
                })
            else:
                log.warning("vision_query: skipping image with unknown format: %s", list(img.keys()))

        messages = [{"role": "user", "content": content}]
        response_msg, usage = self.chat(
            messages=messages,
            model=model,
            tools=None,
            effort=effort,
            max_tokens=max_tokens,
        )
        text = response_msg.get("content") or ""
        return text, usage

    def default_model(self) -> str:
        """Return the default model, checking config file first for hot-reload."""
        return get_default_model()

    def available_models(self) -> List[str]:
        """Return all available models (for switch_model tool schema)."""
        return get_all_available_models()