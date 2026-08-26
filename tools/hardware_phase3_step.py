"""Run one guarded Phase 3 operation through the FastAPI boundary."""

from __future__ import annotations

import argparse
import json
import time

from fastapi.testclient import TestClient

from multiflo.api import create_app
from multiflo.driver import MultiFloDriver
from multiflo.models import (
    CassetteType,
    FlowRate,
    PeristalticDispense,
    PeristalticPrime,
    PeristalticPurge,
    PlateType,
    Shake,
    Soak,
)
from multiflo.runner import ProtocolRunner
from multiflo.transport import D2xxConfig, D2xxTransport


TERMINAL_STATES = {
    "completed",
    "aborted",
    "failed",
    "unknown_execution_state",
}


def _build_step(args: argparse.Namespace):
    if args.operation == "dispense":
        columns: str | tuple[int, ...]
        if args.columns == "all":
            columns = "all"
        else:
            columns = tuple(int(value) for value in args.columns.split(","))
        return PeristalticDispense(
            volume_ul=args.volume_ul,
            flow_rate=FlowRate(args.flow_rate),
            cassette_type=CassetteType(args.cassette),
            plate_type=PlateType(args.plate_type),
            pre_dispense_volume_ul=args.pre_dispense_volume_ul,
            pre_dispense_cycles=args.pre_dispense_cycles,
            columns=columns,
        )
    if args.operation == "prime":
        return PeristalticPrime(
            volume_ul=args.volume_ul,
            flow_rate=FlowRate(args.flow_rate),
            cassette_type=CassetteType(args.cassette),
            plate_type=PlateType(args.plate_type),
        )
    if args.operation == "purge":
        return PeristalticPurge(
            volume_ul=args.volume_ul,
            flow_rate=FlowRate(args.flow_rate),
            cassette_type=CassetteType(args.cassette),
            plate_type=PlateType(args.plate_type),
        )
    if args.operation == "shake":
        return Shake(
            duration_seconds=args.duration_seconds,
            move_carrier_home=args.move_carrier_home,
            plate_type=PlateType(args.plate_type),
        )
    return Soak(
        duration_seconds=args.duration_seconds,
        move_carrier_home=args.move_carrier_home,
        plate_type=PlateType(args.plate_type),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-serial", required=True)
    parser.add_argument("--expected-description", default="MultiFlo")
    parser.add_argument(
        "--operation",
        required=True,
        choices=("dispense", "prime", "purge", "shake", "soak"),
    )
    parser.add_argument("--plate-type", choices=("96_well", "96_deep_well"), default="96_well")
    parser.add_argument("--cassette", choices=("1ul", "5ul", "10ul"), default="5ul")
    parser.add_argument("--volume-ul", type=int)
    parser.add_argument("--flow-rate", choices=("low", "medium", "high"), default="medium")
    parser.add_argument("--duration-seconds", type=int)
    parser.add_argument("--columns", default="all")
    parser.add_argument("--pre-dispense-volume-ul", type=int, default=10)
    parser.add_argument("--pre-dispense-cycles", type=int, default=2)
    parser.add_argument(
        "--no-move-carrier-home",
        action="store_false",
        dest="move_carrier_home",
    )
    parser.set_defaults(move_carrier_home=True)
    parser.add_argument("--read-timeout-ms", type=int, default=120_000)
    parser.add_argument("--completion-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--authorization", required=True)
    args = parser.parse_args()

    expected_authorization = f"PHASE3_{args.operation.upper()}_SETUP_CONFIRMED"
    if args.authorization != expected_authorization:
        parser.error(f"--authorization must be {expected_authorization}")
    if args.operation in {"dispense", "prime", "purge"} and args.volume_ul is None:
        parser.error("--volume-ul is required for dispense, prime, and purge")
    if args.operation in {"shake", "soak"} and args.duration_seconds is None:
        parser.error("--duration-seconds is required for shake and soak")
    if not 1_000 <= args.read_timeout_ms <= 300_000:
        parser.error("--read-timeout-ms must be between 1000 and 300000")
    if not 1 <= args.completion_timeout_seconds <= 3600:
        parser.error("--completion-timeout-seconds must be between 1 and 3600")

    step = _build_step(args)
    transport = D2xxTransport(
        expected_serial=args.expected_serial,
        expected_description=args.expected_description,
        config=D2xxConfig(read_timeout_ms=args.read_timeout_ms),
    )
    runner = ProtocolRunner(
        MultiFloDriver(
            transport,
            expected_product_serial=args.expected_serial,
            completion_timeout_seconds=args.completion_timeout_seconds,
        )
    )
    client = TestClient(create_app(runner))
    try:
        response = client.post(
            "/v1/runs",
            json={
                "protocol": {
                    "name": f"guarded Phase 3 {args.operation}",
                    "steps": [step.model_dump(mode="json")],
                },
                "operator_confirmed_idle": True,
            },
        )
        response.raise_for_status()
        run = response.json()
        deadline = time.monotonic() + args.completion_timeout_seconds + 30
        while run["state"] not in TERMINAL_STATES and time.monotonic() < deadline:
            time.sleep(0.1)
            polled = client.get(f"/v1/runs/{run['run_id']}")
            polled.raise_for_status()
            run = polled.json()
        print(json.dumps({"requested_step": step.model_dump(mode="json"), "run": run}, indent=2))
        return 0 if run["state"] == "completed" else 2
    finally:
        runner.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
