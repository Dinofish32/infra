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
- **Load balancing:** `get_backend()` is a thread-safe round-robin that
  skips unhealthy backends; returns `None` if none are healthy.
- **Health checks:** background daemon thread hits each backend `/health`
  with `MAX_RETRIES = 2`, timeout `2s`. Poll interval is `5s` when all
  healthy, `1s` when any are down. Updates the shared `backend_status` map.
- **Request handling (`do_GET`):**
  - `GET /metrics` → returns `metrics.render_metrics()` (not counted as a
    proxied request).
  - Otherwise: records request + active connection, picks a backend,
    forwards the `GET`, streams the response back (stripping hop-by-hop
    headers: `Transfer-Encoding`, `Content-Encoding`, `Content-Length`,
    `Connection`).
  - No healthy backend → `503`. Backend connection error → `502`. Backend
    `5xx` → counted as a failure but response is still relayed.
  - Records latency for every request in a `finally` block.
- **Note:** only `GET` is proxied; there is no `do_POST` on the proxy yet.

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

---

## Configuration (environment variables)

| Variable       | Used by            | Default        | Purpose                                            |
| -------------- | ------------------ | -------------- | -------------------------------------------------- |
| `BIND_HOST`    | `main.py`, `proxy.py` | `0.0.0.0`   | Interface to bind. Use `127.0.0.1` for local-only. |
| `BACKEND_HOST` | `proxy.py`         | `127.0.0.1`    | Host where backends live (`backend` in Compose).   |
| `PROXY_URL`    | `loadgenerator.py` | `http://localhost:9999` | Target for the load generator.            |
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
- Builds two images: `Dockerfile.backend` (backend) and `Dockerfile.proxy`
  (proxy + `metrics.py` + `requests`).
- Only the proxy publishes a host port: `9999:9999`. Backends are reachable
  only inside the Compose network via the service name `backend`
  (`expose`d, not published).
- Then hit `http://localhost:9999/` and `http://localhost:9999/metrics`.

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
| (no healthy backend)| proxy   | GET    | `503`                                             |
| (backend error)     | proxy   | GET    | `502`                                             |

---

## Files

| File                 | Role                                                        |
| -------------------- | ----------------------------------------------------------- |
| `main.py`            | Backend HTTP servers (stdlib only).                         |
| `proxy.py`           | Load balancer + health checks + `/metrics`.                 |
| `metrics.py`         | Thread-safe in-process metrics + Prometheus rendering.      |
| `dashboard.py`       | Live terminal dashboard client for `/metrics`.              |
| `loadgenerator.py`   | Open-loop load generator against the proxy (percentiles).   |
| `requirements.txt`   | Python deps (`requests==2.32.3`); backend needs none.       |
| `Dockerfile.backend` | Image for `main.py` (Python 3.13-slim, non-root).           |
| `Dockerfile.proxy`   | Image for `proxy.py` + `metrics.py` (installs deps).        |
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

- Proxy only forwards **GET**; `POST` is implemented on the backend but not
  proxied.
- Ports and backend count are hardcoded (not env-configurable).
- Metrics are **in-process** on the proxy and reset on restart; not
  persisted or scraped by a real Prometheus in this setup.
- No automated tests.
- Health-check failover reacts on the check interval, not per-request; a
  backend that dies between checks will produce `502`s until the next check
  marks it unhealthy.
- `docker-compose.yml` runs a single backend container hosting all three
  ports (not three separate backend instances).

---

## Suggested next steps (for agents extending this)

- Proxy `POST`/other methods through the load balancer.
- Make ports, backend count, and timeouts env-configurable.
- Add tests (unit for `metrics.py`, integration for proxy failover).
- Consider persistent/scrapeable metrics or a real Prometheus + Grafana.
- Per-request retry to the next healthy backend on connection failure.
