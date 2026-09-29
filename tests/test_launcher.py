"""Tests for PocketFleet Launcher and Singleton Guard"""
from __future__ import annotations

import sys
import unittest
from unittest import mock

from pocketfleet import launcher


class TestLauncher(unittest.TestCase):
    def test_single_instance_guard(self):
        guard1 = launcher.SingleInstanceGuard("Local\\test_pocketfleet_mutex_unique")
        guard2 = launcher.SingleInstanceGuard("Local\\test_pocketfleet_mutex_unique")

        self.assertTrue(guard1.acquire())
        if sys.platform == "win32":
            self.assertFalse(guard2.acquire())
            self.assertTrue(guard2.already_running)

        guard1.release()
        guard2.release()

    def test_launcher_main_missing_token(self):
        with mock.patch.dict(launcher.os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as cm:
                launcher.main(argv=[])
            self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
