"""
Resilience module: Circuit breaker and graceful degradation for LLM API failures.

Prevents cascading failures and death during API fluctuations.
"""

import os
import time
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from enum import Enum
import logging

log = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"  # Normal operation
    OPEN = "open"      # Blocked - too many failures
    HALF_OPEN = "half_open"  # Testing if recovered


@dataclass
class ModelHealth:
    """Track health of a single model."""
    consecutive_failures: int = 0
    total_failures: int = 0
    total_successes: int = 0
    last_failure_time: Optional[float] = None
    last_success_time: Optional[float] = None
    circuit_state: CircuitState = CircuitState.CLOSED
    blocked_until: Optional[float] = None
    last_error: str = ""
    
    def record_success(self):
        """Record a successful call."""
        self.consecutive_failures = 0
        self.total_successes += 1
        self.last_success_time = time.time()
        self.circuit_state = CircuitState.CLOSED
        self.blocked_until = None
        self.last_error = ""
    
    def record_failure(self, error: str = ""):
        """Record a failed call."""
        self.consecutive_failures += 1
        self.total_failures += 1
        self.last_failure_time = time.time()
        self.last_error = str(error)[:200]  # Truncate
    
    def is_available(self, threshold: int, cooldown: float) -> bool:
        """Check if model is available for use."""
        if self.circuit_state == CircuitState.OPEN:
            if self.blocked_until and time.time() >= self.blocked_until:
                # Cooldown passed, try again
                self.circuit_state = CircuitState.HALF_OPEN
                return True
            return False
        return self.consecutive_failures < threshold
    
    def open_circuit(self, cooldown: float):
        """Open the circuit breaker."""
        self.circuit_state = CircuitState.OPEN
        self.blocked_until = time.time() + cooldown


class CircuitBreaker:
    """
    Singleton circuit breaker for all LLM models.
    
    Prevents cascading failures by temporarily blocking models that are failing.
    """
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.models: Dict[str, ModelHealth] = {}
        self.global_lock = threading.Lock()
        self.threshold = int(os.environ.get("OUROBOROS_CIRCUIT_BREAKER_THRESHOLD", "3"))
        self.cooldown = float(os.environ.get("OUROBOROS_CIRCUIT_BREAKER_COOLDOWN", "300.0"))
        log.info(f"CircuitBreaker initialized: threshold={self.threshold}, cooldown={self.cooldown}s")
    
    def get_model_health(self, model: str) -> ModelHealth:
        """Get or create health tracker for a model."""
        with self.global_lock:
            if model not in self.models:
                self.models[model] = ModelHealth()
            return self.models[model]
    
    def is_available(self, model: str) -> bool:
        """Check if a model is available."""
        return self.get_model_health(model).is_available(self.threshold, self.cooldown)
    
    def record_success(self, model: str):
        """Record a successful API call."""
        health = self.get_model_health(model)
        with self.global_lock:
            health.record_success()
        log.debug(f"CircuitBreaker: {model} success, circuit={health.circuit_state.value}")
    
    def record_failure(self, model: str, error: str = ""):
        """Record a failed API call."""
        health = self.get_model_health(model)
        with self.global_lock:
            health.record_failure(error)
            if health.consecutive_failures >= self.threshold:
                health.open_circuit(self.cooldown)
                log.warning(f"CircuitBreaker: {model} OPENED after {health.consecutive_failures} failures: {error[:100]}")
    
    def get_available_models(self, models: List[str]) -> List[str]:
        """Filter to only available models."""
        return [m for m in models if self.is_available(m)]
    
    def get_status(self) -> Dict:
        """Get current status of all models."""
        with self.global_lock:
            return {
                m: {
                    "state": h.circuit_state.value,
                    "consecutive_failures": h.consecutive_failures,
                    "total_failures": h.total_failures,
                    "total_successes": h.total_successes,
                    "last_error": h.last_error[:100] if h.last_error else None
                }
                for m, h in self.models.items()
            }
    
    def reset(self, model: str = None):
        """Reset circuit breaker state."""
        with self.global_lock:
            if model:
                if model in self.models:
                    self.models[model] = ModelHealth()
                    log.info(f"CircuitBreaker: {model} reset")
            else:
                self.models.clear()
                log.info("CircuitBreaker: all models reset")


class IterationGuardian:
    """
    Singleton that tracks iteration depth to prevent infinite loops.
    
    Used during self-modification to ensure graceful degradation if
    LLM becomes unavailable mid-iteration.
    """
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.iteration_depth = 0
        self.max_depth = int(os.environ.get("OUROBOROS_MAX_ITERATION_DEPTH", "50"))
        self._local = threading.local()
    
    def enter_iteration(self):
        """Called at start of each tool iteration."""
        if not hasattr(self._local, 'depth'):
            self._local.depth = 0
        self._local.depth += 1
    
    def exit_iteration(self):
        """Called at end of each tool iteration."""
        if hasattr(self._local, 'depth'):
            self._local.depth = max(0, self._local.depth - 1)
    
    def is_in_critical_iteration(self) -> bool:
        """Check if we're in a multi-step iteration."""
        return getattr(self._local, 'depth', 0) > 0
    
    def get_depth(self) -> int:
        """Get current iteration depth."""
        return getattr(self._local, 'depth', 0)
    
    def should_abort(self) -> bool:
        """Check if we should abort due to depth."""
        return self.get_depth() > self.max_depth


# Global accessors
_circuit_breaker = None
_iteration_guardian = None


def get_circuit_breaker() -> CircuitBreaker:
    """Get the global circuit breaker instance."""
    global _circuit_breaker
    if _circuit_breaker is None:
        _circuit_breaker = CircuitBreaker()
    return _circuit_breaker


def get_iteration_guardian() -> IterationGuardian:
    """Get the global iteration guardian instance."""
    global _iteration_guardian
    if _iteration_guardian is None:
        _iteration_guardian = IterationGuardian()
    return _iteration_guardian