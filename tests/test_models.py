from __future__ import annotations

import unittest

from pydantic import ValidationError

from multiflo.models import PeristalticDispense, Protocol


class ModelTests(unittest.TestCase):
    def test_valid_protocol(self) -> None:
        protocol = Protocol(
            name="safe test",
            steps=[PeristalticDispense(volume_ul=100, cassette_type="5ul")],
        )
        self.assertEqual(len(protocol.steps), 1)

    def test_cassette_range_and_increment(self) -> None:
        with self.assertRaisesRegex(ValidationError, "between 5 and 2500"):
            PeristalticDispense(volume_ul=4, cassette_type="5ul")
        with self.assertRaisesRegex(ValidationError, "full 5 uL increment"):
            PeristalticDispense(volume_ul=101, cassette_type="5ul")

    def test_pre_dispense_pair_is_consistent(self) -> None:
        with self.assertRaisesRegex(ValidationError, "both be zero or both be positive"):
            PeristalticDispense(
                volume_ul=100,
                pre_dispense_volume_ul=0,
                pre_dispense_cycles=2,
            )

    def test_unknown_fields_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            PeristalticDispense(volume_ul=100, unsupported=True)


if __name__ == "__main__":
    unittest.main()
