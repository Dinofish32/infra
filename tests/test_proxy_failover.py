"""Integration tests for the proxy's load balancing and failover.

These start real backend HTTP servers on ephemeral ports and a real proxy
`ThreadingHTTPServer`, then drive traffic through the proxy over the loopback
interface. The proxy module's globals are pointed at the test backends.
"""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import requests

import proxy


class _BackendHandler(BaseHTTPRequestHandler):
    """Minimal backend: 200 OK on /health, and echoes the server's name on
    every other path so tests can tell which backend served a request."""

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, b"OK")
        else:
            self._respond(200, self.server.name.encode("utf-8"))

    def _respond(self, status: int, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve(handler_cls, name: str | None = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    if name is not None:
        server.name = name
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _stop(server: ThreadingHTTPServer) -> None:
    # Both calls are safe to invoke more than once.
    try:
        server.shutdown()
    finally:
        server.server_close()


def _collect(url: str, count: int) -> set[str]:
    """Send `count` GET / requests through the proxy; return the set of
    backend names that served them. Asserts each returned 200."""
    seen: set[str] = set()
    for _ in range(count):
        response = requests.get(url + "/", timeout=5)
        assert response.status_code == 200, response.status_code
        seen.add(response.text)
    return seen


def _wait_until(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def stack():
    """Two backends (A, B) + a proxy wired to them. Function-scoped so each
    test gets fresh servers and a clean proxy routing state."""
    backend_a = _serve(_BackendHandler, name="A")
    backend_b = _serve(_BackendHandler, name="B")
    port_a = backend_a.server_address[1]
    port_b = backend_b.server_address[1]

    proxy.BACKEND_HOST = "127.0.0.1"
    proxy.BACKEND_PORTS = [port_a, port_b]
    proxy.backend_status = {port_a: True, port_b: True}
    proxy._backend_index = 0

    proxy_server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.ProxyHandler)
    threading.Thread(target=proxy_server.serve_forever, daemon=True).start()

    stack = SimpleNamespace(
        url=f"http://127.0.0.1:{proxy_server.server_address[1]}",
        port_a=port_a,
        port_b=port_b,
        backend_a=backend_a,
        backend_b=backend_b,
    )
    try:
        yield stack
    finally:
        _stop(proxy_server)
        _stop(backend_a)
        _stop(backend_b)


def test_round_robin_distributes_across_healthy_backends(stack):
    assert _collect(stack.url, 6) == {"A", "B"}


def test_failover_routes_around_unhealthy_backend(stack):
    # Simulate the health checker having marked backend B unhealthy.
    proxy.backend_status[stack.port_b] = False

    # All traffic must now go to A only, and still succeed.
    assert _collect(stack.url, 6) == {"A"}


def test_no_healthy_backend_returns_503(stack):
    proxy.backend_status[stack.port_a] = False
    proxy.backend_status[stack.port_b] = False

    response = requests.get(stack.url + "/", timeout=5)
    assert response.status_code == 503


def test_dead_but_healthy_marked_backend_returns_502(stack):
    # B is still marked healthy but its server is gone. Force routing to B by
    # marking A unhealthy; the proxy should hit a connection error -> 502.
    _stop(stack.backend_b)
    proxy.backend_status[stack.port_a] = False
    proxy.backend_status[stack.port_b] = True

    response = requests.get(stack.url + "/", timeout=5)
    assert response.status_code == 502


# NOTE: keep this test last in the file. It starts the real (infinite) health
# check loop in a daemon thread; that thread keeps mutating proxy globals, so
# no later test in this module should rely on a clean routing state.
def test_health_check_detects_dead_backend_and_fails_over(stack, monkeypatch):
    # Speed the health-check loop way up: one retry, near-instant sleeps.
    monkeypatch.setattr(proxy, "MAX_RETRIES", 1)
    real_sleep = time.sleep
    monkeypatch.setattr(proxy.time, "sleep", lambda seconds: real_sleep(min(seconds, 0.02)))

    threading.Thread(target=proxy.health_check, daemon=True).start()

    # Both backends should be observed healthy first.
    assert _wait_until(
        lambda: proxy.backend_status[stack.port_a] and proxy.backend_status[stack.port_b]
    )

    # Kill B and let the real health checker notice.
    _stop(stack.backend_b)
    assert _wait_until(lambda: proxy.backend_status[stack.port_b] is False)

    # Once detected, every request should be served by the surviving backend A.
    assert _collect(stack.url, 8) == {"A"}
