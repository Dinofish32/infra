"""MCP server exposing the proxy's metrics and backend controls as tools.

This runs as its own process and talks to the proxy over HTTP (via the
`/api/*` endpoints), so the proxy must be running and reachable at `PROXY_URL`
(default http://localhost:9999).

Tools:
    get_metrics          - structured JSON snapshot of proxy metrics (read)
    get_backend_status   - health/drain/availability of each backend (read)
    drain_backend        - take a backend out of rotation (write)
    restore_backend      - return a backend to rotation (write)

Run:
    pip install -r requirements-mcp.txt
    python mcp_server.py                          # stdio (default, for local clients)
    MCP_TRANSPORT=streamable-http python mcp_server.py   # HTTP on MCP_PORT (default 8080)

Configuration (environment variables):
    PROXY_URL      proxy base URL             (default http://localhost:9999)
    MCP_TRANSPORT  stdio | streamable-http | sse (default stdio)
    MCP_HOST       bind host for HTTP transport  (default 0.0.0.0)
    MCP_PORT       bind port for HTTP transport  (default 8080)

Over the HTTP transport the MCP endpoint is served at `/mcp` on MCP_PORT.
"""
import os

import requests
from mcp.server.fastmcp import FastMCP

PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:9999").rstrip("/")
TIMEOUT = 5.0
# Bearer token for the proxy's /api/* endpoints (must match the proxy's
# ADMIN_TOKEN). Sent only when set.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8080"))

mcp = FastMCP("infra-proxy", host=MCP_HOST, port=MCP_PORT)


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"} if ADMIN_TOKEN else {}


def _get(path: str) -> dict:
    response = requests.get(PROXY_URL + path, headers=_auth_headers(), timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def _post(path: str, port: int) -> dict:
    response = requests.post(
        PROXY_URL + path, json={"port": port}, headers=_auth_headers(), timeout=TIMEOUT
    )
    if response.status_code >= 400:
        # Surface the proxy's validation message (e.g. unknown port) to the client.
        raise ValueError(response.json().get("error", response.text))
    return response.json()


@mcp.tool()
def get_metrics() -> dict:
    """Return a structured JSON snapshot of the proxy's live metrics: total
    requests, failures, active connections, requests/sec, and latency
    avg/p50/p95/p99 (ms)."""
    return _get("/api/metrics")


@mcp.tool()
def get_backend_status() -> dict:
    """Return the status of every backend port: whether it is `healthy` (passing
    health checks), `drained` (administratively removed from rotation), and
    `available` (healthy and not drained, i.e. currently receiving traffic)."""
    return _get("/api/backends")


@mcp.tool()
def drain_backend(port: int) -> dict:
    """Drain a backend: take the given port out of the load-balancer rotation
    without stopping the process. The drain persists across health checks until
    restored. Returns the updated backend status."""
    return _post("/api/backends/drain", port)


@mcp.tool()
def restore_backend(port: int) -> dict:
    """Restore a previously drained backend: return the given port to the
    load-balancer rotation (health checks resume governing it). Returns the
    updated backend status."""
    return _post("/api/backends/restore", port)


if __name__ == "__main__":
    mcp.run(transport=MCP_TRANSPORT)
