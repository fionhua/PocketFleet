# -*- coding: utf-8 -*-
"""Unit tests for built-in PocketFleetBridgeServer (Port 18765 protocol)."""
from __future__ import annotations

import json
import time
import unittest
import urllib.request
import urllib.error

from pocketfleet.bridge_server import PocketFleetBridgeServer


class TestPocketFleetBridgeServer(unittest.TestCase):
    def setUp(self) -> None:
        self.port = 18799  # Isolated test port
        self.token = "test_token_1234567890_abcdefghijklmnop"
        self.received_posts = []

        def _on_post(payload: dict) -> None:
            self.received_posts.append(payload)

        self.server = PocketFleetBridgeServer(
            host="127.0.0.1",
            port=self.port,
            token=self.token,
            on_telegram_post=_on_post,
        )
        self.assertTrue(self.server.start())
        time.sleep(0.05)

    def tearDown(self) -> None:
        self.server.stop()
        time.sleep(0.05)

    def test_status_unauthorized_without_token(self) -> None:
        url = f"http://127.0.0.1:{self.port}/api/v1/status"
        req = urllib.request.Request(url)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 401)

    def test_status_ok_with_token(self) -> None:
        url = f"http://127.0.0.1:{self.port}/api/v1/status"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("principal"), "PocketFleet-Local-Bridge")
            self.assertFalse(data.get("kill_switch_active"))
            self.assertIn("@AiSoulJudgeBot", data.get("codeai_allowed_recipients", []))

    def test_sessions_list(self) -> None:
        url = f"http://127.0.0.1:{self.port}/api/v1/sessions"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("ok"))
            self.assertIn("chatgpt", data.get("sessions", []))

    def test_control_pause_and_resume(self) -> None:
        # 1. Pause
        pause_url = f"http://127.0.0.1:{self.port}/api/v1/control/pause"
        req_pause = urllib.request.Request(
            pause_url,
            data=b"{}",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_pause) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("kill_switch_active"))

        # Verify status reflects pause
        status_url = f"http://127.0.0.1:{self.port}/api/v1/status"
        req_status = urllib.request.Request(status_url, headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req_status) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("kill_switch_active"))

        # 2. Resume
        resume_url = f"http://127.0.0.1:{self.port}/api/v1/control/resume"
        req_resume = urllib.request.Request(
            resume_url,
            data=b"{}",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_resume) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertFalse(data.get("kill_switch_active"))

    def test_telegram_post_dispatches_callback(self) -> None:
        url = f"http://127.0.0.1:{self.port}/api/v1/telegram/post"
        payload = {
            "text": "[Telegram]re:@AiSoulJudgeBot 任务已完成",
            "sender": "chatgpt",
            "source_key": "https://chatgpt.com/c/123",
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("ok"))
            self.assertTrue(data.get("delivered"))

        self.assertEqual(len(self.received_posts), 1)
        self.assertEqual(self.received_posts[0]["sender"], "chatgpt")
        self.assertIn("任务已完成", self.received_posts[0]["text"])


class TestFleetManagerBridgeIntegration(unittest.TestCase):
    def test_fleet_manager_bridge_lifecycle(self) -> None:
        from pocketfleet.control_panel import FleetManager
        from pocketfleet.bridge_server import stop_global_bridge_server

        logs = []
        mgr = FleetManager(log_cb=lambda msg: logs.append(msg))
        
        # Test start
        self.assertTrue(mgr.start_bridge())
        self.assertTrue(mgr.is_bridge_running())

        # Test duplicate start is idempotent and does not crash
        self.assertTrue(mgr.start_bridge())

        # Test stop
        mgr.stop_bridge()
        stop_global_bridge_server()
        time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()

