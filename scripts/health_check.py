#!/usr/bin/env python3
"""Simple health check script for Docker HEALTHCHECK directive."""
from __future__ import annotations

import sys

import httpx


def main() -> None:
    try:
        resp = httpx.get("http://localhost:8000/health", timeout=5.0)
        if resp.status_code == 200:
            sys.exit(0)
        print(f"Health check returned {resp.status_code}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Health check failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
