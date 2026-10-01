#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import multiprocessing
import os
import queue
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union
import tkinter as tk
from tkinter import font as tkfont, messagebox, ttk

try:
    from PIL import Image, ImageDraw
    import pystray
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False
    Image = None
    ImageDraw = None
    pystray = None

from pocketfleet.cockpit import CockpitServer, telemetry
from pocketfleet.config_env import load_env_file
from pocketfleet.core import (
    WorkerType,
    RoleAssignment,
    SeatRole,
    SeatConfig,
    FleetSeatsConfig,
    ALLOWED_CHAT_ENGINES,
    ALLOWED_CODE_ENGINES,
    get_default_seats_config,
    get_default_command_for_engine,
    validate_seats_config,
)
from pocketfleet.loop import DispatchLoop

from pocketfleet.antigravity_tracks import (
    AntigravityTrackController,
    ImportDetectionError,
    ImportRequiredError,
    TrackCandidate,
    is_valid_uuid,
)
from pocketfleet.onboard import FleetConfig, save_token_to_env
from pocketfleet.transport.telegram import (
    verify_bot_token,
    verify_chat_member,
    send_bot_checkin,
)
from pocketfleet.broker import (
    TelegramUpdateBroker,
    hash_pin,
    BrokerConflictError,
    SenderAuthorizationError,
)
from pocketfleet.session_hub import SessionHub, SessionWorker
from pocketfleet.state import StateStore
from pocketfleet.transport.telegram import TelegramTransport

# ==============================================================================
# Environment & Paths
# ==============================================================================
if getattr(sys, "frozen", False):
    REPO_ROOT = Path(sys.executable).resolve().parent
else:
    REPO_ROOT = Path(__file__).resolve().parent.parent.parent

CONFIG_FILE = REPO_ROOT / "pocketfleet.json"
load_env_file(REPO_ROOT / ".env")
logger = logging.getLogger(__name__)


def is_port_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.close()
        return True
    except OSError:
        return False


def create_tray_image(color: str = "cyan"):
    if not HAS_TRAY or Image is None or ImageDraw is None:
        return None
    color_map = {
        "cyan": "#06b6d4",
        "green": "#22c55e",
        "red": "#ef4444",
        "yellow": "#f59e0b",
    }
    hex_color = color_map.get(color, "#06b6d4")
    img = Image.new("RGBA", (64, 64), color=(0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([4, 4, 60, 60], radius=16, fill="#0f172a", outline=hex_color, width=3)
    draw.polygon([(32, 12), (48, 48), (32, 40), (16, 48)], fill=hex_color)
    draw.ellipse([28, 24, 36, 32], fill="#ffffff")
    return img


def open_telegram_group_deep_link(bot_username: str, startgroup_param: str) -> bool:
    """Open Telegram client directly to add bot to group without browser intermediaries.
    
    1. Tries native tg://resolve?domain=...&startgroup=... protocol to bypass browser
       prompts (Chrome popup & webpage with START BOT button).
    2. Falls back to https://t.me/ startgroup link if native protocol cannot be opened.
    """
    clean_uname = bot_username.lstrip("@")
    tg_proto = f"tg://resolve?domain={clean_uname}&startgroup={startgroup_param}"
    https_url = f"https://t.me/{clean_uname}?startgroup={startgroup_param}"

    if hasattr(os, "startfile"):
        try:
            os.startfile(tg_proto)
            return True
        except Exception as proto_err:
            logger.info("Direct tg:// protocol failed (%s), falling back to browser: %s", proto_err, https_url)

    try:
        webbrowser.open(https_url)
        return True
    except Exception as e:
        logger.warning("Failed to open deep link: %s", e)
        return False


# ==============================================================================
# Three Seats Configuration Dialog (Triad Seats Setup)
# ==============================================================================
ROLE_PRESETS = [
    {
        "key": "chat",
        "btn_label": "🗣️ 面向人类交互",
        "desc_text": "对话AI·推演与宏观对账",
        "hint": "【面向人类交互】\n承担与人类指挥官第一人称的推演对话、需求澄清与宏观对账，作为星舰前端交流主通道。",
        "bg": "#0284c7",
        "fg": "#ffffff",
    },
    {
        "key": "lead",
        "btn_label": "🎖️ 研发总监",
        "desc_text": "施工指挥·架构守门与改卷验收",
        "hint": "【研发总监】\n统领工程落地与代码审查，负责系统生存率守门、架构验收与质量裁决。",
        "bg": "#059669",
        "fg": "#ffffff",
    },
    {
        "key": "builder",
        "btn_label": "🛠️ 主力程序员",
        "desc_text": "主力程序员·核心施工与算法定桩",
        "hint": "【主力程序员】\n专注具体模块编码、算法定桩与攻坚施工，接受改卷验收并交付高质量代码。",
        "bg": "#d97706",
        "fg": "#ffffff",
    },
    {
        "key": "custom",
        "btn_label": "✏️ 自定义",
        "desc_text": "",
        "hint": "【自定义职能】\n手动为当前选中的席位自由输入自定义职责描述文本。",
        "bg": "#475569",
        "fg": "#ffffff",
    },
]


class SeatTelegramDialog(tk.Toplevel):
    """Dedicated modal for single-seat Telegram Bot configuration, credential validation,
    and ephemeral binding via single-consumer TelegramUpdateBroker (PF-03R7).
    """

    def __init__(
        self,
        parent,
        role_key: str,
        seat_name: str,
        engine_name: str,
        role_title: str,
        bot_token_env: str,
        bot_username: str,
        global_chat_id: str,
        global_group_name: str,
        on_success_callback,
        is_daemon_running_fn=None,
        authorized_user_ids: Optional[Sequence[int]] = None,
    ):
        super().__init__(parent)
        self.parent = parent
        self.role_key = role_key
        self.seat_name = seat_name
        self.engine_name = engine_name
        self.role_title = role_title
        self.bot_token_env = bot_token_env
        self.bot_username = bot_username
        self.global_chat_id = str(global_chat_id or "").strip()
        self.global_group_name = str(global_group_name or "").strip()
        self.on_success_callback = on_success_callback
        self.is_daemon_running_fn = is_daemon_running_fn
        self.authorized_user_ids = list(authorized_user_ids) if authorized_user_ids else []

        self.state_store = StateStore()
        self.broker: Optional[TelegramUpdateBroker] = None
        self.current_pin: Optional[str] = None
        existing_events = self.state_store.get_broker_events(after_event_id=0, limit=1000)
        self.last_event_id = max([e.get("event_id", 0) for e in existing_events], default=0)
        self.is_listening = True
        self.is_advanced_open = False

        self.title(f"Telegram 席位凭据与战队群设置 — [{self.seat_name}]")
        self.geometry("680x420")
        self.resizable(False, False)
        self.configure(bg="#0b0f19")
        self.transient(parent)
        self.grab_set()

        self.font_title = tkfont.Font(family="Segoe UI", size=12, weight="bold")
        self.font_sub = tkfont.Font(family="Segoe UI", size=9)
        self.font_bold = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        self.font_mono = tkfont.Font(family="Consolas", size=9)

        # Center on parent
        self.update_idletasks()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        cx = max(0, px + (pw - 680) // 2)
        cy = max(0, py + (ph - 420) // 2)
        self.geometry(f"+{cx}+{cy}")

        self.show_token = False
        self._build_ui()
        self._init_broker_session()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        # Header banner
        header = tk.Frame(self, bg="#0f172a", padx=20, pady=12)
        header.pack(fill=tk.X)
        tk.Label(
            header,
            text=f"🤖 Telegram 席位凭据与战队群设置",
            fg="#38bdf8",
            bg="#0f172a",
            font=self.font_title,
        ).pack(anchor="w")
        tk.Label(
            header,
            text=f"席位: {self.seat_name} ｜ 引擎: {self.engine_name.upper()} ｜ 战队协同群为全席位共享资产",
            fg="#94a3b8",
            bg="#0f172a",
            font=self.font_sub,
        ).pack(anchor="w", pady=(2, 0))

        content = tk.Frame(self, bg="#0b0f19", padx=20, pady=12)
        content.pack(fill=tk.BOTH, expand=True)

        # --- Section 1: Bot Token 与身份验证 ---
        bot_frame = tk.LabelFrame(
            content,
            text=f" 🔐 1. 填入并验证 Bot Token ",
            fg="#10b981",
            bg="#1e293b",
            font=self.font_bold,
            padx=14,
            pady=8,
            relief=tk.SOLID,
            bd=1,
            highlightthickness=1,
            highlightbackground="#334155",
        )
        bot_frame.pack(fill=tk.X, pady=(0, 10))

        b_r1 = tk.Frame(bot_frame, bg="#1e293b")
        b_r1.pack(fill=tk.X, pady=2)
        tk.Label(b_r1, text="Bot Token:", fg="#f1f5f9", bg="#1e293b", font=self.font_sub, width=11, anchor="w").pack(side=tk.LEFT)
        self.entry_token = tk.Entry(b_r1, bg="#0f172a", fg="#f8fafc", insertbackground="#f8fafc", font=self.font_mono, width=42, relief=tk.FLAT, bd=4, show="*")
        env_val = (os.environ.get(self.bot_token_env) or "").strip()
        if env_val:
            self.entry_token.insert(0, env_val)
        self.entry_token.pack(side=tk.LEFT, padx=(0, 6))
        self.entry_token.bind("<FocusOut>", lambda e: self._init_broker_session())
        self.entry_token.bind("<Return>", lambda e: self._init_broker_session())

        self.btn_toggle = tk.Button(
            b_r1, text="👁️", bg="#334155", fg="#f8fafc", activebackground="#475569",
            font=self.font_sub, relief=tk.FLAT, padx=6, pady=1, cursor="hand2", command=self._toggle_token_visibility,
        )
        self.btn_toggle.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_verify = None

        self.lbl_token_status = tk.Label(b_r1, text="", fg="#94a3b8", bg="#1e293b", font=self.font_sub)
        self.lbl_token_status.pack(side=tk.LEFT)

        b_r2 = tk.Frame(bot_frame, bg="#1e293b")
        b_r2.pack(fill=tk.X, pady=2)
        tk.Label(b_r2, text="Bot 用户名:", fg="#94a3b8", bg="#1e293b", font=self.font_sub, width=11, anchor="w").pack(side=tk.LEFT)
        self.lbl_bot_uname = tk.Label(b_r2, text=self.bot_username or "（尚未验证）", fg="#38bdf8", bg="#1e293b", font=self.font_mono)
        self.lbl_bot_uname.pack(side=tk.LEFT, padx=(0, 12))

        # Hidden auth user entry for compatibility with internal helpers & tests (omitted from UI per Commander directive)
        self.entry_auth_user = tk.Entry(self)
        init_auth_id = ""
        if self.authorized_user_ids:
            init_auth_id = str(self.authorized_user_ids[0])
        else:
            env_auth = (os.environ.get("POCKETFLEET_AUTHORIZED_USER_IDS") or os.environ.get("TELEGRAM_AUTHORIZED_USER_IDS") or "").strip()
            if env_auth:
                init_auth_id = env_auth.split(",")[0].strip()
        if init_auth_id:
            self.entry_auth_user.insert(0, init_auth_id)

        # Hidden/internal env field
        self.entry_env = tk.Entry(b_r2)
        self.entry_env.insert(0, self.bot_token_env)

        # --- Section 2: 战队群一键入群绑定 ---
        self.group_frame = tk.LabelFrame(
            content,
            text=" 📢 2. 战队协同群一键绑定 ",
            fg="#38bdf8",
            bg="#1e293b",
            font=self.font_bold,
            padx=16,
            pady=12,
            relief=tk.SOLID,
            bd=1,
            highlightthickness=1,
            highlightbackground="#334155",
        )
        self.group_frame.pack(fill=tk.X, pady=(0, 10))

        if self.global_chat_id and self.role_key != "lead":
            hint_txt = (
                f"全舰已锁定协同群：{self.global_group_name or '战队群'} (ID: {self.global_chat_id})\n"
                f"点击下方按钮将在 Telegram 中打开选群页面，请务必选择同一个战队群添加 Bot 完成绑定："
            )
        else:
            hint_txt = (
                "点击下方按钮将在 Telegram 中打开官方选群页面，选择战队群确认后，\n"
                "系统将自动接收授权入站消息并完成战队群绑定与指挥官确权："
            )

        tk.Label(
            self.group_frame,
            text=hint_txt,
            fg="#cbd5e1",
            bg="#1e293b",
            font=self.font_sub,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 8))

        btn_row = tk.Frame(self.group_frame, bg="#1e293b")
        btn_row.pack(fill=tk.X, pady=(2, 6))

        self.btn_open_tg = tk.Button(
            btn_row,
            text="🚀 打开 Telegram，选择战队群",
            bg="#0284c7",
            fg="#ffffff",
            activebackground="#0369a1",
            font=self.font_bold,
            relief=tk.FLAT,
            padx=16,
            pady=8,
            cursor="hand2",
            command=self._open_telegram_deep_link,
        )
        self.btn_open_tg.pack(anchor="w")

        status_row = tk.Frame(self.group_frame, bg="#1e293b")
        status_row.pack(fill=tk.X, pady=(4, 0))
        self.lbl_broker_status = tk.Label(
            status_row,
            text="📡 准备就绪：粘贴 Token 后，点击上方按钮即可一键加群绑定",
            fg="#94a3b8",
            bg="#1e293b",
            font=self.font_sub,
        )
        self.lbl_broker_status.pack(side=tk.LEFT)

        # Hidden manual entry fields for compatibility with fallback handlers & tests (omitted from UI per Commander directive)
        self.entry_chat_id = tk.Entry(self)
        if self.global_chat_id:
            self.entry_chat_id.insert(0, self.global_chat_id)
        self.entry_group_name = tk.Entry(self)
        if self.global_group_name:
            self.entry_group_name.insert(0, self.global_group_name)

        # --- Section 3: 底部操作栏 ---
        actions = tk.Frame(self, bg="#0f172a", padx=20, pady=12)
        actions.pack(fill=tk.X, side=tk.BOTTOM)

        btn_cancel = tk.Button(
            actions,
            text="完成并关闭 (Done)",
            bg="#334155", fg="#f8fafc", activebackground="#475569",
            font=self.font_bold, relief=tk.FLAT, padx=14, pady=6, cursor="hand2",
            command=self.destroy,
        )
        btn_cancel.pack(side=tk.RIGHT)

    def _toggle_advanced(self):
        pass

    def _toggle_token_visibility(self):
        self.show_token = not self.show_token
        if self.show_token:
            self.entry_token.config(show="")
            self.btn_toggle.config(text="🙈")
        else:
            self.entry_token.config(show="*")
            self.btn_toggle.config(text="👁️")

    def _get_active_token(self) -> str:
        token_input = self.entry_token.get().strip()
        env_val = (os.environ.get(self.bot_token_env) or "").strip()
        return token_input or env_val

    def _init_broker_session(self):
        token = self._get_active_token()
        if not token:
            self.lbl_token_status.config(text="⚪ 请输入 Token", fg="#94a3b8")
            return

        ok, bot_id, uname, err = verify_bot_token(token)
        if ok and bot_id:
            self.bot_id = bot_id
            if uname:
                self.bot_username = "@" + uname.lstrip("@")
            final_user = self.bot_username or f"Bot_{bot_id}"
            self.lbl_bot_uname.config(text=f"{final_user} (ID: {bot_id})")
            self.lbl_token_status.config(text="🟢 Token有效", fg="#10b981")

            try:
                env_file = REPO_ROOT / ".env"
                save_token_to_env(token, env_path=env_file, var_name=self.bot_token_env)
                os.environ[self.bot_token_env] = token
            except Exception:
                pass

            auth_user_val = self.entry_auth_user.get().strip() if hasattr(self, "entry_auth_user") else ""
            auth_id = int(auth_user_val) if auth_user_val.isdigit() else (self.authorized_user_ids[0] if self.authorized_user_ids else None)
            effective_auth_ids = [auth_id] if auth_id else list(self.authorized_user_ids)

            self.broker = TelegramUpdateBroker(
                bot_token=token,
                seat_role=self.role_key,
                state_store=self.state_store,
                authorized_user_ids=effective_auth_ids,
                repo_root=REPO_ROOT,
                global_chat_id=self.global_chat_id,
            )
        else:
            self.lbl_token_status.config(text=f"❌ {err or '无效'}", fg="#ef4444")

    def _verify_token_action(self):
        token = self._get_active_token()
        if not token:
            messagebox.showerror("缺少 Token", "请先填入 Telegram Bot Token！", parent=self)
            return
        self._init_broker_session()

    def _open_telegram_deep_link(self) -> Optional[str]:
        """PF-TG-R8: Generate ephemeral deep link and open Telegram client/web for 1-click binding."""
        token = self._get_active_token()
        if not token:
            messagebox.showerror("缺少 Token", "请先填入 Telegram Bot Token！", parent=self)
            return None

        ok, bot_id, uname, err = verify_bot_token(token)
        if not ok or not bot_id:
            self.lbl_token_status.config(text=f"❌ {err or '无效'}", fg="#ef4444")
            messagebox.showerror("Token 校验失败", f"Bot Token 校验失败: {err}", parent=self)
            return None

        self.bot_id = bot_id
        if uname:
            self.bot_username = "@" + uname.lstrip("@")
        final_user = self.bot_username or f"Bot_{bot_id}"
        self.lbl_bot_uname.config(text=f"{final_user} (ID: {bot_id})")
        self.lbl_token_status.config(text="🟢 Token有效", fg="#10b981")

        try:
            env_file = REPO_ROOT / ".env"
            save_token_to_env(token, env_path=env_file, var_name=self.bot_token_env)
            os.environ[self.bot_token_env] = token
        except Exception:
            pass

        auth_user_val = self.entry_auth_user.get().strip() if hasattr(self, "entry_auth_user") else ""
        auth_id = int(auth_user_val) if auth_user_val.isdigit() else (self.authorized_user_ids[0] if self.authorized_user_ids else None)
        effective_auth_ids = [auth_id] if auth_id else list(self.authorized_user_ids)

        if not self.broker:
            self.broker = TelegramUpdateBroker(
                bot_token=token,
                seat_role=self.role_key,
                state_store=self.state_store,
                authorized_user_ids=effective_auth_ids,
                repo_root=REPO_ROOT,
                global_chat_id=self.global_chat_id,
            )
        else:
            self.broker.bot_token = token
            self.broker.authorized_user_ids = effective_auth_ids
            self.broker.global_chat_id = self.global_chat_id

        clean_uname = (uname or self.bot_username).lstrip("@")
        self.current_param = self.broker.create_ephemeral_param(
            bot_id=bot_id,
            bot_username=clean_uname,
            seat_role=self.role_key,
            authorized_user_id=auth_id,
            ttl_seconds=300,
        )
        self.current_pin = self.current_param
        deep_link = self.broker.get_startgroup_deep_link(clean_uname, self.current_param)

        is_daemon = self.is_daemon_running_fn() if self.is_daemon_running_fn else False
        if is_daemon:
            self.lbl_broker_status.config(text="⚡ 后台服务运行中：已接入全局总线，请在 Telegram 中选择战队群...", fg="#38bdf8")
        else:
            self.lbl_broker_status.config(text="📡 正在监听中：请在 Telegram 中选择战队群并确认添加...", fg="#fbbf24")
            self.broker.start_temporary_poller()

        self.is_listening = True
        self.after(500, self._poll_broker_events)

        try:
            open_telegram_group_deep_link(clean_uname, self.current_param)
        except Exception as e:
            logger.warning("Failed to open deep link: %s", e)

        return deep_link

    def _copy_binding_command(self):
        """Legacy helper retained for compatibility."""
        if hasattr(self, "current_param") and self.current_param:
            uname = (self.bot_username or "").lstrip("@")
            cmd = f"/start@{uname} {self.current_param}"
            self.clipboard_clear()
            self.clipboard_append(cmd)
            messagebox.showinfo("已复制指令", f"指令已复制到剪贴板：\n\n{cmd}", parent=self)

    def _poll_broker_events(self):
        if not self.is_listening:
            return

        events = self.state_store.get_broker_events(after_event_id=self.last_event_id)
        for ev in events:
            ev_id = ev.get("event_id", 0)
            self.last_event_id = max(self.last_event_id, ev_id)
            ev_type = ev.get("event_type")
            payload = ev.get("payload") or {}

            if ev_type == "CHAT_BOUND":
                cid = str(payload.get("chat_id", ""))
                title = payload.get("chat_title", "")
                cmd_id = payload.get("commander_id") or payload.get("sender_id")

                self.entry_chat_id.delete(0, tk.END)
                self.entry_chat_id.insert(0, cid)
                self.entry_group_name.delete(0, tk.END)
                self.entry_group_name.insert(0, title)
                if cmd_id and hasattr(self, "entry_auth_user"):
                    self.entry_auth_user.delete(0, tk.END)
                    self.entry_auth_user.insert(0, str(cmd_id))

                if hasattr(self, "lbl_broker_status"):
                    cmd_txt = f" ｜ 指挥官 ID: {cmd_id}" if cmd_id else ""
                    self.lbl_broker_status.config(text=f"🟢 战队群已连接：{title} (ID: {cid}){cmd_txt}", fg="#10b981")

                # Persist to .env and os.environ
                try:
                    env_file = REPO_ROOT / ".env"
                    save_token_to_env(cid, env_path=env_file, var_name="TELEGRAM_GROUP_ID")
                    os.environ["TELEGRAM_GROUP_ID"] = cid
                    if cmd_id:
                        save_token_to_env(str(cmd_id), env_path=env_file, var_name="POCKETFLEET_AUTHORIZED_USER_IDS")
                        os.environ["POCKETFLEET_AUTHORIZED_USER_IDS"] = str(cmd_id)
                except Exception:
                    pass

                if self.on_success_callback:
                    try:
                        self.on_success_callback(
                            env_var=self.bot_token_env,
                            username=self.bot_username,
                            chat_id=cid,
                            group_name=title,
                            token=self._get_active_token(),
                        )
                    except TypeError:
                        self.on_success_callback(
                            self.bot_token_env,
                            self.bot_username,
                            cid,
                            title,
                            self._get_active_token(),
                        )

                cmd_info = f"\n授权指挥官 ID: {cmd_id}" if cmd_id else ""
                messagebox.showinfo(
                    "🎉 绑定成功！",
                    f"🎉 战队群组绑定成功！\n\n"
                    f"群组名称: {title}\n"
                    f"群 Chat ID: {cid}{cmd_info}\n\n"
                    f"PocketFleet 通信链路已正式连接！",
                    parent=self,
                )
                self.is_listening = False
                return

            elif ev_type == "CONFLICT_409":
                if hasattr(self, "lbl_broker_status"):
                    self.lbl_broker_status.config(text="❌ HTTP 409 Conflict: Bot Token 被外部占用", fg="#ef4444")
                messagebox.showerror(
                    "HTTP 409 冲突",
                    "【外部占用拦截】HTTP 409 Conflict\n\n"
                    "检测到该 Bot Token 正在被另一个外部程序（或未关闭的旧脚本）占用！\n"
                    "根据防御纪律，PocketFleet 严禁强行接管或破坏外部进程，已主动退出监听。\n\n"
                    "请排查并关闭其他使用该 Token 的进程后重试。",
                    parent=self,
                )
                self.is_listening = False
                return

            elif ev_type == "BIND_FAILED":
                err_msg = payload.get("error", "绑定失败")
                if hasattr(self, "lbl_broker_status"):
                    self.lbl_broker_status.config(text=f"⚠️ {err_msg}", fg="#ef4444")
                messagebox.showwarning(
                    "入群绑定失败",
                    f"【绑定被拦截】\n\n{err_msg}",
                    parent=self,
                )

        self.after(500, self._poll_broker_events)

    def _test_follower_checkin(self):
        token = self._get_active_token()
        if not token:
            messagebox.showerror("缺少 Token", "请先填入当前席位的 Bot Token！", parent=self)
            return

        ok, bot_id, uname, err = verify_bot_token(token)
        if not ok or not bot_id:
            messagebox.showerror("Token 校验失败", f"Bot Token 校验失败: {err}", parent=self)
            return

        in_chat, status_msg = verify_chat_member(token, self.global_chat_id, bot_id)
        if not in_chat:
            messagebox.showerror(
                "Bot 尚未入群",
                f"【尚未入群】\n{status_msg}\n\n👉 请在 Telegram 客户端中将 @{uname} 邀请加入战队群后再点击测试！",
                parent=self,
            )
            return

        sent_ok, send_msg = send_bot_checkin(
            bot_token=token,
            chat_id=self.global_chat_id,
            bot_name=self.seat_name,
            role_title=self.role_title,
            engine_name=self.engine_name,
        )
        if not sent_ok:
            messagebox.showerror("发送测试消息失败", f"发送失败: {send_msg}", parent=self)
            return

        if self.on_success_callback:
            self.on_success_callback(
                env_var=self.bot_token_env,
                username="@" + uname.lstrip("@"),
                chat_id=self.global_chat_id,
                group_name=self.global_group_name,
                token=token,
            )

        messagebox.showinfo(
            "测试成功",
            f"✅ 席位 [{self.seat_name}] 入群验证通过！\n\n"
            f"Bot @{uname} 已成功在群组中发送通信连接测试消息。",
            parent=self,
        )
        self.destroy()

    def _manual_save_action(self):
        token = self._get_active_token()
        cid = self.entry_chat_id.get().strip()
        gname = self.entry_group_name.get().strip() or "战队群"
        if not cid:
            messagebox.showerror("缺少群 ID", "请输入群 Chat ID（如 -1004309197838）", parent=self)
            return

        try:
            env_file = REPO_ROOT / ".env"
            if token:
                save_token_to_env(token, env_path=env_file, var_name=self.bot_token_env)
                os.environ[self.bot_token_env] = token
            save_token_to_env(cid, env_path=env_file, var_name="TELEGRAM_GROUP_ID")
            os.environ["TELEGRAM_GROUP_ID"] = cid
        except Exception as e:
            messagebox.showerror("保存异常", f"无法保存配置: {e}", parent=self)
            return

        if self.on_success_callback:
            self.on_success_callback(
                env_var=self.bot_token_env,
                username=self.bot_username,
                chat_id=cid,
                group_name=gname,
                token=token,
            )

        messagebox.showinfo("保存成功", f"✅ 手动配置已保存！\n\n群 Chat ID: {cid}\n群组名称: {gname}", parent=self)
        self.destroy()

    def _on_close(self):
        self.is_listening = False
        if self.broker:
            self.broker.stop_temporary_poller()
        self.destroy()


CODE_AI_BUTTONS = [
    {"key": "codex", "label": "⚡ OpenAI Codex", "bg": "#10b981", "hint": "【OpenAI Codex】\n自动化终端编码代理，擅长精确单任务施工与脚本生成。"},
    {"key": "antigravity", "label": "🪐 Antigravity", "bg": "#0284c7", "hint": "【Google Antigravity】\n全尺寸 IDE 与 CLI 双模代理，支持会话轨道挂载与会话回放。"},
    {"key": "claude_code", "label": "🧠 Claude Code", "bg": "#d97706", "hint": "【Anthropic Claude Code】\n深度逻辑推演与高阶代码重构代理。"},
    {"key": "aider", "label": "🛠️ Aider", "bg": "#8b5cf6", "hint": "【Aider CLI】\n经典 Git 伴侣式终端多文件编辑代理。"},
    {"key": "copilot", "label": "🐙 GitHub Copilot", "bg": "#6366f1", "hint": "【GitHub Copilot CLI】\nGitHub 官方终端命令行伴随式智能体。"},
]

CHAT_AI_BUTTONS = [
    {"key": "gemini", "label": "✨ Google Gemini", "bg": "#38bdf8", "hint": "【Google Gemini】\n长上下文超大窗口对话模型，适合宏观推演与知识库对账。"},
    {"key": "chatgpt", "label": "🤖 OpenAI ChatGPT", "bg": "#10b981", "hint": "【OpenAI ChatGPT】\n通用全能对话助手，适合人机协作日常答疑与指令转译。"},
    {"key": "claude", "label": "🔮 Anthropic Claude", "bg": "#d97706", "hint": "【Anthropic Claude】\n严谨细致的长文本推理对话模型，具备极高宪法安全度。"},
]

ALL_ENGINE_MAP: dict[str, dict] = {
    item["key"].lower(): item for item in (CODE_AI_BUTTONS + CHAT_AI_BUTTONS)
}
ALL_ENGINE_MAP["claude-code"] = ALL_ENGINE_MAP["claude_code"]
ALL_ENGINE_MAP["claudecode"] = ALL_ENGINE_MAP["claude_code"]
ALL_ENGINE_MAP["github_copilot"] = ALL_ENGINE_MAP["copilot"]
ALL_ENGINE_MAP["github-copilot"] = ALL_ENGINE_MAP["copilot"]
ALL_ENGINE_MAP["openai_codex"] = ALL_ENGINE_MAP["codex"]
ALL_ENGINE_MAP["openai-codex"] = ALL_ENGINE_MAP["codex"]
ALL_ENGINE_MAP["google_gemini"] = ALL_ENGINE_MAP["gemini"]
ALL_ENGINE_MAP["google-gemini"] = ALL_ENGINE_MAP["gemini"]
ALL_ENGINE_MAP["openai_chatgpt"] = ALL_ENGINE_MAP["chatgpt"]
ALL_ENGINE_MAP["anthropic_claude"] = ALL_ENGINE_MAP["claude"]


class EngineBadge:
    """Widget adapter representing a seat's execution engine as an interactive styled button.

    Provides .get() and .set(val) interface matching ttk.Combobox / StringVar for full
    backward compatibility with tests and controller logic.
    """

    def __init__(self, button: tk.Button, tooltip_binder=None):
        self.button = button
        self.tooltip_binder = tooltip_binder
        self._val = ""

    def get(self) -> str:
        return self._val

    def set(self, val: str):
        self._val = (val or "").strip()
        self.update_ui()

    def update_ui(self):
        val = self._val.lower()
        engine_info = ALL_ENGINE_MAP.get(val)
        if engine_info:
            self.button.config(
                text=engine_info["label"],
                bg=engine_info["bg"],
                fg="#ffffff",
                activebackground=engine_info["bg"],
                activeforeground="#ffffff",
            )
            if self.tooltip_binder:
                self.tooltip_binder(
                    self.button,
                    f"【{engine_info['label']}】\n点击此按钮清除席位执行引擎，设为【未设置】",
                )
        elif self._val:
            self.button.config(
                text=f"⚙️ {self._val}",
                bg="#475569",
                fg="#ffffff",
                activebackground="#64748b",
                activeforeground="#ffffff",
            )
            if self.tooltip_binder:
                self.tooltip_binder(
                    self.button,
                    f"【{self._val}】\n点击此按钮清除席位执行引擎，设为【未设置】",
                )
        else:
            self.button.config(
                text="⚪ 未设置 (点击上方按钮指定)",
                bg="#334155",
                fg="#94a3b8",
                activebackground="#475569",
                activeforeground="#f8fafc",
            )
            if self.tooltip_binder:
                self.tooltip_binder(
                    self.button,
                    "当前席位未设置执行引擎。\n请选中本席位后，点击上方按钮一键指定。",
                )

    def __getattr__(self, name):
        return getattr(self.button, name)



class ThreeSeatsConfigDialog(tk.Toplevel):
    def __init__(self, parent, fleet_mgr, on_save_callback=None):
        super().__init__(parent)
        self.parent = parent
        self.mgr = fleet_mgr
        self.on_save_callback = on_save_callback

        self.title("Fleet Triad Seats Configuration (席位战队编排) — PocketFleet")
        self.geometry("860x740")
        self.resizable(False, False)
        self.configure(bg="#0b0f19")
        self.transient(parent)
        self.grab_set()

        self.font_title = tkfont.Font(family="Segoe UI", size=13, weight="bold")
        self.font_sub = tkfont.Font(family="Segoe UI", size=9)
        self.font_bold = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        self.font_mono = tkfont.Font(family="Consolas", size=9)

        # Center on parent
        self.update_idletasks()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        cx = max(0, px + (pw - 860) // 2)
        cy = max(0, py + (ph - 740) // 2)
        self.geometry(f"+{cx}+{cy}")

        self.active_tab: str = "code"
        self.selected_role: str = "lead"
        self.global_chat_id: str = ""
        self.global_group_name: str = ""
        self.cards: dict[str, tk.LabelFrame] = {}
        self.card_titles: dict[str, str] = {}
        self.widgets: dict[str, dict] = {}
        self._drawer_brokers: dict[str, Any] = {}
        self._drawer_last_event_ids: dict[str, int] = {}
        self._tooltip_win: tk.Toplevel | None = None

        self._build_ui()
        self._load_values()
        self._switch_tab("code")
        self._select_seat("lead")

    def _build_ui(self):
        # Header banner
        header = tk.Frame(self, bg="#0f172a", padx=20, pady=12)
        header.pack(fill=tk.X)
        tk.Label(
            header,
            text="👥 Fleet Triad Seats Configuration (席位战队编排)",
            fg="#38bdf8",
            bg="#0f172a",
            font=self.font_title,
        ).pack(anchor="w")
        tk.Label(
            header,
            text="二元页签架构：左侧【代码 AI】(多席位施工) ｜ 右侧【对话 AI】(人类交互与对账) ｜ 战队群全席位共用",
            fg="#94a3b8",
            bg="#0f172a",
            font=self.font_sub,
        ).pack(anchor="w", pady=(2, 0))

        content = tk.Frame(self, bg="#0b0f19", padx=20, pady=10)
        content.pack(fill=tk.BOTH, expand=True)

        # Tab Switcher (Segmented Buttons)
        tab_bar = tk.Frame(content, bg="#0b0f19")
        tab_bar.pack(fill=tk.X, pady=(0, 10))

        self.btn_tab_code = tk.Button(
            tab_bar,
            text="💻 代码 AI (Code AI — 2席)",
            font=self.font_bold,
            relief=tk.FLAT,
            padx=18,
            pady=6,
            cursor="hand2",
            bg="#0284c7",
            fg="#ffffff",
            activebackground="#0369a1",
            activeforeground="#ffffff",
            command=lambda: self._switch_tab("code"),
        )
        self.btn_tab_code.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_tab_chat = tk.Button(
            tab_bar,
            text="💬 对话 AI (Chat AI — 1席)",
            font=self.font_bold,
            relief=tk.FLAT,
            padx=18,
            pady=6,
            cursor="hand2",
            bg="#1e293b",
            fg="#94a3b8",
            activebackground="#334155",
            activeforeground="#ffffff",
            command=lambda: self._switch_tab("chat"),
        )
        self.btn_tab_chat.pack(side=tk.LEFT)

        # ======================================================================
        # Tab 1: 代码 AI (Code AI)
        # ======================================================================
        self.tab_frame_code = tk.Frame(content, bg="#0b0f19")

        # Top Engine Quick Bar (Row of 5 mainstream overseas Code AIs)
        engine_bar_code = tk.Frame(self.tab_frame_code, bg="#1e293b", padx=12, pady=8)
        engine_bar_code.pack(fill=tk.X, pady=(0, 10))

        tk.Label(
            engine_bar_code,
            text="⚡ 快速指定代码执行引擎 (选中下方任一席位后，点击按钮一键替换):",
            fg="#94a3b8",
            bg="#1e293b",
            font=self.font_bold,
        ).pack(anchor="w", pady=(0, 6))

        btn_row_code = tk.Frame(engine_bar_code, bg="#1e293b")
        btn_row_code.pack(fill=tk.X)

        for eng_item in CODE_AI_BUTTONS:
            btn = tk.Button(
                btn_row_code,
                text=eng_item["label"],
                bg=eng_item["bg"],
                fg="#ffffff",
                activebackground=eng_item["bg"],
                activeforeground="#ffffff",
                font=self.font_bold,
                relief=tk.FLAT,
                padx=10,
                pady=3,
                cursor="hand2",
                command=lambda k=eng_item["key"]: self._apply_code_engine(k),
            )
            btn.pack(side=tk.LEFT, padx=(0, 8))
            self._bind_btn_tooltip(btn, eng_item["hint"])

        # Code Seat 1: 规划席 (CTO) & Code Seat 2: 执行席 (主力)
        self._create_seat_card(
            self.tab_frame_code,
            role_key="lead",
            title="🎖️ 席位 1: 规划席·CTO (架构守门与改卷验收)",
            color="#10b981",
            engine_choices=list(ALLOWED_CODE_ENGINES),
        )
        self._create_seat_card(
            self.tab_frame_code,
            role_key="builder",
            title="🛠️ 席位 2: 执行席·主力 (核心施工与算法定桩)",
            color="#f59e0b",
            engine_choices=list(ALLOWED_CODE_ENGINES),
        )

        # ======================================================================
        # Tab 2: 对话 AI (Chat AI)
        # ======================================================================
        self.tab_frame_chat = tk.Frame(content, bg="#0b0f19")

        # Top Engine Quick Bar (Row of mainstream Chat AIs)
        engine_bar_chat = tk.Frame(self.tab_frame_chat, bg="#1e293b", padx=12, pady=8)
        engine_bar_chat.pack(fill=tk.X, pady=(0, 10))

        tk.Label(
            engine_bar_chat,
            text="✨ 快速指定对话执行引擎 (点击按钮一键替换人类交互席引擎):",
            fg="#94a3b8",
            bg="#1e293b",
            font=self.font_bold,
        ).pack(anchor="w", pady=(0, 6))

        btn_row_chat = tk.Frame(engine_bar_chat, bg="#1e293b")
        btn_row_chat.pack(fill=tk.X)

        for eng_item in CHAT_AI_BUTTONS:
            btn = tk.Button(
                btn_row_chat,
                text=eng_item["label"],
                bg=eng_item["bg"],
                fg="#ffffff",
                activebackground=eng_item["bg"],
                activeforeground="#ffffff",
                font=self.font_bold,
                relief=tk.FLAT,
                padx=12,
                pady=3,
                cursor="hand2",
                command=lambda k=eng_item["key"]: self._apply_chat_engine(k),
            )
            btn.pack(side=tk.LEFT, padx=(0, 10))
            self._bind_btn_tooltip(btn, eng_item["hint"])

        # Chat Seat 1: 人类交互席
        self._create_seat_card(
            self.tab_frame_chat,
            role_key="chat",
            title="💬 席位 1: 人类交互席 (推演与宏观对账 - 非必选)",
            color="#38bdf8",
            engine_choices=list(ALLOWED_CHAT_ENGINES),
        )

        # Initially pack code tab
        self.tab_frame_code.pack(fill=tk.BOTH, expand=True)

        # ======================================================================
        # Shared Bottom Area (Collaborative group, context window, actions)
        # ======================================================================
        # Global Telegram Collaborative Group Banner
        self.lbl_global_group = tk.Label(
            content,
            text="📢 Telegram 协同战队群: 尚未配置 (点击任一席位的 TG 状态按钮进行配置/打卡)",
            fg="#94a3b8",
            bg="#0b0f19",
            font=self.font_sub,
            anchor="w",
        )
        self.lbl_global_group.pack(fill=tk.X, pady=(4, 2))

        # Extra options (Context window synchronization)
        opt_frame = tk.Frame(content, bg="#0b0f19")
        opt_frame.pack(fill=tk.X, pady=(6, 0))

        self.var_sync_context = tk.BooleanVar(value=True)
        self.cb_sync_context = tk.Checkbutton(
            opt_frame,
            text="对被 @ 的席位同步最近对话历史 (Context Window):",
            variable=self.var_sync_context,
            bg="#0b0f19",
            fg="#38bdf8",
            selectcolor="#1e293b",
            activebackground="#0b0f19",
            activeforeground="#38bdf8",
            font=self.font_sub,
            command=self._on_sync_context_toggle,
        )
        self.cb_sync_context.pack(side=tk.LEFT)

        self.entry_cw = tk.Entry(opt_frame, bg="#1e293b", fg="#f8fafc", font=self.font_mono, width=5, relief=tk.FLAT, bd=4)
        self.entry_cw.insert(0, "20")
        self.entry_cw.pack(side=tk.LEFT, padx=(4, 6))

        tk.Label(opt_frame, text="条", fg="#94a3b8", bg="#0b0f19", font=self.font_sub).pack(side=tk.LEFT)

        # Internal flag kept for backward compatibility
        self.var_nositu = tk.BooleanVar(value=True)

        # Bottom Actions
        actions = tk.Frame(self, bg="#0f172a", padx=20, pady=12)
        actions.pack(fill=tk.X, side=tk.BOTTOM)

        btn_save = tk.Button(
            actions,
            text="💾 保存配置 (Save)",
            bg="#10b981",
            fg="#ffffff",
            activebackground="#059669",
            activeforeground="#ffffff",
            font=self.font_bold,
            relief=tk.FLAT,
            padx=16,
            pady=6,
            cursor="hand2",
            command=self._save_seats,
        )
        btn_save.pack(side=tk.RIGHT, padx=(8, 0))

        btn_reset = tk.Button(
            actions,
            text="🔄 恢复默认编组 (Reset)",
            bg="#334155",
            fg="#cbd5e1",
            activebackground="#475569",
            font=self.font_sub,
            relief=tk.FLAT,
            padx=12,
            pady=6,
            cursor="hand2",
            command=self._reset_defaults,
        )
        btn_reset.pack(side=tk.RIGHT, padx=(8, 0))

        btn_cancel = tk.Button(
            actions,
            text="取消 (Cancel)",
            bg="#1e293b",
            fg="#94a3b8",
            activebackground="#334155",
            font=self.font_sub,
            relief=tk.FLAT,
            padx=12,
            pady=6,
            cursor="hand2",
            command=self.destroy,
        )
        btn_cancel.pack(side=tk.RIGHT)

    def _create_seat_card(self, parent: tk.Widget, role_key: str, title: str, color: str, engine_choices: list[str]):
        card = tk.LabelFrame(
            parent,
            text=f" {title} ",
            fg=color,
            bg="#1e293b",
            font=self.font_bold,
            padx=12,
            pady=8,
            relief=tk.SOLID,
            bd=1,
            highlightthickness=1,
            highlightbackground="#334155",
            highlightcolor="#334155",
        )
        card.pack(fill=tk.X, pady=(0, 8))
        self.cards[role_key] = card
        self.card_titles[role_key] = title

        # Card click selection binding
        card.bind("<Button-1>", lambda e, rk=role_key: self._select_seat(rk))

        # Card body with two columns: left for seat properties, right for TG in-card drawer
        card_body = tk.Frame(card, bg="#1e293b")
        card_body.pack(fill=tk.BOTH, expand=True)
        card_body.bind("<Button-1>", lambda e, rk=role_key: self._select_seat(rk))

        card_left = tk.Frame(card_body, bg="#1e293b")
        card_left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        card_left.bind("<Button-1>", lambda e, rk=role_key: self._select_seat(rk))

        card_right = tk.Frame(card_body, bg="#1e293b")
        card_right.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))

        # Row 1: Engine, Track, and Bot Identity Badge (in card_left)
        r1 = tk.Frame(card_left, bg="#1e293b")
        r1.pack(fill=tk.X, pady=2)
        r1.bind("<Button-1>", lambda e, rk=role_key: self._select_seat(rk))

        lbl_eng = tk.Label(r1, text="执行引擎:", fg="#f1f5f9", bg="#1e293b", font=self.font_sub, width=10, anchor="w")
        lbl_eng.pack(side=tk.LEFT)
        lbl_eng.bind("<Button-1>", lambda e, rk=role_key: self._select_seat(rk))

        btn_engine = tk.Button(
            r1,
            text="⚪ 未设置 (点击上方按钮指定)",
            bg="#334155",
            fg="#94a3b8",
            activebackground="#475569",
            activeforeground="#f8fafc",
            font=self.font_bold,
            relief=tk.FLAT,
            padx=10,
            pady=2,
            cursor="hand2",
            command=lambda rk=role_key: self._on_engine_badge_click(rk),
        )
        btn_engine.pack(side=tk.LEFT)

        engine_adapter = EngineBadge(
            button=btn_engine,
            tooltip_binder=self._bind_btn_tooltip,
        )

        # Dynamic Antigravity Track Config button
        btn_track = tk.Button(
            r1,
            text="🧭 轨道配置",
            bg="#0d9488",
            fg="#ffffff",
            activebackground="#0f766e",
            activeforeground="#ffffff",
            font=self.font_sub,
            relief=tk.FLAT,
            padx=8,
            pady=1,
            cursor="hand2",
            command=self._open_antigravity_tracks,
        )

        # Chat AI verification & launch button
        btn_chat_check = None
        if role_key == "chat":
            btn_chat_check = tk.Button(
                r1,
                text="🌐 启动/自检",
                bg="#0284c7",
                fg="#ffffff",
                activebackground="#0369a1",
                activeforeground="#ffffff",
                font=self.font_sub,
                relief=tk.FLAT,
                padx=8,
                pady=1,
                cursor="hand2",
                command=self._verify_chat_ai_bridge,
            )
            btn_chat_check.pack(side=tk.LEFT, padx=(8, 0))

        # Bot identity badge (auto-populated from getMe or seat config)
        lbl_bot_badge = tk.Label(
            r1,
            text="",
            fg="#38bdf8",
            bg="#1e293b",
            font=self.font_bold,
        )
        lbl_bot_badge.pack(side=tk.LEFT, padx=(12, 0))
        lbl_bot_badge.bind("<Button-1>", lambda e, rk=role_key: self._select_seat(rk))

        # Headless entry_name (maintained for tests & internal state, not packed per Commander directive)
        entry_name = tk.Entry(card_left)

        # Row 2 (old TG协同 row: cut from visual UI, kept headless for test compatibility)
        r2 = tk.Frame(card_left)
        lbl_tg = tk.Label(r2)
        lbl_env_tag = tk.Label(r2)
        entry_env = tk.Entry(r2)
        entry_token = tk.Entry(r2, show="*")
        lbl_hint = tk.Label(r2)
        entry_user = tk.Entry(r2)

        # Row 2 (visually Row 2 in card_left): Role Assignment Display
        r3 = tk.Frame(card_left, bg="#1e293b")
        r3.pack(fill=tk.X, pady=4)
        r3.bind("<Button-1>", lambda e, rk=role_key: self._select_seat(rk))

        lbl_role_tag = tk.Label(r3, text="席位职能:", fg="#f1f5f9", bg="#1e293b", font=self.font_sub, width=10, anchor="w")
        lbl_role_tag.pack(side=tk.LEFT)
        lbl_role_tag.bind("<Button-1>", lambda e, rk=role_key: self._select_seat(rk))

        btn_role_badge = tk.Button(
            r3,
            text="【职能配置】",
            bg="#334155",
            fg="#f8fafc",
            activebackground="#475569",
            activeforeground="#ffffff",
            font=self.font_bold,
            relief=tk.FLAT,
            padx=12,
            pady=2,
            cursor="hand2",
            command=lambda rk=role_key: self._select_seat(rk),
        )
        btn_role_badge.pack(side=tk.LEFT, padx=(0, 8))

        entry_custom = tk.Entry(
            r3,
            bg="#0f172a",
            fg="#cbd5e1",
            insertbackground="#cbd5e1",
            font=self.font_sub,
            relief=tk.FLAT,
            bd=4,
        )
        entry_custom.bind("<FocusIn>", lambda e, rk=role_key: self._select_seat(rk))

        cmd_var = tk.StringVar(value="")
        desc_var = tk.StringVar(value="")

        # ======================================================================
        # In-Card Telegram Collapsible Drawer (in card_right)
        # ======================================================================
        # State A: Collapsed (compact button + status label)
        drawer_collapsed = tk.Frame(card_right, bg="#1e293b")
        drawer_collapsed.pack(fill=tk.BOTH, expand=True)

        btn_tg_config = tk.Button(
            drawer_collapsed,
            text="✈️ TG设置",
            bg="#0284c7",
            fg="#ffffff",
            activebackground="#0369a1",
            activeforeground="#ffffff",
            font=self.font_bold,
            relief=tk.FLAT,
            padx=14,
            pady=6,
            cursor="hand2",
            command=lambda rk=role_key: self._expand_tg_drawer(rk),
        )
        btn_tg_config.pack(fill=tk.X, expand=True)
        self._bind_btn_tooltip(btn_tg_config, "点击向左展开本席位 Telegram Bot Token 配置与战队群加入面板")

        lbl_tg_status = tk.Label(
            drawer_collapsed,
            text="⚪ 待配置",
            fg="#94a3b8",
            bg="#1e293b",
            font=self.font_sub,
            cursor="hand2",
        )
        lbl_tg_status.pack(anchor="center", pady=(4, 0))
        lbl_tg_status.bind("<Button-1>", lambda e, rk=role_key: self._expand_tg_drawer(rk))
        self._bind_btn_tooltip(lbl_tg_status, "点击展开右侧 Telegram Bot Token 与战队群快捷设置")

        # State B: Expanded (in-card quick binding panel, +100% width)
        drawer_expanded = tk.Frame(card_right, bg="#0f172a", bd=1, relief=tk.SOLID, padx=12, pady=6)
        # Initially hidden (pack_forget)

        # Drawer Row 1: Header
        dr_head = tk.Frame(drawer_expanded, bg="#0f172a")
        dr_head.pack(fill=tk.X, pady=(0, 4))
        lbl_dr_title = tk.Label(dr_head, text="✈️ TG设置", fg="#38bdf8", bg="#0f172a", font=self.font_bold)
        lbl_dr_title.pack(side=tk.LEFT)
        btn_dr_close = tk.Button(
            dr_head,
            text="✖ 收起",
            bg="#1e293b",
            fg="#94a3b8",
            activebackground="#334155",
            activeforeground="#f8fafc",
            font=self.font_sub,
            relief=tk.FLAT,
            padx=8,
            pady=1,
            cursor="hand2",
            command=lambda rk=role_key: self._collapse_tg_drawer(rk),
        )
        btn_dr_close.pack(side=tk.RIGHT)

        # Drawer Row 2: Token Input with Eye Toggle (+100% width: width=38)
        dr_tok_row = tk.Frame(drawer_expanded, bg="#0f172a")
        dr_tok_row.pack(fill=tk.X, pady=(0, 4))
        lbl_dr_tok = tk.Label(dr_tok_row, text="Token:", fg="#cbd5e1", bg="#0f172a", font=self.font_sub)
        lbl_dr_tok.pack(side=tk.LEFT, padx=(0, 4))
        entry_dr_tok = tk.Entry(
            dr_tok_row,
            bg="#1e293b",
            fg="#f8fafc",
            insertbackground="#f8fafc",
            font=self.font_mono,
            width=38,
            relief=tk.FLAT,
            bd=3,
            show="*",
        )
        entry_dr_tok.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        btn_dr_eye = tk.Button(
            dr_tok_row,
            text="👁️",
            bg="#334155",
            fg="#f8fafc",
            activebackground="#475569",
            font=self.font_sub,
            relief=tk.FLAT,
            padx=6,
            pady=1,
            cursor="hand2",
            command=lambda rk=role_key: self._toggle_drawer_token_eye(rk),
        )
        btn_dr_eye.pack(side=tk.LEFT)
        self._bind_btn_tooltip(btn_dr_eye, "切换显示/隐藏明文 Token (好用第一)")

        # Drawer Row 3: Action Button + Status
        dr_act_row = tk.Frame(drawer_expanded, bg="#0f172a")
        dr_act_row.pack(fill=tk.X)
        btn_dr_join = tk.Button(
            dr_act_row,
            text="🚀 加入TG群",
            bg="#10b981",
            fg="#ffffff",
            activebackground="#059669",
            activeforeground="#ffffff",
            font=self.font_bold,
            relief=tk.FLAT,
            padx=12,
            pady=3,
            cursor="hand2",
            command=lambda rk=role_key: self._drawer_join_tg(rk),
        )
        btn_dr_join.pack(side=tk.LEFT, padx=(0, 8))
        lbl_dr_status = tk.Label(dr_act_row, text="⚪ 待配置", fg="#94a3b8", bg="#0f172a", font=self.font_sub)
        lbl_dr_status.pack(side=tk.LEFT)

        self.widgets[role_key] = {
            "name": entry_name,
            "lbl_bot_badge": lbl_bot_badge,
            "engine": engine_adapter,
            "btn_engine": btn_engine,
            "btn_track": btn_track,
            "btn_chat_check": btn_chat_check,
            "btn_tg_config": btn_tg_config,
            "btn_tg_capsule": btn_tg_config,
            "lbl_tg_status": lbl_tg_status,
            "lbl_env_tag": lbl_env_tag,
            "env": entry_env,
            "token": entry_token,
            "hint": lbl_hint,
            "user": entry_user,
            "role_badge": btn_role_badge,
            "custom_entry": entry_custom,
            "cmd_var": cmd_var,
            "desc_var": desc_var,
            "is_custom": False,
            "drawer_collapsed": drawer_collapsed,
            "drawer_expanded": drawer_expanded,
            "drawer_token_entry": entry_dr_tok,
            "drawer_token_eye": btn_dr_eye,
            "drawer_join_btn": btn_dr_join,
            "drawer_status_lbl": lbl_dr_status,
        }

    def _switch_tab(self, tab_name: str):
        self.active_tab = tab_name
        if tab_name == "code":
            self.btn_tab_code.config(bg="#0284c7", fg="#ffffff")
            self.btn_tab_chat.config(bg="#1e293b", fg="#94a3b8")
            self.tab_frame_chat.pack_forget()
            self.tab_frame_code.pack(fill=tk.BOTH, expand=True)
            if self.selected_role not in ("lead", "builder"):
                self._select_seat("lead")
            else:
                self._select_seat(self.selected_role)
        else:
            self.btn_tab_code.config(bg="#1e293b", fg="#94a3b8")
            self.btn_tab_chat.config(bg="#0284c7", fg="#ffffff")
            self.tab_frame_code.pack_forget()
            self.tab_frame_chat.pack(fill=tk.BOTH, expand=True)
            self._select_seat("chat")

    def _apply_code_engine(self, engine_key: str):
        target = self.selected_role
        if target not in ("lead", "builder"):
            target = "lead"
            self._select_seat("lead")
        w = self.widgets.get(target)
        if not w:
            return
        w["engine"].set(engine_key)
        self._on_engine_change(target)

    def _apply_chat_engine(self, engine_key: str):
        w = self.widgets.get("chat")
        if not w:
            return
        w["engine"].set(engine_key)
        self._on_engine_change("chat")
        self._select_seat("chat")

    # --- Tooltip Helper for Presets ---

    def _bind_btn_tooltip(self, widget: tk.Widget, text: str):
        widget.bind("<Enter>", lambda e: self._show_tooltip(e.x_root, e.y_root, text))
        widget.bind("<Leave>", lambda e: self._hide_tooltip())

    def _show_tooltip(self, x: int, y: int, text: str):
        self._hide_tooltip()
        tw = tk.Toplevel(self)
        tw.wm_overrideredirect(True)
        tw.configure(bg="#0f172a", bd=1, relief=tk.SOLID)
        tw.geometry(f"+{x + 12}+{y + 16}")

        frame = tk.Frame(tw, bg="#0f172a", padx=10, pady=8)
        frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(
            frame,
            text=text,
            fg="#38bdf8",
            bg="#0f172a",
            font=self.font_sub,
            justify=tk.LEFT,
            wraplength=340,
        ).pack()
        self._tooltip_win = tw

    def _hide_tooltip(self):
        if self._tooltip_win:
            try:
                self._tooltip_win.destroy()
            except Exception:
                pass
            self._tooltip_win = None

    # --- Seat Selection & Preset Assignment with Mutual Exclusion ---

    def _select_seat(self, role_key: str):
        self.selected_role = role_key
        for rk, card in self.cards.items():
            base_title = self.card_titles[rk]
            if rk == role_key:
                card.config(
                    highlightbackground="#38bdf8",
                    highlightcolor="#38bdf8",
                    highlightthickness=2,
                    text=f" {base_title} [⭐ 当前选中编排] ",
                )
            else:
                card.config(
                    highlightbackground="#334155",
                    highlightcolor="#334155",
                    highlightthickness=1,
                    text=f" {base_title} ",
                )

    def _apply_preset_to_selected(self, preset: dict):
        if not self.selected_role or self.selected_role not in self.widgets:
            return
        target_role = self.selected_role
        preset_key = preset["key"]

        if preset_key == "custom":
            # Custom is non-exclusive
            w = self.widgets[target_role]
            w["is_custom"] = True
            w["role_badge"].config(text="【✏️ 自定义】", bg="#475569", fg="#ffffff")
            w["custom_entry"].pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
            w["custom_entry"].focus_set()
        else:
            # Presets 1-3 are strictly mutually exclusive: if another seat already holds this preset, downgrade it to custom
            for other_role, other_w in self.widgets.items():
                if other_role != target_role:
                    if not other_w["is_custom"] and other_w["desc_var"].get() == preset["desc_text"]:
                        other_w["is_custom"] = True
                        other_w["desc_var"].set("")
                        other_w["role_badge"].config(text="【✏️ 自定义】", bg="#475569", fg="#ffffff")
                        other_w["custom_entry"].delete(0, tk.END)
                        other_w["custom_entry"].pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

            w = self.widgets[target_role]
            w["is_custom"] = False
            w["desc_var"].set(preset["desc_text"])
            w["role_badge"].config(text=f"【{preset['btn_label']}】", bg=preset["bg"], fg=preset["fg"])
            w["custom_entry"].pack_forget()

    def _on_engine_badge_click(self, role_key: str):
        w = self.widgets.get(role_key)
        if not w:
            return
        current_eng = w["engine"].get().strip()
        if current_eng:
            # Click on assigned engine badge clears it back to unset
            w["engine"].set("")
            self._on_engine_change(role_key)
        else:
            # Click on unset badge selects the seat for quick assignment
            self._select_seat(role_key)

    def _on_engine_change(self, role_key: str):
        w = self.widgets[role_key]
        eng = w["engine"].get().strip().lower()
        cmd = get_default_command_for_engine(eng) if eng else ""
        w["cmd_var"].set(cmd)
        if eng == "antigravity":
            if "lbl_bot_badge" in w and w["lbl_bot_badge"].winfo_exists():
                w["btn_track"].pack(side=tk.LEFT, padx=(8, 0), before=w["lbl_bot_badge"])
            else:
                w["btn_track"].pack(side=tk.LEFT, padx=(8, 0))
        else:
            w["btn_track"].pack_forget()
        self._select_seat(role_key)

    def _on_sync_context_toggle(self):
        if self.var_sync_context.get():
            self.entry_cw.config(state="normal", bg="#1e293b", fg="#f8fafc")
        else:
            self.entry_cw.config(state="disabled", bg="#0f172a", fg="#64748b")

    def _verify_chat_ai_bridge(self):
        is_listening = is_port_listening(8765)
        if is_listening:
            messagebox.showinfo(
                "对话席位自检成功",
                "✅ 对话席位 Web 桥接网关正常运行 (Port 8765 已就绪)！\n\n"
                "• 浏览器插件 / Web 桥接已连接；\n"
                "• 您可在 TG 协同群中发送 '@Bot 你好' 测试端到端连通性。",
                parent=self,
            )
        else:
            messagebox.showwarning(
                "Web 网关未启动",
                "⚠️ 本地 Web 桥接网关 (Port 8765) 尚未启动。\n\n"
                "请在 PocketFleet 主控制面板中点击【🚀 Start All Services】启动服务，"
                "启动后 Web 网关将自动监听。",
                parent=self,
            )

    def _expand_tg_drawer(self, role_key: str):
        for rk, other_w in self.widgets.items():
            if rk != role_key and "drawer_collapsed" in other_w:
                self._collapse_tg_drawer(rk)
        w = self.widgets.get(role_key)
        if not w:
            return
        if "drawer_collapsed" in w and "drawer_expanded" in w:
            w["drawer_collapsed"].pack_forget()
            w["drawer_expanded"].pack(fill=tk.BOTH, expand=True)
            # If entry empty, check env
            env_var = w["env"].get().strip()
            cur_tok = w["drawer_token_entry"].get().strip()
            if not cur_tok:
                tok_from_env = (os.environ.get(env_var) or "").strip()
                if tok_from_env:
                    w["drawer_token_entry"].delete(0, tk.END)
                    w["drawer_token_entry"].insert(0, tok_from_env)
                    w["drawer_status_lbl"].config(text="🟢 Token已就位", fg="#10b981")
            w["drawer_token_entry"].focus_set()
        self._select_seat(role_key)

    def _collapse_tg_drawer(self, role_key: str):
        w = self.widgets.get(role_key)
        if not w:
            return
        if "drawer_expanded" in w and "drawer_collapsed" in w:
            w["drawer_expanded"].pack_forget()
            w["drawer_collapsed"].pack(fill=tk.BOTH, expand=True)

    def _toggle_drawer_token_eye(self, role_key: str):
        w = self.widgets.get(role_key)
        if not w:
            return
        entry = w["drawer_token_entry"]
        btn = w["drawer_token_eye"]
        if entry.cget("show") == "*":
            entry.config(show="")
            btn.config(text="🙈")
        else:
            entry.config(show="*")
            btn.config(text="👁️")

    def _drawer_join_tg(self, role_key: str):
        w = self.widgets.get(role_key)
        if not w:
            return
        token = w["drawer_token_entry"].get().strip()
        env_var = w["env"].get().strip() or f"TELEGRAM_BOT_{role_key.upper()}_TOKEN"
        if not token:
            token = (os.environ.get(env_var) or "").strip()

        if not token:
            w["drawer_status_lbl"].config(text="❌ 请输入Token", fg="#ef4444")
            messagebox.showerror("缺少 Token", "请先填入 Telegram Bot Token！", parent=self)
            return

        w["drawer_status_lbl"].config(text="🔄 验证Token...", fg="#facc15")
        self.update_idletasks()

        ver_res = verify_bot_token(token)
        ok, bot_id, uname, err = ver_res[0], ver_res[1], ver_res[2], ver_res[3]
        if not ok or not bot_id:
            w["drawer_status_lbl"].config(text=f"❌ {err or '无效'}", fg="#ef4444")
            messagebox.showerror("Token 校验失败", f"Bot Token 校验失败: {err}", parent=self)
            return

        clean_uname = (uname or w["user"].get().strip()).lstrip("@")
        bot_username = "@" + clean_uname
        w["user"].delete(0, tk.END)
        w["user"].insert(0, bot_username)

        # Auto-fetch Bot nickname from getMe first_name
        bot_fname = getattr(ver_res, "first_name", None) or ""
        if bot_fname:
            w["name"].delete(0, tk.END)
            w["name"].insert(0, bot_fname)

        # Persist token to .env and os.environ
        try:
            env_file = REPO_ROOT / ".env"
            save_token_to_env(token, env_path=env_file, var_name=env_var)
            os.environ[env_var] = token
        except Exception as e:
            logger.warning("Failed to save token to .env: %s", e)

        w["token"].delete(0, tk.END)
        w["token"].insert(0, token)
        self._update_seat_tg_capsule(role_key)

        # Check if a warroom group is already known across session, env, or disk
        target_chat_id = self.global_chat_id or (os.environ.get("TELEGRAM_GROUP_ID") or "").strip()
        if not target_chat_id and hasattr(self, "mgr"):
            try:
                disk_cfg = self.mgr.load_seats_config()
                target_chat_id = disk_cfg.telegram_chat_id
                if not self.global_group_name:
                    self.global_group_name = disk_cfg.telegram_group_name
            except Exception:
                pass

        if target_chat_id:
            self.global_chat_id = target_chat_id
            in_chat, _ = verify_chat_member(token, target_chat_id, bot_id)
            if in_chat:
                sent_ok, _ = send_bot_checkin(
                    bot_token=token,
                    chat_id=target_chat_id,
                    bot_name=w["name"].get().strip() or role_key,
                    seat_title=w["desc_var"].get().strip(),
                    engine_name=w["engine"].get().strip(),
                )
                if sent_ok:
                    group_disp = self.global_group_name or target_chat_id
                    w["drawer_status_lbl"].config(text="🟢 已连接战队群", fg="#10b981")
                    self._update_seat_tg_capsule(role_key)
                    self._update_global_group_banner()
                    if self.on_save_callback:
                        self.on_save_callback()
                    messagebox.showinfo(
                        "战队群连接成功",
                        f"✅ Bot @{clean_uname} 已在战队群【{group_disp}】中就位！\n\n通信链路已打通，无需重复选群。",
                        parent=self,
                    )
                    return

        auth_ids = []
        fleet_cfg = getattr(self, "fleet_config", None)
        if fleet_cfg and hasattr(fleet_cfg, "authorized_user_ids") and fleet_cfg.authorized_user_ids:
            auth_ids = list(fleet_cfg.authorized_user_ids)
        elif os.environ.get("POCKETFLEET_AUTHORIZED_USER_IDS"):
            raw_ids = os.environ.get("POCKETFLEET_AUTHORIZED_USER_IDS", "")
            for p in raw_ids.split(","):
                if p.strip().isdigit():
                    auth_ids.append(int(p.strip()))

        state_store = StateStore()
        existing_events = state_store.get_broker_events(after_event_id=0, limit=1000)
        last_event_id = max([e.get("event_id", 0) for e in existing_events], default=0)

        broker = TelegramUpdateBroker(
            bot_token=token,
            seat_role=role_key,
            state_store=state_store,
            authorized_user_ids=auth_ids,
            repo_root=REPO_ROOT,
            global_chat_id=self.global_chat_id,
        )
        self._drawer_brokers[role_key] = broker
        self._drawer_last_event_ids[role_key] = last_event_id

        param = broker.create_ephemeral_param(
            bot_id=bot_id,
            bot_username=clean_uname,
            seat_role=role_key,
            authorized_user_id=auth_ids[0] if auth_ids else None,
            ttl_seconds=300,
        )
        deep_link = broker.get_startgroup_deep_link(clean_uname, param)

        is_daemon = self.mgr.is_daemon_running() if hasattr(self.mgr, "is_daemon_running") else False
        if not is_daemon:
            broker.start_temporary_poller()

        w["drawer_status_lbl"].config(text="📡 正在监听加群...", fg="#facc15")
        self.after(500, lambda rk=role_key: self._poll_drawer_broker(rk))

        try:
            open_telegram_group_deep_link(clean_uname, param)
        except Exception as e:
            logger.warning("Failed to open deep link automatically: %s", e)

    def _poll_drawer_broker(self, role_key: str):
        broker = self._drawer_brokers.get(role_key)
        if not broker:
            return
        last_id = self._drawer_last_event_ids.get(role_key, 0)
        events = broker.state_store.get_broker_events(after_event_id=last_id, limit=50)
        for ev in events:
            ev_id = ev.get("event_id", 0)
            if ev_id > last_id:
                last_id = ev_id
            ev_type = ev.get("event_type")
            payload = ev.get("payload", {})
            if ev_type == "CHAT_BOUND":
                chat_id = str(payload.get("chat_id", ""))
                chat_title = payload.get("chat_title", "")
                self.global_chat_id = chat_id
                self.global_group_name = chat_title
                self._update_global_group_banner()
                self._update_seat_tg_capsule(role_key)
                w = self.widgets.get(role_key, {})
                if "drawer_status_lbl" in w:
                    w["drawer_status_lbl"].config(text="🟢 已连接战队群", fg="#10b981")
                broker.stop_temporary_poller()
                if self.on_save_callback:
                    self.on_save_callback()
                return
            elif ev_type == "CONFLICT_409":
                w = self.widgets.get(role_key, {})
                if "drawer_status_lbl" in w:
                    w["drawer_status_lbl"].config(text="⚠️ 端口/Bot冲突(409)", fg="#ef4444")
                broker.stop_temporary_poller()
                return

        self._drawer_last_event_ids[role_key] = last_id
        self.after(500, lambda rk=role_key: self._poll_drawer_broker(rk))

    def _open_seat_telegram_dialog(self, role_key: str):
        """In-card drawer is the primary UX; expand drawer without external popup modal."""
        self._expand_tg_drawer(role_key)

    def destroy(self):
        if hasattr(self, "_drawer_brokers"):
            for b in self._drawer_brokers.values():
                try:
                    b.stop_temporary_poller()
                except Exception:
                    pass
        super().destroy()

    def _update_seat_tg_capsule(self, role_key: str):
        w = self.widgets[role_key]
        env_var = w["env"].get().strip()
        user = w["user"].get().strip()
        token_val = (os.environ.get(env_var) or "").strip()
        if not token_val and "drawer_token_entry" in w:
            token_val = w["drawer_token_entry"].get().strip()

        w["lbl_env_tag"].config(text=f"({env_var})")

        if token_val:
            w["lbl_tg_status"].config(text="🟢 已就位", fg="#10b981")
            if "drawer_status_lbl" in w:
                w["drawer_status_lbl"].config(text="🟢 已就位", fg="#10b981")
            bot_name = w["name"].get().strip()
            clean_u = user if (user.startswith("@") or not user) else f"@{user}"
            if bot_name:
                badge_text = f"🏷️ {bot_name} ({clean_u})" if clean_u else f"🏷️ {bot_name}"
            else:
                badge_text = f"🏷️ {clean_u}" if clean_u else ""
            if "lbl_bot_badge" in w:
                w["lbl_bot_badge"].config(text=badge_text, fg="#38bdf8")
        else:
            w["lbl_tg_status"].config(text="⚪ 待配置", fg="#94a3b8")
            if "drawer_status_lbl" in w:
                w["drawer_status_lbl"].config(text="⚪ 待配置", fg="#94a3b8")
            if "lbl_bot_badge" in w:
                w["lbl_bot_badge"].config(text="")

    def _update_global_group_banner(self):
        if not self.global_chat_id:
            self.global_chat_id = (os.environ.get("TELEGRAM_GROUP_ID") or "").strip()
        if not self.global_group_name and hasattr(self, "mgr"):
            try:
                disk_cfg = self.mgr.load_seats_config()
                if disk_cfg.telegram_chat_id:
                    if not self.global_chat_id:
                        self.global_chat_id = disk_cfg.telegram_chat_id
                    if disk_cfg.telegram_chat_id == self.global_chat_id:
                        self.global_group_name = disk_cfg.telegram_group_name
            except Exception:
                pass

        if self.global_chat_id:
            name_part = f"【{self.global_group_name}】" if self.global_group_name else ""
            self.lbl_global_group.config(
                text=f"📢 Telegram 协同战队群: {name_part} Chat ID: {self.global_chat_id} (全席位共用)",
                fg="#38bdf8",
            )
        else:
            has_token = False
            for r_key, w in self.widgets.items():
                env_var = w["env"].get().strip()
                if (os.environ.get(env_var) or "").strip():
                    has_token = True
                    break
            if has_token:
                self.lbl_global_group.config(
                    text="📢 Telegram 协同战队群: ⚪ 待入群发言自动绑定 (在群内发一条消息或点击席位【TG设置】即可锁定)",
                    fg="#facc15",
                )
            else:
                self.lbl_global_group.config(
                    text="📢 Telegram 协同战队群: 尚未配置 (点击任一席位的 TG 状态按钮进行配置/打卡)",
                    fg="#94a3b8",
                )

    def _open_antigravity_tracks(self):
        AntigravityTracksDialog(
            parent=self,
            fleet_mgr=self.mgr,
            on_bind_callback=self._on_track_bound_callback,
        )

    def _on_track_bound_callback(self):
        self.mgr.log("🧭 [TRACK] Antigravity 轨道已在席位编排中更新。")
        if self.on_save_callback:
            self.on_save_callback()

    def _load_values(self):
        cfg = self.mgr.load_seats_config()
        self._populate_fields(cfg)

    def _populate_fields(self, cfg: FleetSeatsConfig):
        self.entry_cw.delete(0, tk.END)
        self.entry_cw.insert(0, str(cfg.context_window))
        self.var_sync_context.set(getattr(cfg, "sync_context_window", True))
        self._on_sync_context_toggle()

        self.global_chat_id = getattr(cfg, "telegram_chat_id", "") or (os.environ.get("TELEGRAM_GROUP_ID") or "").strip()
        self.global_group_name = getattr(cfg, "telegram_group_name", "")
        self._update_global_group_banner()

        # Track used preset texts to ensure mutual exclusion on load
        used_preset_texts: set[str] = set()

        for role_key, w in self.widgets.items():
            seat = cfg.seats.get(role_key)
            if not seat:
                continue
            w["name"].delete(0, tk.END)
            w["name"].insert(0, seat.name)
            w["engine"].set(seat.engine)
            w["env"].delete(0, tk.END)
            w["env"].insert(0, seat.bot_token_env)
            w["user"].delete(0, tk.END)
            w["user"].insert(0, seat.bot_username)
            w["cmd_var"].set(seat.command or get_default_command_for_engine(seat.engine))

            # Dynamic track config button
            if seat.engine.lower() == "antigravity":
                if "lbl_bot_badge" in w and w["lbl_bot_badge"].winfo_exists():
                    w["btn_track"].pack(side=tk.LEFT, padx=(8, 0), before=w["lbl_bot_badge"])
                else:
                    w["btn_track"].pack(side=tk.LEFT, padx=(8, 0))
            else:
                w["btn_track"].pack_forget()

            # Match description to presets or custom with mutual exclusion
            desc = seat.description.strip()
            matched = False
            for p in ROLE_PRESETS[:3]:
                if (desc == p["desc_text"] or desc == p["btn_label"]) and p["desc_text"] not in used_preset_texts:
                    w["desc_var"].set(p["desc_text"])
                    w["is_custom"] = False
                    w["role_badge"].config(text=f"【{p['btn_label']}】", bg=p["bg"], fg=p["fg"])
                    w["custom_entry"].pack_forget()
                    used_preset_texts.add(p["desc_text"])
                    matched = True
                    break
            if not matched:
                w["desc_var"].set(desc)
                w["is_custom"] = True
                w["role_badge"].config(text="【✏️ 自定义】", bg="#475569", fg="#ffffff")
                w["custom_entry"].delete(0, tk.END)
                w["custom_entry"].insert(0, desc)
                w["custom_entry"].pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

            # Masked token and last 4 characters hint (never full plaintext echo)
            env_val = (os.environ.get(seat.bot_token_env) or "").strip()
            w["token"].delete(0, tk.END)
            if env_val:
                masked = "••••••••" + (env_val[-4:] if len(env_val) >= 4 else env_val)
                w["token"].insert(0, masked)
                hint_str = f"末4位: ...{env_val[-4:]}" if len(env_val) >= 4 else "已设置"
                w["hint"].config(text=hint_str, fg="#10b981")
                if "drawer_token_entry" in w:
                    w["drawer_token_entry"].delete(0, tk.END)
                    w["drawer_token_entry"].insert(0, env_val)
                if "drawer_status_lbl" in w:
                    w["drawer_status_lbl"].config(text="🟢 Token已就位", fg="#10b981")
            else:
                w["hint"].config(text="未配置", fg="#ef4444")
                if "drawer_status_lbl" in w:
                    w["drawer_status_lbl"].config(text="⚪ 待配置", fg="#94a3b8")

            self._update_seat_tg_capsule(role_key)

        self._update_global_group_banner()

    def _reset_defaults(self):
        default_cfg = get_default_seats_config()
        self._populate_fields(default_cfg)

    def _save_seats(self):
        try:
            cw_val = int(self.entry_cw.get().strip() or "20")
            if cw_val <= 0:
                raise ValueError("必须为正整数")
        except ValueError:
            messagebox.showerror("格式错误", "最近消息条数 (context_window) 必须为正整数 (如 20)。", parent=self)
            return

        seats_dict = {}
        for role_key, w in self.widgets.items():
            name = w["name"].get().strip()
            engine = w["engine"].get().strip()
            env_var = w["env"].get().strip()
            user = w["user"].get().strip()

            if not engine:
                messagebox.showerror(
                    "执行引擎未设置",
                    f"席位 '{role_key}' ({name or role_key}) 尚未指定执行引擎！\n\n请点击上方引擎按钮为该席位指定执行引擎后再保存。",
                    parent=self,
                )
                if role_key == "chat":
                    self._switch_tab("chat")
                else:
                    self._switch_tab("code")
                self._select_seat(role_key)
                return

            if w["is_custom"]:
                desc = w["custom_entry"].get().strip()
            else:
                desc = w["desc_var"].get().strip()

            # Automatic engine-to-command binding (cleanly hidden from UI)
            cmd = w["cmd_var"].get().strip() or get_default_command_for_engine(engine)
            token_val = w["token"].get().strip()

            # Plaintext token check (fail loud)
            if ":" in env_var or " " in env_var:
                messagebox.showerror(
                    "安全违规 (Plain Token Detected)",
                    f"【安全违规】席位 '{role_key}' 检测到明文 Token！\n\n"
                    "配置层严禁写入任何明文 Token，只能填写环境变量名称（例如 TELEGRAM_BOT_JUDGE_TOKEN）。\n"
                    "实际 Token 请写入本地 .env 文件。",
                    parent=self,
                )
                w["env"].focus_set()
                return

            if not env_var:
                messagebox.showerror("缺失配置", f"席位 '{role_key}' 必须指定环境变量名 (Token变量)！", parent=self)
                return

            tok_dr = ""
            if "drawer_token_entry" in w:
                tok_dr = w["drawer_token_entry"].get().strip()
            effective_tok = tok_dr or token_val

            # Auto-resolve username if empty but token is provided
            if not user and effective_tok and not effective_tok.startswith("•"):
                ok, _, un, _ = verify_bot_token(effective_tok)
                if ok and un:
                    user = "@" + un.lstrip("@")
                    w["user"].delete(0, tk.END)
                    w["user"].insert(0, user)

            if not user:
                messagebox.showerror("缺失配置", f"席位 '{role_key}' 必须指定 Bot 用户名！", parent=self)
                return

            # If user entered a fresh token (not the masked placeholder), safely write to .env
            if effective_tok and not effective_tok.startswith("•") and not effective_tok.endswith("••••"):
                try:
                    env_file = REPO_ROOT / ".env"
                    save_token_to_env(effective_tok, env_path=env_file, var_name=env_var)
                    os.environ[env_var] = effective_tok
                except Exception as e:
                    messagebox.showerror("写入.env失败", f"无法写入 Token 到 .env: {e}", parent=self)
                    return

            seat_cfg = SeatConfig(
                role=role_key,
                name=name,
                engine=engine,
                bot_token_env=env_var,
                bot_username=user,
                description=desc,
                command=cmd,
                read_watermark=0,
            )
            seats_dict[role_key] = seat_cfg

        new_fleet_cfg = FleetSeatsConfig(
            seats=seats_dict,
            context_window=cw_val,
            no_situ=True,
            telegram_chat_id=self.global_chat_id,
            telegram_group_name=self.global_group_name,
            sync_context_window=self.var_sync_context.get(),
        )

        try:
            validate_seats_config(new_fleet_cfg)
        except ValueError as err:
            messagebox.showerror("校验失败", f"配置校验未通过:\n{err}", parent=self)
            return

        try:
            self.mgr.save_seats_config(new_fleet_cfg)
        except Exception as e:
            messagebox.showerror("保存失败", f"无法写入配置文件:\n{e}", parent=self)
            return

        # Ensure TELEGRAM_GROUP_ID is saved to .env if configured
        if self.global_chat_id:
            try:
                env_file = REPO_ROOT / ".env"
                save_token_to_env(self.global_chat_id, env_path=env_file, var_name="TELEGRAM_GROUP_ID")
                os.environ["TELEGRAM_GROUP_ID"] = self.global_chat_id
            except Exception:
                pass

        self.destroy()
        if self.on_save_callback:
            self.on_save_callback()


# ==============================================================================
# Modern Floating Toast Notification
# ==============================================================================
def show_floating_toast(
    parent: tk.Misc,
    title: str,
    message: str = "",
    icon: str = "🎉",
    duration_ms: int = 2800,
    bg_color: str = "#0f172a",
    border_color: str = "#10b981",
):
    """Display an upward-floating, non-blocking toast notification over the parent window."""
    try:
        root_win = parent.winfo_toplevel()
    except Exception:
        root_win = parent

    toast = tk.Toplevel(root_win)
    toast.wm_overrideredirect(True)
    toast.configure(bg=border_color)
    try:
        toast.attributes("-topmost", True)
    except Exception:
        pass

    inner = tk.Frame(toast, bg=bg_color, padx=18, pady=12)
    inner.pack(padx=2, pady=2, fill=tk.BOTH, expand=True)

    head = tk.Frame(inner, bg=bg_color)
    head.pack(fill=tk.X)

    tk.Label(
        head,
        text=f"{icon}  {title}",
        fg=border_color,
        bg=bg_color,
        font=tkfont.Font(family="Segoe UI", size=11, weight="bold"),
    ).pack(side=tk.LEFT)

    if message:
        tk.Label(
            inner,
            text=message,
            fg="#cbd5e1",
            bg=bg_color,
            font=tkfont.Font(family="Segoe UI", size=9),
            justify=tk.LEFT,
            wraplength=460,
        ).pack(fill=tk.X, pady=(6, 0))

    toast.update_idletasks()
    tw = max(380, toast.winfo_width())
    th = toast.winfo_height()

    try:
        pw = root_win.winfo_width()
        ph = root_win.winfo_height()
        px = root_win.winfo_rootx()
        py = root_win.winfo_rooty()
    except Exception:
        pw, ph, px, py = 860, 600, 200, 200

    target_x = max(0, px + (pw - tw) // 2)
    start_y = py + (ph // 2) + 25
    final_y = py + (ph // 2) - 35

    toast.geometry(f"{tw}x{th}+{target_x}+{start_y}")

    steps = 14
    dy = (final_y - start_y) / steps
    cur_step = [0]
    cur_y = [float(start_y)]

    def _float_step():
        if not toast.winfo_exists():
            return
        if cur_step[0] < steps:
            cur_step[0] += 1
            cur_y[0] += dy
            toast.geometry(f"{tw}x{th}+{target_x}+{int(cur_y[0])}")
            toast.after(16, _float_step)
        else:
            toast.geometry(f"{tw}x{th}+{target_x}+{final_y}")
            toast.after(duration_ms, _fade_out)

    def _fade_out():
        if not toast.winfo_exists():
            return
        alpha = [1.0]

        def _step():
            if not toast.winfo_exists():
                return
            alpha[0] -= 0.12
            if alpha[0] <= 0.05:
                toast.destroy()
            else:
                try:
                    toast.attributes("-alpha", alpha[0])
                    toast.after(20, _step)
                except Exception:
                    toast.destroy()

        _step()

    toast.after(16, _float_step)
    return toast


# ==============================================================================
# Antigravity Tracks Dialog (Dialogue-Centric Track Selector & Smart Binder)
# ==============================================================================
class AntigravityTracksDialog(tk.Toplevel):
    def __init__(self, parent, fleet_mgr, on_bind_callback=None):
        super().__init__(parent)
        self.parent = parent
        self.mgr = fleet_mgr
        self.on_bind_callback = on_bind_callback

        self.title("Antigravity 轨道管理与会话绑定 — PocketFleet")
        self.geometry("960x650")
        self.minsize(860, 560)
        self.configure(bg="#0b0f19")
        self.transient(parent)
        self.grab_set()

        self.font_title = tkfont.Font(family="Segoe UI", size=13, weight="bold")
        self.font_sub = tkfont.Font(family="Segoe UI", size=9)
        self.font_bold = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        self.font_mono = tkfont.Font(family="Consolas", size=9)

        # Center on parent
        self.update_idletasks()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        cx = max(0, px + (pw - 960) // 2)
        cy = max(0, py + (ph - 650) // 2)
        self.geometry(f"+{cx}+{cy}")

        self.controller = AntigravityTrackController(workspace_cwd=REPO_ROOT)
        self.tracks_data: list[TrackCandidate] = []
        self._item_to_candidate: dict[str, TrackCandidate] = {}
        self.show_all_var = tk.BooleanVar(value=False)
        self.is_scanning = False

        # Tooltip state
        self._tooltip_win: tk.Toplevel | None = None
        self._tooltip_timer: str | None = None
        self._hovered_row_id: str | None = None

        self._build_ui()
        self.refresh_tracks()

    def _build_ui(self):
        # Header banner
        header = tk.Frame(self, bg="#0f172a", padx=20, pady=12)
        header.pack(fill=tk.X)
        tk.Label(
            header,
            text="🧭 Antigravity 对话轨发现与一键绑定 (Track Selector)",
            fg="#38bdf8",
            bg="#0f172a",
            font=self.font_title,
        ).pack(anchor="w")

        self.lbl_bound_status = tk.Label(
            header,
            text="当前绑定的对话轨: 正在检查...",
            fg="#f8fafc",
            bg="#0f172a",
            font=self.font_sub,
        )
        self.lbl_bound_status.pack(anchor="w", pady=(3, 0))

        # Compact instruction bar (replaces cumbersome 3-step guide box)
        tip_bar = tk.Frame(self, bg="#1e293b", padx=16, pady=6)
        tip_bar.pack(fill=tk.X, padx=16, pady=(10, 6))
        tk.Label(
            tip_bar,
            text="💡 提示：鼠标悬停在任意行上可预览完整末轮对话。选中目标会话后（包括 IDE 正在进行的对话），直接点击下方【🔗 绑定为对话轨】即可一键秒级绑定！",
            fg="#38bdf8",
            bg="#1e293b",
            font=self.font_sub,
        ).pack(anchor="w")

        # Treeview table Frame
        table_frame = tk.Frame(self, bg="#0b0f19", padx=16, pady=4)
        table_frame.pack(fill=tk.BOTH, expand=True)

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Tracks.Treeview",
            background="#1e293b",
            foreground="#f8fafc",
            fieldbackground="#1e293b",
            rowheight=26,
            font=("Segoe UI", 9),
        )
        style.configure(
            "Tracks.Treeview.Heading",
            background="#334155",
            foreground="#f1f5f9",
            font=("Segoe UI", 9, "bold"),
        )
        style.map(
            "Tracks.Treeview",
            background=[("selected", "#0284c7")],
            foreground=[("selected", "#ffffff")],
        )

        columns = ("status", "snippet", "last_activity", "total_bytes")
        self.tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            style="Tracks.Treeview",
            selectmode="browse",
        )

        self.tree.heading("status", text="状态 / 来源")
        self.tree.heading("snippet", text="最新对话内容 (Last Dialogue Message)")
        self.tree.heading("last_activity", text="最后活动时间 (Activity)")
        self.tree.heading("total_bytes", text="会话容量 (Size)")

        self.tree.column("status", width=140, anchor="center")
        self.tree.column("snippet", width=520, anchor="w")
        self.tree.column("last_activity", width=140, anchor="center")
        self.tree.column("total_bytes", width=90, anchor="e")

        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Hover Tooltip Event Bindings
        self.tree.bind("<Motion>", self._on_tree_motion)
        self.tree.bind("<Leave>", self._hide_tooltip)
        self.tree.bind("<ButtonPress>", self._hide_tooltip)
        self.tree.bind("<Double-1>", lambda e: self._on_bind_click())

        # Bottom toolbar
        bar = tk.Frame(self, bg="#0b0f19", padx=16, pady=12)
        bar.pack(fill=tk.X)

        btn_refresh = tk.Button(
            bar,
            text="🔄 刷新列表",
            bg="#334155",
            fg="#f8fafc",
            activebackground="#475569",
            activeforeground="#ffffff",
            font=self.font_bold,
            relief=tk.FLAT,
            padx=12,
            pady=5,
            cursor="hand2",
            command=self.refresh_tracks,
        )
        btn_refresh.pack(side=tk.LEFT, padx=(0, 8))

        chk_show_all = tk.Checkbutton(
            bar,
            text="显示全部 (<512KB 小轨)",
            variable=self.show_all_var,
            command=self.refresh_tracks,
            bg="#0b0f19",
            fg="#94a3b8",
            activebackground="#0b0f19",
            activeforeground="#f8fafc",
            selectcolor="#1e293b",
            font=self.font_sub,
        )
        chk_show_all.pack(side=tk.LEFT, padx=(0, 20))

        # Unified single smart action button
        self.btn_bind = tk.Button(
            bar,
            text="🔗 绑定为对话轨 (Bind Track)",
            bg="#10b981",
            fg="#ffffff",
            activebackground="#059669",
            activeforeground="#ffffff",
            font=tkfont.Font(family="Segoe UI", size=10, weight="bold"),
            relief=tk.FLAT,
            padx=20,
            pady=5,
            cursor="hand2",
            command=self._on_bind_click,
        )
        self.btn_bind.pack(side=tk.LEFT)

        btn_close = tk.Button(
            bar,
            text="关闭",
            bg="#475569",
            fg="#ffffff",
            activebackground="#64748b",
            activeforeground="#ffffff",
            font=self.font_sub,
            relief=tk.FLAT,
            padx=14,
            pady=5,
            cursor="hand2",
            command=self.destroy,
        )
        btn_close.pack(side=tk.RIGHT)

    # --- Tooltip Management ---

    def _on_tree_motion(self, event):
        row_id = self.tree.identify_row(event.y)
        if row_id != self._hovered_row_id:
            self._hide_tooltip()
            self._hovered_row_id = row_id
            if row_id:
                if self._tooltip_timer:
                    self.after_cancel(self._tooltip_timer)
                # 350ms debounce hover delay
                self._tooltip_timer = self.after(
                    350,
                    lambda: self._show_tooltip(event.x_root, event.y_root, row_id),
                )

    def _show_tooltip(self, x: int, y: int, row_id: str):
        cand = self._item_to_candidate.get(row_id)
        if not cand:
            return

        self._hide_tooltip()

        tw = tk.Toplevel(self)
        tw.wm_overrideredirect(True)
        tw.configure(bg="#0f172a", bd=1, relief=tk.SOLID)
        # Position slightly offset from cursor
        tw.geometry(f"+{x + 18}+{y + 12}")

        frame = tk.Frame(tw, bg="#0f172a", padx=12, pady=10)
        frame.pack(fill=tk.BOTH, expand=True)

        # Header tag
        src_tag = "💬 IDE 对话空间 (可一键直连绑定)" if cand.source == "ide" else "⚡ CLI 对话空间 (可直接绑定)"
        tk.Label(
            frame,
            text=f"📌 {src_tag}",
            fg="#38bdf8",
            bg="#0f172a",
            font=self.font_bold,
            anchor="w",
        ).pack(fill=tk.X, pady=(0, 4))

        # User prompt preview
        from pocketfleet.antigravity_tracks import clean_dialogue_snippet
        clean_user = clean_dialogue_snippet(cand.last_user_prompt, max_chars=320)
        clean_resp = clean_dialogue_snippet(cand.last_model_response, max_chars=220)

        if clean_user:
            tk.Label(
                frame,
                text="👤 用户指令 (已回溯至最新有效发言):",
                fg="#f1f5f9",
                bg="#0f172a",
                font=self.font_bold,
                anchor="w",
            ).pack(fill=tk.X, pady=(2, 0))
            tk.Label(
                frame,
                text=f"“{clean_user}”",
                fg="#7dd3fc",
                bg="#0f172a",
                font=self.font_sub,
                justify=tk.LEFT,
                wraplength=480,
                anchor="w",
            ).pack(fill=tk.X, pady=(1, 6))
        elif clean_resp:
            tk.Label(
                frame,
                text="💡 提示: 此会话未检测到人类独立提问，仅包含助手执行:",
                fg="#fbbf24",
                bg="#0f172a",
                font=self.font_sub,
                anchor="w",
            ).pack(fill=tk.X, pady=(2, 4))
        else:
            tk.Label(
                frame,
                text="📌 空白新会话 (暂无任何交互记录)",
                fg="#94a3b8",
                bg="#0f172a",
                font=self.font_sub,
                anchor="w",
            ).pack(fill=tk.X, pady=(2, 6))

        # Model response preview if available
        if clean_resp:
            tk.Label(
                frame,
                text="🤖 助手最新回复:",
                fg="#94a3b8",
                bg="#0f172a",
                font=self.font_bold,
                anchor="w",
            ).pack(fill=tk.X, pady=(2, 0))
            tk.Label(
                frame,
                text=f"{clean_resp}",
                fg="#cbd5e1",
                bg="#0f172a",
                font=self.font_sub,
                justify=tk.LEFT,
                wraplength=480,
                anchor="w",
            ).pack(fill=tk.X, pady=(1, 6))

        # Bottom metadata row
        row_dict = self.controller.format_row(cand)
        meta_str = f"UUID: {cand.conversation_id}  |  容量: {row_dict['total_bytes']}  |  活动: {row_dict['last_activity']}"
        tk.Label(
            frame,
            text=meta_str,
            fg="#64748b",
            bg="#0f172a",
            font=self.font_mono,
            anchor="w",
        ).pack(fill=tk.X, pady=(4, 0))

        self._tooltip_win = tw

    def _hide_tooltip(self, event=None):
        if self._tooltip_timer:
            self.after_cancel(self._tooltip_timer)
            self._tooltip_timer = None
        if self._tooltip_win:
            try:
                self._tooltip_win.destroy()
            except Exception:
                pass
            self._tooltip_win = None
        self._hovered_row_id = None

    # --- Data & Binding Methods ---

    def _update_bound_label(self):
        bound_id = self.controller.get_current_bound_id()
        if bound_id:
            from pocketfleet.antigravity_tracks import get_default_ide_root
            ide_db = get_default_ide_root() / "conversations" / f"{bound_id}.db"
            origin_type = "IDE 原生轨" if ide_db.is_file() else "CLI 外勤轨"
            self.lbl_bound_status.config(
                text=f"当前绑定的对话轨: {bound_id} ({origin_type}，已同步至项目 .env)",
                fg="#38bdf8",
            )
        else:
            self.lbl_bound_status.config(
                text="当前未绑定持久轨 (默认以全新独立会话启动)",
                fg="#94a3b8",
            )
        return bound_id

    def refresh_tracks(self):
        if self.is_scanning:
            return
        self.is_scanning = True
        show_all = self.show_all_var.get()

        def _worker():
            err = None
            tracks = []
            try:
                tracks = self.controller.scan_tracks(show_all=show_all)
            except Exception as ex:
                err = ex
            self.after(0, lambda: self._on_scan_done(tracks, err))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_scan_done(self, tracks: list[TrackCandidate], err: Exception | None):
        self.is_scanning = False
        if err:
            self.mgr.log(f"[TRACK] 轨道扫描异常: {err}")
            messagebox.showwarning("扫描提示", f"扫描轨道时出现异常:\n{err}", parent=self)
            return

        self.tracks_data = tracks
        self._item_to_candidate.clear()
        bound_id = self._update_bound_label()

        # Clear and repopulate tree
        for item in self.tree.get_children():
            self.tree.delete(item)

        select_item = None
        for cand in self.tracks_data:
            row_dict = self.controller.format_row(cand, bound_id=bound_id)
            values = (
                row_dict["status"],
                row_dict["snippet"],
                row_dict["last_activity"],
                row_dict["total_bytes"],
            )
            item_id = self.tree.insert("", tk.END, values=values)
            self._item_to_candidate[item_id] = cand

            if row_dict["bound"]:
                select_item = item_id

        if select_item:
            self.tree.selection_set(select_item)
            self.tree.see(select_item)

    def _get_selected_candidate(self) -> TrackCandidate | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return self._item_to_candidate.get(sel[0])

    def _on_bind_click(self):
        self._hide_tooltip()
        cand = self._get_selected_candidate()
        if not cand:
            messagebox.showinfo("请先选择", "请先在列表中选中一条对话轨！", parent=self)
            return

        is_running = self.mgr.is_daemon_running()
        if is_running:
            messagebox.showwarning(
                "服务运行中禁止换轨",
                "Telegram daemon 正在运行，禁止修改轨道绑定。\n\n"
                "请先在控制面板点击【Stop All Services】停止服务，\n"
                "避免长轮询冲突造成 409 Conflict。",
                parent=self,
            )
            return

        try:
            self.controller.bind_track(cand, is_daemon_running=is_running)
            from pocketfleet.antigravity_tracks import clean_dialogue_snippet
            snippet = clean_dialogue_snippet(cand.last_user_prompt or cand.last_model_response, 50)
            self.mgr.log(f"🧭 [TRACK] Antigravity lead conversation bound: {cand.conversation_id}")

            parent_win = self.parent
            if self.on_bind_callback:
                try:
                    self.on_bind_callback()
                except Exception as cb_err:
                    self.mgr.log(f"[TRACK] on_bind_callback error: {cb_err}")

            # Close tracks window per Commander directive
            self.destroy()

            clean_snip = snippet[:36] + ("..." if len(snippet) > 36 else "")
            cid_short = f"{cand.conversation_id[:8]}...{cand.conversation_id[-4:]}"
            if cand.source == "ide":
                toast_msg = (
                    f"会话已同步至执行环境：\n“{clean_snip}”\n(UUID: {cid_short})\n\n"
                    f"✅ 已完成数据原子克隆。在 Telegram 发送指令，裁决者将直接在此会话施工！"
                )
            else:
                toast_msg = (
                    f"已锁定 CLI 会话为施工续轨：\n“{clean_snip}”\n(UUID: {cid_short})\n\n"
                    f"在 Telegram 发送指令，裁决者将直接在此轨道继续施工！"
                )

            show_floating_toast(
                parent=parent_win,
                title="Antigravity 轨道绑定成功！",
                message=toast_msg,
                icon="🧭",
                duration_ms=3200,
            )
        except Exception as e:
            messagebox.showerror("绑定失败", f"无法绑定轨道:\n{e}", parent=self)


# ==============================================================================
# Fleet Manager (In-Process Engine)
# ==============================================================================
class FleetManager:
    def __init__(self, log_cb):
        self.log = log_cb
        self.cockpit_server: CockpitServer | None = None
        self.dispatch_loop: DispatchLoop | None = None
        self.loop_thread: threading.Thread | None = None
        self.cockpit_port = 8765
        self.session_hub: SessionHub | None = None
        self.lead_worker: SessionWorker | None = None

    def is_daemon_running(self) -> bool:
        return bool(self.dispatch_loop and getattr(self.dispatch_loop, "running", False))

    def is_cockpit_running(self) -> bool:
        return is_port_listening(self.cockpit_port)

    def start_cockpit(self, port: int = 8765, open_browser: bool = False) -> None:
        self.cockpit_port = port
        if self.is_cockpit_running():
            self.log(f"[COCKPIT] Web Cockpit already listening on port {port}")
            if open_browser:
                webbrowser.open(f"http://127.0.0.1:{port}")
            return

        self.log(f"[COCKPIT] Starting Web Cockpit on port {port}...")
        try:
            telemetry.workspace = str(REPO_ROOT)
            self.cockpit_server = CockpitServer(port=port)
            self.cockpit_server.start(auto_open=open_browser)
            self.log(f"[COCKPIT] Web Cockpit active at http://127.0.0.1:{port}")
        except Exception as e:
            self.log(f"[ERROR] Failed to start Web Cockpit: {e}")

    def stop_cockpit(self) -> None:
        if self.cockpit_server:
            self.log("[COCKPIT] Stopping Web Cockpit...")
            try:
                self.cockpit_server.stop()
            except Exception:
                pass
            self.cockpit_server = None
            self.log("[COCKPIT] Web Cockpit stopped.")

    def load_fleet_config(self) -> dict:
        default_cfg = {
            "authorized_user_ids": [],
            "role_assignment": {"lead": "antigravity", "builder": "codex"},
            "bots": {
                "antigravity": {
                    "name": "Google Antigravity agent",
                    "executor": "antigravity"
                },
                "codex": {
                    "name": "OpenAI Codex worker",
                    "executor": "codex"
                }
            }
        }
        if CONFIG_FILE.is_file():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                if "bots" not in data:
                    data["bots"] = default_cfg["bots"]
                for agent in data.get("bots", {}).values():
                    if isinstance(agent, dict):
                        agent.pop("token", None)
                if "role_assignment" not in data:
                    data["role_assignment"] = default_cfg["role_assignment"]
                return data
            except Exception:
                pass
        return default_cfg

    def load_seats_config(self) -> FleetSeatsConfig:
        if not CONFIG_FILE.is_file():
            default_cfg = get_default_seats_config()
            self.save_seats_config(default_cfg)
            return default_cfg
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValueError(f"配置文件 {CONFIG_FILE.name} 损坏 (JSON解析失败): {e}") from e

        if not isinstance(data, dict):
            data = {}

        if "seats" not in data or not isinstance(data.get("seats"), dict):
            # 自动平滑升级旧版配置文件：补全默认三席位定义并写回落盘
            default_cfg = get_default_seats_config()
            payload = default_cfg.to_dict()
            if "context_window" in data and isinstance(data["context_window"], int):
                payload["context_window"] = data["context_window"]
            if "no_situ" in data and isinstance(data["no_situ"], bool):
                payload["no_situ"] = data["no_situ"]
            if "authorized_user_ids" in data and isinstance(data["authorized_user_ids"], list):
                payload["authorized_user_ids"] = data["authorized_user_ids"]

            try:
                CONFIG_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
                self.log(f"🔄 [CONFIG] 检测到旧版 {CONFIG_FILE.name}，已自动平滑升级并补全三席位架构。")
            except Exception as w_err:
                self.log(f"[WARN] 自动升级写回配置异常: {w_err}")
            return FleetSeatsConfig.from_dict(payload)

        return FleetSeatsConfig.from_dict(data)

    def save_seats_config(self, seats_cfg: FleetSeatsConfig) -> None:
        validate_seats_config(seats_cfg)
        payload = {}
        if CONFIG_FILE.is_file():
            try:
                payload = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
        payload.update(seats_cfg.to_dict())
        CONFIG_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        self.log(f"💾 [CONFIG] Three seats configuration updated in {CONFIG_FILE.name}")

    def save_fleet_config(self, cfg: dict) -> None:
        try:
            for agent in cfg.get("bots", {}).values():
                if isinstance(agent, dict):
                    agent.pop("token", None)
            CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            self.log(f"[CONFIG] Failed to save config: {e}")

    def switch_roles(self, lead: str, builder: str) -> None:
        cfg = self.load_fleet_config()
        cfg["role_assignment"] = {"lead": lead, "builder": builder}
        self.save_fleet_config(cfg)
        if self.dispatch_loop:
            self.dispatch_loop.set_role_assignment(RoleAssignment(lead=lead, builder=builder))
        self.log(f"👥 [ROLE] Swapped WarRoom: 【任务负责人】{lead.title()} ↔ 【主力程序员】{builder.title()}")

    def start_daemon(self, executor: str | None = None, allow_auto_bind: bool = False) -> bool:
        if self.is_daemon_running():
            self.log("[WARN] Telegram Daemon is already active.")
            return True

        # 1. Reload .env on daemon startup (ensures fresh token / track bindings)
        load_env_file(REPO_ROOT / ".env", override=True)

        cfg = self.load_fleet_config()
        roles = cfg.get("role_assignment", {"lead": "antigravity", "builder": "codex"})
        token, allowed_ids, configured_executor = self._load_credentials()

        # Fail Closed Gate 1: Token verification
        if not token or token == "YOUR_TELEGRAM_BOT_TOKEN":
            self.log("[CONFIG] No valid Bot Token found! Wizard prompt triggered.")
            return False

        # Fail Closed Gate 2: Security Whitelist verification (empty list strictly rejects startup unless explicit allow_auto_bind)
        if not allowed_ids:
            if not allow_auto_bind:
                self.log("❌ [SECURITY] 授权用户列表为空！根据安全默认规则拒绝启动 Telegram 守护进程（禁止解释为 allow-all）。")
                return False
            else:
                self.log("⚡ [AUTO-BIND] 当前尚未绑定协同群 ID，守护进程进入【待入群发言自动绑定】模式。")
                self.log("👉 请在 Telegram 将 Bot 邀请加入战队群，并在群内发送一条消息或 /start，系统将自动识别并锁定该群！")
        else:
            self.log(f"🔒 [SECURITY] 协同群安全白名单已锁定: {list(allowed_ids)}")

        # Fail Closed Gate 3: Legal Antigravity CLI track binding verification
        track_ctrl = AntigravityTrackController(workspace_cwd=REPO_ROOT)
        bound_id = track_ctrl.get_current_bound_id()
        if not bound_id or not is_valid_uuid(bound_id):
            self.log("❌ [START REJECTED] 未绑定合法 Antigravity CLI 轨道！拒绝启动 Daemon。请先在控制台点击【Antigravity 轨道】进行官方导入与绑定。")
            return False

        active_executor = executor or configured_executor or cfg.get("executor") or "fleet_triad"
        self.log("[DAEMON] Initializing evidence-backed dispatch loop and SessionWorker...")
        try:
            state_store = StateStore()
            self.session_hub = SessionHub(state_store)

            # Register authoritative lead Session and launch resident SessionWorker
            self.session_hub.register_or_update_session(
                seat_id="lead",
                engine="antigravity",
                conversation_id=bound_id,
                workspace=str(REPO_ROOT),
                role="adjudicator",
            )
            self.lead_worker = self.session_hub.create_worker(seat_id="lead")
            self.lead_worker.start()
            self.log(f"🧭 [SESSION_HUB] Lead SessionWorker active on CLI track '{bound_id[:8]}...{bound_id[-4:]}'.")

            auth_uids = set(seats_cfg.authorized_user_ids) if seats_cfg.authorized_user_ids else None
            transport = TelegramTransport(bot_token=token, state_store=state_store)
            self.dispatch_loop = DispatchLoop(
                transport=transport,
                workspace_cwd=str(REPO_ROOT),
                default_worker=WorkerType(active_executor),
                allowed_chat_ids=allowed_ids,
                state_store=state_store,
                bots_config=cfg.get("bots", {}),
                role_assignment=RoleAssignment(lead=roles.get("lead", "antigravity"), builder=roles.get("builder", "codex")),
                session_hub=self.session_hub,
                authorized_user_ids=auth_uids,
                on_chat_bound=self._on_chat_auto_bound,
                seats_config=seats_cfg,
            )

            telemetry.allowed_chat_ids = list(allowed_ids) if allowed_ids else []
            telemetry.available_workers = [w.value for w in self.dispatch_loop.get_available_workers()]
            telemetry.telegram_connected = True

            def _run():
                try:
                    self.dispatch_loop.run_forever(poll_interval=1.0)
                except Exception as ex:
                    self.log(f"[DAEMON] Loop error: {ex}")

            self.loop_thread = threading.Thread(target=_run, daemon=True)
            self.loop_thread.start()
            self.log(f"[DAEMON] Telegram Bridge Daemon running with '{active_executor}'. Listening...")
            return True
        except Exception as e:
            self.log(f"[ERROR] Failed to start daemon: {e}")
            # Clean up partially initialized resources on failure
            if self.lead_worker:
                try:
                    self.lead_worker.stop(cancel_active=True, timeout=2.0)
                except Exception:
                    pass
                self.lead_worker = None
            if self.dispatch_loop:
                try:
                    self.dispatch_loop.stop()
                except Exception:
                    pass
                self.dispatch_loop = None
            self.session_hub = None
            return False

    def switch_executor(self, executor_type: str) -> None:
        try:
            wt = WorkerType(executor_type)
            if self.dispatch_loop:
                self.dispatch_loop.default_worker = wt
                self.log(f"[ENGINE] Active daemon engine dynamically switched to: '{wt.value}'")
            else:
                self.log(f"[ENGINE] Default configured engine set to: '{wt.value}'")

            if CONFIG_FILE.is_file():
                try:
                    data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                    data["executor"] = wt.value
                    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
                except Exception:
                    pass
        except Exception as e:
            self.log(f"[ERROR] Failed to switch engine: {e}")

    def stop_daemon(self) -> None:
        if not self.is_daemon_running() and not self.lead_worker:
            self.log("[DAEMON] Daemon is not running.")
            return
        self.log("[DAEMON] Stopping Telegram Bridge Daemon and SessionWorker...")
        if self.lead_worker:
            try:
                self.lead_worker.stop(cancel_active=True, timeout=5.0)
            except Exception as w_err:
                self.log(f"[WARN] Error stopping lead worker: {w_err}")
            self.lead_worker = None

        if self.dispatch_loop:
            self.dispatch_loop.stop()
            self.dispatch_loop = None
        self.session_hub = None
        telemetry.telegram_connected = False
        self.log("[DAEMON] Daemon and workers stopped.")

    def _on_chat_auto_bound(self, chat_id: int, chat_title: str) -> None:
        self.log(f"🎉 [AUTO-BIND] 协同战役室自动绑定成功！")
        self.log(f"   Chat ID: {chat_id} ｜ 群名称: {chat_title or '战队群'}")
        self.log("🔒 [SECURITY] 战队群安全白名单已全面锁定，Fail-Closed 审计已就绪。")

    def _load_credentials(self) -> tuple[str | None, set[int] | None, str | None]:
        seats_cfg = self.load_seats_config()
        lead_seat = seats_cfg.seats.get("lead")
        token_env = lead_seat.bot_token_env if lead_seat else "TELEGRAM_BOT_JUDGE_TOKEN"
        tok = os.environ.get(token_env) or os.environ.get("POCKETFLEET_BOT_TOKEN")

        # Authoritative single source for whitelist: POCKETFLEET_AUTHORIZED_USER_IDS env var has highest priority
        env_auth_str = os.environ.get("POCKETFLEET_AUTHORIZED_USER_IDS")
        if env_auth_str is not None:
            import re
            parts = re.split(r"[,;|\s]+", env_auth_str.strip())
            ids = set()
            for p in parts:
                if p:
                    try:
                        ids.add(int(p))
                    except ValueError:
                        pass
        elif seats_cfg.authorized_user_ids:
            ids = set(seats_cfg.authorized_user_ids)
        else:
            ids = set()

        # Also incorporate configured group chat ID into allowed IDs
        env_grp = (os.environ.get("TELEGRAM_GROUP_ID") or "").strip()
        if env_grp:
            try:
                ids.add(int(env_grp))
            except ValueError:
                pass
        if seats_cfg.telegram_chat_id:
            try:
                ids.add(int(seats_cfg.telegram_chat_id.strip()))
            except ValueError:
                pass

        exec_type = lead_seat.engine if lead_seat else "fleet_triad"
        return tok, (ids if ids else None), exec_type



# ==============================================================================
# Main GUI Window (Tkinter)
# ==============================================================================
class PocketFleetControlApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PocketFleet Control Panel (Solo Hacker Edition)")
        self.root.geometry("860x720")
        self.root.minsize(800, 640)

        self.root.configure(bg="#0b0f19")

        # Thread-safe log queue
        self.log_queue = queue.Queue()

        # Fonts
        self.font_title = tkfont.Font(family="Segoe UI", size=15, weight="bold")
        self.font_sub = tkfont.Font(family="Segoe UI", size=9)
        self.font_bold = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        self.font_regular = tkfont.Font(family="Segoe UI", size=9)
        self.font_mono = tkfont.Font(family="Consolas", size=9)

        # Logic
        self.mgr = FleetManager(self.append_log)
        self.is_quitting = False
        self.tray_icon = None

        # Verify configuration integrity on startup (fail-loud if invalid/corrupt)
        try:
            self.mgr.load_seats_config()
        except Exception as e:
            messagebox.showerror(
                "配置文件严重错误 (Config Error)",
                f"【无法启动 PocketFleet】\n\n{e}\n\n请修复 {CONFIG_FILE.name} 或删除后重新启动自动生成默认配置。",
            )
            self.root.destroy()
            return

        self._build_header()
        self._build_table()
        self._build_three_seats_panel()
        self._build_toolbar()
        self._build_log_console()


        self._setup_tray()
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        self.append_log("🚀 PocketFleet Control Panel initialized. Ready to command.")

        # Start drain log loop on main thread
        self.root.after(100, self._drain_log_queue)

        # Start periodic status refresh on main thread
        self.root.after(500, self._refresh_status)

        # Auto-start Web Cockpit after mainloop starts (zero-poll, web UI only)
        self.root.after(600, lambda: threading.Thread(target=lambda: self.mgr.start_cockpit(8765, open_browser=False), daemon=True).start())

    def append_log(self, text: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] {text}\n")

    def _drain_log_queue(self) -> None:
        while not self.log_queue.empty():
            try:
                msg = self.log_queue.get_nowait()
                self.log_text.config(state=tk.NORMAL)
                self.log_text.insert(tk.END, msg)
                self.log_text.see(tk.END)
                self.log_text.config(state=tk.DISABLED)
            except Exception:
                break
        if not self.is_quitting:
            self.root.after(100, self._drain_log_queue)

    def _refresh_status(self) -> None:
        try:
            is_d = self.mgr.is_daemon_running()
            self._update_row(
                self.row_daemon,
                is_running=is_d,
                detail="Active (Polling Telegram)" if is_d else "Stopped",
            )

            is_c = self.mgr.is_cockpit_running()
            self._update_row(
                self.row_cockpit,
                is_running=is_c,
                detail="Listening on http://127.0.0.1:8765" if is_c else "Offline",
            )

            seats_cfg = self.mgr.load_seats_config()
            lead_s = seats_cfg.seats.get("lead")
            builder_s = seats_cfg.seats.get("builder")
            chat_s = seats_cfg.seats.get("chat")
            l_info = f"{lead_s.name} ({lead_s.engine})" if lead_s else "Lead"
            b_info = f"{builder_s.name} ({builder_s.engine})" if builder_s else "Builder"
            c_info = f"{chat_s.name} ({chat_s.engine})" if chat_s else "Chat"
            self._update_row(
                self.row_agent,
                is_running=True,
                detail=f"Triad: {l_info} ↔ {b_info} ↔ {c_info}",
            )


            if self.tray_icon:
                color = "green" if (is_d and is_c) else ("cyan" if (is_d or is_c) else "yellow")
                self.tray_icon.icon = create_tray_image(color)
        except Exception:
            pass

        if not self.is_quitting:
            self.root.after(1000, self._refresh_status)

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg="#0f172a", height=70)
        header.pack(fill=tk.X)

        title_lbl = tk.Label(
            header,
            text="🚀 PocketFleet Control Panel (Solo Hacker Edition)",
            fg="#38bdf8",
            bg="#0f172a",
            font=self.font_title,
        )
        title_lbl.pack(anchor="w", padx=18, pady=(12, 2))

        sub_lbl = tk.Label(
            header,
            text="AI STARFLEET COMMUNICATION HUB | XAMPP-STYLE TRAY CONTROLLER | v0.2.0",
            fg="#94a3b8",
            bg="#0f172a",
            font=self.font_sub,
        )
        sub_lbl.pack(anchor="w", padx=18, pady=(0, 10))

    def _build_table(self) -> None:
        table_card = tk.Frame(self.root, bg="#1e293b", bd=1, relief=tk.SOLID)
        table_card.pack(fill=tk.X, padx=16, pady=12)

        header_row = tk.Frame(table_card, bg="#334155", height=28)
        header_row.pack(fill=tk.X)
        tk.Label(header_row, text="Status", fg="#f1f5f9", bg="#334155", font=self.font_bold, width=8).pack(side=tk.LEFT, padx=6)
        tk.Label(header_row, text="Service Name", fg="#f1f5f9", bg="#334155", font=self.font_bold, width=24, anchor="w").pack(side=tk.LEFT, padx=6)
        tk.Label(header_row, text="Runtime / Port / PID Details", fg="#f1f5f9", bg="#334155", font=self.font_bold, width=32, anchor="w").pack(side=tk.LEFT, padx=6)
        tk.Label(header_row, text="Action", fg="#f1f5f9", bg="#334155", font=self.font_bold, width=12).pack(side=tk.LEFT, padx=6)
        tk.Label(header_row, text="Shortcut", fg="#f1f5f9", bg="#334155", font=self.font_bold, width=12).pack(side=tk.LEFT, padx=6)

        # Row 1: Telegram Daemon
        self.row_daemon = self._create_service_row(
            table_card,
            name="1. Telegram Bridge Daemon",
            on_start=self._action_start_daemon,
            on_stop=lambda: threading.Thread(target=self.mgr.stop_daemon, daemon=True).start(),
            aux_text="Configure Seats",
            aux_cmd=self.open_three_seats_dialog,
        )

        # Row 2: Local Web Cockpit
        self.row_cockpit = self._create_service_row(
            table_card,
            name="2. Local Web Cockpit (UI)",
            on_start=lambda: threading.Thread(target=lambda: self.mgr.start_cockpit(8765, True), daemon=True).start(),
            on_stop=lambda: threading.Thread(target=self.mgr.stop_cockpit, daemon=True).start(),
            aux_text="Open Browser",
            aux_cmd=self._action_open_browser,
        )

        # Row 3: Coding Agent Target
        self.row_agent = self._create_service_row(
            table_card,
            name="3. AI Coding Agent",
            on_start=None,
            on_stop=None,
            aux_text="Docs",
            aux_cmd=lambda: webbrowser.open("https://fionhua.github.io/PocketFleet/"),
            is_readonly=True,
        )

    def _create_service_row(
        self, parent, name: str, on_start, on_stop, aux_text: str, aux_cmd, is_readonly: bool = False
    ) -> dict:
        row = tk.Frame(parent, bg="#1e293b", height=42)
        row.pack(fill=tk.X, pady=2)

        canvas = tk.Canvas(row, width=24, height=24, bg="#1e293b", highlightthickness=0)
        canvas.pack(side=tk.LEFT, padx=12)
        light = canvas.create_oval(4, 4, 20, 20, fill="#ef4444", outline="")

        name_lbl = tk.Label(row, text=name, fg="#f8fafc", bg="#1e293b", font=self.font_bold, width=24, anchor="w")
        name_lbl.pack(side=tk.LEFT, padx=6)

        detail_lbl = tk.Label(row, text="Probing...", fg="#94a3b8", bg="#1e293b", font=self.font_mono, width=32, anchor="w")
        detail_lbl.pack(side=tk.LEFT, padx=6)

        btn_frame = tk.Frame(row, bg="#1e293b", width=12)
        btn_frame.pack(side=tk.LEFT, padx=6)

        if not is_readonly:
            action_btn = tk.Button(
                btn_frame,
                text="Start",
                bg="#10b981",
                fg="#ffffff",
                activebackground="#059669",
                activeforeground="#ffffff",
                font=self.font_bold,
                relief=tk.FLAT,
                width=8,
                cursor="hand2",
            )
            action_btn.pack()
        else:
            action_btn = tk.Label(btn_frame, text="Auto/Local", fg="#64748b", bg="#1e293b", font=self.font_regular, width=8)
            action_btn.pack()

        aux_btn = tk.Button(
            row,
            text=aux_text,
            bg="#334155",
            fg="#f8fafc",
            activebackground="#475569",
            activeforeground="#ffffff",
            font=self.font_regular,
            relief=tk.FLAT,
            width=11,
            cursor="hand2",
            command=aux_cmd,
        )
        aux_btn.pack(side=tk.LEFT, padx=6)

        return {
            "canvas": canvas,
            "light": light,
            "detail": detail_lbl,
            "button": action_btn,
            "on_start": on_start,
            "on_stop": on_stop,
            "state": "unknown",
        }

    def _build_three_seats_panel(self) -> None:
        card = tk.Frame(self.root, bg="#1e293b", bd=1, relief=tk.SOLID)
        card.pack(fill=tk.X, padx=16, pady=(0, 10))

        hdr = tk.Frame(card, bg="#1e293b", padx=12, pady=6)
        hdr.pack(fill=tk.X)

        tk.Label(
            hdr,
            text="👥 Fleet Triad Seats (三席位战队独立编排):",
            fg="#38bdf8",
            bg="#1e293b",
            font=self.font_bold,
        ).pack(side=tk.LEFT)

        btn_cfg = tk.Button(
            hdr,
            text="⚙️ 配置三席位 (Configure Seats)",
            bg="#0284c7",
            fg="#ffffff",
            activebackground="#0369a1",
            activeforeground="#ffffff",
            font=self.font_bold,
            relief=tk.FLAT,
            padx=12,
            pady=3,
            cursor="hand2",
            command=self.open_three_seats_dialog,
        )
        btn_cfg.pack(side=tk.RIGHT)

        # Container for the 3 seat cards
        self.seats_container = tk.Frame(card, bg="#0f172a", padx=10, pady=8)
        self.seats_container.pack(fill=tk.X, padx=8, pady=(0, 8))
        self.seats_container.columnconfigure(0, weight=1)
        self.seats_container.columnconfigure(1, weight=1)
        self.seats_container.columnconfigure(2, weight=1)

        self._render_seat_cards()

    def _render_seat_cards(self) -> None:
        for widget in self.seats_container.winfo_children():
            widget.destroy()

        seats_cfg = self.mgr.load_seats_config()
        track_ctrl = AntigravityTrackController(workspace_cwd=REPO_ROOT)
        bound_id = track_ctrl.get_current_bound_id()
        bound_summary = f"{bound_id[:8]}...{bound_id[-4:]}" if bound_id else "未绑定"

        seat_roles = [
            ("chat", "💬 人类交互席 (Chat)", "#38bdf8"),
            ("lead", "🎖️ 规划席·CTO (Lead)", "#10b981"),
            ("builder", "🛠️ 执行席·主力 (Builder)", "#f59e0b"),
        ]

        # Dynamically append any extended code seats if configured
        for rk, s_cfg in seats_cfg.seats.items():
            if rk not in ("chat", "lead", "builder"):
                seat_roles.append((rk, f"⚙️ 扩展席位 ({rk})", "#a855f7"))

        for col, (role_key, role_label, accent_color) in enumerate(seat_roles):
            self.seats_container.columnconfigure(col, weight=1)
            seat = seats_cfg.seats.get(role_key)
            card_sub = tk.Frame(self.seats_container, bg="#1e293b", bd=1, relief=tk.RIDGE, padx=10, pady=8)
            card_sub.grid(row=0, column=col, sticky="nsew", padx=4)

            tk.Label(
                card_sub,
                text=role_label,
                fg=accent_color,
                bg="#1e293b",
                font=self.font_bold,
                anchor="w",
            ).pack(fill=tk.X)

            name_text = seat.name if seat else "未配置"
            eng_text = (seat.engine.upper() if seat else "UNKNOWN")
            token_env = seat.bot_token_env if seat else ""
            desc = seat.description if seat else ""
            user = seat.bot_username if seat else ""
            token_val = (os.environ.get(token_env) or "").strip() if token_env else ""

            tk.Label(
                card_sub,
                text=f"代号: {name_text}",
                fg="#f8fafc",
                bg="#1e293b",
                font=self.font_bold,
                anchor="w",
            ).pack(fill=tk.X, pady=(4, 0))

            tk.Label(
                card_sub,
                text=f"引擎: {eng_text}",
                fg="#38bdf8",
                bg="#1e293b",
                font=self.font_sub,
                anchor="w",
            ).pack(fill=tk.X)

            if seat and seat.engine.lower() == "antigravity":
                tk.Label(
                    card_sub,
                    text="轨道: 🟢 已就绪" if bound_id else "轨道: ⚪ 待绑定",
                    fg="#10b981" if bound_id else "#94a3b8",
                    bg="#1e293b",
                    font=self.font_sub,
                    anchor="w",
                ).pack(fill=tk.X)

            tg_status_text = f"TG: 🟢 {user}" if (token_val and user) else "TG: 🔴 待配置"
            tg_status_color = "#10b981" if (token_val and user) else "#ef4444"
            tk.Label(
                card_sub,
                text=tg_status_text,
                fg=tg_status_color,
                bg="#1e293b",
                font=self.font_sub,
                anchor="w",
            ).pack(fill=tk.X)

            if desc:
                tk.Label(
                    card_sub,
                    text=f"职责: {desc}",
                    fg="#64748b",
                    bg="#1e293b",
                    font=self.font_sub,
                    anchor="w",
                ).pack(fill=tk.X, pady=(2, 0))

    def open_three_seats_dialog(self) -> None:
        ThreeSeatsConfigDialog(self.root, fleet_mgr=self.mgr, on_save_callback=self._on_seats_saved)

    def _on_seats_saved(self) -> None:
        self.append_log("👥 [SEATS] Fleet Triad seats configuration updated and verified.")
        self._render_seat_cards()

    def open_antigravity_tracks_dialog(self) -> None:
        AntigravityTracksDialog(self.root, fleet_mgr=self.mgr, on_bind_callback=self._on_track_bound)

    def _on_track_bound(self) -> None:
        self.append_log("🧭 [TRACK] Antigravity track bound to lead executor.")
        self._render_seat_cards()

    def _build_toolbar(self) -> None:

        toolbar = tk.Frame(self.root, bg="#0b0f19")
        toolbar.pack(fill=tk.X, padx=16, pady=4)

        btn_start_all = tk.Button(
            toolbar,
            text="🚀 Start All Services",
            bg="#10b981",
            fg="#ffffff",
            activebackground="#059669",
            activeforeground="#ffffff",
            font=self.font_bold,
            relief=tk.FLAT,
            padx=12,
            pady=4,
            cursor="hand2",
            command=self.action_start_all,
        )
        btn_start_all.pack(side=tk.LEFT, padx=(0, 8))

        btn_stop_all = tk.Button(
            toolbar,
            text="🛑 Stop All Services",
            bg="#ef4444",
            fg="#ffffff",
            activebackground="#dc2626",
            activeforeground="#ffffff",
            font=self.font_bold,
            relief=tk.FLAT,
            padx=12,
            pady=4,
            cursor="hand2",
            command=self.action_stop_all,
        )
        btn_stop_all.pack(side=tk.LEFT, padx=(0, 8))

        btn_tray = tk.Button(
            toolbar,
            text="⬇ Minimize to Tray",
            bg="#38bdf8",
            fg="#0f172a",
            activebackground="#0284c7",
            activeforeground="#ffffff",
            font=self.font_bold,
            relief=tk.FLAT,
            padx=12,
            pady=4,
            cursor="hand2",
            command=self.hide_to_tray,
        )
        btn_tray.pack(side=tk.RIGHT)

    def _build_log_console(self) -> None:
        console_frame = tk.Frame(self.root, bg="#0b0f19")
        console_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=(8, 16))

        hdr = tk.Frame(console_frame, bg="#1e293b", height=26)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="Live Console Output & Bridge Activity", fg="#94a3b8", bg="#1e293b", font=self.font_bold).pack(side=tk.LEFT, padx=8)

        btn_clear = tk.Button(
            hdr,
            text="Clear",
            bg="#334155",
            fg="#cbd5e1",
            font=self.font_sub,
            relief=tk.FLAT,
            padx=6,
            command=self._clear_log,
        )
        btn_clear.pack(side=tk.RIGHT, padx=4)

        self.log_text = tk.Text(
            console_frame,
            bg="#030712",
            fg="#38bdf8",
            font=self.font_mono,
            insertbackground="#38bdf8",
            relief=tk.FLAT,
            bd=4,
            state=tk.DISABLED,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _clear_log(self) -> None:
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)

    def open_setup_wizard(self) -> None:
        self.open_three_seats_dialog()

    def _action_open_browser(self) -> None:
        if not self.mgr.is_cockpit_running():
            self.mgr.start_cockpit(8765, open_browser=True)
        else:
            webbrowser.open("http://127.0.0.1:8765")

    def _action_start_daemon(self) -> None:
        def _task():
            ok = self.mgr.start_daemon()
            if not ok:
                # Open wizard dialog smoothly on the main UI thread
                self.root.after(0, self.open_setup_wizard)
        threading.Thread(target=_task, daemon=True).start()

    def action_start_all(self) -> None:
        self.append_log("Starting all PocketFleet services...")
        def _run():
            self.mgr.start_cockpit(8765, open_browser=False)
            time.sleep(0.3)
            self._action_start_daemon()
        threading.Thread(target=_run, daemon=True).start()

    def action_stop_all(self) -> None:
        self.append_log("Stopping all PocketFleet services...")
        def _run():
            self.mgr.stop_daemon()
            self.mgr.stop_cockpit()
            self.append_log("All services stopped.")
        threading.Thread(target=_run, daemon=True).start()

    # ---------------- System Tray ----------------
    def _setup_tray(self) -> None:
        if not HAS_TRAY or pystray is None:
            return
        menu = pystray.Menu(
            pystray.MenuItem("🚀 Open PocketFleet Control Panel", self.show_from_tray, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("👥 Configure Seats (三席位编排)", self.open_three_seats_dialog),
            pystray.MenuItem("🌐 Open Web Cockpit (Port 8765)", self._action_open_browser),
            pystray.MenuItem("⚡ Start All Services", self.action_start_all),
            pystray.MenuItem("🛑 Stop All Services", self.action_stop_all),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("❌ Exit PocketFleet", self.quit_app),
        )
        self.tray_icon = pystray.Icon(
            "PocketFleetControl",
            create_tray_image("cyan"),
            "PocketFleet Control Panel",
            menu,
        )
        self.tray_icon.run_detached()

    def hide_to_tray(self) -> None:
        self.root.withdraw()
        if self.tray_icon:
            try:
                self.tray_icon.notify("PocketFleet is minimized to the system tray and running in background.", "PocketFleet")
            except Exception:
                pass

    def show_from_tray(self, icon=None, item=None) -> None:
        self.root.after(0, self._restore_window)

    def _restore_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit_app(self, icon=None, item=None) -> None:
        self.is_quitting = True
        self.mgr.stop_daemon()
        self.mgr.stop_cockpit()
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.after(0, self.root.destroy)

    def _update_row(self, row: dict, is_running: bool, detail: str) -> None:
        row["detail"].config(text=detail)
        fill_color = "#10b981" if is_running else "#ef4444"
        row["canvas"].itemconfig(row["light"], fill=fill_color)
        if row.get("button") and row.get("on_start"):
            if is_running:
                row["button"].config(
                    text="Stop",
                    bg="#ef4444",
                    activebackground="#dc2626",
                    command=row["on_stop"],
                )
            else:
                row["button"].config(
                    text="Start",
                    bg="#10b981",
                    activebackground="#059669",
                    command=row["on_start"],
                )


def main():
    multiprocessing.freeze_support()
    root = tk.Tk()
    app = PocketFleetControlApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
