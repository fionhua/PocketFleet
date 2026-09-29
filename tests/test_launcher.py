"""Tests for PocketFleet Launcher and Singleton Guard"""
from __future__ import annotations

import sys
import pytest
from unittest import mock

from pocketfleet import launcher


def test_single_instance_guard():
    guard1 = launcher.SingleInstanceGuard("Local\\test_pocketfleet_mutex_unique")
    guard2 = launcher.SingleInstanceGuard("Local\\test_pocketfleet_mutex_unique")

    assert guard1.acquire() is True
    if sys.platform == "win32":
        assert guard2.acquire() is False
        assert guard2.already_running is True

    guard1.release()
    guard2.release()


def test_launcher_main_missing_token(monkeypatch):
    monkeypatch.setattr(launcher.os.environ, "get", lambda *args: None)
    with pytest.raises(SystemExit) as excinfo:
        launcher.main(argv=[])
    assert excinfo.value.code == 1
