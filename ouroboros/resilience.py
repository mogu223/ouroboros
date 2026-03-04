import os
import time
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from enum import Enum
import logging

log = logging.getLogger(__name__)

class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

@dataclass
class ModelHealth:
    consecutive_failures: int = 0
    total_failures: int = 0
    total_successes: int = 0
    last_failure_time: Optional[float] = None
    last_success_time: Optional[float] = None
    circuit_state: CircuitState = CircuitState.CLOSED
    blocked_until: Optional[float] = None
    last_error: str = ""
    
    def record_success(self):
        self.consecutive_failures = 0
        self.total_successes += 1
        self.last_success_time = time.time()
        self.circuit_state = CircuitState.CLOSED
        self.blocked_until = None
        self.last_error = ""
    
    def record_failure(self, error: str = ""):
        self.consecutive_failures += 1
        self.total_failures += 1
        self.last_failure_time = time.time()
        self.last_error = str(error)[:200]
    
    def is_available(self, threshold: int, cooldown: float) -> bool:
        if self.circuit_state == CircuitState.OPEN:
            if self.blocked_until and time.time() >= self.blocked_until:
                self.circuit_state = CircuitState.HALF_OPEN
                return True
            return False
        return self.consecutive_failures < threshold
    
    def open_circuit(self, cooldown: float):
        self.circuit_state = CircuitState.OPEN
        self.blocked_until = time.time() + cooldown

    def to_dict(self) -> Dict[str, Any]:
        """Serialize model health status for monitoring."""
        return {
            "consecutive_failures": self.consecutive_failures,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
            "last_failure_time": self.last_failure_time,
            "last_success_time": self.last_success_time,
            "circuit_state": self.circuit_state.value,
            "is_available": self.is_available(3, 300.0),
            "blocked_until": self.blocked_until,
            "last_error": self.last_error,
        }

class CircuitBreaker:
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
        if self._initialized: return
        self._initialized = True
        self.models: Dict[str, ModelHealth] = {}
        self.global_lock = threading.Lock()
        self.threshold = 3
        self.cooldown = 300.0
    def get_model_health(self, model: str) -> ModelHealth:
        with self.global_lock:
            if model not in self.models: self.models[model] = ModelHealth()
            return self.models[model]
    def is_available(self, model: str) -> bool:
        return self.get_model_health(model).is_available(self.threshold, self.cooldown)
    def record_success(self, model: str):
        self.get_model_health(model).record_success()
    def record_failure(self, model: str, error: str = ""):
        # Circuit breaker disabled by user request
        pass

    def get_model_status(self, model: str) -> Dict[str, Any]:
        """Get status of a specific model for monitoring."""
        return self.get_model_health(model).to_dict()

    def get_all_status(self) -> Dict[str, Dict[str, Any]]:
        """Get status of all tracked models for monitoring."""
        with self.global_lock:
            return {model: health.to_dict() for model, health in self.models.items()}

    def get_blocked_models(self) -> List[str]:
        """Get list of currently blocked models."""
        with self.global_lock:
            return [
                model for model, health in self.models.items()
                if not health.is_available(self.threshold, self.cooldown)
            ]

class GlobalApiHealth:
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
        if self._initialized: return
        self._initialized = True
        self.global_blocked_until = None
    def is_globally_blocked(self) -> bool:
        if self.global_blocked_until and time.time() < self.global_blocked_until: return True
        return False
    def record_global_failure(self, errors=None):
        self.global_blocked_until = time.time() + 60.0
    def record_failure(self, error: str = ""):
        self.record_global_failure([error])
    def record_success(self):
        self.global_blocked_until = None

class IterationGuardian:
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
        if self._initialized: return
        self._initialized = True
        self._local = threading.local()
        self.max_depth = 50
    def enter_iteration(self):
        if not hasattr(self._local, 'depth'): self._local.depth = 0
        self._local.depth += 1
    def exit_iteration(self):
        if hasattr(self._local, 'depth'): self._local.depth = max(0, self._local.depth - 1)
    def get_depth(self) -> int: return getattr(self._local, 'depth', 0)
    def should_abort(self) -> bool: return self.get_depth() > self.max_depth
    def should_stop(self, round_idx: int, task_type: str) -> bool:
        return round_idx >= 200

def get_circuit_breaker(): return CircuitBreaker()
def get_iteration_guardian(): return IterationGuardian()
def get_global_api_health(): return GlobalApiHealth()