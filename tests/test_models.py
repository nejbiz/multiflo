from __future__ import annotations

import unittest

from pydantic import ValidationError

from multiflo.models import PeristalticDispense, PlateType, Protocol, Shake, Soak


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

        self.assertEqual(
            PeristalticDispense(volume_ul=1200, cassette_type="1ul").volume_ul,
            1200,
        )
        with self.assertRaisesRegex(ValidationError, "between 1 and 1200"):
            PeristalticDispense(volume_ul=1201, cassette_type="1ul")

    def test_dispense_defaults_to_standard_96_well(self) -> None:
        self.assertIs(
            PeristalticDispense(volume_ul=100).plate_type,
            PlateType.WELL_96,
        )

    def test_384_plates_reject_an_explicit_incompatible_cassette(self) -> None:
        for plate_type in ("384_well", "384_deep_well"):
            for cassette_type in ("5ul", "10ul"):
                with self.subTest(
                    plate_type=plate_type,
                    cassette_type=cassette_type,
                ):
                    with self.assertRaisesRegex(
                        ValidationError,
                        "requires a 1ul cassette",
                    ):
                        PeristalticDispense(
                            volume_ul=100,
                            plate_type=plate_type,
                            cassette_type=cassette_type,
                        )

            step = PeristalticDispense(volume_ul=100, plate_type=plate_type)
            self.assertEqual(step.cassette_type.value, "any")

    def test_384_deep_well_clones_384_selection_geometry(self) -> None:
        step = PeristalticDispense(
            volume_ul=10,
            plate_type="384_deep_well",
            cassette_type="1ul",
            columns=(1, 24),
            row_sections="odd",
        )

        self.assertIs(step.plate_type, PlateType.DEEP_WELL_384)
        self.assertEqual(step.columns, (1, 24))
        self.assertEqual(step.row_sections, "odd")
        with self.assertRaisesRegex(ValidationError, "between 1 and 24"):
            PeristalticDispense(
                volume_ul=10,
                plate_type="384_deep_well",
                cassette_type="1ul",
                columns=(25,),
            )

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

    def test_columns_are_unique_and_bounded_by_plate_geometry(self) -> None:
        with self.assertRaisesRegex(ValidationError, "unique"):
            PeristalticDispense(volume_ul=100, columns=(1, 1))
        with self.assertRaisesRegex(ValidationError, "between 1 and 12"):
            PeristalticDispense(volume_ul=100, columns=(13,))
        with self.assertRaisesRegex(ValidationError, "between 1 and 24"):
            PeristalticDispense(
                volume_ul=10,
                plate_type="384_well",
                cassette_type="1ul",
                columns=(25,),
            )

    def test_calib21_1ul_cassette_volume_is_accepted(self) -> None:
        step = PeristalticDispense(
            volume_ul=750,
            plate_type="96_well",
            flow_rate="high",
            cassette_type="1ul",
        )
        self.assertEqual(step.volume_ul, 750)

    def test_shake_and_soak_accept_600_seconds_and_reject_601(self) -> None:
        self.assertEqual(Shake(duration_seconds=600).duration_seconds, 600)
        self.assertEqual(Soak(duration_seconds=600).duration_seconds, 600)
        for step_type in (Shake, Soak):
            with self.subTest(step_type=step_type.__name__):
                with self.assertRaises(ValidationError):
                    step_type(duration_seconds=601)

    def test_calib31_partial_384_well_column_map_is_accepted(self) -> None:
        step = PeristalticDispense(
            volume_ul=1,
            plate_type="384_well",
            cassette_type="1ul",
            columns=tuple(range(1, 25, 2)),
        )
        self.assertEqual(step.columns, tuple(range(1, 25, 2)))

    def test_calib25_odd_row_section_is_limited_to_384_well_plates(self) -> None:
        step = PeristalticDispense(
            volume_ul=10,
            plate_type="384_well",
            flow_rate="high",
            cassette_type="1ul",
            row_sections="odd",
        )
        self.assertEqual(step.row_sections, "odd")

        with self.assertRaisesRegex(ValidationError, "only for 384-well"):
            PeristalticDispense(volume_ul=10, row_sections="odd")

    def test_calib30_even_row_section_is_limited_to_384_well_plates(self) -> None:
        step = PeristalticDispense(
            volume_ul=10,
            plate_type="384_well",
            cassette_type="1ul",
            row_sections="even",
        )
        self.assertEqual(step.row_sections, "even")

        with self.assertRaisesRegex(ValidationError, "only for 384-well"):
            PeristalticDispense(volume_ul=10, row_sections="even")

    def test_xy_offsets_are_cassette_independent_and_manual_bounded(self) -> None:
        for cassette_type in ("any", "1ul", "5ul", "10ul"):
            with self.subTest(cassette_type=cassette_type):
                step = PeristalticDispense(
                    volume_ul=10,
                    cassette_type=cassette_type,
                    x_offset_steps=19,
                    y_offset_steps=-6,
                )
                self.assertEqual((step.x_offset_steps, step.y_offset_steps), (19, -6))

        for field, value in (
            ("x_offset_steps", -61),
            ("x_offset_steps", 61),
            ("y_offset_steps", -41),
            ("y_offset_steps", 41),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValidationError):
                    PeristalticDispense(volume_ul=10, **{field: value})

    def test_dispense_height_has_an_absolute_driver_limit(self) -> None:
        self.assertEqual(
            PeristalticDispense(
                volume_ul=10,
                dispense_height_steps=1100,
            ).dispense_height_steps,
            1100,
        )
        for height in (99, 1101):
            with self.subTest(height=height):
                with self.assertRaises(ValidationError):
                    PeristalticDispense(
                        volume_ul=10,
                        dispense_height_steps=height,
                    )

    def test_protocol_discriminates_phase3_steps(self) -> None:
        protocol = Protocol.model_validate(
            {
                "name": "phase 3",
                "steps": [
                    {"operation": "peristaltic_prime", "volume_ul": 3000},
                    {"operation": "peristaltic_purge", "volume_ul": 2000},
                    {"operation": "shake", "duration_seconds": 5},
                    {"operation": "soak", "duration_seconds": 30},
                ],
            }
        )
        self.assertEqual(
            [step.operation for step in protocol.steps],
            ["peristaltic_prime", "peristaltic_purge", "shake", "soak"],
        )


if __name__ == "__main__":
    unittest.main()
