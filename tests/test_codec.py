from __future__ import annotations

import unittest

from multiflo.codec import (
    Endpoint,
    Frame,
    FrameStreamDecoder,
    MessageClass,
    decode_frame,
    encode_request,
)
from multiflo.errors import ProtocolError


COMMUNICATION_TEST_PACKET = bytes.fromhex("01 02 73 00 01 00 00 00 00 89 FF")
DISPENSE_PACKET = bytes.fromhex(
    "01 02 8F 00 01 00 00 18 00 40 F8 "
    "05 64 00 01 00 00 00 A1 03 0A 00 02 FF FF FF FF FF FF 00 01 00 00 00 00"
)


class CodecTests(unittest.TestCase):
    def test_communication_test_golden_packet(self) -> None:
        self.assertEqual(encode_request(0x0073), COMMUNICATION_TEST_PACKET)

    def test_dispense_golden_packet_decodes_and_reencodes(self) -> None:
        frame = decode_frame(DISPENSE_PACKET)
        self.assertEqual(frame.command_id, 0x008F)
        self.assertEqual(len(frame.body), 24)
        self.assertEqual(frame.encode(), DISPENSE_PACKET)

    def test_fragmented_stream(self) -> None:
        packet = Frame(
            MessageClass.RESPONSE,
            Endpoint.PC,
            0x0073,
            Endpoint.INSTRUMENT,
            5,
            b"ok",
        ).encode()
        decoder = FrameStreamDecoder()
        frames = []
        for byte in packet:
            frames.extend(decoder.feed(bytes((byte,))))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].body, b"ok")
        self.assertEqual(decoder.buffered_bytes, 0)

    def test_bad_checksum(self) -> None:
        packet = bytearray(COMMUNICATION_TEST_PACKET)
        packet[-1] ^= 1
        with self.assertRaisesRegex(ProtocolError, "checksum"):
            decode_frame(bytes(packet))

    def test_malformed_declared_length(self) -> None:
        packet = bytearray(COMMUNICATION_TEST_PACKET)
        packet[7:9] = (9).to_bytes(2, "little")
        with self.assertRaisesRegex(ProtocolError, "exceeds limit"):
            FrameStreamDecoder(max_body_length=8).feed(bytes(packet))

    def test_truncated_packet(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "does not match"):
            decode_frame(DISPENSE_PACKET[:-1])


if __name__ == "__main__":
    unittest.main()
