"""Guarded read-only Phase 1 identity, firmware, and module inventory."""

from __future__ import annotations

import argparse
from dataclasses import asdict

from multiflo.driver import MultiFloDriver

from ._harness import add_device_args, build_transport, emit, start_logging


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_device_args(parser)
    args = parser.parse_args()

    start_logging(args)
    transport = build_transport(args)
    exit_code = 0
    with MultiFloDriver(transport) as driver:
        driver.communication_test()
        program_status = asdict(driver.query_program_step_status())
        program_status["state"] = program_status["state"].name.lower()
        if program_status["state"] == "ready":
            inventory = asdict(driver.inspect_device())
            inventory["basecode"]["reserved"] = inventory["basecode"]["reserved"].hex(" ")
            inventory["modules"]["primary_cassette"] = inventory["modules"][
                "primary_cassette"
            ].value
        else:
            inventory = {
                "inventory_skipped": "program-step state is not ready",
            }
            exit_code = 2
        inventory["ftdi"] = {
            "serial": transport.device_info.serial_number,
            "description": transport.device_info.description,
            "vid": f"{transport.device_info.vendor_id:04x}",
            "pid": f"{transport.device_info.product_id:04x}",
            "location_id": transport.device_info.location_id,
            "device_type": transport.device_info.device_type,
            "latency_timer_ms": transport.latency_timer_ms,
        }
        inventory["program_step_status"] = program_status
    emit(inventory)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
