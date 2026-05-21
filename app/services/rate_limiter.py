from __future__ import annotations
import time
from collections import defaultdict, deque

_windows: dict[int, deque[float]] = defaultdict(lambda: deque())


def is_rate_limited(user_id: int, max_requests: int = 10, window_seconds: float = 10.0) -> bool:
    """Return True if user has exceeded rate limit. Thread/async safe for single event loop."""
    now = time.monotonic()
    window = _windows[user_id]
    while window and window[0] < now - window_seconds:
        window.popleft()
    if not window:
        _windows.pop(user_id, None)
    if len(window) >= max_requests:
        return True
    window.append(now)
    return False


def clear_user(user_id: int) -> None:
    _windows.pop(user_id, None)
