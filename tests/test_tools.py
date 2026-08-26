from __future__ import annotations

from argparse import Namespace
import unittest

from multiflo.tools.hardware_phase3_step import _build_step


class HardwarePhase3ToolTests(unittest.TestCase):
    def test_builds_calib25_odd_row_dispense(self) -> None:
        step = _build_step(
            Namespace(
                operation="dispense",
                volume_ul=10,
                flow_rate="high",
                cassette="1ul",
                plate_type="384_well",
                pre_dispense_volume_ul=10,
                pre_dispense_cycles=2,
                columns="all",
                row_sections="odd",
                x_offset_steps=-19,
                y_offset_steps=6,
            )
        )

        self.assertEqual(step.row_sections, "odd")
        self.assertEqual((step.x_offset_steps, step.y_offset_steps), (-19, 6))
        self.assertEqual(step.model_dump(mode="json")["plate_type"], "384_well")


if __name__ == "__main__":
    unittest.main()
