"""Single-owner command exchange for the base MultiFlo."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from .codec import (
    Endpoint,
    Frame,
    HEADER_SIZE,
    MAX_BODY_LENGTH,
    MessageClass,
    decode_frame,
    decode_header,
    encode_peristaltic_dispense,
    encode_request,
)
from .errors import DeviceError, ProtocolError, TransportError, UnknownExecutionState
from .models import (
    CASSETTE_TYPES_BY_CODE,
    CassetteType,
    PeristalticDispense,
    validate_volume_for_cassette,
)
from .transport import ByteTransport


COMMUNICATION_TEST = 0x0073
PERISTALTIC_DISPENSE = 0x008F
ACK = 0x06
NAK = 0x15


@dataclass(frozen=True, slots=True)
class ExchangeResult:
    response: Frame
    indications: tuple[Frame, ...]


@dataclass(frozen=True, slots=True)
class BasecodeVersionInfo:
    part_number: str
    software_version: str
    ui_checksum: str
    motion_controller_checksum: str
    data_version: str
    ui_version: str
    motion_controller_version: str
    reserved: bytes


@dataclass(frozen=True, slots=True)
class InstalledModules:
    primary_peristaltic: bool
    secondary_peristaltic: bool
    half_microliter_supported: bool
    primary_cassette: CassetteType


@dataclass(frozen=True, slots=True)
class ReadOnlyDeviceInfo:
    product_serial_number: str
    basecode: BasecodeVersionInfo
    modules: InstalledModules


class MultiFloDriver:
    """Serialize request/response exchanges over one owned transport."""

    def __init__(
        self,
        transport: ByteTransport,
        *,
        expected_product_serial: str | None = None,
        max_body_length: int = MAX_BODY_LENGTH,
    ) -> None:
        self.transport = transport
        self.expected_product_serial = expected_product_serial
        self.max_body_length = max_body_length
        self._next_message_id = 0
        self._exchange_lock = Lock()
        self._is_open = False
        self._operator_confirmed_idle = False
        self._motion_preflight: ReadOnlyDeviceInfo | None = None

    def open(self) -> None:
        if not self._is_open:
            self.transport.open()
            self._is_open = True

    def close(self) -> None:
        if self._is_open:
            try:
                self.transport.close()
            finally:
                self._is_open = False
                self._operator_confirmed_idle = False
                self._motion_preflight = None

    def communication_test(self) -> ExchangeResult:
        """Send the bodyless, non-motion communication-test command only."""

        result = self._exchange(COMMUNICATION_TEST)
        self._response_data(COMMUNICATION_TEST, result.response.body)
        return result

    def query_basecode_version(self) -> BasecodeVersionInfo:
        data = self._read_only_data(0x00A0)
        if len(data) < 34:
            raise ProtocolError(f"basecode version response is only {len(data)} bytes")
        return BasecodeVersionInfo(
            part_number=data[0:7].decode("ascii").strip(),
            software_version=data[7:15].decode("ascii").strip(),
            ui_checksum=data[15:19].decode("ascii").strip(),
            motion_controller_checksum=data[19:23].decode("ascii").strip(),
            data_version=data[23:28].decode("ascii").strip(),
            ui_version=data[28:31].decode("ascii").strip(),
            motion_controller_version=data[31:34].decode("ascii").strip(),
            reserved=data[34:],
        )

    def query_product_serial_number(self) -> str:
        data = self._read_only_data(0x0100)
        return data.split(b"\x00", 1)[0].decode("ascii").strip()

    def query_peristaltic_installed(self, pump: int) -> bool:
        if pump not in (1, 2):
            raise ProtocolError("peristaltic pump must be 1 or 2")
        data = self._read_only_data(0x0104, bytes((pump,)))
        if len(data) != 1:
            raise ProtocolError("peristaltic-installed response must contain one data byte")
        return bool(data[0])

    def query_half_microliter_support(self) -> bool:
        data = self._read_only_data(0x0154)
        if len(data) != 1:
            raise ProtocolError("half-microliter response must contain one data byte")
        return bool(data[0])

    def query_peristaltic_cassette(self, pump: int) -> CassetteType:
        if pump not in (1, 2):
            raise ProtocolError("peristaltic pump must be 1 or 2")
        data = self._read_only_data(0x0108, bytes((pump,)))
        if len(data) != 1:
            raise ProtocolError("peristaltic-cassette response must contain one data byte")
        try:
            return CASSETTE_TYPES_BY_CODE[data[0]]
        except KeyError as error:
            raise ProtocolError(
                f"instrument returned unknown cassette type 0x{data[0]:02x}"
            ) from error

    def inspect_device(self) -> ReadOnlyDeviceInfo:
        serial = self.query_product_serial_number()
        version = self.query_basecode_version()
        primary = self.query_peristaltic_installed(1)
        secondary = self.query_peristaltic_installed(2)
        half_microliter = self.query_half_microliter_support()
        cassette = self.query_peristaltic_cassette(1)
        return ReadOnlyDeviceInfo(
            serial,
            version,
            InstalledModules(
                primary,
                secondary,
                half_microliter,
                cassette,
            ),
        )

    def authorize_motion(self, *, operator_confirmed_idle: bool) -> None:
        """Record an operator's per-run idle/setup confirmation.

        The base protocol's authoritative busy query is not yet recovered. This
        confirmation is therefore deliberately required before the fresh
        communication and inventory preflight used by Phase 2.
        """

        if not operator_confirmed_idle:
            raise ProtocolError("operator idle/setup confirmation is required")
        self._operator_confirmed_idle = True
        self._motion_preflight = None

    def prepare_motion(self) -> ReadOnlyDeviceInfo:
        if not self._operator_confirmed_idle:
            raise ProtocolError("motion has not been authorized by the operator")
        if not self.expected_product_serial:
            raise ProtocolError("an expected product serial is required for motion")
        self.communication_test()
        info = self.inspect_device()
        if info.product_serial_number != self.expected_product_serial:
            raise ProtocolError(
                "connected instrument serial does not match the motion allowlist"
            )
        if not info.modules.primary_peristaltic:
            raise ProtocolError("primary peristaltic pump is not installed")
        if info.modules.primary_cassette is CassetteType.ANY:
            raise ProtocolError("instrument cassette setting must be 1ul, 5ul, or 10ul")
        self._motion_preflight = info
        return info

    def peristaltic_dispense(self, step: PeristalticDispense) -> ExchangeResult:
        info = self._motion_preflight
        if info is None:
            raise ProtocolError("motion preflight has not completed")
        installed = info.modules.primary_cassette
        if step.cassette_type is not CassetteType.ANY and step.cassette_type is not installed:
            raise ProtocolError(
                f"requested {step.cassette_type.value} cassette does not match "
                f"instrument setting {installed.value}"
            )
        validate_volume_for_cassette(step.volume_ul, installed)
        if step.pre_dispense_volume_ul:
            validate_volume_for_cassette(step.pre_dispense_volume_ul, installed)
        body = encode_peristaltic_dispense(step)
        try:
            result = self._exchange(PERISTALTIC_DISPENSE, body)
            self._response_data(PERISTALTIC_DISPENSE, result.response.body)
            return result
        except DeviceError:
            raise
        except (TransportError, ProtocolError) as error:
            self._operator_confirmed_idle = False
            self._motion_preflight = None
            raise UnknownExecutionState(
                "communication failed after the dispense command was sent; "
                "do not retry automatically"
            ) from error

    def _read_only_data(self, command_id: int, body: bytes = b"") -> bytes:
        result = self._exchange(command_id, body)
        return self._response_data(command_id, result.response.body)

    @staticmethod
    def _response_data(command_id: int, body: bytes) -> bytes:
        if len(body) < 2:
            raise ProtocolError(
                f"response for command 0x{command_id:04x} has no two-byte device status"
            )
        status = int.from_bytes(body[:2], "little")
        if status:
            raise DeviceError(
                f"command 0x{command_id:04x} returned device status 0x{status:04x}"
            )
        return body[2:]

    def _exchange(self, command_id: int, body: bytes = b"") -> ExchangeResult:
        if not self._is_open:
            raise TransportError("driver is not open")
        with self._exchange_lock:
            message_id = self._next_message_id
            self._next_message_id = (self._next_message_id + 1) & 0xFFFF
            self.transport.write(encode_request(command_id, message_id, body))
            acknowledgement = self._read_exactly(1)[0]
            if acknowledgement == NAK:
                raise DeviceError(f"instrument rejected command 0x{command_id:04x} with NAK")
            if acknowledgement != ACK:
                raise ProtocolError(
                    f"expected ACK 0x06 before response, got 0x{acknowledgement:02x}"
                )
            indications: list[Frame] = []
            while True:
                frame = self._read_frame()
                if frame.message_class == MessageClass.INDICATION:
                    indications.append(frame)
                    continue
                planned_response = (
                    frame.message_class == MessageClass.RESPONSE
                    and frame.destination == Endpoint.PC
                    and frame.source == Endpoint.INSTRUMENT
                    and frame.message_id == message_id
                )
                observed_base_response = (
                    frame.message_class == MessageClass.REQUEST
                    and frame.destination == 0
                    and frame.source == 0
                    and frame.message_id == 0
                )
                if not (planned_response or observed_base_response):
                    raise ProtocolError(
                        "response header does not match the planned or observed base MultiFlo profile"
                    )
                if frame.command_id != command_id:
                    raise ProtocolError(
                        f"response command 0x{frame.command_id:04x} does not match "
                        f"request 0x{command_id:04x}"
                    )
                return ExchangeResult(frame, tuple(indications))

    def _read_frame(self) -> Frame:
        header_bytes = self._read_exactly(HEADER_SIZE)
        header = decode_header(header_bytes, max_body_length=self.max_body_length)
        body = self._read_exactly(header.body_length) if header.body_length else b""
        return decode_frame(header_bytes + body, max_body_length=self.max_body_length)

    def _read_exactly(self, size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            chunk = self.transport.read(size - len(result))
            if not chunk:
                raise TransportError(
                    f"transport disconnected after {len(result)} of {size} expected bytes"
                )
            result.extend(chunk)
        return bytes(result)

    def __enter__(self) -> "MultiFloDriver":
        self.open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
