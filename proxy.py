from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
import requests
import time
from threading import Thread

import metrics

BACKEND_PORTS = [8001, 8002, 8003]
_backend_index = 0
HOST = "localhost"
PORT = 9999
MAX_RETRIES = 2
backend_status = {port: True for port in BACKEND_PORTS}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

def health_check():
    while True:
        for port in BACKEND_PORTS:
            retries = 0
            while retries < MAX_RETRIES:
                try:
                    response = requests.get(
                        f"http://localhost:{port}/health",
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
    def get_backend():
        global _backend_index
        start = _backend_index
        while not backend_status[BACKEND_PORTS[_backend_index]]:
            _backend_index = (_backend_index + 1) % len(BACKEND_PORTS)
            if _backend_index == start:
                return None
        port = BACKEND_PORTS[_backend_index]
        _backend_index = (_backend_index + 1) % len(BACKEND_PORTS)
        return f"http://localhost:{port}"

    def _send_error(self, status: int, message: str):
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/metrics":
            body = metrics.render_metrics().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        metrics.record_request()
        start = time.perf_counter()
        backend_url = ProxyHandler.get_backend()
        if backend_url is None:
            self._send_error(503, "No healthy backends available")
            return
        logging.info(f"Forwarding to {backend_url}")
        try:
            backend_response = requests.get(
                backend_url + self.path,
                timeout=(2, 10),
            )
        except requests.RequestException as exc:
            logging.error(f"Backend {backend_url} failed: {exc}")
            self._send_error(502, f"Bad gateway: {backend_url} unavailable")
            return

        latency_ms = (time.perf_counter() - start) * 1000
        logging.info(f"Latency: {latency_ms:.2f} ms")

        self.send_response(backend_response.status_code)

        excluded_headers = [
            "Transfer-Encoding",
            "Content-Encoding",
            "Content-Length",
            "Connection"
        ]

        for key, value in backend_response.headers.items():
            if key not in excluded_headers:
                self.send_header(key, value)

        self.end_headers()
        self.wfile.write(backend_response.content)

def main():
    health_check_thread = Thread(target=health_check, daemon=True)
    health_check_thread.start()

    server = ThreadingHTTPServer((HOST, PORT), ProxyHandler)
    logging.info("Proxy now running")
    server.serve_forever()

if __name__ == "__main__":
    main()