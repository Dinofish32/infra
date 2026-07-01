"""Unit and thread-safety tests for metrics.py.

The module keeps global state, so `reset_metrics` (autouse) restores a clean
slate before every test.
"""
import threading

import pytest

import metrics


@pytest.fixture(autouse=True)
def reset_metrics():
    metrics._http_requests_total = 0
    metrics._http_failures_total = 0
    metrics._active_connections = 0
    metrics._request_times.clear()
    metrics._latencies_ms.clear()
    yield


# --------------------------------------------------------------------------- #
# Basic counters
# --------------------------------------------------------------------------- #

def test_record_request_increments_total():
    for _ in range(5):
        metrics.record_request()
    assert metrics.http_requests_total() == 5


def test_record_failure_increments_total():
    metrics.record_failure()
    metrics.record_failure()
    assert metrics.http_failures_total() == 2


def test_active_connections_inc_and_dec():
    metrics.inc_active()
    metrics.inc_active()
    metrics.dec_active()
    assert metrics.active_connections() == 1


# --------------------------------------------------------------------------- #
# Rolling requests-per-second window
# --------------------------------------------------------------------------- #

def test_requests_per_second_counts_recent(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(metrics.time, "monotonic", lambda: clock["t"])

    metrics.record_request()
    metrics.record_request()
    assert metrics.requests_per_second() == 2.0


def test_requests_per_second_prunes_old_entries(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(metrics.time, "monotonic", lambda: clock["t"])

    metrics.record_request()
    metrics.record_request()
    assert metrics.requests_per_second() == 2.0

    # Advance past the 1s window; old timestamps should be pruned out.
    clock["t"] += 2.0
    assert metrics.requests_per_second() == 0.0


# --------------------------------------------------------------------------- #
# Latency stats & percentiles
# --------------------------------------------------------------------------- #

def test_latency_stats_empty_returns_zeros():
    assert metrics.latency_stats() == {"avg": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0}


def test_latency_stats_computes_avg_and_percentiles():
    for value in [10, 20, 30, 40, 50]:
        metrics.record_latency(value)

    stats = metrics.latency_stats()
    assert stats["avg"] == 30.0
    assert stats["p50"] == pytest.approx(30.0)
    assert stats["p95"] == pytest.approx(48.0)
    assert stats["p99"] == pytest.approx(49.6)


def test_percentile_helper_edge_cases():
    assert metrics._percentile([], 50) == 0.0
    assert metrics._percentile([5.0], 99) == 5.0
    assert metrics._percentile([10, 20, 30, 40, 50], 50) == pytest.approx(30.0)


def test_latency_deque_is_bounded():
    for i in range(1500):
        metrics.record_latency(float(i))

    # deque(maxlen=1000) keeps only the most recent 1000 samples.
    assert len(metrics._latencies_ms) == 1000
    assert metrics._latencies_ms[0] == 500.0


# --------------------------------------------------------------------------- #
# Snapshot / render
# --------------------------------------------------------------------------- #

def test_snapshot_has_expected_keys():
    snap = metrics.snapshot()
    assert set(snap) == {
        "requests_total",
        "failures_total",
        "active_connections",
        "requests_per_second",
        "latency_avg_ms",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
    }


def test_render_metrics_format():
    metrics.record_request()
    metrics.record_failure()
    metrics.record_latency(12.5)

    lines = metrics.render_metrics().strip().split("\n")
    assert lines[0] == "http_requests_total 1"
    assert any(line == "http_request_failures_total 1" for line in lines)
    # Every line must be a "key value" pair.
    for line in lines:
        assert len(line.split()) == 2


# --------------------------------------------------------------------------- #
# Thread safety — the lock must prevent lost updates on the shared counters.
# --------------------------------------------------------------------------- #

def _run_concurrently(target, thread_count):
    barrier = threading.Barrier(thread_count)

    def worker():
        barrier.wait()  # release all threads at once to maximize contention
        target()

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def test_record_request_is_thread_safe():
    threads, iterations = 20, 500

    def hammer():
        for _ in range(iterations):
            metrics.record_request()

    _run_concurrently(hammer, threads)
    assert metrics.http_requests_total() == threads * iterations


def test_record_failure_is_thread_safe():
    threads, iterations = 20, 500

    def hammer():
        for _ in range(iterations):
            metrics.record_failure()

    _run_concurrently(hammer, threads)
    assert metrics.http_failures_total() == threads * iterations


def test_active_connections_is_thread_safe():
    threads, iterations = 20, 500

    def hammer():
        for _ in range(iterations):
            metrics.inc_active()

    _run_concurrently(hammer, threads)
    assert metrics.active_connections() == threads * iterations


def test_balanced_inc_dec_returns_to_zero():
    threads, iterations = 20, 500

    def hammer():
        for _ in range(iterations):
            metrics.inc_active()
            metrics.dec_active()

    _run_concurrently(hammer, threads)
    assert metrics.active_connections() == 0


def test_record_latency_is_thread_safe():
    # Keep total <= deque maxlen (1000) so the count assertion is exact.
    threads, iterations = 8, 125

    def hammer():
        for _ in range(iterations):
            metrics.record_latency(1.0)

    _run_concurrently(hammer, threads)
    assert len(metrics._latencies_ms) == threads * iterations
