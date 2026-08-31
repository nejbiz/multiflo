"""MultiFlo packet framing and incremental response decoding."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import struct
from typing import Literal

from .errors import ProtocolError, ValidationError
from .models import (
    CASSETTE_CODES,
    PLATE_COLUMN_COUNTS,
    FlowRate,
    PeristalticDispense,
    PeristalticPrime,
    PeristalticPurge,
    PlateType,
    Shake,
    Soak,
)


HEADER_SIZE = 11
MAX_BODY_LENGTH = 0xFFFF
_HEADER_WITHOUT_CHECKSUM = struct.Struct("<BBHBHH")
_HEADER = struct.Struct("<BBHBHHH")
_PERISTALTIC_DISPENSE = struct.Struct("<BHBBbbHHB6sBB4s")
_PERISTALTIC_PRIME_PURGE = struct.Struct("<BHHBBBB2s")
_SHAKE_SOAK = struct.Struct("<BBHBBH4s")

_PLATE_CODES = {
    PlateType.WELL_384: 1,
    PlateType.WELL_96: 4,
    PlateType.DEEP_WELL_96: 5,
}
_DISPENSE_HEIGHTS = {
    PlateType.WELL_384: 333,
    PlateType.WELL_96: 336,
    PlateType.DEEP_WELL_96: 929,
}
_FLOW_CODES = {FlowRate.LOW: 0, FlowRate.MEDIUM: 1, FlowRate.HIGH: 2}
_ROW_SKIP_MASKS = {
    "all": 0x00,
    # The 384-well row field is an inverted skip mask over the two cassette
    # sections. calib25's 1011 map clears bit 1 and dispensed only odd rows on
    # hardware; calib30's 0111 map clears bit 0 and selects the even rows.
    "odd": 0x02,
    "even": 0x01,
}
# LHC keeps the unused trailing positions of a partial column map enabled for
# the deep-well and 384-well geometries and clears them for standard 96-well.
_UNUSED_MAP_POSITIONS_ENABLED = {
    PlateType.WELL_384: True,
    PlateType.WELL_96: False,
    PlateType.DEEP_WELL_96: True,
}


class MessageClass(IntEnum):
    REQUEST = 1
    RESPONSE = 2
    INDICATION = 3


class Endpoint(IntEnum):
    PC = 1
    INSTRUMENT = 2


def checksum(data_without_checksum: bytes) -> int:
    """Return the 16-bit additive two's-complement packet checksum."""

    return (-sum(data_without_checksum)) & 0xFFFF


@dataclass(frozen=True, slots=True)
class Frame:
    message_class: int
    destination: int
    command_id: int
    source: int
    message_id: int
    body: bytes = b""

    def __post_init__(self) -> None:
        for name in ("message_class", "destination", "source"):
            value = int(getattr(self, name))
            if not 0 <= value <= 0xFF:
                raise ValidationError(f"{name} must fit in one byte")
        for name in ("command_id", "message_id"):
            value = int(getattr(self, name))
            if not 0 <= value <= 0xFFFF:
                raise ValidationError(f"{name} must fit in two bytes")
        if not isinstance(self.body, bytes):
            raise ValidationError("body must be bytes")
        if len(self.body) > MAX_BODY_LENGTH:
            raise ValidationError("body exceeds the 16-bit packet length")

    def encode(self) -> bytes:
        prefix = _HEADER_WITHOUT_CHECKSUM.pack(
            int(self.message_class),
            int(self.destination),
            self.command_id,
            int(self.source),
            self.message_id,
            len(self.body),
        )
        packet_checksum = checksum(prefix + self.body)
        return prefix + struct.pack("<H", packet_checksum) + self.body


def encode_request(command_id: int, message_id: int = 0, body: bytes = b"") -> bytes:
    return Frame(
        MessageClass.REQUEST,
        Endpoint.INSTRUMENT,
        command_id,
        Endpoint.PC,
        message_id,
        body,
    ).encode()


def encode_peristaltic_dispense(step: PeristalticDispense) -> bytes:
    """Encode a fixture-proven primary peristaltic dispense body."""

    position_map = _encode_column_map(step.plate_type, step.columns)
    return _PERISTALTIC_DISPENSE.pack(
        _PLATE_CODES[step.plate_type],
        step.volume_ul,
        _FLOW_CODES[step.flow_rate],
        CASSETTE_CODES[step.cassette_type],
        step.x_offset_steps,
        step.y_offset_steps,
        _DISPENSE_HEIGHTS[step.plate_type],
        step.pre_dispense_volume_ul,
        step.pre_dispense_cycles,
        position_map,
        _ROW_SKIP_MASKS[step.row_sections],
        1,  # Primary peristaltic pump.
        b"\x00" * 4,
    )


def _encode_column_map(
    plate_type: PlateType,
    columns: Literal["all"] | tuple[int, ...],
) -> bytes:
    if columns == "all":
        return b"\xff" * 6
    bits = [0] * 48
    for column in columns:
        bits[column - 1] = 1
    column_count = PLATE_COLUMN_COUNTS[plate_type]
    if _UNUSED_MAP_POSITIONS_ENABLED[plate_type]:
        bits[column_count:] = [1] * (48 - column_count)
    packed = bytearray(6)
    for index, enabled in enumerate(bits):
        packed[index // 8] |= enabled << (index % 8)
    return bytes(packed)


def encode_peristaltic_prime(step: PeristalticPrime) -> bytes:
    return _encode_peristaltic_prime_purge(step)


def encode_peristaltic_purge(step: PeristalticPurge) -> bytes:
    return _encode_peristaltic_prime_purge(step)


def _encode_peristaltic_prime_purge(
    step: PeristalticPrime | PeristalticPurge,
) -> bytes:
    return _PERISTALTIC_PRIME_PURGE.pack(
        _PLATE_CODES[step.plate_type],
        step.volume_ul,
        0,  # Duration mode is intentionally unsupported.
        _FLOW_CODES[step.flow_rate],
        1,  # Vendor volume-mode fixture flag.
        CASSETTE_CODES[step.cassette_type],
        1,  # Primary peristaltic pump.
        b"\x00" * 2,
    )


def encode_shake(step: Shake) -> bytes:
    return _SHAKE_SOAK.pack(
        _PLATE_CODES[step.plate_type],
        int(step.move_carrier_home),
        step.duration_seconds,
        3,  # Medium (5 Hz), recovered from calib10/calib15/calib16.
        0,  # X axis.
        0,
        b"\x00" * 4,
    )


def encode_soak(step: Soak) -> bytes:
    return _SHAKE_SOAK.pack(
        _PLATE_CODES[step.plate_type],
        int(step.move_carrier_home),
        0,
        3,  # Retained vendor medium-speed field; no shake is requested.
        0,
        step.duration_seconds,
        b"\x00" * 4,
    )


def encode_batch_start(plate_type: PlateType) -> bytes:
    """Encode the plate selector used by the recovered Start Batch command."""

    return bytes((_PLATE_CODES[plate_type],))


@dataclass(frozen=True, slots=True)
class Header:
    message_class: int
    destination: int
    command_id: int
    source: int
    message_id: int
    body_length: int
    checksum: int


def decode_header(header: bytes, *, max_body_length: int = MAX_BODY_LENGTH) -> Header:
    if len(header) != HEADER_SIZE:
        raise ProtocolError(f"header must be {HEADER_SIZE} bytes, got {len(header)}")
    values = _HEADER.unpack(header)
    result = Header(*values)
    if result.body_length > max_body_length:
        raise ProtocolError(
            f"declared body length {result.body_length} exceeds limit {max_body_length}"
        )
    return result


def decode_frame(packet: bytes, *, max_body_length: int = MAX_BODY_LENGTH) -> Frame:
    if len(packet) < HEADER_SIZE:
        raise ProtocolError(f"packet is shorter than the {HEADER_SIZE}-byte header")
    header = decode_header(packet[:HEADER_SIZE], max_body_length=max_body_length)
    expected_length = HEADER_SIZE + header.body_length
    if len(packet) != expected_length:
        raise ProtocolError(
            f"packet length {len(packet)} does not match declared length {expected_length}"
        )
    expected_checksum = checksum(packet[:9] + packet[HEADER_SIZE:])
    if header.checksum != expected_checksum:
        raise ProtocolError("packet checksum is invalid")
    return Frame(
        header.message_class,
        header.destination,
        header.command_id,
        header.source,
        header.message_id,
        packet[HEADER_SIZE:],
    )


class FrameStreamDecoder:
    """Collect arbitrarily fragmented bytes into complete validated frames."""

    def __init__(self, *, max_body_length: int = MAX_BODY_LENGTH) -> None:
        if not 0 <= max_body_length <= MAX_BODY_LENGTH:
            raise ValidationError("max_body_length must be between 0 and 65535")
        self.max_body_length = max_body_length
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        if not isinstance(data, bytes):
            raise ValidationError("stream data must be bytes")
        self._buffer.extend(data)
        frames: list[Frame] = []
        while len(self._buffer) >= HEADER_SIZE:
            header = decode_header(
                bytes(self._buffer[:HEADER_SIZE]),
                max_body_length=self.max_body_length,
            )
            packet_length = HEADER_SIZE + header.body_length
            if len(self._buffer) < packet_length:
                break
            packet = bytes(self._buffer[:packet_length])
            del self._buffer[:packet_length]
            frames.append(decode_frame(packet, max_body_length=self.max_body_length))
        return frames

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)
