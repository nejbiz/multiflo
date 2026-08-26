"""MultiFlo packet framing and incremental response decoding."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import struct

from .errors import ProtocolError, ValidationError


HEADER_SIZE = 11
MAX_BODY_LENGTH = 0xFFFF
_HEADER_WITHOUT_CHECKSUM = struct.Struct("<BBHBHH")
_HEADER = struct.Struct("<BBHBHHH")


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
