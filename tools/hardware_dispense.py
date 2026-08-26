"""Guarded one-shot Phase 2 dispense through the FastAPI boundary."""

from __future__ import annotations

import argparse
import json
import time
from uuid import uuid4

from fastapi.testclient import TestClient

from multiflo.api import create_app
from multiflo.driver import MultiFloDriver
from multiflo.models import CassetteType, FlowRate, PeristalticDispense
from multiflo.runner import ProtocolRunner
from multiflo.transport import D2xxConfig, D2xxTransport


AUTHORIZATION = "CASSETTE_PLATE_TUBING_IDLE_CONFIRMED"
TERMINAL_STATES = {
    "completed",
    "aborted",
    "failed",
    "unknown_execution_state",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-serial", required=True)
    parser.add_argument("--expected-description", default="MultiFlo")
    parser.add_argument("--cassette", required=True, choices=("1ul", "5ul", "10ul"))
    parser.add_argument("--volume-ul", required=True, type=int)
    parser.add_argument("--flow-rate", choices=("low", "medium"), default="medium")
    parser.add_argument("--pre-dispense-volume-ul", type=int, default=10)
    parser.add_argument("--pre-dispense-cycles", type=int, default=2)
    parser.add_argument("--read-timeout-ms", type=int, default=120_000)
    parser.add_argument("--request-id")
    parser.add_argument("--authorization", required=True)
    args = parser.parse_args()

    if args.authorization != AUTHORIZATION:
        parser.error(
            "--authorization must be CASSETTE_PLATE_TUBING_IDLE_CONFIRMED after "
            "the physical checklist has been completed"
        )
    if not 1_000 <= args.read_timeout_ms <= 300_000:
        parser.error("--read-timeout-ms must be between 1000 and 300000")

    step = PeristalticDispense(
        volume_ul=args.volume_ul,
        flow_rate=FlowRate(args.flow_rate),
        cassette_type=CassetteType(args.cassette),
        pre_dispense_volume_ul=args.pre_dispense_volume_ul,
        pre_dispense_cycles=args.pre_dispense_cycles,
    )
    transport = D2xxTransport(
        expected_serial=args.expected_serial,
        expected_description=args.expected_description,
        config=D2xxConfig(read_timeout_ms=args.read_timeout_ms),
    )
    runner = ProtocolRunner(
        MultiFloDriver(transport, expected_product_serial=args.expected_serial)
    )
    client = TestClient(create_app(runner))
    try:
        response = client.post(
            "/v1/runs",
            json={
                "request_id": args.request_id or f"phase2-dispense-{uuid4().hex[:12]}",
                "protocol": {
                    "name": "guarded Phase 2 hardware dispense",
                    "steps": [step.model_dump(mode="json")],
                },
                "operator_confirmed_idle": True,
            },
        )
        response.raise_for_status()
        run = response.json()
        deadline = time.monotonic() + (args.read_timeout_ms / 1000) + 30
        while run["state"] not in TERMINAL_STATES and time.monotonic() < deadline:
            time.sleep(0.1)
            polled = client.get(f"/v1/runs/{run['run_id']}")
            polled.raise_for_status()
            run = polled.json()
        print(json.dumps(run, indent=2))
        return 0 if run["state"] == "completed" else 2
    finally:
        runner.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
