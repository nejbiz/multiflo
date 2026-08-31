"""Run one guarded Phase 3 operation through the FastAPI boundary."""

from __future__ import annotations

import argparse
from uuid import uuid4

from fastapi.testclient import TestClient

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
from multiflo.service import build_service

from ._harness import (
    add_device_args,
    emit,
    exit_code_for,
    poll_until_terminal,
    require_token,
    start_logging,
)


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
            row_sections=args.row_sections,
            x_offset_steps=args.x_offset_steps,
            y_offset_steps=args.y_offset_steps,
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
    add_device_args(parser, marker=True)
    parser.add_argument(
        "--operation",
        required=True,
        choices=("dispense", "prime", "purge", "shake", "soak"),
    )
    parser.add_argument(
        "--plate-type",
        choices=("96_well", "96_deep_well", "384_well"),
        default="96_well",
    )
    parser.add_argument("--cassette", choices=("1ul", "5ul", "10ul"), default="5ul")
    parser.add_argument("--volume-ul", type=int)
    parser.add_argument("--flow-rate", choices=("low", "medium", "high"), default="medium")
    parser.add_argument("--duration-seconds", type=int)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="encode the step and print its body without opening the instrument",
    )
    parser.add_argument("--columns", default="all")
    parser.add_argument(
        "--row-sections",
        choices=("all", "odd", "even"),
        default="all",
    )
    parser.add_argument("--x-offset-steps", type=int, default=0)
    parser.add_argument("--y-offset-steps", type=int, default=0)
    parser.add_argument("--pre-dispense-volume-ul", type=int, default=10)
    parser.add_argument("--pre-dispense-cycles", type=int, default=2)
    parser.add_argument(
        "--no-move-carrier-home",
        action="store_false",
        dest="move_carrier_home",
    )
    parser.set_defaults(move_carrier_home=True)
    parser.add_argument("--request-id")
    parser.add_argument("--authorization", required=True)
    args = parser.parse_args()

    expected_authorization = f"PHASE3_{args.operation.upper()}_SETUP_CONFIRMED"
    if args.operation in {"dispense", "prime", "purge"} and args.volume_ul is None:
        parser.error("--volume-ul is required for dispense, prime, and purge")
    if args.operation in {"shake", "soak"} and args.duration_seconds is None:
        parser.error("--duration-seconds is required for shake and soak")
    if not 100 <= args.read_timeout_ms <= 300_000:
        parser.error("--read-timeout-ms must be between 100 and 300000")
    if not 1 <= args.completion_timeout_seconds <= 3600:
        parser.error("--completion-timeout-seconds must be between 1 and 3600")

    start_logging(args)
    step = _build_step(args)

    if args.dry_run:
        # Check what would go on the wire without touching the instrument.
        from multiflo.codec import encode_batch_start

        encoder = {
            "dispense": "encode_peristaltic_dispense",
            "prime": "encode_peristaltic_prime",
            "purge": "encode_peristaltic_purge",
            "shake": "encode_shake",
            "soak": "encode_soak",
        }[args.operation]
        import multiflo.codec as codec

        emit(
            {
                "dry_run": True,
                "requested_step": step.model_dump(mode="json"),
                "start_batch_body": encode_batch_start(step.plate_type).hex(" "),
                "command_body": getattr(codec, encoder)(step).hex(" "),
            }
        )
        return 0

    require_token(parser, args.authorization, expected_authorization)

    app, runner = build_service(
        expected_serial=args.expected_serial,
        expected_description=args.expected_description,
        crash_marker_path=args.marker_path,
        read_timeout_ms=args.read_timeout_ms,
        completion_timeout_seconds=args.completion_timeout_seconds,
    )
    client = TestClient(app)
    try:
        response = client.post(
            "/v1/runs",
            json={
                "request_id": args.request_id or f"phase3-{args.operation}-{uuid4().hex[:12]}",
                "protocol": {
                    "name": f"guarded Phase 3 {args.operation}",
                    "steps": [step.model_dump(mode="json")],
                },
                "operator_confirmed_idle": True,
            },
        )
        response.raise_for_status()
        run = response.json()

        def fetch() -> dict:
            polled = client.get(f"/v1/runs/{run['run_id']}")
            polled.raise_for_status()
            return polled.json()

        run = poll_until_terminal(
            fetch,
            completion_timeout_seconds=args.completion_timeout_seconds,
        )
        emit({"requested_step": step.model_dump(mode="json"), "run": run})
        return exit_code_for(run)
    finally:
        runner.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
