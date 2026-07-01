"""Open-loop load generator for the proxy.

Unlike a closed-loop test (fixed pool of workers that each wait for a response
before sending the next request), this fires requests at a fixed target arrival
rate regardless of how fast the server responds. That models real traffic and
avoids "coordinated omission" — the tendency of closed-loop tests to under-count
slow periods and make latency look better than it is.

Configuration (all via environment variables):
    PROXY_URL    target URL              (default http://localhost:9999)
    RATE         requests per second     (default 50)
    DURATION     total seconds to run    (default 10)
    WARMUP       leading seconds discarded from stats (default 2)
    MAX_WORKERS  in-flight request cap   (default 2000)
"""
import concurrent.futures
import logging
import os
import threading
import time

import requests
from requests.adapters import HTTPAdapter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

URL = os.environ.get("PROXY_URL", "http://localhost:9999")
RATE = float(os.environ.get("RATE", "50"))
DURATION = float(os.environ.get("DURATION", "10"))
WARMUP = float(os.environ.get("WARMUP", "2"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "2000"))
REQUEST_TIMEOUT = 10.0

_thread_local = threading.local()
_lock = threading.Lock()

# Each result is (scheduled_offset_s, latency_ms, ok). latency_ms is measured
# from the request's *scheduled* send time, so time spent waiting for a free
# worker (i.e. the server falling behind) is correctly counted as latency.
_results: list[tuple[float, float, bool]] = []


def _session() -> requests.Session:
    """Return a per-thread Session so connections are pooled/reused (keep-alive)
    without sharing a Session across threads."""
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _thread_local.session = session
    return session


def worker(scheduled_offset: float, scheduled_perf: float) -> None:
    ok = False
    try:
        response = _session().get(URL, timeout=REQUEST_TIMEOUT)
        ok = response.status_code == 200
    except requests.RequestException:
        ok = False
    latency_ms = (time.perf_counter() - scheduled_perf) * 1000
    with _lock:
        _results.append((scheduled_offset, latency_ms, ok))


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _report() -> None:
    steady = [r for r in _results if r[0] >= WARMUP]
    if not steady:
        logging.warning("No requests recorded after warmup; nothing to report.")
        return

    latencies = [latency for _, latency, _ in steady]
    successes = sum(1 for _, _, ok in steady if ok)
    failures = len(steady) - successes
    measured_window = max(DURATION - WARMUP, 1e-9)
    achieved_rps = len(steady) / measured_window
    error_rate = (failures / len(steady)) * 100

    logging.info("=" * 48)
    logging.info(f"Target rate:   {RATE:.1f} req/s over {DURATION:.0f}s "
                 f"(warmup {WARMUP:.0f}s discarded)")
    logging.info(f"Sampled:       {len(steady)} requests")
    logging.info(f"Achieved RPS:  {achieved_rps:.1f} req/s")
    logging.info(f"Success:       {successes}")
    logging.info(f"Failures:      {failures}  ({error_rate:.2f}%)")
    logging.info("Latency (ms):")
    logging.info(f"  avg  {sum(latencies) / len(latencies):8.2f}")
    logging.info(f"  p50  {_percentile(latencies, 50):8.2f}")
    logging.info(f"  p95  {_percentile(latencies, 95):8.2f}")
    logging.info(f"  p99  {_percentile(latencies, 99):8.2f}")
    logging.info(f"  max  {max(latencies):8.2f}")
    logging.info("=" * 48)


def main() -> None:
    interval = 1.0 / RATE
    total_requests = int(RATE * DURATION)
    logging.info(f"Open-loop test -> {URL} at {RATE:.1f} req/s for {DURATION:.0f}s "
                 f"({total_requests} requests)")

    start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for i in range(total_requests):
            scheduled_perf = start + i * interval
            sleep_for = scheduled_perf - time.perf_counter()
            if sleep_for > 0:
                time.sleep(sleep_for)
            # Dispatch on the schedule without blocking on the response, so a
            # slow server can't throttle our arrival rate.
            executor.submit(worker, i * interval, scheduled_perf)
        # Exiting the context manager waits for all in-flight requests.

    _report()


if __name__ == "__main__":
    main()
