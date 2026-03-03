import logging
import threading
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

class ModelHealth:
    def __init__(self):
        self.success_count = 0
        self.failure_count = 0
        self.last_error = ""
        self.last_attempt = 0.0

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
        self._health = {}
        self._initialized = True
    def is_available(self, model: str) -> bool:
        return True
    def record_success(self, model: str): pass
    def record_failure(self, model: str, error: str = ""): pass

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
    def is_globally_blocked(self) -> bool: return False

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
        self._depth = 0
        self.max_depth = 10
        self._initialized = True
    def enter_iteration(self): self._depth += 1
    def exit_iteration(self): self._depth = max(0, self._depth - 1)
    def get_depth(self) -> int: return self._depth
    def should_abort(self) -> bool: return self._depth > self.max_depth
    def should_stop(self, round_idx: int, task_type: str) -> bool:
        return round_idx >= 200

def get_circuit_breaker(): return CircuitBreaker()
def get_iteration_guardian(): return IterationGuardian()
def get_global_api_health(): return GlobalApiHealth()
