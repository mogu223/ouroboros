"""
Ouroboros — Model Router for intelligent model selection and rate limit avoidance.

Features:
- Model pool management
- Smart model selection based on task complexity
- Rate limit awareness and automatic switching
- Cost optimization (cheap models for simple tasks)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict
import threading

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model Categories
# ---------------------------------------------------------------------------

@dataclass
class ModelInfo:
    """Information about a model."""
    model_id: str
    category: str  # "power", "balanced", "light", "code"
    cost_per_1m: float  # Approximate cost per 1M tokens (input + output avg)
    max_rpm: int  # Max requests per minute (estimated)
    capabilities: List[str] = field(default_factory=list)  # ["vision", "reasoning", "coding"]


# Predefined model catalog (can be extended via config)
MODEL_CATALOG: Dict[str, ModelInfo] = {
    # Power models (highest quality, highest cost)
    "anthropic/claude-opus-4.6": ModelInfo(
        model_id="anthropic/claude-opus-4.6",
        category="power",
        cost_per_1m=15.0,
        max_rpm=60,
        capabilities=["reasoning", "coding", "vision"],
    ),
    "openai/o3": ModelInfo(
        model_id="openai/o3",
        category="power",
        cost_per_1m=5.0,
        max_rpm=60,
        capabilities=["reasoning", "coding"],
    ),
    "openai/gpt-5.2": ModelInfo(
        model_id="openai/gpt-5.2",
        category="power",
        cost_per_1m=3.0,
        max_rpm=100,
        capabilities=["reasoning", "coding", "vision"],
    ),
    
    # Balanced models (good quality, moderate cost)
    "anthropic/claude-sonnet-4.5": ModelInfo(
        model_id="anthropic/claude-sonnet-4.5",
        category="balanced",
        cost_per_1m=3.0,
        max_rpm=100,
        capabilities=["reasoning", "coding", "vision"],
    ),
    "glm-5": ModelInfo(
        model_id="glm-5",
        category="balanced",
        cost_per_1m=1.5,
        max_rpm=120,
        capabilities=["reasoning", "coding"],
    ),
    "google/gemini-3-pro-preview": ModelInfo(
        model_id="google/gemini-3-pro-preview",
        category="balanced",
        cost_per_1m=3.0,
        max_rpm=80,
        capabilities=["reasoning", "coding", "vision"],
    ),
    
    # Light models (fast, cheap, simple tasks)
    "gemini-2.5-flash-lite": ModelInfo(
        model_id="gemini-2.5-flash-lite",
        category="light",
        cost_per_1m=0.15,
        max_rpm=300,
        capabilities=["reasoning"],
    ),
    "x-ai/grok-3-mini": ModelInfo(
        model_id="x-ai/grok-3-mini",
        category="light",
        cost_per_1m=0.40,
        max_rpm=200,
        capabilities=["reasoning"],
    ),
    "qwen3.5-plus": ModelInfo(
        model_id="qwen3.5-plus",
        category="light",
        cost_per_1m=0.50,
        max_rpm=150,
        capabilities=["reasoning", "coding"],
    ),
    
    # Code-specialized models
    "openai/gpt-5.2-codex": ModelInfo(
        model_id="openai/gpt-5.2-codex",
        category="code",
        cost_per_1m=3.0,
        max_rpm=100,
        capabilities=["coding"],
    ),
}


# Task type to category mapping
TASK_CATEGORY_MAP = {
    # High complexity -> power models
    "evolution": "power",
    "review": "power",
    "code": "code",
    
    # Medium complexity -> balanced models
    "task": "balanced",
    "analysis": "balanced",
    "research": "balanced",
    
    # Low complexity -> light models
    "message": "light",
    "chat": "light",
    "simple": "light",
}


# ---------------------------------------------------------------------------
# Rate Limit Tracker
# ---------------------------------------------------------------------------

@dataclass
class RateLimitState:
    """Track rate limit state for a model."""
    requests_last_minute: int = 0
    last_request_time: float = 0.0
    consecutive_failures: int = 0
    last_failure_time: float = 0.0
    cooldown_until: float = 0.0  # Timestamp when model becomes available again
    
    def can_use(self, current_time: float) -> bool:
        """Check if model is available for use."""
        if current_time < self.cooldown_until:
            return False
        if self.consecutive_failures >= 5:
            return False  # Too many failures, avoid this model
        return True
    
    def record_success(self, current_time: float):
        """Record a successful request."""
        self.requests_last_minute += 1
        self.last_request_time = current_time
        self.consecutive_failures = max(0, self.consecutive_failures - 1)
        
        # Decay request count over time (simple sliding window)
        if current_time - self.last_request_time > 60:
            self.requests_last_minute = 1
    
    def record_failure(self, current_time: float, rate_limited: bool = False):
        """Record a failed request."""
        self.consecutive_failures += 1
        self.last_failure_time = current_time
        
        if rate_limited:
            # Rate limited - cooldown for 60 seconds
            self.cooldown_until = current_time + 60.0
            log.warning("Model rate limited, cooldown until %s", self.cooldown_until)
        elif self.consecutive_failures >= 3:
            # Multiple failures - cooldown for 30 seconds
            self.cooldown_until = current_time + 30.0
            log.warning("Model has %d consecutive failures, cooldown until %s", 
                       self.consecutive_failures, self.cooldown_until)
    
    def reset(self):
        """Reset state (e.g., after successful switch)."""
        self.requests_last_minute = 0
        self.consecutive_failures = 0
        self.cooldown_until = 0.0


# ---------------------------------------------------------------------------
# Model Router
# ---------------------------------------------------------------------------

class ModelRouter:
    """
    Intelligent model router with rate limit awareness.
    
    Usage:
        router = ModelRouter()
        model = router.select_model(task_type="task", complexity="medium")
        # ... use model for LLM call ...
        router.record_success(model)  # or router.record_failure(model, rate_limited=True)
    """
    
    def __init__(self, model_pool: Optional[List[str]] = None):
        """
        Initialize model router.
        
        Args:
            model_pool: List of model IDs to use. If None, uses all available models.
        """
        self._lock = threading.Lock()
        self._rate_limits: Dict[str, RateLimitState] = defaultdict(RateLimitState)
        
        # Use provided pool or all models
        if model_pool:
            self.model_pool = [m for m in model_pool if m in MODEL_CATALOG]
        else:
            self.model_pool = list(MODEL_CATALOG.keys())
        
        log.info("ModelRouter initialized with %d models: %s", 
                len(self.model_pool), self.model_pool)
    
    def _get_model_info(self, model_id: str) -> Optional[ModelInfo]:
        """Get model info from catalog."""
        return MODEL_CATALOG.get(model_id)
    
    def select_model(
        self,
        task_type: str = "task",
        complexity: str = "medium",
        prefer_vision: bool = False,
        prefer_coding: bool = False,
        budget_conscious: bool = False,
    ) -> str:
        """
        Select the best model for the given task.
        
        Args:
            task_type: Type of task ("evolution", "review", "code", "task", "message", etc.)
            complexity: Task complexity ("low", "medium", "high")
            prefer_vision: Prefer models with vision capability
            prefer_coding: Prefer models with coding capability
            budget_conscious: Prefer cheaper models when possible
        
        Returns:
            Selected model ID
        """
        with self._lock:
            current_time = time.time()
            
            # Determine target category
            category = TASK_CATEGORY_MAP.get(task_type, "balanced")
            
            # Adjust for complexity
            if complexity == "high" and category in ("balanced", "light"):
                category = "power" if task_type != "code" else "code"
            elif complexity == "low" and budget_conscious:
                category = "light"
            
            # Adjust for special requirements
            if prefer_coding and category != "code":
                category = "code"
            
            # Get candidate models in target category
            candidates = []
            for model_id in self.model_pool:
                info = self._get_model_info(model_id)
                if not info:
                    continue
                
                # Check category match
                if info.category != category:
                    continue
                
                # Check availability (rate limit, failures)
                state = self._rate_limits[model_id]
                if not state.can_use(current_time):
                    continue
                
                # Check capabilities
                if prefer_vision and "vision" not in info.capabilities:
                    continue
                
                candidates.append((model_id, info))
            
            # If no candidates in target category, try fallback categories
            if not candidates:
                fallback_order = ["balanced", "light", "power", "code"]
                for fallback_cat in fallback_order:
                    if fallback_cat == category:
                        continue
                    for model_id in self.model_pool:
                        info = self._get_model_info(model_id)
                        if info and info.category == fallback_cat:
                            state = self._rate_limits[model_id]
                            if state.can_use(current_time):
                                if prefer_vision and "vision" not in info.capabilities:
                                    continue
                                candidates.append((model_id, info))
                    if candidates:
                        break
            
            # If still no candidates, use any available model
            if not candidates:
                for model_id in self.model_pool:
                    state = self._rate_limits[model_id]
                    if state.can_use(current_time):
                        info = self._get_model_info(model_id)
                        if info:
                            candidates.append((model_id, info))
            
            # Select best candidate
            if not candidates:
                # All models are rate-limited or unavailable - return first model anyway
                log.warning("All models are rate-limited, using first available model")
                return self.model_pool[0] if self.model_pool else "glm-5"
            
            # Sort by cost (if budget conscious) or by capability match
            if budget_conscious:
                candidates.sort(key=lambda x: x[1].cost_per_1m)
            else:
                # Prefer models with more capabilities
                candidates.sort(key=lambda x: (-len(x[1].capabilities), x[1].cost_per_1m))
            
            selected = candidates[0][0]
            log.debug("Selected model: %s (category=%s, task=%s)", 
                     selected, MODEL_CATALOG[selected].category, task_type)
            return selected
    
    def record_success(self, model_id: str):
        """Record a successful LLM call."""
        with self._lock:
            state = self._rate_limits[model_id]
            state.record_success(time.time())
    
    def record_failure(self, model_id: str, rate_limited: bool = False):
        """Record a failed LLM call."""
        with self._lock:
            state = self._rate_limits[model_id]
            state.record_failure(time.time(), rate_limited)
    
    def get_model_status(self) -> Dict[str, Dict[str, Any]]:
        """Get status of all models."""
        with self._lock:
            current_time = time.time()
            status = {}
            for model_id in self.model_pool:
                info = self._get_model_info(model_id)
                state = self._rate_limits[model_id]
                status[model_id] = {
                    "category": info.category if info else "unknown",
                    "available": state.can_use(current_time),
                    "requests_last_minute": state.requests_last_minute,
                    "consecutive_failures": state.consecutive_failures,
                    "cooldown_remaining": max(0, state.cooldown_until - current_time),
                    "cost_per_1m": info.cost_per_1m if info else 0,
                }
            return status
    
    def reset_model(self, model_id: str):
        """Reset rate limit state for a model (e.g., after manual intervention)."""
        with self._lock:
            state = self._rate_limits[model_id]
            state.reset()
            log.info("Reset rate limit state for model: %s", model_id)


# ---------------------------------------------------------------------------
# Global Router Instance
# ---------------------------------------------------------------------------

_global_router: Optional[ModelRouter] = None
_router_lock = threading.Lock()


def get_model_router(model_pool: Optional[List[str]] = None) -> ModelRouter:
    """Get or create the global model router instance."""
    global _global_router
    
    with _router_lock:
        if _global_router is None:
            _global_router = ModelRouter(model_pool)
        return _global_router


def reset_model_router(model_pool: Optional[List[str]] = None):
    """Reset the global router (e.g., for testing or reconfiguration)."""
    global _global_router
    with _router_lock:
        _global_router = ModelRouter(model_pool) if model_pool else None
