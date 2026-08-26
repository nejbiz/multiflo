"""Guarded read-only Phase 1 identity, firmware, and module inventory."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json

from multiflo.driver import MultiFloDriver
from multiflo.transport import D2xxConfig, D2xxTransport


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-serial", required=True)
    parser.add_argument("--expected-description", default="MultiFlo")
    args = parser.parse_args()

    transport = D2xxTransport(
        expected_serial=args.expected_serial,
        expected_description=args.expected_description,
        config=D2xxConfig(read_timeout_ms=2_000),
    )
    with MultiFloDriver(transport) as driver:
        driver.communication_test()
        inventory = asdict(driver.inspect_device())
        inventory["basecode"]["reserved"] = inventory["basecode"]["reserved"].hex(" ")
        inventory["modules"]["primary_cassette"] = inventory["modules"][
            "primary_cassette"
        ].value
        inventory["ftdi"] = {
            "serial": transport.device_info.serial_number,
            "description": transport.device_info.description,
            "vid": f"{transport.device_info.vendor_id:04x}",
            "pid": f"{transport.device_info.product_id:04x}",
            "location_id": transport.device_info.location_id,
            "device_type": transport.device_info.device_type,
            "latency_timer_ms": transport.latency_timer_ms,
        }
    print(json.dumps(inventory, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
