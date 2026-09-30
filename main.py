from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import argparse
import json
import logging
import os
import time
import threading

# Uses env var BIND_HOST to bind to all interfaces
HOST = os.environ.get("BIND_HOST", "0.0.0.0")
PORTS = [8001, 8002, 8003]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

class BackendHandler(BaseHTTPRequestHandler):

    # Allows us to respond to get requests
    def do_GET(self):
        logging.info(f"{self.client_address[0]} GET {self.path}")
        start_time = time.perf_counter()
        backend_port = self.server.server_port
        if self.path == "/":
            self.send_response(200) # status 
            self.send_header("Content-type", "text/html")
            self.send_header("Port", str(backend_port))
            self.end_headers()

            html = f"""
            <html>
                <head>
                    <title>Networking Project</title>
                </head>
                <body>
                    <h1>Akarsh's Infra Project</h1>
                    <p>Served by port {backend_port}</p>
                </body>
            </html>          
            """

            self.wfile.write(bytes(html, "utf-8"))

        elif self.path == "/health":
            self.send_response(200) # status 
            self.send_header("Content-type", "text/plain")
            self.end_headers()

            self.wfile.write(bytes("OK", "utf-8"))
        
        else:
            self.send_response(404)
            self.send_header("Content-type", "text/plain")
            self.end_headers()

            self.wfile.write(bytes("404 - Page not found", "utf-8"))

        end_time = time.perf_counter()
        logging.info(f"Request finished in {(end_time - start_time) * 1000:.2f}ms")

    def do_POST(self):
        logging.info(f"{self.client_address[0]} POST {self.path}")
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        body = body.decode("utf-8")

        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()

        date = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
        response = {
            "body": body,
            "time": date
        }
        self.wfile.write(bytes(json.dumps(response), "utf-8"))


def run_server(port: int):
    server = ThreadingHTTPServer((HOST, port), BackendHandler)
    logging.info(f"Backend started on port {port}")
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="Backend HTTP server(s)")
    parser.add_argument(
        "ports",
        nargs="*",
        type=int,
        default=PORTS,
        help="port(s) to serve; omit to run all defaults (8001 8002 8003). "
             "Pass a single port to run one killable backend for failover testing.",
    )
    args = parser.parse_args()

    threads: list[threading.Thread] = []

    for port in args.ports:
        thread = threading.Thread(
            target=run_server,
            args=(port,)
        )

        thread.start()

        threads.append(thread)
    
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()