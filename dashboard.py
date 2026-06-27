"""Live terminal dashboard for the proxy's /metrics endpoint.

Usage:
    python dashboard.py [--interval 1.0] [--url http://localhost:9999/metrics]
"""
import argparse
import ctypes
import os
import shutil
import sys
import time

import requests


def _enable_ansi() -> None:
    """Enable ANSI/VT escape sequences and UTF-8 output on Windows terminals."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if os.name != "nt":
        return
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(-11)
    mode = ctypes.c_uint32()
    if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)

DEFAULT_URL = "http://localhost:9999/metrics"
DEFAULT_INTERVAL = 1.0

CLEAR_SCREEN = "\033[2J\033[3J"
CURSOR_HOME = "\033[H"
CLEAR_LINE = "\033[K"
CLEAR_DOWN = "\033[J"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def parse_metrics(text: str) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            continue
        key, raw = parts
        try:
            metrics[key] = float(raw)
        except ValueError:
            continue
    return metrics


def fetch_metrics(url: str, timeout: float) -> tuple[dict[str, float] | None, str | None]:
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        return None, str(exc)
    return parse_metrics(response.text), None


def bar(value: float, max_value: float, width: int = 30) -> str:
    if max_value <= 0:
        filled = 0
    else:
        filled = int(min(value / max_value, 1.0) * width)
    return "█" * filled + DIM + "─" * (width - filled) + RESET


def color_for_latency(ms: float) -> str:
    if ms < 50:
        return GREEN
    if ms < 200:
        return YELLOW
    return RED


def color_for_failures(failures: float, total: float) -> str:
    if total <= 0 or failures == 0:
        return GREEN
    ratio = failures / total
    if ratio < 0.01:
        return YELLOW
    return RED


def render(metrics: dict[str, float], url: str, error: str | None) -> str:
    width = min(shutil.get_terminal_size((80, 24)).columns, 70)
    now = time.strftime("%H:%M:%S")
    lines: list[str] = []

    lines.append(f"{BOLD}{CYAN}╔{'═' * (width - 2)}╗{RESET}")
    title = " PROXY OBSERVABILITY DASHBOARD "
    pad = (width - 2 - len(title)) // 2
    lines.append(
        f"{BOLD}{CYAN}║{RESET}{' ' * pad}{BOLD}{title}{RESET}"
        f"{' ' * (width - 2 - pad - len(title))}{BOLD}{CYAN}║{RESET}"
    )
    lines.append(f"{BOLD}{CYAN}╚{'═' * (width - 2)}╝{RESET}")
    lines.append(f"{DIM}{url}  ·  updated {now}{RESET}")
    lines.append("")

    if error is not None:
        lines.append(f"{RED}{BOLD}  ✗ proxy unreachable{RESET}")
        lines.append(f"{DIM}    {error}{RESET}")
        lines.append("")
        lines.append(f"{DIM}  retrying...{RESET}")
        return "\n".join(lines)

    rps = metrics.get("http_requests_per_second", 0.0)
    total = metrics.get("http_requests_total", 0.0)
    failures = metrics.get("http_request_failures_total", 0.0)
    active = metrics.get("http_active_connections", 0.0)
    lat_avg = metrics.get("http_request_latency_ms_avg", 0.0)
    lat_p50 = metrics.get("http_request_latency_ms_p50", 0.0)
    lat_p95 = metrics.get("http_request_latency_ms_p95", 0.0)
    lat_p99 = metrics.get("http_request_latency_ms_p99", 0.0)

    fail_color = color_for_failures(failures, total)

    lines.append(f"  {BOLD}Requests/sec{RESET}   {CYAN}{BOLD}{rps:>8.1f}{RESET}  {bar(rps, 100)}")
    lines.append(f"  {BOLD}Active conns{RESET}   {CYAN}{BOLD}{int(active):>8}{RESET}  {bar(active, 50)}")
    lines.append("")
    lines.append(f"  {BOLD}Total served{RESET}   {int(total):>8}")
    lines.append(f"  {BOLD}Failures{RESET}       {fail_color}{int(failures):>8}{RESET}")
    lines.append("")
    lines.append(f"  {BOLD}Latency (ms){RESET}")
    lines.append(f"    avg   {color_for_latency(lat_avg)}{lat_avg:>8.2f}{RESET}  {bar(lat_avg, 500)}")
    lines.append(f"    p50   {color_for_latency(lat_p50)}{lat_p50:>8.2f}{RESET}")
    lines.append(f"    p95   {color_for_latency(lat_p95)}{lat_p95:>8.2f}{RESET}")
    lines.append(f"    p99   {color_for_latency(lat_p99)}{lat_p99:>8.2f}{RESET}")
    lines.append("")
    lines.append(f"{DIM}  Ctrl-C to quit{RESET}")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Live proxy metrics dashboard")
    parser.add_argument("--url", default=DEFAULT_URL, help="metrics endpoint URL")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="refresh seconds")
    args = parser.parse_args()

    _enable_ansi()
    sys.stdout.write(HIDE_CURSOR + CLEAR_SCREEN)
    try:
        while True:
            metrics, error = fetch_metrics(args.url, timeout=args.interval)
            frame = render(metrics or {}, args.url, error)
            body = frame.replace("\n", CLEAR_LINE + "\n") + CLEAR_LINE
            sys.stdout.write(CURSOR_HOME + body + CLEAR_DOWN)
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(SHOW_CURSOR + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
