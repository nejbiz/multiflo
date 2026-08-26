from __future__ import annotations

import unittest

from multiflo.codec import (
    Endpoint,
    Frame,
    FrameStreamDecoder,
    MessageClass,
    decode_frame,
    encode_batch_start,
    encode_request,
    encode_peristaltic_dispense,
    encode_peristaltic_prime,
    encode_peristaltic_purge,
    encode_shake,
    encode_soak,
)
from multiflo.models import (
    PeristalticDispense,
    PeristalticPrime,
    PeristalticPurge,
    Shake,
    Soak,
    PlateType,
)
from multiflo.errors import ProtocolError


COMMUNICATION_TEST_PACKET = bytes.fromhex("01 02 73 00 01 00 00 00 00 89 FF")
DISPENSE_PACKET = bytes.fromhex(
    "01 02 8F 00 01 00 00 18 00 40 F8 "
    "05 64 00 01 00 00 00 A1 03 0A 00 02 FF FF FF FF FF FF 00 01 00 00 00 00"
)


class CodecTests(unittest.TestCase):
    def test_start_batch_plate_selector(self) -> None:
        self.assertEqual(encode_batch_start(PlateType.WELL_384), b"\x01")
        self.assertEqual(encode_batch_start(PlateType.WELL_96), b"\x04")
        self.assertEqual(encode_batch_start(PlateType.DEEP_WELL_96), b"\x05")

    def test_calib1_peristaltic_dispense_golden_packet(self) -> None:
        step = PeristalticDispense(volume_ul=100)
        body = bytes.fromhex(
            "05 64 00 01 00 00 00 A1 03 0A 00 02 "
            "FF FF FF FF FF FF 00 01 00 00 00 00"
        )
        self.assertEqual(encode_peristaltic_dispense(step), body)
        self.assertEqual(
            encode_request(0x008F, body=body),
            bytes.fromhex(
                "01 02 8F 00 01 00 00 18 00 40 F8 "
                "05 64 00 01 00 00 00 A1 03 0A 00 02 "
                "FF FF FF FF FF FF 00 01 00 00 00 00"
            ),
        )

    def test_calib3_partial_deep_well_golden_body(self) -> None:
        step = PeristalticDispense(volume_ul=200, columns=(1,))
        self.assertEqual(
            encode_peristaltic_dispense(step),
            bytes.fromhex(
                "05 C8 00 01 00 00 00 A1 03 0A 00 02 "
                "01 F0 FF FF FF FF 00 01 00 00 00 00"
            ),
        )

    def test_calib4_standard_plate_high_flow_golden_body(self) -> None:
        step = PeristalticDispense(
            volume_ul=200,
            plate_type="96_well",
            flow_rate="high",
            columns=(1,),
        )
        self.assertEqual(
            encode_peristaltic_dispense(step),
            bytes.fromhex(
                "04 C8 00 02 00 00 00 50 01 0A 00 02 "
                "01 00 00 00 00 00 00 01 00 00 00 00"
            ),
        )

    def test_calib8_prime_golden_body(self) -> None:
        self.assertEqual(
            encode_peristaltic_prime(PeristalticPrime(volume_ul=3000)),
            bytes.fromhex("04 B8 0B 00 00 01 01 00 01 00 00"),
        )

    def test_calib9_purge_golden_body(self) -> None:
        self.assertEqual(
            encode_peristaltic_purge(PeristalticPurge(volume_ul=2000)),
            bytes.fromhex("04 D0 07 00 00 01 01 00 01 00 00"),
        )

    def test_calib10_shake_golden_body(self) -> None:
        self.assertEqual(
            encode_shake(Shake(duration_seconds=5)),
            bytes.fromhex("04 01 05 00 03 00 00 00 00 00 00 00"),
        )

    def test_calib11_soak_golden_body(self) -> None:
        self.assertEqual(
            encode_soak(Soak(duration_seconds=30)),
            bytes.fromhex("04 01 00 00 03 00 1E 00 00 00 00 00"),
        )

    def test_calib20_required_5ul_cassette_golden_body(self) -> None:
        step = PeristalticDispense(
            volume_ul=750,
            plate_type="96_well",
            flow_rate="high",
            cassette_type="5ul",
        )
        self.assertEqual(
            encode_peristaltic_dispense(step),
            bytes.fromhex(
                "04 EE 02 02 02 00 00 50 01 0A 00 02 "
                "FF FF FF FF FF FF 00 01 00 00 00 00"
            ),
        )

    def test_calib22_required_10ul_cassette_golden_body(self) -> None:
        step = PeristalticDispense(
            volume_ul=750,
            plate_type="96_well",
            flow_rate="high",
            cassette_type="10ul",
        )
        self.assertEqual(
            encode_peristaltic_dispense(step),
            bytes.fromhex(
                "04 EE 02 02 03 00 00 50 01 0A 00 02 "
                "FF FF FF FF FF FF 00 01 00 00 00 00"
            ),
        )

    def test_calib23_384_well_dispense_golden_body(self) -> None:
        step = PeristalticDispense(
            volume_ul=1,
            plate_type="384_well",
            flow_rate="low",
            cassette_type="1ul",
        )
        self.assertEqual(
            encode_peristaltic_dispense(step),
            bytes.fromhex(
                "01 01 00 00 01 00 00 4D 01 0A 00 02 "
                "FF FF FF FF FF FF 00 01 00 00 00 00"
            ),
        )

    def test_calib24_384_well_prime_golden_body(self) -> None:
        step = PeristalticPrime(
            volume_ul=3000,
            plate_type="384_well",
            flow_rate="high",
            cassette_type="1ul",
        )
        self.assertEqual(
            encode_peristaltic_prime(step),
            bytes.fromhex("01 B8 0B 00 00 02 01 01 01 00 00"),
        )

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
