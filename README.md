# infra — HTTP Load Balancer with Observability

A from-scratch HTTP reverse proxy / load balancer written in pure Python
(standard library only, plus `requests`). It fronts a pool of backend HTTP
servers, does round-robin load balancing with health checking, exposes
Prometheus-style metrics, and ships with a live terminal dashboard and a
load generator. Everything is containerized with Docker Compose.

This document is both a human README and a machine-readable spec for AI
agents working on the codebase. It reflects the **current state** of the
project — no aspirational features are listed as if they exist.

---

## Architecture

```
                         ┌─────────────────────────────────────┐
   client / loadgen ───▶ │  proxy.py  (:9999)                  │
                         │  - round-robin load balancer        │
                         │  - health checks backends           │
                         │  - /metrics endpoint                │
                         │  - metrics.py (in-process counters) │
                         └───────────────┬─────────────────────┘
                                         │ forwards GET
                                         ▼
                         ┌─────────────────────────────────────┐
                         │  main.py  (backend)                 │
                         │  one ThreadingHTTPServer per port:  │
                         │  :8001  :8002  :8003                │
                         └─────────────────────────────────────┘

   dashboard.py  ──polls──▶  proxy /metrics  (read-only, external process)
```

- **Backend (`main.py`)** — runs three `ThreadingHTTPServer` instances (one
  per thread) on ports `8001`, `8002`, `8003`. Serves a small HTML page on
  `/`, `OK` on `/health`, `404` otherwise. Also handles `POST` (echoes body
  + timestamp as JSON). No third-party dependencies.
- **Proxy (`proxy.py`)** — single `ThreadingHTTPServer` on port `9999`.
  Round-robins `GET` requests across healthy backends, retries/fails over,
  and records metrics. A background daemon thread health-checks each backend.
- **Metrics (`metrics.py`)** — thread-safe in-process counters: total
  requests, failures, active connections, rolling requests/sec, and latency
  avg/p50/p95/p99. Rendered as Prometheus-style plaintext.
- **Dashboard (`dashboard.py`)** — standalone CLI that polls `/metrics` and
  renders a live, colored terminal dashboard (ANSI, Windows-aware).
- **Load generator (`loadgenerator.py`)** — open-loop tester that sends `GET`
  requests at a fixed target rate and reports achieved throughput, error rate,
  and latency percentiles (avg/p50/p95/p99/max).

---

## Components

### `main.py` — Backend servers
- Binds to `BIND_HOST` (default `0.0.0.0`) on ports `[8001, 8002, 8003]`.
- Each port runs in its own thread via `ThreadingHTTPServer`.
- CLI: `python main.py` runs all default ports; `python main.py 8001` (or any
  subset) runs only the given port(s) — useful for launching backends as
  separate, individually-killable processes when testing failover.
- Routes:
  - `GET /` → `200`, HTML page that includes the serving port (adds a
    custom `Port` response header).
  - `GET /health` → `200`, body `OK`.
  - `GET <other>` → `404`.
  - `POST <any>` → `200`, JSON `{"body": <request body>, "time": <ts>}`.
- Logs each request and its duration in ms.

### `proxy.py` — Load balancer
- Binds to `BIND_HOST` (default `0.0.0.0`) on port `9999`.
- Backends resolved at `http://{BACKEND_HOST}:{port}` for
  `port in [8001, 8002, 8003]`. `BACKEND_HOST` defaults to `127.0.0.1`
  (local) and is set to `backend` under Docker Compose.
- **Load balancing:** `healthy_backend_ports()` is a thread-safe round-robin
  that returns the currently-healthy backends in failover order for a request
  (advancing the shared index by one per request for fair distribution).
- **Health checks:** background daemon thread hits each backend `/health`
  with `MAX_RETRIES = 2`, timeout `2s`. Poll interval is `5s` when all
  healthy, `1s` when any are down. Updates the shared `backend_status` map.
- **Admin / observability endpoints (not proxied):**
  - `GET /metrics` → Prometheus-style plaintext (for the dashboard).
  - `GET /api/metrics` → `metrics.snapshot()` as structured JSON.
  - `GET /api/backends` → JSON list of each backend's `healthy` / `drained` /
    `available` state.
  - `POST /api/backends/drain` and `POST /api/backends/restore` with body
    `{"port": <int>}` → take a backend out of / return it to rotation.
- **Auth:** if `ADMIN_TOKEN` is set, all `/api/*` endpoints require
  `Authorization: Bearer <token>` (constant-time checked) and return `401`
  otherwise. The plaintext `/metrics` endpoint is intentionally left open for
  the dashboard. When `ADMIN_TOKEN` is unset the API is open (logged as a
  warning at startup).
- **Draining:** `drained_ports` is a routing-layer override. A drained backend
  is excluded from rotation but the health checker keeps reporting its true
  health, so a drain **survives health checks** until explicitly restored
  (`available = healthy AND not drained`).
- **Request handling (`do_GET`):**
  - `GET /metrics` → returns `metrics.render_metrics()` (not counted as a
    proxied request).
  - Otherwise: records request + active connection, then **tries each healthy
    backend in turn**. On a connection-level failure it marks that backend
    unhealthy and **retries the next healthy backend** (per-request failover),
    streaming the first successful response back (stripping hop-by-hop headers:
    `Transfer-Encoding`, `Content-Encoding`, `Content-Length`, `Connection`).
  - No healthy backend → `503`. Every healthy backend unreachable → `502`.
    Backend `5xx` → counted as a failure but the response is still relayed.
  - Records latency for every request in a `finally` block.
- **Note:** only `GET` is proxied to backends. The proxy's `do_POST` handles
  the admin drain/restore endpoints only; client `POST`s are not forwarded.

### `metrics.py` — In-process metrics
- Thread-safe via a module-level `Lock`.
- Rolling RPS over a 1.0s window (`deque` of request timestamps).
- Latency samples kept in a bounded `deque(maxlen=1000)`; percentiles via
  linear interpolation.
- `render_metrics()` emits these keys (plaintext, `key value` per line):
  - `http_requests_total`
  - `http_requests_per_second`
  - `http_request_failures_total`
  - `http_active_connections`
  - `http_request_latency_ms_avg`
  - `http_request_latency_ms_p50`
  - `http_request_latency_ms_p95`
  - `http_request_latency_ms_p99`

### `dashboard.py` — Live terminal dashboard
- Usage: `python dashboard.py [--interval 1.0] [--url http://localhost:9999/metrics]`
- Polls `/metrics`, parses the plaintext, and renders RPS, active
  connections, totals, failures, and latency stats with color thresholds
  and ASCII bars. Enables ANSI/UTF-8 on Windows. `Ctrl-C` to quit.
- Shows a clear "proxy unreachable" state and keeps retrying on errors.

### `loadgenerator.py` — Load generator
- **Open-loop** test: dispatches `GET` requests to `PROXY_URL`
  (default `http://localhost:9999`) on a fixed schedule (`RATE` req/s for
  `DURATION` s) without waiting for responses, so a slow server can't throttle
  the arrival rate. This avoids coordinated omission.
- Latency is measured from each request's **scheduled** send time, so time
  spent waiting for a free worker (server falling behind) counts as latency.
- Reuses connections via a per-thread pooled `requests.Session` (keep-alive),
  and discards the first `WARMUP` seconds from stats.
- Reports achieved RPS, success/failure counts, error rate, and latency
  avg/p50/p95/p99/max. Configurable via `RATE`, `DURATION`, `WARMUP`,
  `MAX_WORKERS` (see Configuration).

### `mcp_server.py` — MCP server
- Exposes the proxy's admin/observability endpoints as MCP tools so an MCP
  client (e.g. an agent) can read metrics and control backends. Talks to the
  proxy via HTTP at `PROXY_URL` (default `http://localhost:9999`).
- Transport is env-selectable via `MCP_TRANSPORT`: `stdio` (default, for local
  desktop MCP clients) or `streamable-http` (serves the MCP endpoint at `/mcp`
  on `MCP_HOST:MCP_PORT`, default `0.0.0.0:8080` — used by the Docker image).
- Tools:
  - `get_metrics` (read) → structured JSON metrics snapshot.
  - `get_backend_status` (read) → each backend's health/drain/availability.
  - `drain_backend(port)` (write) → take a backend out of rotation.
  - `restore_backend(port)` (write) → return a backend to rotation.
- If the proxy has `ADMIN_TOKEN` set, give the MCP server the same value so it
  sends `Authorization: Bearer <token>` on its calls.
- Run: `pip install -r requirements-mcp.txt` then `python mcp_server.py`
  (with the proxy running).

### `failover_check.py` — Manual failover checker
- Usage: `python failover_check.py [--url http://localhost:9999] [--rate 5]`
- Sends a steady stream of `GET /` requests and prints a live tally of which
  backend served each one (from the relayed `Port` header) plus status counts.
- Intended workflow: run each backend on its own port (`python main.py 8001`,
  etc.), start the proxy and this checker, then kill/restart a backend and
  watch traffic fail over to the survivors and rejoin on recovery.

---

## Configuration (environment variables)

| Variable       | Used by            | Default        | Purpose                                            |
| -------------- | ------------------ | -------------- | -------------------------------------------------- |
| `BIND_HOST`    | `main.py`, `proxy.py` | `0.0.0.0`   | Interface to bind. Use `127.0.0.1` for local-only. |
| `BACKEND_HOST` | `proxy.py`         | `127.0.0.1`    | Host where backends live (`backend` in Compose).   |
| `ADMIN_TOKEN`  | `proxy.py`, `mcp_server.py` | `""` (unset) | Bearer token for `/api/*`. Empty = open. Must match across proxy + MCP. |
| `PROXY_URL`    | `loadgenerator.py`, `mcp_server.py` | `http://localhost:9999` | Proxy base URL for the load generator / MCP server. |
| `MCP_TRANSPORT`| `mcp_server.py`    | `stdio`        | `stdio` or `streamable-http` (HTTP is used in Docker). |
| `MCP_HOST`     | `mcp_server.py`    | `0.0.0.0`      | Bind host for the HTTP transport.                  |
| `MCP_PORT`     | `mcp_server.py`    | `8080`         | Bind port for the HTTP transport (exposed by Docker). |
| `RATE`         | `loadgenerator.py` | `50`           | Target arrival rate in requests/sec.               |
| `DURATION`     | `loadgenerator.py` | `10`           | Total run time in seconds.                         |
| `WARMUP`       | `loadgenerator.py` | `2`            | Leading seconds discarded from stats.              |
| `MAX_WORKERS`  | `loadgenerator.py` | `2000`         | Cap on concurrent in-flight requests.              |

Ports (`8001/8002/8003` backend, `9999` proxy) are currently hardcoded
constants in the source files.

---

## Running

### Docker Compose (recommended)
```bash
docker compose up --build
```
- Builds three images: `Dockerfile.backend` (backend), `Dockerfile.proxy`
  (proxy + `metrics.py` + `requests`), and `Dockerfile.mcp` (MCP server).
- Published host ports: proxy `9999:9999` and MCP `8080:8080`. Backends are
  reachable only inside the Compose network via the service name `backend`
  (`expose`d, not published). The `mcp` service reaches the proxy at
  `http://proxy:9999`.
- Then hit `http://localhost:9999/` and `http://localhost:9999/metrics`; the
  MCP endpoint is served at `http://localhost:8080/mcp`.

### Local (without Docker)
```bash
pip install -r requirements.txt      # installs requests (backend needs no deps)
python main.py                       # terminal 1: backends on 8001/8002/8003
python proxy.py                      # terminal 2: proxy on 9999
python dashboard.py                  # terminal 3: live dashboard (optional)
python loadgenerator.py              # terminal 4: fire load (optional)
```
A `Procfile` is also provided (`backend: python main.py`, `proxy: python proxy.py`)
for process managers like `foreman`/`honcho`.

### Tests
```bash
pip install -r requirements-dev.txt   # requests + pytest
pytest                                # runs the full suite
```
- `tests/test_metrics.py` — unit tests for `metrics.py` (counters, rolling RPS
  window/pruning, percentiles, bounded latency deque, render format) plus
  thread-safety tests that hammer the shared counters from many threads.
- `tests/test_proxy_failover.py` — integration tests that start real backend
  servers + a real proxy and verify round-robin, failover to healthy backends,
  per-request retry on connection failure, `503` when none are healthy, `502`
  when all are unreachable, and live health-check detection.

### Manual failover test
```bash
python main.py 8001                  # terminal 1: one backend per terminal
python main.py 8002                  # terminal 2
python main.py 8003                  # terminal 3
python proxy.py                      # terminal 4
python failover_check.py             # terminal 5: live per-backend tally
```
Kill a backend terminal (Ctrl-C) and watch traffic fail over to the remaining
backends with no (or a single) error, then restart it to see it rejoin.

---

## Endpoints reference

| Endpoint            | Server  | Method | Response                                          |
| ------------------- | ------- | ------ | ------------------------------------------------- |
| `/`                 | backend | GET    | `200` HTML page, `Port` header with backend port  |
| `/health`           | backend | GET    | `200` `OK`                                        |
| any other path      | backend | GET    | `404`                                             |
| any path            | backend | POST   | `200` JSON `{body, time}`                         |
| `/`                 | proxy   | GET    | Round-robined backend response                    |
| `/metrics`          | proxy   | GET    | `200` Prometheus-style plaintext metrics          |
| `/api/metrics`      | proxy   | GET    | `200` JSON metrics snapshot                        |
| `/api/backends`     | proxy   | GET    | `200` JSON per-backend `healthy/drained/available` |
| `/api/backends/drain` | proxy | POST   | `200` JSON; body `{"port": N}` → drain a backend  |
| `/api/backends/restore` | proxy | POST | `200` JSON; body `{"port": N}` → restore a backend |
| (unknown/invalid port) | proxy | POST | `400` JSON `{"error": ...}`                       |
| (missing/bad token, when `ADMIN_TOKEN` set) | proxy | GET/POST `/api/*` | `401` JSON `{"error": ...}` |
| (no healthy backend)| proxy   | GET    | `503`                                             |
| (all backends unreachable) | proxy | GET | `502` (after retrying each healthy backend)     |

---

## Files

| File                 | Role                                                        |
| -------------------- | ----------------------------------------------------------- |
| `main.py`            | Backend HTTP servers (stdlib only).                         |
| `proxy.py`           | Load balancer + health checks + `/metrics`.                 |
| `metrics.py`         | Thread-safe in-process metrics + Prometheus rendering.      |
| `dashboard.py`       | Live terminal dashboard client for `/metrics`.              |
| `loadgenerator.py`   | Open-loop load generator against the proxy (percentiles).   |
| `failover_check.py`  | Live per-backend tally for manual failover testing.         |
| `mcp_server.py`      | MCP server exposing metrics + drain/restore backend tools.  |
| `requirements-mcp.txt` | MCP server deps (`mcp` + runtime deps).                   |
| `requirements.txt`   | Python deps (`requests==2.32.3`); backend needs none.       |
| `requirements-dev.txt` | Dev/test deps (`pytest` + runtime deps).                  |
| `tests/`             | pytest suite: `metrics` unit/thread-safety + proxy failover.|
| `conftest.py`        | Makes repo root importable and quiets logging during tests. |
| `Dockerfile.backend` | Image for `main.py` (Python 3.13-slim, non-root).           |
| `Dockerfile.proxy`   | Image for `proxy.py` + `metrics.py` (installs deps).        |
| `Dockerfile.mcp`     | Image for `mcp_server.py` (HTTP transport on `8080`).       |
| `docker-compose.yml` | Wires backend + proxy; publishes proxy `9999`.              |
| `Procfile`           | Process definitions for foreman/honcho-style runners.       |
| `.dockerignore`      | Excludes caches, VCS, docs, Docker files from build context.|

---

## Tech & conventions

- **Python 3.13**, standard library HTTP servers (`http.server`,
  `ThreadingHTTPServer`). Only external dependency is `requests` (proxy,
  dashboard, load generator).
- Threading-based concurrency; shared state guarded by `Lock`s.
- Logging via `logging` at `INFO`, format `%(asctime)s [%(levelname)s] %(message)s`.
- Docker images run as a non-root `appuser`, Python 3.13-slim base.
- Repo: `https://github.com/Dinofish32/infra.git` (branch `main`).

---

## Known limitations / current state

- Proxy only forwards **GET** to backends; `POST` is implemented on the backend
  but not proxied (the proxy's `POST` is used only for admin drain/restore).
- The `/api/*` endpoints are protected by an optional bearer token
  (`ADMIN_TOKEN`); if it is left unset the API is open, and the token is sent in
  clear text unless the proxy is fronted by TLS.
- Ports and backend count are hardcoded (not env-configurable).
- Metrics are **in-process** on the proxy and reset on restart; not
  persisted or scraped by a real Prometheus in this setup.
- Test suite covers `metrics.py` (incl. thread safety) and proxy failover;
  `main.py`, `dashboard.py`, and `loadgenerator.py` are not yet covered.
- The proxy retries the next healthy backend per-request on connection
  failure, so a backend dying between health checks is transparently failed
  over (and marked unhealthy immediately); `502` only occurs if *every* healthy
  backend is unreachable for that request.
- `docker-compose.yml` runs a single backend container hosting all three
  ports (not three separate backend instances).

---

## Suggested next steps (for agents extending this)

- Proxy `POST`/other methods through the load balancer.
- Make ports, backend count, and timeouts env-configurable.
- Extend test coverage to `main.py`, `dashboard.py`, and `loadgenerator.py`.
- Consider persistent/scrapeable metrics or a real Prometheus + Grafana.
