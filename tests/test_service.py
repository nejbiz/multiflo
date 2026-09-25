"""Checks for the loopback single-worker service entry point."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from multiflo.service import is_loopback_host, main


class ServiceEntryPointTests(unittest.TestCase):
    def test_loopback_hosts_are_recognized(self) -> None:
        for host in ("127.0.0.1", "127.0.0.5", "::1", "localhost"):
            with self.subTest(host=host):
                self.assertTrue(is_loopback_host(host))

    def test_routable_hosts_are_not_loopback(self) -> None:
        for host in ("0.0.0.0", "192.168.1.20", "::", "multiflo.example"):
            with self.subTest(host=host):
                self.assertFalse(is_loopback_host(host))

    def test_non_loopback_bind_is_refused_before_opening_the_device(self) -> None:
        argv = [
            "multiflo.service",
            "--expected-serial",
            "14071419",
            "--host",
            "0.0.0.0",
        ]
        with patch("sys.argv", argv), patch("multiflo.service.build_service") as build:
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 2)
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
