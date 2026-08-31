"""Guarded Phase 5 end-to-end API check against the real instrument.

This drives a real loopback Uvicorn server with the real D2XX transport, so the
whole HTTP boundary is exercised, not just an in-process test client. It is
read-only by default. A motion step runs only when `--motion` is given together
with the matching authorization token.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import json
import socket
import threading
import time
from typing import Any
from uuid import uuid4

import httpx
import uvicorn

from multiflo.models import PlateType, Shake, Soak
from multiflo.logs import configure_logging
from multiflo.service import build_service


AUTHORIZATION = "PHASE5_E2E_SETUP_CONFIRMED"
TERMINAL_STATES = {
    "completed",
    "aborted",
    "failed",
    "unknown_execution_state",
}


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_server(server: uvicorn.Server, deadline: float) -> None:
    while time.monotonic() < deadline:
        if server.started:
            return
        time.sleep(0.05)
    raise TimeoutError("loopback API server did not start")


def _poll_until_terminal(
    client: httpx.Client,
    run: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while run["state"] not in TERMINAL_STATES and time.monotonic() < deadline:
        time.sleep(0.25)
        polled = client.get(f"/v1/runs/{run['run_id']}")
        polled.raise_for_status()
        run = polled.json()
    return run


def _checks(
    client: httpx.Client,
    *,
    motion: str,
    plate_type: str,
    duration_seconds: int,
    completion_timeout_seconds: float,
) -> tuple[dict[str, Any], bool]:
    report: dict[str, Any] = {}
    passed = True

    health = client.get("/v1/health")
    health.raise_for_status()
    report["health"] = health.json()

    device = client.get("/v1/device")
    device.raise_for_status()
    report["device"] = device.json()
    if not device.json()["connected"]:
        return report, False
    if not device.json()["serial_matches_expected"]:
        report["serial_check"] = "product serial does not match the allowlist"
        return report, False

    validation = client.post(
        "/v1/protocols/validate",
        json={
            "name": "phase 5 validation",
            "steps": [{"operation": "soak", "duration_seconds": 5}],
        },
    )
    report["validate_valid"] = validation.status_code
    passed &= validation.status_code == 200

    rejected = client.post(
        "/v1/protocols/validate",
        json={
            "name": "phase 5 invalid",
            "steps": [{"operation": "soak", "duration_seconds": 0}],
        },
    )
    report["validate_invalid"] = rejected.status_code
    passed &= rejected.status_code == 422

    missing = client.get(f"/v1/runs/{uuid4()}")
    report["unknown_run"] = missing.status_code
    passed &= missing.status_code == 404

    if motion == "none":
        report["motion"] = "skipped"
        return report, passed

    step = (
        Shake(duration_seconds=duration_seconds, plate_type=PlateType(plate_type))
        if motion == "shake"
        else Soak(duration_seconds=duration_seconds, plate_type=PlateType(plate_type))
    )
    request_id = f"phase5-{motion}-{uuid4().hex[:12]}"
    payload = {
        "request_id": request_id,
        "protocol": {
            "name": f"guarded Phase 5 {motion}",
            "steps": [step.model_dump(mode="json")],
        },
        "operator_confirmed_idle": True,
    }

    started = client.post("/v1/runs", json=payload)
    started.raise_for_status()
    report["start_status_code"] = started.status_code
    passed &= started.status_code == 202
    run = started.json()

    # A repeated request ID must return the same run without new motion.
    repeated = client.post("/v1/runs", json=payload)
    repeated.raise_for_status()
    report["repeat_status_code"] = repeated.status_code
    report["repeat_returned_same_run"] = repeated.json()["run_id"] == run["run_id"]
    passed &= repeated.status_code == 200
    passed &= repeated.json()["run_id"] == run["run_id"]

    # A different protocol while the run is active must be a conflict.
    conflict = client.post(
        "/v1/runs",
        json={
            "request_id": f"{request_id}-other",
            "protocol": {
                "name": "conflicting run",
                "steps": [{"operation": "soak", "duration_seconds": 5}],
            },
            "operator_confirmed_idle": True,
        },
    )
    report["conflict_status_code"] = conflict.status_code
    passed &= conflict.status_code == 409

    run = _poll_until_terminal(client, run, completion_timeout_seconds)
    report["run"] = run
    passed &= run["state"] == "completed"

    final_device = client.get("/v1/device")
    final_device.raise_for_status()
    report["device_after_run"] = final_device.json()
    passed &= final_device.json()["program_step_state"] == "ready"
    return report, passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-serial", required=True)
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--expected-description", default="MultiFlo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--motion",
        choices=("none", "shake", "soak"),
        default="none",
        help="lowest-risk motion step to include; 'none' keeps the check read-only",
    )
    parser.add_argument(
        "--plate-type",
        choices=("96_well", "96_deep_well", "384_well"),
        default="96_well",
    )
    parser.add_argument("--duration-seconds", type=int, default=5)
    parser.add_argument("--completion-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--authorization")
    args = parser.parse_args()
    configure_logging(level=getattr(args, 'log_level', 'INFO'),
                      log_file=getattr(args, 'log_file', None))

    if args.motion != "none" and args.authorization != AUTHORIZATION:
        parser.error(
            f"--authorization must be {AUTHORIZATION} before a motion end-to-end run"
        )
    if not 1 <= args.duration_seconds <= 60:
        parser.error("--duration-seconds must be between 1 and 60")
    if args.host != "127.0.0.1":
        parser.error("the guarded end-to-end check binds loopback only")

    port = args.port or _free_loopback_port()
    app, runner = build_service(
        expected_serial=args.expected_serial,
        expected_description=args.expected_description,
        completion_timeout_seconds=args.completion_timeout_seconds,
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host=args.host, port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, name="multiflo-e2e", daemon=True)
    thread.start()
    try:
        _wait_for_server(server, time.monotonic() + 20)
        with httpx.Client(
            base_url=f"http://{args.host}:{port}",
            timeout=args.completion_timeout_seconds + 30,
        ) as client:
            report, passed = _checks(
                client,
                motion=args.motion,
                plate_type=args.plate_type,
                duration_seconds=args.duration_seconds,
                completion_timeout_seconds=args.completion_timeout_seconds,
            )
        report["passed"] = passed
        print(json.dumps(report, indent=2))
        return 0 if passed else 2
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        runner.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
