"""Single-worker loopback service entry point for the MultiFlo API."""

from __future__ import annotations

import argparse
import ipaddress
from pathlib import Path

from fastapi import FastAPI

from .api import create_app
from .driver import MultiFloDriver
from .logs import configure_logging
from .runner import DEFAULT_CRASH_MARKER_PATH, ProtocolRunner
from .transport import D2xxConfig, D2xxTransport


def is_loopback_host(host: str) -> bool:
    """Return whether a bind host is a loopback address."""

    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def build_service(
    *,
    expected_serial: str,
    expected_description: str = "MultiFlo",
    crash_marker_path: str | Path = DEFAULT_CRASH_MARKER_PATH,
    read_timeout_ms: int = 5_000,
    completion_timeout_seconds: float = 600.0,
) -> tuple[FastAPI, ProtocolRunner]:
    """Build one runner that owns one instrument, plus its API application."""

    transport = D2xxTransport(
        expected_serial=expected_serial,
        expected_description=expected_description,
        config=D2xxConfig(read_timeout_ms=read_timeout_ms),
    )
    runner = ProtocolRunner(
        MultiFloDriver(
            transport,
            expected_product_serial=expected_serial,
            completion_timeout_seconds=completion_timeout_seconds,
        ),
        crash_marker_path=crash_marker_path,
    )
    return create_app(runner), runner


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-serial", required=True)
    parser.add_argument("--expected-description", default="MultiFlo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--marker-path", type=Path, default=DEFAULT_CRASH_MARKER_PATH)
    parser.add_argument("--read-timeout-ms", type=int, default=5_000)
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="DEBUG logs every command exchange with its bytes and timing",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        help="also append JSON-line events here, for an auditable run record",
    )
    parser.add_argument("--completion-timeout-seconds", type=float, default=600.0)
    parser.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="bind a non-loopback address; one instrument still allows one worker only",
    )
    args = parser.parse_args()

    if not is_loopback_host(args.host) and not args.allow_non_loopback:
        parser.error(
            f"--host {args.host} is not a loopback address; development runs bind "
            "127.0.0.1 unless --allow-non-loopback is given"
        )

    configure_logging(level=args.log_level, log_file=args.log_file)
    app, runner = build_service(
        expected_serial=args.expected_serial,
        expected_description=args.expected_description,
        crash_marker_path=args.marker_path,
        read_timeout_ms=args.read_timeout_ms,
        completion_timeout_seconds=args.completion_timeout_seconds,
    )
    import uvicorn  # Imported late so tests never need a server.

    try:
        # One worker only: several workers would compete for one USB handle.
        uvicorn.run(app, host=args.host, port=args.port, workers=1, log_level="info")
    finally:
        runner.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
