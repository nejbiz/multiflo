"""Clear a retained crash marker after read-only or physical reconciliation."""

from __future__ import annotations

import argparse

from multiflo.driver import MultiFloDriver
from multiflo.runner import ProtocolRunner

from ._harness import add_device_args, build_transport, emit, start_logging


ACKNOWLEDGEMENT = "INTERRUPTED_RUN_PHYSICALLY_RECONCILED"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_device_args(parser, marker=True)
    parser.add_argument("--operator-acknowledgement")
    args = parser.parse_args()

    if (
        args.operator_acknowledgement is not None
        and args.operator_acknowledgement != ACKNOWLEDGEMENT
    ):
        parser.error(f"--operator-acknowledgement must be {ACKNOWLEDGEMENT}")

    start_logging(args)
    transport = build_transport(args)
    runner = ProtocolRunner(
        MultiFloDriver(transport, expected_product_serial=args.expected_serial),
        crash_marker_path=args.marker_path,
    )
    try:
        retained = runner.retained_marker
        interrupted = None
        if retained is not None:
            interrupted = {
                "run_id": str(retained.run_id),
                "protocol_name": retained.protocol_name,
                "current_step": retained.current_step,
                "last_confirmed_step": retained.last_confirmed_step,
                "updated_at": retained.updated_at.isoformat(),
            }
        state = runner.reconcile_startup(
            operator_acknowledged=args.operator_acknowledgement is not None,
        )
        emit(
            {
                "controller_state": state.value,
                "marker_path": str(args.marker_path.resolve()),
                "marker_exists": args.marker_path.exists(),
                "interrupted_run": interrupted,
                "marker_unreadable": runner.marker_unreadable,
                "method": (
                    "operator_acknowledgement"
                    if args.operator_acknowledgement is not None
                    else "device_ready"
                ),
            }
        )
        return 0
    finally:
        runner.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
