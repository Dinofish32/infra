from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import logging
import os
import requests
import time
from pathlib import Path
from threading import Thread, Lock

import metrics

BACKEND_PORTS = [8001, 8002, 8003]
_backend_index = 0
_backend_lock = Lock()
# Bind to all interfaces by default so the proxy is reachable from outside
# the container. Override with BIND_HOST=127.0.0.1 for a local-only run.
HOST = os.environ.get("BIND_HOST", "0.0.0.0")
# Where the backend lives. In Docker Compose this is the backend service name.
BACKEND_HOST = os.environ.get("BACKEND_HOST", "127.0.0.1")
PORT = 9999
# Optional bearer token protecting the /api/* endpoints. When set, those
# requests must send `Authorization: Bearer <token>`. When empty, the API is
# open (convenient for local dev, but unauthenticated).
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
MAX_RETRIES = 2
# Live chart view of /metrics, served to browsers (Accept: text/html).
METRICS_PAGE_PATH = Path(__file__).with_name("metrics_page.html")
backend_status = {port: True for port in BACKEND_PORTS}
# Administratively drained ports. Draining is a routing-layer override: the
# health checker keeps reporting a backend's real health, but a drained backend
# is excluded from rotation regardless. This is why a drain survives health
# checks (unlike simply flipping backend_status to False).
drained_ports: set[int] = set()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


def backend_state() -> list[dict]:
    """Structured status for every configured backend."""
    with _backend_lock:
        return [
            {
                "port": port,
                "healthy": backend_status.get(port, False),
                "drained": port in drained_ports,
                "available": backend_status.get(port, False) and port not in drained_ports,
            }
            for port in BACKEND_PORTS
        ]


def drain_backend(port: int) -> None:
    """Take a backend out of rotation administratively (survives health checks)."""
    with _backend_lock:
        drained_ports.add(port)
    logging.info(f"Backend {port} drained")


def restore_backend(port: int) -> None:
    """Return a drained backend to rotation; health checks continue to govern it."""
    with _backend_lock:
        drained_ports.discard(port)
    logging.info(f"Backend {port} restored")

def health_check():
    while True:
        for port in BACKEND_PORTS:
            retries = 0
            while retries < MAX_RETRIES:
                try:
                    response = requests.get(
                        f"http://{BACKEND_HOST}:{port}/health",
                        timeout=2
                    )
                    backend_status[port] = response.status_code == 200
                except requests.RequestException as exc:
                    logging.error(f"Port {port} failed health check: {exc}")
                    retries += 1
                    time.sleep(2)
                else:
                    break
            if retries == MAX_RETRIES:
                backend_status[port] = False
        time.sleep(5 if all(backend_status.values()) else 1)


class ProxyHandler(BaseHTTPRequestHandler):

    @staticmethod
    def healthy_backend_ports() -> list[int]:
        """Currently-healthy backend ports in round-robin order starting from
        the current index. Advances the shared index by one so load spreads
        across successive requests. The returned order is the failover order
        for a single request."""
        global _backend_index
        ordered: list[int] = []
        with _backend_lock:
            count = len(BACKEND_PORTS)
            start = _backend_index
            _backend_index = (_backend_index + 1) % count
            for offset in range(count):
                port = BACKEND_PORTS[(start + offset) % count]
                if backend_status[port] and port not in drained_ports:
                    ordered.append(port)
        return ordered

    def _send_error(self, status: int, message: str):
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        """True if no token is configured (open) or the request carries the
        correct `Authorization: Bearer <token>` header."""
        if not ADMIN_TOKEN:
            return True
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        # Constant-time comparison to avoid leaking the token via timing.
        return hmac.compare_digest(header[len(prefix):], ADMIN_TOKEN)

    def _require_auth(self) -> bool:
        """Enforce auth on protected endpoints; sends 401 and returns False if
        the caller is not authorized."""
        if self._authorized():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", "Bearer")
        body = json.dumps(
            {"error": "unauthorized: provide 'Authorization: Bearer <token>'"}
        ).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def _read_port(self) -> tuple[int | None, str | None]:
        """Parse a JSON body of the form {"port": <int>} for admin endpoints.
        Returns (port, None) on success or (None, error_message) on failure."""
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return None, 'Body must be valid JSON, e.g. {"port": 8002}'
        port = data.get("port")
        if not isinstance(port, int):
            return None, "Missing or non-integer 'port'"
        if port not in BACKEND_PORTS:
            return None, f"Unknown backend port {port}; known ports: {BACKEND_PORTS}"
        return port, None

    def do_POST(self):
        if self.path in ("/api/backends/drain", "/api/backends/restore"):
            if not self._require_auth():
                return
            port, error = self._read_port()
            if error is not None:
                self._send_json(400, {"error": error})
                return
            if self.path.endswith("/drain"):
                drain_backend(port)
                action = "drained"
            else:
                restore_backend(port)
                action = "restored"
            self._send_json(200, {"port": port, "action": action, "backends": backend_state()})
            return
        self._send_error(404, "Not found")

    def do_GET(self):
        if self.path == "/metrics":
            # One URL, three views chosen by the Accept header: browsers get
            # the live chart page, the page itself polls for JSON, and every
            # other client (dashboard.py, curl, scrapers) keeps the plaintext.
            accept = self.headers.get("Accept", "")
            if "application/json" in accept:
                self._send_json(200, metrics.snapshot())
                return
            if "text/html" in accept:
                body = METRICS_PAGE_PATH.read_bytes()
                content_type = "text/html; charset=utf-8"
            else:
                body = metrics.render_metrics().encode("utf-8")
                content_type = "text/plain; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Vary", "Accept")
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/api/metrics":
            if not self._require_auth():
                return
            self._send_json(200, metrics.snapshot())
            return

        if self.path == "/api/backends":
            if not self._require_auth():
                return
            self._send_json(200, {"backends": backend_state()})
            return

        metrics.record_request()
        metrics.inc_active()
        start = time.perf_counter()
        try:
            candidates = ProxyHandler.healthy_backend_ports()
            if not candidates:
                metrics.record_failure()
                self._send_error(503, "No healthy backends available")
                return

            excluded_headers = [
                "Transfer-Encoding",
                "Content-Encoding",
                "Content-Length",
                "Connection"
            ]

            # Try each healthy backend in failover order. A connection-level
            # failure marks that backend unhealthy and retries the next one;
            # only if every candidate is unreachable do we return 502.
            for port in candidates:
                backend_url = f"http://{BACKEND_HOST}:{port}"
                logging.info(f"Forwarding to {backend_url}")
                try:
                    backend_response = requests.get(
                        backend_url + self.path,
                        timeout=(2, 10),
                    )
                except requests.RequestException as exc:
                    logging.error(
                        f"Backend {backend_url} failed: {exc}; "
                        f"marking unhealthy and retrying next backend"
                    )
                    with _backend_lock:
                        backend_status[port] = False
                    continue

                if backend_response.status_code >= 500:
                    metrics.record_failure()

                self.send_response(backend_response.status_code)

                for key, value in backend_response.headers.items():
                    if key not in excluded_headers:
                        self.send_header(key, value)

                self.end_headers()
                self.wfile.write(backend_response.content)
                return

            # Every healthy backend failed to connect.
            metrics.record_failure()
            self._send_error(502, "Bad gateway: all backends unavailable")
        finally:
            latency_ms = (time.perf_counter() - start) * 1000
            metrics.record_latency(latency_ms)
            metrics.dec_active()
            logging.info(f"Latency: {latency_ms:.2f} ms")

def main():
    health_check_thread = Thread(target=health_check, daemon=True)
    health_check_thread.start()

    if not ADMIN_TOKEN:
        logging.warning(
            "ADMIN_TOKEN not set: /api/* endpoints are UNAUTHENTICATED. "
            "Set ADMIN_TOKEN to require a bearer token."
        )

    server = ThreadingHTTPServer((HOST, PORT), ProxyHandler)
    logging.info("Proxy now running")
    server.serve_forever()

if __name__ == "__main__":
    main()