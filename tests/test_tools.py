from __future__ import annotations

import unittest

from multiflo.codec import (
    DEEP_WELL_DISPENSE_HEIGHT_STEPS,
    encode_peristaltic_dispense,
)
from multiflo.tools.hardware_phase3_step import _build_step, build_parser


def parse(*arguments: str):
    """Parse through the tool's real CLI.

    Building an argparse.Namespace by hand made this test break every time a
    flag was added, which says nothing about the tool being wrong.
    """

    return build_parser().parse_args(
        ["--expected-serial", "14071419", "--authorization", "unused", *arguments]
    )


class HardwarePhase3ToolTests(unittest.TestCase):
    def test_builds_calib25_odd_row_dispense(self) -> None:
        step = _build_step(
            parse(
                "--operation", "dispense",
                "--volume-ul", "10",
                "--flow-rate", "high",
                "--cassette", "1ul",
                "--plate-type", "384_well",
                "--row-sections", "odd",
                "--x-offset-steps", "-19",
                "--y-offset-steps", "6",
            )
        )

        self.assertEqual(step.row_sections, "odd")
        self.assertEqual((step.x_offset_steps, step.y_offset_steps), (-19, 6))
        self.assertEqual(step.model_dump(mode="json")["plate_type"], "384_well")

    def test_deep_well_uses_the_project_height_by_default(self) -> None:
        step = _build_step(
            parse(
                "--operation", "dispense",
                "--volume-ul", "100",
                "--cassette", "5ul",
                "--plate-type", "96_deep_well",
            )
        )

        self.assertIsNone(step.dispense_height_steps)
        body = encode_peristaltic_dispense(step)
        self.assertEqual(
            int.from_bytes(body[7:9], "little"),
            DEEP_WELL_DISPENSE_HEIGHT_STEPS,
        )

    def test_the_height_can_be_overridden_per_run(self) -> None:
        step = _build_step(
            parse(
                "--operation", "dispense",
                "--volume-ul", "100",
                "--cassette", "5ul",
                "--plate-type", "96_deep_well",
                "--dispense-height-steps", "975",
            )
        )

        self.assertEqual(step.dispense_height_steps, 975)
        self.assertEqual(
            int.from_bytes(encode_peristaltic_dispense(step)[7:9], "little"),
            975,
        )


if __name__ == "__main__":
    unittest.main()
