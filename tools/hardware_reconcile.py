"""Clear a retained crash marker after read-only or physical reconciliation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from multiflo.driver import MultiFloDriver
from multiflo.runner import DEFAULT_CRASH_MARKER_PATH, ProtocolRunner
from multiflo.transport import D2xxTransport


ACKNOWLEDGEMENT = "INTERRUPTED_RUN_PHYSICALLY_RECONCILED"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-serial", required=True)
    parser.add_argument("--expected-description", default="MultiFlo")
    parser.add_argument(
        "--marker-path",
        type=Path,
        default=DEFAULT_CRASH_MARKER_PATH,
    )
    parser.add_argument("--operator-acknowledgement")
    args = parser.parse_args()

    if (
        args.operator_acknowledgement is not None
        and args.operator_acknowledgement != ACKNOWLEDGEMENT
    ):
        parser.error(f"--operator-acknowledgement must be {ACKNOWLEDGEMENT}")

    transport = D2xxTransport(
        expected_serial=args.expected_serial,
        expected_description=args.expected_description,
    )
    runner = ProtocolRunner(
        MultiFloDriver(transport, expected_product_serial=args.expected_serial),
        crash_marker_path=args.marker_path,
    )
    try:
        state = runner.reconcile_startup(
            operator_acknowledged=args.operator_acknowledgement is not None,
        )
        print(
            json.dumps(
                {
                    "controller_state": state.value,
                    "marker_path": str(args.marker_path.resolve()),
                    "marker_exists": args.marker_path.exists(),
                    "method": (
                        "operator_acknowledgement"
                        if args.operator_acknowledgement is not None
                        else "device_ready"
                    ),
                },
                indent=2,
            )
        )
        return 0
    finally:
        runner.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
