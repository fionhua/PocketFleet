"""Unit tests for ThreeSeatsConfigDialog (PF-01/PF-03 UI refinement).

Tests the 4 preset role buttons, seat card selection highlight, role badge display,
and automatic command-to-engine binding without showing command text inputs.
"""
from __future__ import annotations

import os
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

from pocketfleet.control_panel import FleetManager, ThreeSeatsConfigDialog, ROLE_PRESETS
from pocketfleet.core import get_default_seats_config


class TestThreeSeatsConfigDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()
        except Exception:
            raise unittest.SkipTest("Tkinter display not available in this environment")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.config_file = self.workspace / "pocketfleet.json"
        
        # Write default seats config
        default_cfg = get_default_seats_config()
        default_cfg.save_to_file(self.config_file)
        
        self.mgr = FleetManager(log_cb=lambda msg: None)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_preset_buttons_and_seat_selection_highlight(self):
        """Verify clicking preset buttons assigns role to the selected seat and updates badge."""
        with mock.patch("pocketfleet.control_panel.CONFIG_FILE", self.config_file):
            dialog = ThreeSeatsConfigDialog(self.root, self.mgr)
            
            # Initial selected seat is 'lead' (in Code AI tab)
            self.assertEqual(dialog.selected_role, "lead")
            self.assertEqual(dialog.active_tab, "code")
            self.assertIn("当前选中编排", dialog.cards["lead"].cget("text"))
            self.assertNotIn("当前选中编排", dialog.cards["chat"].cget("text"))

            # 1. Switch selection to 'builder'
            dialog._select_seat("builder")
            self.assertEqual(dialog.selected_role, "builder")
            self.assertIn("当前选中编排", dialog.cards["builder"].cget("text"))
            self.assertNotIn("当前选中编排", dialog.cards["lead"].cget("text"))

            # 2. Click preset 'builder' on 'lead' seat
            dialog._select_seat("lead")
            builder_preset = next(p for p in ROLE_PRESETS if p["key"] == "builder")
            dialog._apply_preset_to_selected(builder_preset)

            w_lead = dialog.widgets["lead"]
            self.assertFalse(w_lead["is_custom"])
            self.assertEqual(w_lead["desc_var"].get(), "主力程序员·核心施工与算法定桩")
            self.assertIn("主力程序员", w_lead["role_badge"].cget("text"))

            # 3. Click preset 'custom' on 'lead' seat -> custom Entry appears
            custom_preset = next(p for p in ROLE_PRESETS if p["key"] == "custom")
            dialog._apply_preset_to_selected(custom_preset)
            self.assertTrue(w_lead["is_custom"])
            self.assertIn("自定义", w_lead["role_badge"].cget("text"))
            
            # Type custom description into custom_entry
            w_lead["custom_entry"].delete(0, tk.END)
            w_lead["custom_entry"].insert(0, "安全合规审计员")

            # 4. Engine change automatically updates cmd_var without showing command widget
            w_lead["engine"].set("antigravity")
            dialog._on_engine_change("lead")
            self.assertEqual(w_lead["cmd_var"].get(), "agy")

            w_lead["engine"].set("codex")
            dialog._on_engine_change("lead")
            self.assertEqual(w_lead["cmd_var"].get(), "codex")

            dialog.destroy()

    def test_preset_mutual_exclusion(self):
        """Verify that presets 1-3 are strictly mutually exclusive, while custom is not."""
        with mock.patch("pocketfleet.control_panel.CONFIG_FILE", self.config_file):
            dialog = ThreeSeatsConfigDialog(self.root, self.mgr)

            w_chat = dialog.widgets["chat"]
            w_lead = dialog.widgets["lead"]
            w_builder = dialog.widgets["builder"]

            self.assertEqual(w_chat["desc_var"].get(), "对话AI·推演与宏观对账")
            self.assertEqual(w_lead["desc_var"].get(), "施工指挥·架构守门与改卷验收")
            self.assertEqual(w_builder["desc_var"].get(), "主力程序员·核心施工与算法定桩")

            # 1. Switch to 'builder' seat and click '研发总监' (preset lead)
            dialog._select_seat("builder")
            lead_preset = next(p for p in ROLE_PRESETS if p["key"] == "lead")
            dialog._apply_preset_to_selected(lead_preset)

            # Builder now holds '研发总监'
            self.assertEqual(w_builder["desc_var"].get(), "施工指挥·架构守门与改卷验收")
            self.assertIn("研发总监", w_builder["role_badge"].cget("text"))

            # Lead (which previously had '研发总监') must be downgraded to custom to prevent conflict!
            self.assertTrue(w_lead["is_custom"])
            self.assertIn("自定义", w_lead["role_badge"].cget("text"))

            dialog.destroy()

    def test_antigravity_track_button_visibility(self):
        """Verify Antigravity track config button dynamically shows only when engine is antigravity."""
        def is_packed(w: tk.Widget) -> bool:
            try:
                w.pack_info()
                return True
            except tk.TclError:
                return False

        with mock.patch("pocketfleet.control_panel.CONFIG_FILE", self.config_file):
            dialog = ThreeSeatsConfigDialog(self.root, self.mgr)
            w_lead = dialog.widgets["lead"]
            w_builder = dialog.widgets["builder"]
            w_chat = dialog.widgets["chat"]

            # Lead defaults to antigravity -> track button is packed
            self.assertEqual(w_lead["engine"].get(), "antigravity")
            self.assertTrue(is_packed(w_lead["btn_track"]))

            # Builder defaults to codex -> track button is not packed
            self.assertEqual(w_builder["engine"].get(), "codex")
            self.assertFalse(is_packed(w_builder["btn_track"]))

            # Switch lead to codex -> button is forgotten
            w_lead["engine"].set("codex")
            dialog._on_engine_change("lead")
            self.assertFalse(is_packed(w_lead["btn_track"]))

            # Switch lead back to antigravity -> button is packed
            w_lead["engine"].set("antigravity")
            dialog._on_engine_change("lead")
            self.assertTrue(is_packed(w_lead["btn_track"]))

            dialog.destroy()

    def test_tab_switching_and_engine_quick_buttons(self):
        """Verify 2 tabs switching and 5 code buttons / 3 chat buttons replace engine for selected seat."""
        def is_packed(w: tk.Widget) -> bool:
            try:
                w.pack_info()
                return True
            except tk.TclError:
                return False

        with mock.patch("pocketfleet.control_panel.CONFIG_FILE", self.config_file):
            dialog = ThreeSeatsConfigDialog(self.root, self.mgr)

            # 1. Initially code tab active
            self.assertEqual(dialog.active_tab, "code")
            self.assertTrue(is_packed(dialog.tab_frame_code))
            self.assertFalse(is_packed(dialog.tab_frame_chat))

            # 2. Test 5 code buttons on selected seat (lead)
            dialog._select_seat("lead")
            dialog._apply_code_engine("copilot")
            self.assertEqual(dialog.widgets["lead"]["engine"].get(), "copilot")
            self.assertEqual(dialog.widgets["lead"]["cmd_var"].get(), "copilot")

            dialog._apply_code_engine("antigravity")
            self.assertEqual(dialog.widgets["lead"]["engine"].get(), "antigravity")
            self.assertEqual(dialog.widgets["lead"]["cmd_var"].get(), "agy")
            self.assertTrue(is_packed(dialog.widgets["lead"]["btn_track"]))

            # 3. Test on builder seat
            dialog._select_seat("builder")
            dialog._apply_code_engine("aider")
            self.assertEqual(dialog.widgets["builder"]["engine"].get(), "aider")
            self.assertEqual(dialog.widgets["builder"]["cmd_var"].get(), "aider")
            self.assertFalse(is_packed(dialog.widgets["builder"]["btn_track"]))

            # 4. Switch to Chat tab
            dialog._switch_tab("chat")
            self.assertEqual(dialog.active_tab, "chat")
            self.assertEqual(dialog.selected_role, "chat")
            self.assertFalse(is_packed(dialog.tab_frame_code))
            self.assertTrue(is_packed(dialog.tab_frame_chat))

            # 5. Test 3 chat buttons
            dialog._apply_chat_engine("chatgpt")
            self.assertEqual(dialog.widgets["chat"]["engine"].get(), "chatgpt")
            self.assertEqual(dialog.widgets["chat"]["cmd_var"].get(), "chatgpt")

            dialog._apply_chat_engine("claude")
            self.assertEqual(dialog.widgets["chat"]["engine"].get(), "claude")

            dialog.destroy()

    def test_seat_telegram_dialog_broker_init(self):
        """Verify SeatTelegramDialog initializes TelegramUpdateBroker and opens startgroup deep link."""
        from pocketfleet.control_panel import SeatTelegramDialog

        callback_called = []

        def on_success(env_var, username, chat_id, group_name, token):
            callback_called.append((env_var, username, chat_id, group_name, token))

        mock_token = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_LEAD_TOKEN": mock_token}), \
             mock.patch("pocketfleet.control_panel.verify_bot_token", return_value=(True, 987654, "VerifiedLeadBot", None)):
            dialog = SeatTelegramDialog(
                parent=self.root,
                role_key="lead",
                seat_name="规划席(CTO)",
                engine_name="antigravity",
                role_title="施工指挥·架构守门",
                bot_token_env="TELEGRAM_BOT_LEAD_TOKEN",
                bot_username="@VerifiedLeadBot",
                global_chat_id="",
                global_group_name="",
                on_success_callback=on_success,
                is_daemon_running_fn=lambda: False,
                authorized_user_ids=[8419345550],
            )

            # 1. Test token visibility toggle
            self.assertEqual(dialog.entry_token.cget("show"), "*")
            dialog._toggle_token_visibility()
            self.assertEqual(dialog.entry_token.cget("show"), "")
            dialog._toggle_token_visibility()
            self.assertEqual(dialog.entry_token.cget("show"), "*")

            # 2. Test deep link generation and direct protocol launch
            with mock.patch("pocketfleet.control_panel.open_telegram_group_deep_link") as mock_open_link:
                deep_link = dialog._open_telegram_deep_link()
                self.assertIsNotNone(deep_link)
                self.assertTrue(deep_link.startswith("https://t.me/VerifiedLeadBot?startgroup="))
                self.assertIsNotNone(dialog.current_param)
                self.assertLessEqual(len(dialog.current_param), 64)
                mock_open_link.assert_called_once_with("VerifiedLeadBot", dialog.current_param)

            # 3. Test legacy copy command
            with mock.patch.object(dialog, "clipboard_clear"), \
                 mock.patch.object(dialog, "clipboard_append") as mock_append, \
                 mock.patch("tkinter.messagebox.showinfo"):
                dialog._copy_binding_command()
                self.assertTrue(mock_append.called)
                self.assertIn(dialog.current_param, mock_append.call_args[0][0])

            dialog.destroy()

    def test_collaborative_group_shared_ssot(self):
        """Verify collaborative group setting is shared across all seats and saved."""
        with mock.patch("pocketfleet.control_panel.CONFIG_FILE", self.config_file):
            dialog = ThreeSeatsConfigDialog(self.root, self.mgr)

            # Initially empty or default
            dialog.global_chat_id = "-100777888999"
            dialog.global_group_name = "全员协同战队群"
            dialog._update_global_group_banner()
            self.assertIn("-100777888999", dialog.lbl_global_group.cget("text"))
            self.assertIn("全员协同战队群", dialog.lbl_global_group.cget("text"))

            # Save seats
            with mock.patch("pocketfleet.control_panel.save_token_to_env") as mock_save_env:
                dialog._save_seats()
                # Verify TELEGRAM_GROUP_ID was persisted
                saved_cfg = self.mgr.load_seats_config()
                self.assertEqual(saved_cfg.telegram_chat_id, "-100777888999")
                self.assertEqual(saved_cfg.telegram_group_name, "全员协同战队群")
                self.assertTrue(any(call[1].get("var_name") == "TELEGRAM_GROUP_ID" for call in mock_save_env.call_args_list))

    def test_context_window_sync_checkbox_toggle(self):
        """Verify context window sync checkbox enables/disables the entry."""
        with mock.patch("pocketfleet.control_panel.CONFIG_FILE", self.config_file):
            dialog = ThreeSeatsConfigDialog(self.root, self.mgr)

            # Enabled by default
            self.assertTrue(dialog.var_sync_context.get())
            self.assertEqual(dialog.entry_cw.cget("state"), "normal")

            # Toggle off
            dialog.var_sync_context.set(False)
            dialog._on_sync_context_toggle()
            self.assertEqual(dialog.entry_cw.cget("state"), "disabled")

            # Toggle on
            dialog.var_sync_context.set(True)
            dialog._on_sync_context_toggle()
            self.assertEqual(dialog.entry_cw.cget("state"), "normal")

            dialog.destroy()

    def test_chat_ai_bridge_verification_button(self):
        """Verify Chat AI launch/self-test button checks port 8765."""
        with mock.patch("pocketfleet.control_panel.CONFIG_FILE", self.config_file):
            dialog = ThreeSeatsConfigDialog(self.root, self.mgr)

            # 1. When port 8765 listening
            with mock.patch("pocketfleet.control_panel.is_port_listening", return_value=True):
                with mock.patch("tkinter.messagebox.showinfo") as mock_info:
                    dialog._verify_chat_ai_bridge()
                    self.assertTrue(mock_info.called)
                    self.assertIn("对话席位自检成功", mock_info.call_args[0][0])

            # 2. When port 8765 not listening
            with mock.patch("pocketfleet.control_panel.is_port_listening", return_value=False):
                with mock.patch("tkinter.messagebox.showwarning") as mock_warn:
                    dialog._verify_chat_ai_bridge()
                    self.assertTrue(mock_warn.called)
                    self.assertIn("Web 网关未启动", mock_warn.call_args[0][0])

            dialog.destroy()

    def test_engine_badge_draw_and_clear_flow(self):
        """Verify clicking top 5 buttons draws engine badge in place, and clicking badge clears to unset."""
        with mock.patch("pocketfleet.control_panel.CONFIG_FILE", self.config_file):
            dialog = ThreeSeatsConfigDialog(self.root, self.mgr)
            w_lead = dialog.widgets["lead"]
            btn_lead = w_lead["btn_engine"]

            # 1. Lead seat initially antigravity
            self.assertEqual(w_lead["engine"].get(), "antigravity")
            self.assertEqual(btn_lead.cget("text"), "🪐 Antigravity")
            self.assertEqual(btn_lead.cget("bg"), "#0284c7")

            # 2. Click lead's engine badge -> clears to unset
            dialog._on_engine_badge_click("lead")
            self.assertEqual(w_lead["engine"].get(), "")
            self.assertEqual(w_lead["cmd_var"].get(), "")
            self.assertIn("未设置", btn_lead.cget("text"))
            self.assertEqual(btn_lead.cget("bg"), "#334155")

            # 3. Attempting to save with unset engine prompts error and aborts
            with mock.patch("tkinter.messagebox.showerror") as mock_err:
                dialog._save_seats()
                self.assertTrue(mock_err.called)
                self.assertIn("执行引擎未设置", mock_err.call_args[0][0])

            # 4. Click top button 'codex' while lead is selected -> paints Codex badge
            dialog._select_seat("lead")
            dialog._apply_code_engine("codex")
            self.assertEqual(w_lead["engine"].get(), "codex")
            self.assertEqual(btn_lead.cget("text"), "⚡ OpenAI Codex")
            self.assertEqual(btn_lead.cget("bg"), "#10b981")
            self.assertEqual(w_lead["cmd_var"].get(), "codex")

            # 5. Click top button 'aider' -> paints Aider badge
            dialog._apply_code_engine("aider")
            self.assertEqual(w_lead["engine"].get(), "aider")
            self.assertEqual(btn_lead.cget("text"), "🛠️ Aider")
            self.assertEqual(btn_lead.cget("bg"), "#8b5cf6")
            self.assertEqual(w_lead["cmd_var"].get(), "aider")

            # 6. Click engine badge again -> clears to unset
            dialog._on_engine_badge_click("lead")
            self.assertEqual(w_lead["engine"].get(), "")
            self.assertIn("未设置", btn_lead.cget("text"))

            dialog.destroy()

    def test_tg_config_button_and_status_display(self):
        """Verify dedicated 'TG设置' button exists and status label updates correctly."""
        with mock.patch("pocketfleet.control_panel.CONFIG_FILE", self.config_file), \
             mock.patch.dict(os.environ, {"TELEGRAM_BOT_JUDGE_TOKEN": ""}):
            dialog = ThreeSeatsConfigDialog(self.root, self.mgr)
            w_lead = dialog.widgets["lead"]

            # 1. Prominent TG settings button exists with correct text and styling
            btn_tg = w_lead["btn_tg_config"]
            self.assertEqual(btn_tg.cget("text"), "✈️ TG设置")
            self.assertEqual(btn_tg.cget("bg"), "#0284c7")

            # 2. Status label initially displays waiting/unconfigured if no env token
            lbl_status = w_lead["lbl_tg_status"]
            self.assertIn("待配置", lbl_status.cget("text"))

            # 3. When token is populated in env, capsule updates to '已就位'
            with mock.patch.dict(os.environ, {"TELEGRAM_BOT_JUDGE_TOKEN": "123456789:AaBbCcDdEeFfGgHhIiJj1234"}):
                dialog._update_seat_tg_capsule("lead")
                self.assertIn("已就位", lbl_status.cget("text"))
                self.assertIn("裁决者", w_lead["lbl_bot_badge"].cget("text"))

            dialog.destroy()

    def test_seat_telegram_dialog_event_chat_bound(self):
        """Verify _poll_broker_events updates dialog and triggers callback upon CHAT_BOUND event."""
        from pocketfleet.control_panel import SeatTelegramDialog

        callback_called = []

        def on_success(env_var, username, chat_id, group_name, token):
            callback_called.append((env_var, username, chat_id, group_name, token))

        mock_token = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
        with mock.patch("pocketfleet.control_panel.verify_bot_token", return_value=(True, 987654, "VerifiedLeadBot", None)):
            dialog = SeatTelegramDialog(
                parent=self.root,
                role_key="lead",
                seat_name="裁决者",
                engine_name="antigravity",
                role_title="架构守门",
                bot_token_env="TELEGRAM_BOT_TEST_TOKEN",
                bot_username="@VerifiedLeadBot",
                global_chat_id="",
                global_group_name="",
                on_success_callback=on_success,
                is_daemon_running_fn=lambda: False,
                authorized_user_ids=[8419345550],
            )
            dialog.entry_token.delete(0, tk.END)
            dialog.entry_token.insert(0, mock_token)

            # Publish a mock CHAT_BOUND event
            dialog.state_store.publish_broker_event(
                event_type="CHAT_BOUND",
                seat_role="lead",
                payload={
                    "chat_id": -1004309197838,
                    "chat_title": "Fleet WarRoom",
                    "sender_id": 8419345550,
                    "sender_name": "Commander",
                    "bot_username": "VerifiedLeadBot",
                },
            )

            with mock.patch("tkinter.messagebox.showinfo") as mock_info:
                dialog._poll_broker_events()
                self.assertTrue(mock_info.called)
                self.assertEqual(dialog.entry_chat_id.get(), "-1004309197838")
                self.assertEqual(dialog.entry_group_name.get(), "Fleet WarRoom")
                self.assertEqual(len(callback_called), 1)
                self.assertEqual(callback_called[0][2], "-1004309197838")

            dialog.destroy()

    def test_seat_telegram_dialog_event_conflict_409(self):
        """Verify _poll_broker_events raises modal alert upon CONFLICT_409 event."""
        from pocketfleet.control_panel import SeatTelegramDialog

        mock_token = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
        with mock.patch("pocketfleet.control_panel.verify_bot_token", return_value=(True, 987654, "VerifiedLeadBot", None)):
            dialog = SeatTelegramDialog(
                parent=self.root,
                role_key="lead",
                seat_name="裁决者",
                engine_name="antigravity",
                role_title="架构守门",
                bot_token_env="TELEGRAM_BOT_TEST_TOKEN",
                bot_username="@VerifiedLeadBot",
                global_chat_id="",
                global_group_name="",
                on_success_callback=lambda *args, **kwargs: None,
                is_daemon_running_fn=lambda: False,
                authorized_user_ids=[8419345550],
            )

            dialog.state_store.publish_broker_event(
                event_type="CONFLICT_409",
                seat_role="lead",
                payload={"error": "Occupied by external consumer"},
            )

            with mock.patch("tkinter.messagebox.showerror") as mock_err:
                dialog._poll_broker_events()
                self.assertTrue(mock_err.called)
                self.assertIn("HTTP 409", mock_err.call_args[0][0])
                self.assertFalse(dialog.is_listening)

            dialog.destroy()

    def test_seat_telegram_dialog_follower_checkin(self):
        """Verify follower seats inherit warroom chat_id and send live test checkin."""
        from pocketfleet.control_panel import SeatTelegramDialog

        callback_called = []

        def on_success(env_var, username, chat_id, group_name, token):
            callback_called.append((env_var, username, chat_id, group_name, token))

        mock_token = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
        with mock.patch("pocketfleet.control_panel.verify_bot_token", return_value=(True, 777666, "BuilderBot", None)), \
             mock.patch("pocketfleet.control_panel.verify_chat_member", return_value=(True, "member")), \
             mock.patch("pocketfleet.control_panel.send_bot_checkin", return_value=(True, "通信测试成功")) as mock_send, \
             mock.patch("tkinter.messagebox.showinfo") as mock_info:

            dialog = SeatTelegramDialog(
                parent=self.root,
                role_key="builder",
                seat_name="泥蛇",
                engine_name="codex",
                role_title="主力程序员",
                bot_token_env="TELEGRAM_BOT_MUDSNAKE_TOKEN",
                bot_username="@BuilderBot",
                global_chat_id="-1004309197838",
                global_group_name="Fleet WarRoom",
                on_success_callback=on_success,
                is_daemon_running_fn=lambda: False,
                authorized_user_ids=[8419345550],
            )
            dialog.entry_token.delete(0, tk.END)
            dialog.entry_token.insert(0, mock_token)

            dialog._test_follower_checkin()

            self.assertTrue(mock_send.called)
            self.assertEqual(mock_send.call_args[1]["chat_id"], "-1004309197838")
            self.assertEqual(mock_send.call_args[1]["bot_name"], "泥蛇")
            self.assertEqual(len(callback_called), 1)
            self.assertEqual(callback_called[0][2], "-1004309197838")
            self.assertTrue(mock_info.called)

    def test_seat_telegram_dialog_manual_emergency_save(self):
        """Verify manual emergency drawer saves chat_id and token."""
        from pocketfleet.control_panel import SeatTelegramDialog

        callback_called = []

        def on_success(env_var, username, chat_id, group_name, token):
            callback_called.append((env_var, username, chat_id, group_name, token))

        with mock.patch("pocketfleet.control_panel.verify_bot_token", return_value=(False, None, None, "Invalid")), \
             mock.patch("pocketfleet.control_panel.save_token_to_env") as mock_save, \
             mock.patch("tkinter.messagebox.showinfo"):

            dialog = SeatTelegramDialog(
                parent=self.root,
                role_key="lead",
                seat_name="裁决者",
                engine_name="antigravity",
                role_title="架构守门",
                bot_token_env="TELEGRAM_BOT_LEAD_TOKEN",
                bot_username="@AiSoulJudgeBot",
                global_chat_id="",
                global_group_name="",
                on_success_callback=on_success,
            )

            dialog.entry_token.delete(0, tk.END)
            dialog.entry_token.insert(0, "mock_manual_token")
            dialog.entry_chat_id.delete(0, tk.END)
            dialog.entry_chat_id.insert(0, "-100888999")
            dialog.entry_group_name.delete(0, tk.END)
            dialog.entry_group_name.insert(0, "Manual Group")

            dialog._manual_save_action()

            self.assertTrue(mock_save.called)
            self.assertEqual(len(callback_called), 1)
            self.assertEqual(callback_called[0][2], "-100888999")

    def test_in_card_telegram_drawer_interactions(self):
        """Verify in-card collapsible Telegram drawer expands, collapses, toggles plaintext eye,
        and joins group with zero external modal popups."""
        with mock.patch("pocketfleet.control_panel.CONFIG_FILE", self.config_file):
            dialog = ThreeSeatsConfigDialog(self.root, self.mgr)
            w_lead = dialog.widgets["lead"]
            w_builder = dialog.widgets["builder"]

            # 1. Initially, drawer is collapsed: btn_tg_config has caption '✈️ TG设置'
            self.assertEqual(w_lead["btn_tg_config"].cget("text"), "✈️ TG设置")

            # 2. Expand lead drawer
            dialog._expand_tg_drawer("lead")
            entry_token = w_lead["drawer_token_entry"]
            btn_eye = w_lead["drawer_token_eye"]
            self.assertEqual(entry_token.cget("show"), "*")

            # 3. Eye toggle shows full plaintext token
            dialog._toggle_drawer_token_eye("lead")
            self.assertEqual(entry_token.cget("show"), "")
            self.assertEqual(btn_eye.cget("text"), "🙈")

            dialog._toggle_drawer_token_eye("lead")
            self.assertEqual(entry_token.cget("show"), "*")
            self.assertEqual(btn_eye.cget("text"), "👁️")

            # 4. Mutual exclusion: expanding builder drawer collapses lead drawer
            dialog._expand_tg_drawer("builder")
            self.assertFalse(w_lead["drawer_expanded"].winfo_manager())

            # 5. Collapse builder drawer
            dialog._collapse_tg_drawer("builder")
            self.assertFalse(w_builder["drawer_expanded"].winfo_manager())

            # 6. Test drawer join group flow
            mock_token = "999888777:MockSecretTokenForFleet"
            w_lead["drawer_token_entry"].delete(0, tk.END)
            w_lead["drawer_token_entry"].insert(0, mock_token)

            with mock.patch("pocketfleet.control_panel.verify_bot_token", return_value=(True, 112233, "InCardBot", None)), \
                 mock.patch("pocketfleet.control_panel.save_token_to_env") as mock_save, \
                 mock.patch("pocketfleet.control_panel.open_telegram_group_deep_link") as mock_open_link:
                dialog._drawer_join_tg("lead")

                self.assertTrue(mock_save.called)
                self.assertTrue(mock_open_link.called)
                self.assertEqual(mock_open_link.call_args[0][0], "InCardBot")
                self.assertIsNotNone(mock_open_link.call_args[0][1])
                self.assertIn("正在监听加群", w_lead["drawer_status_lbl"].cget("text"))

                # Simulate broker event CHAT_BOUND
                broker = dialog._drawer_brokers["lead"]
                broker.state_store.publish_broker_event(
                    event_type="CHAT_BOUND",
                    seat_role="lead",
                    payload={"chat_id": -100999000111, "chat_title": "InCard WarRoom"},
                )

                dialog._poll_drawer_broker("lead")
                self.assertEqual(dialog.global_chat_id, "-100999000111")
                self.assertEqual(dialog.global_group_name, "InCard WarRoom")
                self.assertIn("已连接战队群", w_lead["drawer_status_lbl"].cget("text"))
                self.assertIn("InCard WarRoom", dialog.lbl_global_group.cget("text"))

            dialog.destroy()

    def test_open_telegram_group_deep_link_protocol(self):
        """Verify open_telegram_group_deep_link prioritizes native tg:// protocol, then browser fallback."""
        from pocketfleet.control_panel import open_telegram_group_deep_link

        # 1. Native protocol succeeds
        if hasattr(os, "startfile"):
            with mock.patch("os.startfile") as mock_startfile:
                res = open_telegram_group_deep_link("@TestBot", "param123")
                self.assertTrue(res)
                mock_startfile.assert_called_once_with("tg://resolve?domain=TestBot&startgroup=param123")

        # 2. Native protocol raises error -> fallback to webbrowser.open
        with mock.patch("os.startfile", side_effect=OSError("No association")), \
             mock.patch("webbrowser.open") as mock_browser:
            res = open_telegram_group_deep_link("@TestBot", "param123")
            self.assertTrue(res)
            mock_browser.assert_called_once_with("https://t.me/TestBot?startgroup=param123")

    def test_drawer_auto_populate_nickname_and_badge(self):
        """Verify token verification automatically extracts first_name and displays bot badge."""
        from pocketfleet.transport.telegram import BotVerificationResult

        with mock.patch("pocketfleet.control_panel.CONFIG_FILE", self.config_file), \
             mock.patch.dict(os.environ, {"TELEGRAM_BOT_LEAD_TOKEN": ""}):
            dialog = ThreeSeatsConfigDialog(self.root, self.mgr)
            w_lead = dialog.widgets["lead"]

            # 1. Initially no bot badge
            self.assertEqual(w_lead["lbl_bot_badge"].cget("text"), "")

            # 2. When token is verified, Bot nickname is auto-fetched
            mock_res = BotVerificationResult(True, 998877, "NewJudgeBot", None, first_name="神盾裁决者")
            with mock.patch("pocketfleet.control_panel.verify_bot_token", return_value=mock_res), \
                 mock.patch("pocketfleet.control_panel.save_token_to_env"), \
                 mock.patch("pocketfleet.control_panel.open_telegram_group_deep_link"):
                w_lead["drawer_token_entry"].insert(0, "123456:SecretToken")
                dialog._drawer_join_tg("lead")

                # w["name"] was auto-updated to first_name
                self.assertEqual(w_lead["name"].get(), "神盾裁决者")
                # Bot badge is displayed
                badge_text = w_lead["lbl_bot_badge"].cget("text")
                self.assertIn("神盾裁决者", badge_text)
                self.assertIn("@NewJudgeBot", badge_text)

    def test_memo_area_formatting_and_dynamic_refresh(self):
        """Verify the Memo area formats the 2-line header correctly and updates dynamically."""
        with mock.patch("pocketfleet.control_panel.CONFIG_FILE", self.config_file):
            dialog = ThreeSeatsConfigDialog(self.root, self.mgr)
            
            # Initial human state
            dialog._apply_detected_human_name("ENTJ指挥官")

            memo_text = dialog.txt_memo.get("1.0", tk.END).strip()
            self.assertIn("[来自TG多AI协作];[人类用户:ENTJ指挥官];参与者:", memo_text)
            self.assertIn("回复格式要求:以 [Telegram]re:{someone} 或 [Telegram][mailto:{someone}] 为开头（指明单一收件人）。", memo_text)
            self.assertIn("@AiSoulJudgeBot", memo_text)
            self.assertIn("@AiSoulMudSnakeBot", memo_text)

            # Test detected human name update
            dialog._apply_detected_human_name("泥蛇结算闭环者")
            updated_text = dialog.txt_memo.get("1.0", tk.END).strip()
            self.assertIn("[人类用户:泥蛇结算闭环者]", updated_text)
            self.assertIn("泥蛇结算闭环者", dialog.lbl_human_display.cget("text"))

            # Test changing seat bot username updates memo
            dialog.widgets["lead"]["user"].delete(0, tk.END)
            dialog.widgets["lead"]["user"].insert(0, "@NewJudgeBot")
            dialog._refresh_memo()
            updated_ai_text = dialog.txt_memo.get("1.0", tk.END).strip()
            self.assertIn("@NewJudgeBot", updated_ai_text)

            dialog.destroy()

    def test_fetch_group_human_nickname_mock(self):
        """Verify fetch_group_human_nickname parses TG getChatAdministrators response correctly."""
        from pocketfleet.transport.telegram import fetch_group_human_nickname
        import json
        import io

        fake_admins = {
            "ok": True,
            "result": [
                {
                    "status": "administrator",
                    "user": {"id": 111, "is_bot": True, "first_name": "AiSoulJudgeBot"}
                },
                {
                    "status": "creator",
                    "user": {"id": 222, "is_bot": False, "first_name": "ENTJ指挥官", "last_name": ""}
                }
            ]
        }

        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = json.dumps(fake_admins).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with mock.patch("urllib.request.urlopen", return_value=mock_resp):
            name = fetch_group_human_nickname("fake_token", -1004309197838)
            self.assertEqual(name, "ENTJ指挥官")


if __name__ == "__main__":
    unittest.main()





