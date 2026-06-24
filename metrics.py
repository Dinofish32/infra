import threading
import time
from collections import deque

_lock = threading.Lock()
_http_requests_total = 0
_request_times: deque[float] = deque()
_WINDOW_SECONDS = 1.0


def record_request() -> None:
    """Increment total requests and track timestamp for rolling RPS."""
    global _http_requests_total
    now = time.monotonic()
    with _lock:
        _http_requests_total += 1
        _request_times.append(now)
        _prune_old(now)


def _prune_old(now: float) -> None:
    cutoff = now - _WINDOW_SECONDS
    while _request_times and _request_times[0] < cutoff:
        _request_times.popleft()


def http_requests_total() -> int:
    with _lock:
        return _http_requests_total


def requests_per_second() -> float:
    """Requests handled in the last second (rolling window)."""
    now = time.monotonic()
    with _lock:
        _prune_old(now)
        return float(len(_request_times))


def render_metrics() -> str:
    total = http_requests_total()
    rps = requests_per_second()
    return (
        f"http_requests_total {total}\n"
        f"http_requests_per_second {rps:.2f}\n"
    )
