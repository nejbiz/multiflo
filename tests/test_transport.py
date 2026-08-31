from __future__ import annotations

import unittest

from multiflo.errors import TransportError
from multiflo.tests.fakes import ScriptedFakeTransport


class ScriptedFakeTransportTests(unittest.TestCase):
    def test_splits_a_large_fragment(self) -> None:
        fake = ScriptedFakeTransport([b"abcdef"], expected_writes=[b"request"])
        fake.open()
        fake.write(b"request")
        self.assertEqual(fake.read(2), b"ab")
        self.assertEqual(fake.read(8), b"cdef")
        fake.assert_script_consumed()

    def test_rejects_io_while_closed(self) -> None:
        fake = ScriptedFakeTransport()
        with self.assertRaises(TransportError):
            fake.read(1)
        with self.assertRaises(TransportError):
            fake.write(b"x")

    def test_scripted_error(self) -> None:
        fake = ScriptedFakeTransport([TransportError("USB removed")])
        fake.open()
        with self.assertRaisesRegex(TransportError, "USB removed"):
            fake.read(1)


if __name__ == "__main__":
    unittest.main()
