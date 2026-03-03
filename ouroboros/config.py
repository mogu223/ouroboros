"""
Ouroboros — Dynamic configuration loader.

Reads model configuration from /etc/ouroboros.env at runtime,
allowing model switching without service restart.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# Configuration file path
CONFIG_FILE = "/etc/ouroboros.env"

# Cache with TTL
_config_cache: Dict[str, str] = {}
_cache_time: float = 0
CACHE_TTL = 30  # seconds


def _parse_env_file(path: str) -> Dict[str, str]:
    """Parse a .env file and return key-value dict."""
    result = {}
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    result[key.strip()] = value.strip().strip('"\'')
    except Exception as e:
        log.debug("Failed to read config file %s: %s", path, e)
    return result


def get_config(key: str, default: str = "") -> str:
    """
    Get a configuration value, checking config file first, then env.

    The config file is cached with a 30-second TTL to avoid
    excessive file reads while still allowing hot-reloading.
    """
    global _config_cache, _cache_time

    import time
    now = time.time()

    # Refresh cache if expired
    if now - _cache_time > CACHE_TTL:
        _config_cache = _parse_env_file(CONFIG_FILE)
        _cache_time = now
        if _config_cache:
            log.debug("Reloaded config from %s: %d keys", CONFIG_FILE, len(_config_cache))

    # Check config file first
    if key in _config_cache:
        return _config_cache[key]

    # Fall back to environment variable
    return os.environ.get(key, default)


def get_default_model() -> str:
    """Get the default model ID, checking config file first."""
    return get_config("OUROBOROS_MODEL", "glm-5")


def get_fallback_models() -> List[str]:
    """Get the list of fallback models, checking config file first."""
    raw = get_config("OUROBOROS_MODEL_FALLBACK_LIST", "")
    if not raw:
        # Fall back to env
        raw = os.environ.get("OUROBOROS_MODEL_FALLBACK_LIST", "")
    
    models = [m.strip() for m in raw.split(",") if m.strip()]
    return models


def get_all_available_models() -> List[str]:
    """Get all available models (primary + fallbacks + code + light)."""
    models = []
    
    # Primary model
    primary = get_default_model()
    if primary:
        models.append(primary)
    
    # Fallback models
    for m in get_fallback_models():
        if m not in models:
            models.append(m)
    
    # Code model
    code = get_config("OUROBOROS_MODEL_CODE", "")
    if code and code not in models:
        models.append(code)
    
    # Light model
    light = get_config("OUROBOROS_MODEL_LIGHT", "")
    if light and light not in models:
        models.append(light)
    
    return models


def get_openrouter_api_key() -> Optional[str]:
    """Get the OpenRouter API key, checking config file first."""
    return get_config("OPENROUTER_API_KEY") or None


def get_openai_base_url() -> str:
    """Get the OpenAI base URL, checking config file first."""
    return get_config("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")