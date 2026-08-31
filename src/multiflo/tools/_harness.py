"""Shared scaffolding for the guarded hardware tools.

Every tool used to build its own transport, driver, runner, argparse block,
confirmation check and terminal-state poll. Five different read timeouts had
drifted apart across seven construction sites, three of them unreachable from
the command line, and one tool derived its poll deadline from the USB read
timeout rather than the completion timeout - so a long dispense was reported as
a failure while the driver was still waiting for it.

Just as important: no tool configured logging, so the runner's entire audit
trail (run_started / step_started / step_completed / run_terminal) was written
to an unconfigured root logger and discarded. `configure_logging` is called
here for every tool.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import time
from typing import Any

from multiflo.logs import configure_logging
from multiflo.runner import DEFAULT_CRASH_MARKER_PATH, TERMINAL_STATES, RunStatus


# One terminal-state set, derived from RunState rather than hand-copied.
TERMINAL_STATE_VALUES = frozenset(state.value for state in TERMINAL_STATES)

# The read timeout only bounds one D2XX read. Completion is bounded separately
# by the driver's completion timeout, and the tool's poll deadline is derived
# from that, never from the read timeout.
DEFAULT_READ_TIMEOUT_MS = 5_000
DEFAULT_COMPLETION_TIMEOUT_SECONDS = 600.0
POLL_GRACE_SECONDS = 30.0


def add_device_args(parser: argparse.ArgumentParser, *, marker: bool = False) -> None:
    """Add the device-selection, timeout, and logging flags every tool needs."""

    parser.add_argument("--expected-serial", required=True)
    parser.add_argument("--expected-description", default="MultiFlo")
    parser.add_argument("--read-timeout-ms", type=int, default=DEFAULT_READ_TIMEOUT_MS)
    parser.add_argument(
        "--completion-timeout-seconds",
        type=float,
        default=DEFAULT_COMPLETION_TIMEOUT_SECONDS,
    )
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
    if marker:
        parser.add_argument(
            "--marker-path",
            type=Path,
            default=DEFAULT_CRASH_MARKER_PATH,
        )


def start_logging(args: argparse.Namespace) -> None:
    configure_logging(level=args.log_level, log_file=getattr(args, "log_file", None))


def require_token(
    parser: argparse.ArgumentParser,
    supplied: str | None,
    expected: str,
) -> None:
    """Refuse to proceed without the exact operator confirmation token."""

    if supplied != expected:
        parser.error(f"--authorization must be {expected}")


def build_transport(args: argparse.Namespace):
    from multiflo.transport import D2xxConfig, D2xxTransport

    return D2xxTransport(
        expected_serial=args.expected_serial,
        expected_description=args.expected_description,
        config=D2xxConfig(read_timeout_ms=args.read_timeout_ms),
    )


def poll_until_terminal(
    fetch,
    *,
    completion_timeout_seconds: float,
    interval_seconds: float = 0.25,
) -> Any:
    """Poll `fetch` until the run reports a terminal state.

    The deadline comes from the driver's completion timeout plus a grace
    margin, so the tool never gives up while the driver is still legitimately
    waiting for the instrument.
    """

    deadline = time.monotonic() + completion_timeout_seconds + POLL_GRACE_SECONDS
    latest = None
    while time.monotonic() < deadline:
        latest = fetch()
        state = latest.get("state") if isinstance(latest, dict) else latest.state.value
        if state in TERMINAL_STATE_VALUES:
            return latest
        time.sleep(interval_seconds)
    return latest


def emit(payload: object) -> None:
    """Print one tool result as indented JSON on stdout.

    Structured events go to the log stream; this is the tool's own answer.
    """

    print(json.dumps(payload, indent=2, default=str))


def exit_code_for(status: RunStatus | dict | None) -> int:
    if status is None:
        return 2
    state = status.get("state") if isinstance(status, dict) else status.state.value
    return 0 if state == "completed" else 2
