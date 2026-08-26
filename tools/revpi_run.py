"""Run one guarded protocol on a RevPi over the FT232 serial port.

This is the prototyping entry point for the RevPi, where the MultiFlo's FTDI
FT232 is exposed as a `/dev/ttyUSB*` virtual COM port by the `ftdi_sio` kernel
driver. It drives the same `ProtocolRunner`, driver, and motion lifecycle used
by the FastAPI path; only the byte transport differs and the FastAPI/HTTP layer
is bypassed so the Pi needs no web dependencies.

Modes:
  --check      read-only: communication test + full device inventory, no motion.
  --protocol   run a small dispense and/or shake protocol (motion). Requires the
               matching authorization token.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import time
from uuid import UUID

from multiflo.driver import MultiFloDriver
from multiflo.models import (
    CassetteType,
    FlowRate,
    PeristalticDispense,
    PlateType,
    Protocol,
    Shake,
)
from multiflo.runner import ProtocolRunner, RunState
from multiflo.transport import SerialByteTransport, SerialConfig


AUTHORIZATION = "REVPI_DISPENSE_SHAKE_SETUP_CONFIRMED"
TERMINAL_STATES = {
    RunState.COMPLETED,
    RunState.ABORTED,
    RunState.FAILED,
    RunState.UNKNOWN_EXECUTION_STATE,
}


def _build_driver(args: argparse.Namespace) -> MultiFloDriver:
    transport = SerialByteTransport(
        args.port,
        config=SerialConfig(read_timeout_seconds=args.read_timeout_seconds),
    )
    return MultiFloDriver(
        transport,
        expected_product_serial=args.expected_serial,
        completion_timeout_seconds=args.completion_timeout_seconds,
    )


def _check(args: argparse.Namespace) -> int:
    """Read-only communication test and inventory. Sends no motion command."""

    driver = _build_driver(args)
    with driver:
        driver.communication_test()
        program = asdict(driver.query_program_step_status())
        program["state"] = program["state"].name.lower()
        info = asdict(driver.inspect_device())
        info["basecode"]["reserved"] = info["basecode"]["reserved"].hex(" ")
        info["modules"]["primary_cassette"] = info["modules"]["primary_cassette"].value
        report = {
            "port": args.port,
            "program_step_status": program,
            "device": info,
            "serial_matches_expected": info["product_serial_number"]
            == args.expected_serial,
        }
    print(json.dumps(report, indent=2))
    return 0 if report["serial_matches_expected"] else 2


def _build_protocol(args: argparse.Namespace) -> Protocol:
    steps: list = []
    plate = PlateType(args.plate_type)
    if args.dispense:
        steps.append(
            PeristalticDispense(
                plate_type=plate,
                volume_ul=args.volume_ul,
                flow_rate=FlowRate(args.flow_rate),
                cassette_type=CassetteType(args.cassette),
                pre_dispense_volume_ul=args.pre_dispense_volume_ul,
                pre_dispense_cycles=args.pre_dispense_cycles,
            )
        )
    if args.shake:
        steps.append(Shake(plate_type=plate, duration_seconds=args.shake_seconds))
    if not steps:
        raise SystemExit("nothing to run: pass --dispense and/or --shake")
    return Protocol(name="RevPi dispense/shake prototype", steps=steps)


def _run_protocol(args: argparse.Namespace) -> int:
    protocol = _build_protocol(args)
    runner = ProtocolRunner(_build_driver(args), crash_marker_path=args.marker_path)
    try:
        result = runner.start(
            protocol,
            operator_confirmed_idle=True,
            request_id=f"revpi-{int(time.time())}",
        )
        run_id: UUID = result.status.run_id
        deadline = time.monotonic() + args.completion_timeout_seconds + 30
        status = result.status
        while status.state not in TERMINAL_STATES and time.monotonic() < deadline:
            time.sleep(0.25)
            fetched = runner.get(run_id)
            if fetched is not None:
                status = fetched
        print(
            json.dumps(
                {
                    "protocol": protocol.model_dump(mode="json"),
                    "controller_state": runner.state.value,
                    "run": status.model_dump(mode="json"),
                },
                indent=2,
            )
        )
        return 0 if status.state is RunState.COMPLETED else 2
    finally:
        runner.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--expected-serial", required=True)
    parser.add_argument("--read-timeout-seconds", type=float, default=3.0)
    parser.add_argument("--completion-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--marker-path", default=".multiflo-active-run.json")

    parser.add_argument("--check", action="store_true", help="read-only inventory only")

    parser.add_argument("--dispense", action="store_true")
    parser.add_argument("--shake", action="store_true")
    parser.add_argument(
        "--plate-type",
        choices=("96_well", "96_deep_well", "384_well"),
        default="96_well",
    )
    parser.add_argument("--cassette", choices=("1ul", "5ul", "10ul"), default="1ul")
    parser.add_argument("--volume-ul", type=int, default=10)
    parser.add_argument("--flow-rate", choices=("low", "medium", "high"), default="medium")
    parser.add_argument("--pre-dispense-volume-ul", type=int, default=0)
    parser.add_argument("--pre-dispense-cycles", type=int, default=0)
    parser.add_argument("--shake-seconds", type=int, default=5)
    parser.add_argument("--authorization")
    args = parser.parse_args()

    if args.check:
        return _check(args)

    if args.authorization != AUTHORIZATION:
        parser.error(
            f"--authorization must be {AUTHORIZATION} to run motion, or pass --check"
        )
    return _run_protocol(args)


if __name__ == "__main__":
    raise SystemExit(main())
