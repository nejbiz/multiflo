"""Print or save the generated OpenAPI document. Never touches hardware."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from multiflo.api import create_app
from multiflo.driver import MultiFloDriver
from multiflo.runner import ProtocolRunner
from multiflo.transport import NullTransport


def openapi_document(expected_serial: str = "unset") -> dict:
    """Build the schema with a scripted transport that is never opened."""

    runner = ProtocolRunner(
        MultiFloDriver(NullTransport(), expected_product_serial=expected_serial)
    )
    try:
        return create_app(runner).openapi()
    finally:
        runner.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    document = json.dumps(openapi_document(), indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(document + "\n", encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
