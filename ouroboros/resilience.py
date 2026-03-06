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
                # Time for a half-open retry
                self.circuit_state = CircuitState.HALF_OPEN
                return True # Allow one request through
            return False
        # If in HALF_OPEN or CLOSED, allow the request if consecutive failures < threshold
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
            "is_available": self.is_available(3, 300.0), # Example threshold/cooldown
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
        self.threshold = 3 # Number of consecutive failures to open circuit
        self.cooldown = 300.0 # Time in seconds to stay open before half-open attempt
    def get_model_health(self, model: str) -> ModelHealth:
        with self.global_lock:\n            if model not in self.models: self.models[model] = ModelHealth()\n            return self.models[model]\n    def is_available(self, model: str) -> bool:\n        return self.get_model_health(model).is_available(self.threshold, self.cooldown)\n    def record_success(self, model: str):\n        self.get_model_health(model).record_success()\n    def record_failure(self, model: str, error: str = \"\"):\n        health = self.get_model_health(model)\n        health.record_failure(error)\n        if health.consecutive_failures >= self.threshold:\n            health.open_circuit(self.cooldown)\n            log.warning(f\"Circuit breaker opened for model {model} after {health.consecutive_failures} failures. Blocked until {health.blocked_until}\")\n\n    def get_model_status(self, model: str) -> Dict[str, Any]:\n        \"\"\"Get status of a specific model for monitoring.\"\"\"\n        return self.get_model_health(model).to_dict()\n\n    def get_all_status(self) -> Dict[str, Dict[str, Any]]:\n        \"\"\"Get status of all tracked models for monitoring.\"\"\"\n        with self.global_lock:\n            return {model: health.to_dict() for model, health in self.models.items()}\n\n    def get_blocked_models(self) -> List[str]:\n        \"\"\"Get list of currently blocked models.\"\"\"\n        with self.global_lock:\n            return [\n                model for model, health in self.models.items()\n                if not health.is_available(self.threshold, self.cooldown)\n            ]\n\nclass GlobalApiHealth:\n    _instance = None\n    _lock = threading.Lock()\n    def __new__(cls):\n        if cls._instance is None:\n            with cls._lock:\n                if cls._instance is None:\n                    cls._instance = super().__new__(cls)\n                    cls._instance._initialized = False\n        return cls._instance\n    def __init__(self):\n        if self._initialized: return\n        self._initialized = True\n        self.global_blocked_until = None\n    def is_globally_blocked(self) -> bool:\n        if self.global_blocked_until and time.time() < self.global_blocked_until: return True\n        return False\n    def record_global_failure(self, errors=None):\n        self.global_blocked_until = time.time() + 60.0\n    def record_failure(self, error: str = \"\"):\n        self.record_global_failure([error])
    def record_success(self):\n        self.global_blocked_until = None

class IterationGuardian:\n    _instance = None\n    _lock = threading.Lock()\n    def __new__(cls):\n        if cls._instance is None:\n            with cls._lock:\n                if cls._instance is None:\n                    cls._instance = super().__new__(cls)\n                    cls._instance._initialized = False\n        return cls._instance
    def __init__(self):\n        if self._initialized: return\n        self._initialized = True\n        self._local = threading.local()\n        self.max_depth = 50\n    def enter_iteration(self):\n        if not hasattr(self._local, 'depth'): self._local.depth = 0\n        self._local.depth += 1\n    def exit_iteration(self):\n        if hasattr(self._local, 'depth'): self._local.depth = max(0, self._local.depth - 1)\n    def get_depth(self) -> int: return getattr(self._local, 'depth', 0)\n    def should_abort(self) -> bool: return self.get_depth() > self.max_depth
    def should_stop(self, round_idx: int, task_type: str) -> bool:\n        return round_idx >= 200

def get_circuit_breaker(): return CircuitBreaker()\ndef get_iteration_guardian(): return IterationGuardian()\ndef get_global_api_health(): return GlobalApiHealth()
