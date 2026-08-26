"""Recover one stale Busy state with exactly one guarded End Batch command."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json

from multiflo.driver import MultiFloDriver
from multiflo.transport import D2xxConfig, D2xxTransport


AUTHORIZATION = "STATIONARY_AREA_CLEAR_LHC_CLOSED_END_BATCH_CONFIRMED"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-serial", required=True)
    parser.add_argument("--expected-description", default="MultiFlo")
    parser.add_argument("--authorization", required=True)
    args = parser.parse_args()
    if args.authorization != AUTHORIZATION:
        parser.error(f"--authorization must be {AUTHORIZATION}")

    transport = D2xxTransport(
        expected_serial=args.expected_serial,
        expected_description=args.expected_description,
        config=D2xxConfig(read_timeout_ms=65_000),
    )
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
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
