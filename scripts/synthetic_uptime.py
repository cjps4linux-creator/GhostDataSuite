#!/usr/bin/env python3
"""
Synthetic uptime checker for Ghost-Data Suite.

Pings /health every N seconds and exits non-zero if the service is unhealthy.
Designed to be run as a cron job or sidecar container for external monitoring.

Usage:
    python scripts/synthetic_uptime.py --url http://localhost:5000/health --interval 60 --timeout 30 --max-failures 3
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.request
import urllib.error


def check_health(url: str, timeout: float) -> bool:
    """Return True if /health returns 200, False otherwise."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Synthetic uptime checker for Ghost-Data Suite"
    )
    parser.add_argument(
        "--url",
        default="http://localhost:5000/health",
        help="Health check URL (default: http://localhost:5000/health)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Seconds between checks (default: 60)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Request timeout in seconds (default: 10)",
    )
    parser.add_argument(
        "--max-failures",
        type=int,
        default=3,
        help="Exit non-zero after N consecutive failures (default: 3)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single check and exit (useful for cron)",
    )
    args = parser.parse_args()

    consecutive_failures = 0

    while True:
        ok = check_health(args.url, args.timeout)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if ok:
            consecutive_failures = 0
            print(f"[{timestamp}] UP   {args.url}", flush=True)
        else:
            consecutive_failures += 1
            print(
                f"[{timestamp}] DOWN {args.url} "
                f"(failures: {consecutive_failures}/{args.max_failures})",
                flush=True,
            )
            if consecutive_failures >= args.max_failures:
                print(
                    f"[{timestamp}] CRITICAL: {args.max_failures} consecutive failures",
                    flush=True,
                )
                return 1

        if args.once:
            return 0 if ok else 1

        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
