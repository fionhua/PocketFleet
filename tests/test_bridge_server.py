# -*- coding: utf-8 -*-
"""Unit tests for built-in PocketFleetBridgeServer (Port 18765 protocol)."""
from __future__ import annotations

import json
import time
import unittest
import urllib.request
import urllib.error
from unittest import mock

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
        url_status = f"http://127.0.0.1:{self.port}/api/v1/status"
        req_status = urllib.request.Request(
            url_status,
            headers={
                "Authorization": f"Bearer {self.token}",
                "X-Folded-Host-Principal": "folded-host-chatgpt-web",
            },
        )
        with urllib.request.urlopen(req_status) as resp:
            self.assertEqual(resp.status, 200)

        url = f"http://127.0.0.1:{self.port}/api/v1/sessions"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("ok"))
            self.assertIn("folded-host-chatgpt-web", data.get("active_clients", []))
            self.assertIn("folded-host-chatgpt-web", data.get("sessions", []))

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

    def test_codeai_queue_ttl_expiry(self) -> None:
        from pocketfleet.bridge_server import (
            enqueue_codeai_message,
            pop_codeai_message,
            purge_expired_codeai_messages,
            _CODEAI_LOCK,
            _CODEAI_QUEUE,
        )

        with _CODEAI_LOCK:
            _CODEAI_QUEUE.clear()

        # 1. Enqueue item with short TTL (e.g. 0.1s)
        did = enqueue_codeai_message(content="Expired prompt test", ttl_seconds=0.1)
        self.assertNotEqual(did, "duplicate_skipped")

        # Sleep to exceed TTL
        time.sleep(0.15)

        # 2. pop_codeai_message should drop it and return None
        item = pop_codeai_message()
        self.assertIsNone(item, "Expired message must be dropped and not delivered")

    def test_codeai_queue_deduplication(self) -> None:
        from pocketfleet.bridge_server import (
            enqueue_codeai_message,
            pop_codeai_message,
            _CODEAI_LOCK,
            _CODEAI_QUEUE,
            _CODEAI_RECENT_HASHES,
        )

        with _CODEAI_LOCK:
            _CODEAI_QUEUE.clear()
            _CODEAI_RECENT_HASHES.clear()

        # 1. First enqueue succeeds
        did1 = enqueue_codeai_message(content="Identical meeting briefing test")
        self.assertNotEqual(did1, "duplicate_skipped")

        # 2. Duplicate enqueue within dedup window is dropped
        did2 = enqueue_codeai_message(content="Identical meeting briefing test")
        self.assertEqual(did2, "duplicate_skipped")

        # 3. Only one message is popped
        item = pop_codeai_message()
        self.assertIsNotNone(item)
        self.assertEqual(item["content"], "Identical meeting briefing test")
        self.assertIsNone(pop_codeai_message(), "Second duplicate message must not be in queue")


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

    def test_bridge_active_client_probe(self) -> None:
        from pocketfleet.bridge_server import PocketFleetBridgeServer, is_web_bridge_connected

        server = PocketFleetBridgeServer(port=18799)
        self.assertFalse(server.has_active_client())

        # Simulate client checked in
        server.active_clients["folded-host-chatgpt-web"] = time.time()
        # Even with client record, if not listening it returns False
        self.assertFalse(server.has_active_client())

        with mock.patch.object(server, "is_listening", return_value=True):
            self.assertTrue(server.has_active_client(max_idle_sec=30))
            # Expired client
            server.active_clients["folded-host-chatgpt-web"] = time.time() - 100
            self.assertFalse(server.has_active_client(max_idle_sec=30))


if __name__ == "__main__":
    unittest.main()

