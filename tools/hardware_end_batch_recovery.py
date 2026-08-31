"""Recover one stale Busy state with exactly one guarded End Batch command."""

from __future__ import annotations

import argparse
from dataclasses import asdict

from multiflo.driver import MultiFloDriver

from ._harness import add_device_args, build_transport, emit, start_logging


AUTHORIZATION = "STATIONARY_AREA_CLEAR_LHC_CLOSED_END_BATCH_CONFIRMED"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_device_args(parser)
    parser.add_argument("--authorization", required=True)
    args = parser.parse_args()
    if args.authorization != AUTHORIZATION:
        parser.error(f"--authorization must be {AUTHORIZATION}")

    start_logging(args)
    transport = build_transport(args)
    with MultiFloDriver(transport) as driver:
        result = driver.recover_end_batch(operator_confirmed_stationary=True)
    output = {
        "before": asdict(result.before),
        "end_batch_device_status": int.from_bytes(
            result.end_batch.response.body[:2], "little"
        ),
        "after": asdict(result.after),
        "sent_once": True,
    }
    output["before"]["state"] = result.before.state.name.lower()
    output["after"]["state"] = result.after.state.name.lower()
    emit(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
