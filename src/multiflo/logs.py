"""One JSON-line log format shared by the transport, driver, and runner.

Every layer emits the same shape so a failed run can be traced from the HTTP
request down to the bytes on the wire in a single stream:

    {"timestamp":"...","event":"exchange","command_id":"0x0092",...}

`configure_logging` is what makes those events reachable. Without it the root
logger is unconfigured, `logging.lastResort` filters at WARNING, and every
INFO/DEBUG event is silently discarded.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path


LOGGER_NAME = "multiflo"


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: object) -> None:
    """Emit one structured event as a single JSON line."""

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    logger.log(level, json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str))


def configure_logging(
    *,
    level: str | int = logging.INFO,
    log_file: str | Path | None = None,
) -> None:
    """Send `multiflo` events to stderr and optionally to a JSON-lines file.

    Safe to call more than once; existing handlers are replaced so a tool and
    the service never double-log.
    """

    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter("%(message)s")
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    logger.setLevel(level)
    # These events are the audit trail; never let a parent handler reformat or
    # drop them.
    logger.propagate = False


def truncated_hex(data: bytes, limit: int = 64) -> str:
    """Hex for a log line, bounded so a long body cannot flood the stream."""

    if len(data) <= limit:
        return data.hex(" ")
    return f"{data[:limit].hex(' ')} ...+{len(data) - limit}B"
