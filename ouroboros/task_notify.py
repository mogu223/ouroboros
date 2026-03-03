"""
Task notification and error alerting.

Features:
1. Task start/end notification (direct chat only)
2. Error tracking with alert threshold
"""

import threading
import time
from typing import Optional

# Module-level error tracking
_consecutive_errors: int = 0
_error_lock: threading.Lock = threading.Lock()
ERROR_ALERT_THRESHOLD: int = 3  # Alert after 3 consecutive errors
_last_error_ts: float = 0.0
ERROR_WINDOW_SEC: float = 600.0  # Reset count after 10 min of no errors


def record_error() -> tuple:
    """
    Record an error and check if alert threshold reached.
    
    Returns:
        (should_alert, current_error_count)
    """
    global _consecutive_errors, _last_error_ts
    with _error_lock:
        now = time.time()
        # Reset if too much time passed since last error
        if _last_error_ts > 0 and (now - _last_error_ts) > ERROR_WINDOW_SEC:
            _consecutive_errors = 0
        
        _consecutive_errors += 1
        _last_error_ts = now
        current = _consecutive_errors
        
        if _consecutive_errors >= ERROR_ALERT_THRESHOLD:
            _consecutive_errors = 0  # Reset after alert
            return True, current
    return False, current


def clear_error_count() -> None:
    """Clear error count on successful task completion."""
    global _consecutive_errors, _last_error_ts
    with _error_lock:
        _consecutive_errors = 0
        _last_error_ts = 0.0


def get_error_count() -> int:
    """Get current consecutive error count."""
    with _error_lock:
        return _consecutive_errors
