import threading
import time
from collections import deque

_lock = threading.Lock()

_http_requests_total = 0
_http_failures_total = 0
_active_connections = 0

_request_times: deque[float] = deque()
_latencies_ms: deque[float] = deque(maxlen=1000)

_WINDOW_SECONDS = 1.0


def record_request() -> None:
    """Increment total requests and track timestamp for rolling RPS."""
    global _http_requests_total
    now = time.monotonic()
    with _lock:
        _http_requests_total += 1
        _request_times.append(now)
        _prune_old(now)


def record_failure() -> None:
    """Increment the failed-request counter (e.g. 502 / 503)."""
    global _http_failures_total
    with _lock:
        _http_failures_total += 1


def record_latency(latency_ms: float) -> None:
    """Track a request's latency for averages and percentiles."""
    with _lock:
        _latencies_ms.append(latency_ms)


def inc_active() -> None:
    global _active_connections
    with _lock:
        _active_connections += 1


def dec_active() -> None:
    global _active_connections
    with _lock:
        _active_connections -= 1


def _prune_old(now: float) -> None:
    cutoff = now - _WINDOW_SECONDS
    while _request_times and _request_times[0] < cutoff:
        _request_times.popleft()


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def http_requests_total() -> int:
    with _lock:
        return _http_requests_total


def http_failures_total() -> int:
    with _lock:
        return _http_failures_total


def active_connections() -> int:
    with _lock:
        return _active_connections


def requests_per_second() -> float:
    """Requests handled in the last second (rolling window)."""
    now = time.monotonic()
    with _lock:
        _prune_old(now)
        return float(len(_request_times))


def latency_stats() -> dict[str, float]:
    """Average and percentile latencies (ms) over recent requests."""
    with _lock:
        values = list(_latencies_ms)
    if not values:
        return {"avg": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0}
    return {
        "avg": sum(values) / len(values),
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
        "p99": _percentile(values, 99),
    }


def snapshot() -> dict[str, float]:
    """Single consistent read of all metrics for dashboards / rendering."""
    stats = latency_stats()
    return {
        "requests_total": float(http_requests_total()),
        "failures_total": float(http_failures_total()),
        "active_connections": float(active_connections()),
        "requests_per_second": requests_per_second(),
        "latency_avg_ms": stats["avg"],
        "latency_p50_ms": stats["p50"],
        "latency_p95_ms": stats["p95"],
        "latency_p99_ms": stats["p99"],
    }


def render_metrics() -> str:
    snap = snapshot()
    return (
        f"http_requests_total {int(snap['requests_total'])}\n"
        f"http_requests_per_second {snap['requests_per_second']:.2f}\n"
        f"http_request_failures_total {int(snap['failures_total'])}\n"
        f"http_active_connections {int(snap['active_connections'])}\n"
        f"http_request_latency_ms_avg {snap['latency_avg_ms']:.2f}\n"
        f"http_request_latency_ms_p50 {snap['latency_p50_ms']:.2f}\n"
        f"http_request_latency_ms_p95 {snap['latency_p95_ms']:.2f}\n"
        f"http_request_latency_ms_p99 {snap['latency_p99_ms']:.2f}\n"
    )
