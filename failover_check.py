"""Manual proxy failover checker.

Sends a steady stream of GET / requests through the proxy and shows, live,
which backend served each one (via the `Port` response header the backend sets)
along with a running tally of statuses. Use it to watch failover happen:

    1. Start the backends and proxy (see README). To make individual backends
       killable, run each on its own port in its own terminal, e.g.:
           python main.py 8001
           python main.py 8002
           python main.py 8003
           python proxy.py
    2. Run this checker:
           python failover_check.py
    3. Kill one backend (Ctrl-C its terminal) and watch traffic shift to the
       remaining backends with no (or a single) error, then restart it and
       watch it rejoin the rotation.

Usage:
    python failover_check.py [--url http://localhost:9999] [--rate 5]
"""
import argparse
import time
from collections import Counter

import requests


def main() -> None:
    parser = argparse.ArgumentParser(description="Live proxy failover checker")
    parser.add_argument("--url", default="http://localhost:9999",
                        help="proxy base URL")
    parser.add_argument("--rate", type=float, default=5.0,
                        help="requests per second")
    args = parser.parse_args()

    interval = 1.0 / args.rate if args.rate > 0 else 0.0
    by_backend: Counter[str] = Counter()
    by_status: Counter[str] = Counter()

    print(f"Hitting {args.url}/ at {args.rate:.0f} req/s. Ctrl-C to stop.\n")
    try:
        while True:
            try:
                response = requests.get(args.url + "/", timeout=5)
                by_status[str(response.status_code)] += 1
                served_by = response.headers.get("Port", "?")
                if response.status_code == 200:
                    by_backend[served_by] += 1
                    label = f"200 via port {served_by}"
                else:
                    label = f"{response.status_code} (proxy error)"
            except requests.RequestException as exc:
                by_status["conn-error"] += 1
                label = f"connection error: {type(exc).__name__}"

            backends = " ".join(f"{p}:{c}" for p, c in sorted(by_backend.items()))
            statuses = " ".join(f"{s}:{c}" for s, c in sorted(by_status.items()))
            print(f"\rlast={label:<40} | backends[{backends}] | status[{statuses}]",
                  end="", flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n\nSummary")
        print(f"  requests by backend port: {dict(by_backend)}")
        print(f"  requests by status:       {dict(by_status)}")


if __name__ == "__main__":
    main()
