"""Guarded Phase 1 hardware smoke test; sends only command 0x0073."""

from __future__ import annotations

import argparse

from multiflo.driver import MultiFloDriver

from ._harness import add_device_args, build_transport, emit, start_logging


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_device_args(parser)
    parser.add_argument("--repetitions", type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 100:
        parser.error("--repetitions must be between 1 and 100")

    start_logging(args)
    transport = build_transport(args)
    with MultiFloDriver(transport) as driver:
        results = []
        for index in range(args.repetitions):
            exchange = driver.communication_test()
            results.append(
                {
                    "index": index + 1,
                    "message_id": exchange.response.message_id,
                    "response_body_hex": exchange.response.body.hex(" "),
                    "indications": len(exchange.indications),
                }
            )
        output = {
            "device": {
                "serial": transport.device_info.serial_number,
                "description": transport.device_info.description,
                "vid": f"{transport.device_info.vendor_id:04x}",
                "pid": f"{transport.device_info.product_id:04x}",
                "latency_timer_ms": transport.latency_timer_ms,
            },
            "command": "0x0073",
            "results": results,
        }
    emit(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

