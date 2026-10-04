#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import multiprocessing

logger = logging.getLogger("pocketfleet.control_panel")
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

if sys.platform == "win32":
    try:
        import ctypes
        # Tk 8.6 does not handle WM_DPICHANGED reliably. System-DPI awareness
        # lets Windows scale the native frame without cursor/window drift.
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

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
from pocketfleet.ui_assets import (
    get_icon,
    COLOR_WIN_BG,
    COLOR_EMERALD_PRIMARY,
    COLOR_EMERALD_HOVER,
    COLOR_EMERALD_LIGHT,
    COLOR_EMERALD_PILL_BG,
    COLOR_EMERALD_PILL_TEXT,
    COLOR_TG_BLUE,
    COLOR_DANGER_TEXT,
    COLOR_DANGER_BG,
    COLOR_DANGER_BORDER,
    COLOR_BTN_OUTLINE_BG,
    COLOR_BTN_OUTLINE_BORDER,
    COLOR_BTN_OUTLINE_TEXT,
    COLOR_BTN_OUTLINE_HOVER,
    COLOR_TEXT_TITLE,
    COLOR_TEXT_LIGHT,
)

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

# Quiet daylight palette: white work surfaces, soft gray structure, green actions,
# and red/orange reserved for states that genuinely require attention.
COLOR_APP_BG = "#f3f5f2"
COLOR_SURFACE_ALT = "#f8faf7"
COLOR_SURFACE = "#ffffff"
COLOR_CONTROL = "#e6e9e5"
COLOR_CONTROL_HOVER = "#d5dbd5"
COLOR_TITLE_BAR = "#176b87"
COLOR_TITLE_TEXT = "#ffffff"
COLOR_TITLE_SUBTEXT = "#d7eef2"
COLOR_TAB_IDLE = "#dce6df"
COLOR_TAB_IDLE_HOVER = "#c9d8ce"
COLOR_TAB_BORDER = "#a8bbb0"
COLOR_INPUT_BG = "#edf1ed"
COLOR_TEXT = "#24302a"
COLOR_TEXT_SECONDARY = "#34443b"
COLOR_TEXT_MUTED = "#66736b"
COLOR_TEXT_SOFT = "#8a968f"
COLOR_INFO = "#14885d"
COLOR_PRIMARY = "#188f55"
COLOR_PRIMARY_HOVER = "#147c49"
COLOR_SUCCESS = "#1aa35b"
COLOR_SUCCESS_HOVER = "#15894c"
COLOR_DANGER = "#d84a43"
COLOR_DANGER_HOVER = "#bf3934"
COLOR_WARNING = "#d9901f"
COLOR_WARNING_HOVER = "#b96e12"

# Modern Flat SaaS Design Tokens (Aligned with Settlement Host UI)
COLOR_MODERN_BG = "#f6f8fc"
COLOR_CARD_BG = "#ffffff"
COLOR_CARD_BORDER = "#e2e8f0"
COLOR_CARD_BORDER_HOVER = "#cbd5e1"
COLOR_TEXT_MAIN = "#0f172a"
COLOR_TEXT_MUTED = "#64748b"
COLOR_ACCENT_GREEN = "#059669"
COLOR_ACCENT_GREEN_BG = "#ebf8f2"
COLOR_ACCENT_GREEN_HOVER = "#047857"
COLOR_ACCENT_BLUE = "#0ea5e9"
COLOR_ACCENT_BLUE_BG = "#eef7fd"
COLOR_ACCENT_BLUE_BORDER = "#bae6fd"
COLOR_ACCENT_PURPLE = "#8b5cf6"
COLOR_ACCENT_ORANGE = "#f97316"


def apply_windows_titlebar_theme(window: tk.Misc) -> None:
    """Give native Windows title bars a clear, consistent drag target."""
    if sys.platform != "win32":
        return

    def _apply() -> None:
        try:
            window.update_idletasks()
            client_hwnd = window.winfo_id()
            hwnd = ctypes.windll.user32.GetParent(client_hwnd) or client_hwnd

            def _colorref(hex_color: str) -> int:
                red, green, blue = (int(hex_color[index:index + 2], 16) for index in (1, 3, 5))
                return red | (green << 8) | (blue << 16)

            caption_color = ctypes.c_int(_colorref(COLOR_TITLE_BAR))
            text_color = ctypes.c_int(_colorref(COLOR_TITLE_TEXT))
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption_color), ctypes.sizeof(caption_color))
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text_color), ctypes.sizeof(text_color))
        except Exception:
            logger.debug("Native title-bar color is unavailable on this Windows version", exc_info=True)

    window.after_idle(_apply)


def get_ui_font_family() -> str:
    """Return a clean default sans-serif font family, avoiding FangSong/KaiTi fallbacks."""
    try:
        available = tkfont.families()
        for candidate in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"):
            if candidate in available:
                return candidate
    except Exception:
        pass
    return ""


def is_port_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.05) -> bool:
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
        "cyan": COLOR_INFO,
        "green": COLOR_SUCCESS,
        "red": COLOR_DANGER,
        "yellow": COLOR_WARNING,
    }
    hex_color = color_map.get(color, COLOR_INFO)
    img = Image.new("RGBA", (64, 64), color=(0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([4, 4, 60, 60], radius=16, fill=COLOR_SURFACE_ALT, outline=hex_color, width=3)
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
        "btn_label": "🗣️面向人类交互",
        "desc_text": "对话AI·推演与宏观对账",
        "hint": "【面向人类交互】\n承担与人类指挥官第一人称的推演对话、需求澄清与宏观对账，作为星舰前端交流主通道。",
        "bg": COLOR_PRIMARY,
        "fg": "#ffffff",
    },
    {
        "key": "lead",
        "btn_label": "🎖️研发总监",
        "desc_text": "施工指挥·架构守门与改卷验收",
        "hint": "【研发总监】\n统领工程落地与代码审查，负责系统生存率守门、架构验收与质量裁决。",
        "bg": COLOR_SUCCESS_HOVER,
        "fg": "#ffffff",
    },
    {
        "key": "builder",
        "btn_label": "🛠️主力程序员",
        "desc_text": "主力程序员·核心施工与算法定桩",
        "hint": "【主力程序员】\n专注具体模块编码、算法定桩与攻坚施工，接受改卷验收并交付高质量代码。",
        "bg": COLOR_WARNING_HOVER,
        "fg": "#ffffff",
    },
    {
        "key": "custom",
        "btn_label": "✏️自定义",
        "desc_text": "",
        "hint": "【自定义职能】\n手动为当前选中的席位自由输入自定义职责描述文本。",
        "bg": COLOR_CONTROL_HOVER,
        "fg": COLOR_TEXT,
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
        self.configure(bg=COLOR_APP_BG)
        apply_windows_titlebar_theme(self)
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
        header = tk.Frame(self, bg=COLOR_TITLE_BAR, padx=20, pady=12)
        header.pack(fill=tk.X)
        tk.Label(
            header,
            text=f"🤖 Telegram 席位凭据与战队群设置",
            fg=COLOR_TITLE_TEXT,
            bg=COLOR_TITLE_BAR,
            font=self.font_title,
        ).pack(anchor="w")
        tk.Label(
            header,
            text=f"席位: {self.seat_name} ｜ 引擎: {self.engine_name.upper()} ｜ 战队协同群为全席位共享资产",
            fg=COLOR_TITLE_SUBTEXT,
            bg=COLOR_TITLE_BAR,
            font=self.font_sub,
        ).pack(anchor="w", pady=(2, 0))

        content = tk.Frame(self, bg=COLOR_APP_BG, padx=20, pady=12)
        content.pack(fill=tk.BOTH, expand=True)

        # --- Section 1: Bot Token 与身份验证 ---
        bot_frame = tk.LabelFrame(
            content,
            text=f" 🔐 1. 填入并验证 Bot Token ",
            fg=COLOR_SUCCESS,
            bg=COLOR_SURFACE,
            font=self.font_bold,
            padx=14,
            pady=8,
            relief=tk.SOLID,
            bd=1,
            highlightthickness=1,
            highlightbackground=COLOR_CONTROL,
        )
        bot_frame.pack(fill=tk.X, pady=(0, 10))

        b_r1 = tk.Frame(bot_frame, bg=COLOR_SURFACE)
        b_r1.pack(fill=tk.X, pady=2)
        tk.Label(b_r1, text="Bot Token:", fg=COLOR_TEXT, bg=COLOR_SURFACE, font=self.font_sub, width=11, anchor="w").pack(side=tk.LEFT)
        self.entry_token = tk.Entry(b_r1, bg=COLOR_SURFACE_ALT, fg=COLOR_TEXT, insertbackground=COLOR_TEXT, font=self.font_mono, width=42, relief=tk.FLAT, bd=4, show="*")
        env_val = (os.environ.get(self.bot_token_env) or "").strip()
        if env_val:
            self.entry_token.insert(0, env_val)
        self.entry_token.pack(side=tk.LEFT, padx=(0, 6))
        self.entry_token.bind("<FocusOut>", lambda e: self._init_broker_session())
        self.entry_token.bind("<Return>", lambda e: self._init_broker_session())

        self.btn_toggle = tk.Button(
            b_r1, text="👁️", bg=COLOR_CONTROL, fg=COLOR_TEXT, activebackground=COLOR_CONTROL_HOVER,
            font=self.font_sub, relief=tk.FLAT, padx=6, pady=1, cursor="hand2", command=self._toggle_token_visibility,
        )
        self.btn_toggle.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_verify = None

        self.lbl_token_status = tk.Label(b_r1, text="", fg=COLOR_TEXT_MUTED, bg=COLOR_SURFACE, font=self.font_sub)
        self.lbl_token_status.pack(side=tk.LEFT)

        b_r2 = tk.Frame(bot_frame, bg=COLOR_SURFACE)
        b_r2.pack(fill=tk.X, pady=2)
        tk.Label(b_r2, text="Bot 用户名:", fg=COLOR_TEXT_MUTED, bg=COLOR_SURFACE, font=self.font_sub, width=11, anchor="w").pack(side=tk.LEFT)
        self.lbl_bot_uname = tk.Label(b_r2, text=self.bot_username or "（尚未验证）", fg=COLOR_INFO, bg=COLOR_SURFACE, font=self.font_mono)
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
            fg=COLOR_INFO,
            bg=COLOR_SURFACE,
            font=self.font_bold,
            padx=16,
            pady=12,
            relief=tk.SOLID,
            bd=1,
            highlightthickness=1,
            highlightbackground=COLOR_CONTROL,
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
            fg=COLOR_TEXT_SECONDARY,
            bg=COLOR_SURFACE,
            font=self.font_sub,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 8))

        btn_row = tk.Frame(self.group_frame, bg=COLOR_SURFACE)
        btn_row.pack(fill=tk.X, pady=(2, 6))

        self.btn_open_tg = tk.Button(
            btn_row,
            text="🚀 打开 Telegram，选择战队群",
            bg=COLOR_PRIMARY,
            fg="#ffffff",
            activebackground=COLOR_PRIMARY_HOVER,
            font=self.font_bold,
            relief=tk.FLAT,
            padx=16,
            pady=8,
            cursor="hand2",
            command=self._open_telegram_deep_link,
        )
        self.btn_open_tg.pack(anchor="w")

        status_row = tk.Frame(self.group_frame, bg=COLOR_SURFACE)
        status_row.pack(fill=tk.X, pady=(4, 0))
        self.lbl_broker_status = tk.Label(
            status_row,
            text="📡 准备就绪：粘贴 Token 后，点击上方按钮即可一键加群绑定",
            fg=COLOR_TEXT_MUTED,
            bg=COLOR_SURFACE,
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
        actions = tk.Frame(self, bg=COLOR_SURFACE_ALT, padx=20, pady=12)
        actions.pack(fill=tk.X, side=tk.BOTTOM)

        btn_cancel = tk.Button(
            actions,
            text="完成并关闭 (Done)",
            bg=COLOR_CONTROL, fg=COLOR_TEXT, activebackground=COLOR_CONTROL_HOVER,
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
            self.lbl_token_status.config(text="⚪ 请输入 Token", fg=COLOR_TEXT_MUTED)
            return

        ok, bot_id, uname, err = verify_bot_token(token)
        if ok and bot_id:
            self.bot_id = bot_id
            if uname:
                self.bot_username = "@" + uname.lstrip("@")
            final_user = self.bot_username or f"Bot_{bot_id}"
            self.lbl_bot_uname.config(text=f"{final_user} (ID: {bot_id})")
            self.lbl_token_status.config(text="🟢 Token有效", fg=COLOR_SUCCESS)

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
            self.lbl_token_status.config(text=f"❌ {err or '无效'}", fg=COLOR_DANGER)

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
            self.lbl_token_status.config(text=f"❌ {err or '无效'}", fg=COLOR_DANGER)
            messagebox.showerror("Token 校验失败", f"Bot Token 校验失败: {err}", parent=self)
            return None

        self.bot_id = bot_id
        if uname:
            self.bot_username = "@" + uname.lstrip("@")
        final_user = self.bot_username or f"Bot_{bot_id}"
        self.lbl_bot_uname.config(text=f"{final_user} (ID: {bot_id})")
        self.lbl_token_status.config(text="🟢 Token有效", fg=COLOR_SUCCESS)

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
            self.lbl_broker_status.config(text="⚡ 后台服务运行中：已接入全局总线，请在 Telegram 中选择战队群...", fg=COLOR_INFO)
        else:
            self.lbl_broker_status.config(text="📡 正在监听中：请在 Telegram 中选择战队群并确认添加...", fg=COLOR_WARNING)
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
                    self.lbl_broker_status.config(text=f"🟢 战队群已连接：{title} (ID: {cid}){cmd_txt}", fg=COLOR_SUCCESS)

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
                    self.lbl_broker_status.config(text="❌ HTTP 409 Conflict: Bot Token 被外部占用", fg=COLOR_DANGER)
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
                    self.lbl_broker_status.config(text=f"⚠️ {err_msg}", fg=COLOR_DANGER)
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
    {"key": "codex", "label": "⚡ OpenAI Codex", "bg": COLOR_SUCCESS, "hint": "【OpenAI Codex】\n自动化终端编码代理，擅长精确单任务施工与脚本生成。"},
    {"key": "antigravity", "label": "🪐 Antigravity", "bg": COLOR_PRIMARY, "hint": "【Google Antigravity】\n全尺寸 IDE 与 CLI 双模代理，支持会话轨道挂载与会话回放。"},
    {"key": "claude_code", "label": "🧠 Claude Code", "bg": COLOR_WARNING_HOVER, "hint": "【Anthropic Claude Code】\n深度逻辑推演与高阶代码重构代理。"},
    {"key": "aider", "label": "🛠️ Aider", "bg": COLOR_TEXT_MUTED, "hint": "【Aider CLI】\n经典 Git 伴侣式终端多文件编辑代理。"},
    {"key": "copilot", "label": "🐙 GitHub Copilot", "bg": COLOR_TEXT_MUTED, "hint": "【GitHub Copilot CLI】\nGitHub 官方终端命令行伴随式智能体。"},
]

CHAT_AI_BUTTONS = [
    {"key": "gemini", "label": "✨ Google Gemini", "bg": COLOR_INFO, "hint": "【Google Gemini】\n长上下文超大窗口对话模型，适合宏观推演与知识库对账。"},
    {"key": "chatgpt", "label": "🤖 OpenAI ChatGPT", "bg": COLOR_SUCCESS, "hint": "【OpenAI ChatGPT】\n通用全能对话助手，适合人机协作日常答疑与指令转译。"},
    {"key": "claude", "label": "🔮 Anthropic Claude", "bg": COLOR_WARNING_HOVER, "hint": "【Anthropic Claude】\n严谨细致的长文本推理对话模型，具备极高宪法安全度。"},
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
            disp_label = engine_info["label"]
            self.button.config(
                text=f"{disp_label}   ❌",
                bg=COLOR_SURFACE_ALT,
                fg=COLOR_INFO,
                activebackground=COLOR_SURFACE,
                activeforeground=COLOR_DANGER,
                relief=tk.SOLID,
                bd=1,
                highlightthickness=1,
                highlightbackground=COLOR_PRIMARY,
                highlightcolor=COLOR_INFO,
                padx=8,
                pady=2,
                cursor="hand2",
            )
            if self.tooltip_binder:
                self.tooltip_binder(
                    self.button,
                    f"【{disp_label}】\n点击【❌】移除/关闭当前执行引擎，重置为【未设置】",
                )
        elif self._val:
            self.button.config(
                text=f"⚙️ {self._val}   ❌",
                bg=COLOR_SURFACE_ALT,
                fg=COLOR_INFO,
                activebackground=COLOR_SURFACE,
                activeforeground=COLOR_DANGER,
                relief=tk.SOLID,
                bd=1,
                highlightthickness=1,
                highlightbackground=COLOR_PRIMARY,
                highlightcolor=COLOR_INFO,
                padx=8,
                pady=2,
                cursor="hand2",
            )
            if self.tooltip_binder:
                self.tooltip_binder(
                    self.button,
                    f"【{self._val}】\n点击【❌】移除/关闭当前执行引擎，重置为【未设置】",
                )
        else:
            self.button.config(
                text="⚪ 未设置 (点击上方按钮指定)",
                bg=COLOR_SURFACE,
                fg=COLOR_TEXT_SOFT,
                activebackground=COLOR_CONTROL,
                activeforeground=COLOR_TEXT,
                relief=tk.FLAT,
                bd=1,
                highlightthickness=1,
                highlightbackground=COLOR_CONTROL,
                highlightcolor=COLOR_CONTROL_HOVER,
                padx=8,
                pady=2,
                cursor="hand2",
            )
            if self.tooltip_binder:
                self.tooltip_binder(
                    self.button,
                    "当前席位未设置执行引擎。\n请选中本席位后，点击上方按钮一键指定。",
                )

    def __getattr__(self, name):
        return getattr(self.button, name)



class ThreeSeatsConfigPanel(tk.Frame):
    def __init__(self, parent, fleet_mgr, on_save_callback=None, is_dialog=False, show_header=True, **kwargs):
        super().__init__(parent, bg=COLOR_APP_BG, **kwargs)
        self.parent = parent
        self.mgr = fleet_mgr
        self.on_save_callback = on_save_callback
        self.is_dialog = is_dialog
        self.show_header = show_header

        ui_fam = get_ui_font_family()
        self.font_title = tkfont.Font(family=ui_fam, size=13, weight="bold")
        self.font_sub = tkfont.Font(family=ui_fam, size=9)
        self.font_bold = tkfont.Font(family=ui_fam, size=9, weight="bold")
        self.font_mono = tkfont.Font(family="Consolas", size=9)

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
        if self.show_header:
            header = tk.Frame(self, bg=COLOR_TITLE_BAR, padx=20, pady=10)
            header.pack(fill=tk.X)
            tk.Label(
                header,
                text="👥 第一步：配置各 AI 席位接入 Telegram (Step 1: Configure AI Seats & Telegram)",
                fg=COLOR_TITLE_TEXT,
                bg=COLOR_TITLE_BAR,
                font=self.font_title,
            ).pack(anchor="w")
            tk.Label(
                header,
                text="二元页签架构：左侧【代码 AI】(多席位施工) ｜ 右侧【对话 AI】(人类交互与对账) ｜ 战队群全席位共用",
                fg=COLOR_TITLE_SUBTEXT,
                bg=COLOR_TITLE_BAR,
                font=self.font_sub,
            ).pack(anchor="w", pady=(2, 0))

        content = tk.Frame(self, bg=COLOR_APP_BG, padx=20, pady=10)
        content.pack(fill=tk.BOTH, expand=True)

        # Tab Switcher (Segmented Buttons)
        tab_bar = tk.Frame(content, bg=COLOR_APP_BG)
        tab_bar.pack(fill=tk.X, pady=(0, 10))

        self.btn_tab_code = tk.Button(
            tab_bar,
            text="💻 代码 AI (Code AI — 2席)",
            font=self.font_bold,
            relief=tk.SOLID,
            bd=1,
            highlightthickness=1,
            highlightbackground=COLOR_PRIMARY,
            padx=18,
            pady=6,
            cursor="hand2",
            bg=COLOR_PRIMARY,
            fg="#ffffff",
            activebackground=COLOR_PRIMARY_HOVER,
            activeforeground="#ffffff",
            command=lambda: self._switch_tab("code"),
        )
        self.btn_tab_code.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_tab_chat = tk.Button(
            tab_bar,
            text="💬 对话 AI (Chat AI — 1席)",
            font=self.font_bold,
            relief=tk.SOLID,
            bd=1,
            highlightthickness=1,
            highlightbackground=COLOR_TAB_BORDER,
            padx=18,
            pady=6,
            cursor="hand2",
            bg=COLOR_TAB_IDLE,
            fg=COLOR_TEXT,
            activebackground=COLOR_TAB_IDLE_HOVER,
            activeforeground=COLOR_TEXT,
            command=lambda: self._switch_tab("chat"),
        )
        self.btn_tab_chat.pack(side=tk.LEFT)

        # Dedicated container for tab pages so tab switching stays isolated above memo_frame
        self.tab_container = tk.Frame(content, bg=COLOR_APP_BG)
        self.tab_container.pack(fill=tk.BOTH, expand=True)

        # ======================================================================
        # Tab 1: 代码 AI (Code AI)
        # ======================================================================
        self.tab_frame_code = tk.Frame(self.tab_container, bg=COLOR_APP_BG)

        # Top Engine Quick Bar (Row of 5 mainstream overseas Code AIs)
        engine_bar_code = tk.Frame(self.tab_frame_code, bg=COLOR_SURFACE, padx=12, pady=8)
        engine_bar_code.pack(fill=tk.X, pady=(0, 10))

        tk.Label(
            engine_bar_code,
            text="⚡ 快速指定代码执行引擎 (选中下方任一席位后，点击按钮一键替换):",
            fg=COLOR_TEXT_MUTED,
            bg=COLOR_SURFACE,
            font=self.font_bold,
        ).pack(anchor="w", pady=(0, 6))

        btn_row_code = tk.Frame(engine_bar_code, bg=COLOR_SURFACE)
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
            color=COLOR_SUCCESS,
            engine_choices=list(ALLOWED_CODE_ENGINES),
        )
        self._create_seat_card(
            self.tab_frame_code,
            role_key="builder",
            title="🛠️ 席位 2: 执行席·主力 (核心施工与算法定桩)",
            color=COLOR_WARNING,
            engine_choices=list(ALLOWED_CODE_ENGINES),
        )

        # ======================================================================
        # Tab 2: 对话 AI (Chat AI)
        # ======================================================================
        self.tab_frame_chat = tk.Frame(self.tab_container, bg=COLOR_APP_BG)

        # Top Engine Quick Bar (Row of mainstream Chat AIs)
        engine_bar_chat = tk.Frame(self.tab_frame_chat, bg=COLOR_SURFACE, padx=12, pady=8)
        engine_bar_chat.pack(fill=tk.X, pady=(0, 10))

        tk.Label(
            engine_bar_chat,
            text="✨ 快速指定对话执行引擎 (点击按钮一键替换人类交互席引擎):",
            fg=COLOR_TEXT_MUTED,
            bg=COLOR_SURFACE,
            font=self.font_bold,
        ).pack(anchor="w", pady=(0, 6))

        btn_row_chat = tk.Frame(engine_bar_chat, bg=COLOR_SURFACE)
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
            color=COLOR_INFO,
            engine_choices=list(ALLOWED_CHAT_ENGINES),
        )

        # Chat AI Sentinel & Scheduled Inspection Row (Item 4)
        self.sentinel_enabled_var = tk.BooleanVar(value=False)
        self.sentinel_interval_var = tk.StringVar(value="15")

        sentinel_frame = tk.LabelFrame(
            self.tab_frame_chat,
            text=" 🛡️ 战队巡检哨兵配置 (Inspector Sentinel) ",
            bg=COLOR_SURFACE_ALT,
            fg=COLOR_INFO,
            font=self.font_bold,
            padx=12,
            pady=8,
            relief=tk.GROOVE,
            bd=1,
        )
        sentinel_frame.pack(fill=tk.X, pady=(10, 0))

        s_row = tk.Frame(sentinel_frame, bg=COLOR_SURFACE_ALT)
        s_row.pack(fill=tk.X)

        self.chk_sentinel = tk.Checkbutton(
            s_row,
            text="启用战队定时巡检哨兵",
            variable=self.sentinel_enabled_var,
            fg=COLOR_TEXT,
            bg=COLOR_SURFACE_ALT,
            selectcolor=COLOR_SURFACE,
            activebackground=COLOR_SURFACE_ALT,
            activeforeground=COLOR_INFO,
            font=self.font_bold,
        )
        self.chk_sentinel.pack(side=tk.LEFT)

        tk.Label(s_row, text="每隔", fg=COLOR_TEXT_SECONDARY, bg=COLOR_SURFACE_ALT, font=self.font_bold).pack(side=tk.LEFT, padx=(12, 4))

        self.spn_sentinel_interval = tk.Spinbox(
            s_row,
            from_=1,
            to=120,
            textvariable=self.sentinel_interval_var,
            width=4,
            bg=COLOR_SURFACE,
            fg=COLOR_TEXT,
            font=self.font_mono,
            relief=tk.FLAT,
            bd=2,
            insertbackground="#ffffff",
        )
        self.spn_sentinel_interval.pack(side=tk.LEFT)

        tk.Label(s_row, text="分钟向对话席位发起一次任务看板巡检与催办请求", fg=COLOR_TEXT_SECONDARY, bg=COLOR_SURFACE_ALT, font=self.font_bold).pack(side=tk.LEFT, padx=(4, 0))

        tk.Label(
            sentinel_frame,
            text="💡 开启后，系统将定时向对话 AI 注入外勤任务看板；若有停滞未办结任务，由对话 AI 负责 [mailto:@Bot] 催办。",
            fg=COLOR_TEXT_MUTED,
            bg=COLOR_SURFACE_ALT,
            font=self.font_sub,
        ).pack(anchor="w", pady=(6, 0))

        # Initially pack code tab
        self.tab_frame_code.pack(fill=tk.BOTH, expand=True)

        # ======================================================================
        # Shared Memo Area (One-click Copyable Fleet Collaboration Memo)
        # ======================================================================
        self.memo_frame = tk.LabelFrame(
            content,
            text=" 📋 战队协同交互 Memo (AI 会话轨底座配置 / 可一键复制) ",
            bg=COLOR_SURFACE_ALT,
            fg=COLOR_INFO,
            font=self.font_bold,
            padx=12,
            pady=8,
            relief=tk.GROOVE,
            bd=1,
        )
        self.memo_frame.pack(fill=tk.X, pady=(6, 8))

        memo_top_row = tk.Frame(self.memo_frame, bg=COLOR_SURFACE_ALT)
        memo_top_row.pack(fill=tk.X, pady=(0, 6))

        self.human_name = "ENTJ指挥官"

        self.lbl_human_display = tk.Label(
            memo_top_row,
            text=f"👤 关联人类: {self.human_name} (从TG群自动获取)",
            fg=COLOR_INFO,
            bg=COLOR_SURFACE_ALT,
            font=self.font_bold,
        )
        self.lbl_human_display.pack(side=tk.LEFT)

        self.btn_copy_memo = tk.Button(
            memo_top_row,
            text="📋 一键复制 Memo (Copy)",
            bg=COLOR_PRIMARY,
            fg="#ffffff",
            activebackground=COLOR_PRIMARY_HOVER,
            activeforeground="#ffffff",
            font=self.font_bold,
            relief=tk.FLAT,
            padx=14,
            pady=3,
            cursor="hand2",
            command=self._copy_memo_to_clipboard,
        )
        self.btn_copy_memo.pack(side=tk.RIGHT)

        self.txt_memo = tk.Text(
            self.memo_frame,
            height=2,
            bg=COLOR_APP_BG,
            fg=COLOR_INFO,
            font=self.font_mono,
            relief=tk.FLAT,
            bd=4,
            wrap=tk.WORD,
            padx=8,
            pady=4,
        )
        self.txt_memo.pack(fill=tk.X)

        # ======================================================================
        # Shared Bottom Area (Collaborative group, context window, actions)
        # ======================================================================
        # Global Telegram Collaborative Group Banner
        self.lbl_global_group = tk.Label(
            content,
            text="📢 Telegram 协同战队群: 尚未配置 (点击任一席位的 TG 状态按钮进行配置/打卡)",
            fg=COLOR_TEXT_MUTED,
            bg=COLOR_APP_BG,
            font=self.font_sub,
            anchor="w",
        )
        self.lbl_global_group.pack(fill=tk.X, pady=(4, 2))

        # Extra options (Context window synchronization)
        opt_frame = tk.Frame(content, bg=COLOR_APP_BG)
        opt_frame.pack(fill=tk.X, pady=(6, 0))

        self.var_sync_context = tk.BooleanVar(value=True)
        self.cb_sync_context = tk.Checkbutton(
            opt_frame,
            text="对被 @ 的席位同步最近对话历史 (Context Window):",
            variable=self.var_sync_context,
            bg=COLOR_APP_BG,
            fg=COLOR_INFO,
            selectcolor=COLOR_SURFACE,
            activebackground=COLOR_APP_BG,
            activeforeground=COLOR_INFO,
            font=self.font_sub,
            command=self._on_sync_context_toggle,
        )
        self.cb_sync_context.pack(side=tk.LEFT)

        self.entry_cw = tk.Entry(opt_frame, bg=COLOR_SURFACE, fg=COLOR_TEXT, font=self.font_mono, width=5, relief=tk.FLAT, bd=4)
        self.entry_cw.insert(0, "20")
        self.entry_cw.pack(side=tk.LEFT, padx=(4, 6))

        tk.Label(opt_frame, text="条", fg=COLOR_TEXT_MUTED, bg=COLOR_APP_BG, font=self.font_sub).pack(side=tk.LEFT)

        # Internal flag kept for backward compatibility
        self.var_nositu = tk.BooleanVar(value=True)

        # Bottom Actions
        actions = tk.Frame(self, bg=COLOR_SURFACE_ALT, padx=20, pady=12)
        actions.pack(fill=tk.X, side=tk.BOTTOM)

        btn_save = tk.Button(
            actions,
            text="💾 保存配置 (Save)",
            bg=COLOR_SUCCESS,
            fg="#ffffff",
            activebackground=COLOR_SUCCESS_HOVER,
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
            bg=COLOR_CONTROL,
            fg=COLOR_TEXT_SECONDARY,
            activebackground=COLOR_CONTROL_HOVER,
            font=self.font_sub,
            relief=tk.FLAT,
            padx=12,
            pady=6,
            cursor="hand2",
            command=self._reset_defaults,
        )
        btn_reset.pack(side=tk.RIGHT, padx=(8, 0))

        if self.is_dialog:
            btn_cancel = tk.Button(
                actions,
                text="取消 (Cancel)",
                bg=COLOR_SURFACE,
                fg=COLOR_TEXT_MUTED,
                activebackground=COLOR_CONTROL,
                font=self.font_sub,
                relief=tk.FLAT,
                padx=12,
                pady=6,
                cursor="hand2",
                command=self._close_panel,
            )
            btn_cancel.pack(side=tk.RIGHT)

    def _create_seat_card(self, parent: tk.Widget, role_key: str, title: str, color: str, engine_choices: list[str]):
        card = tk.LabelFrame(
            parent,
            text=f" {title} ",
            fg=color,
            bg=COLOR_SURFACE,
            font=self.font_bold,
            padx=12,
            pady=8,
            relief=tk.SOLID,
            bd=1,
            highlightthickness=1,
            highlightbackground=COLOR_CONTROL,
            highlightcolor=COLOR_CONTROL,
        )
        card.pack(fill=tk.X, pady=(0, 8))
        self.cards[role_key] = card
        self.card_titles[role_key] = title

        # Card click selection binding
        card.bind("<Button-1>", lambda e, rk=role_key: self._select_seat(rk))

        # Card body with two columns: left for seat properties, right for TG in-card drawer
        card_body = tk.Frame(card, bg=COLOR_SURFACE)
        card_body.pack(fill=tk.BOTH, expand=True)
        card_body.bind("<Button-1>", lambda e, rk=role_key: self._select_seat(rk))

        card_right = tk.Frame(card_body, bg=COLOR_SURFACE)
        card_right.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))

        card_left = tk.Frame(card_body, bg=COLOR_SURFACE)
        card_left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        card_left.bind("<Button-1>", lambda e, rk=role_key: self._select_seat(rk))

        # Row 1: Engine, Track, and Bot Identity Badge (in card_left)
        r1 = tk.Frame(card_left, bg=COLOR_SURFACE)
        r1.pack(fill=tk.X, pady=2)
        r1.bind("<Button-1>", lambda e, rk=role_key: self._select_seat(rk))

        lbl_eng = tk.Label(r1, text="执行引擎:", fg=COLOR_TEXT, bg=COLOR_SURFACE, font=self.font_sub, width=10, anchor="w")
        lbl_eng.pack(side=tk.LEFT)
        lbl_eng.bind("<Button-1>", lambda e, rk=role_key: self._select_seat(rk))

        btn_engine = tk.Button(
            r1,
            text="⚪ 未设置 (点击上方按钮指定)",
            bg=COLOR_CONTROL,
            fg=COLOR_TEXT_MUTED,
            activebackground=COLOR_CONTROL_HOVER,
            activeforeground=COLOR_TEXT,
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
            bg=COLOR_PRIMARY,
            fg="#ffffff",
            activebackground=COLOR_PRIMARY_HOVER,
            activeforeground="#ffffff",
            font=self.font_sub,
            relief=tk.FLAT,
            padx=8,
            pady=1,
            cursor="hand2",
            command=self._open_antigravity_tracks,
        )

        btn_ext_guide = None
        btn_chat_check = None

        # Chat AI action buttons (Items 1, 3 - merged with open web)
        if role_key == "chat":
            btn_ext_guide = tk.Button(
                r1,
                text="🧩 浏览器扩展",
                bg=COLOR_CONTROL,
                fg=COLOR_TEXT,
                activebackground=COLOR_CONTROL_HOVER,
                activeforeground=COLOR_TEXT,
                font=self.font_sub,
                relief=tk.FLAT,
                padx=8,
                pady=1,
                cursor="hand2",
                command=self._open_browser_extension_guide,
            )
            btn_ext_guide.pack(side=tk.LEFT, padx=(6, 0))
            self._bind_btn_tooltip(btn_ext_guide, "打开浏览器扩展目录并弹出 Chrome 加载安装向导")

            btn_chat_check = tk.Button(
                r1,
                text="🌐 桥接自检",
                bg=COLOR_PRIMARY,
                fg="#ffffff",
                activebackground=COLOR_PRIMARY_HOVER,
                activeforeground="#ffffff",
                font=self.font_sub,
                relief=tk.FLAT,
                padx=8,
                pady=1,
                cursor="hand2",
                command=self._verify_chat_ai_bridge,
            )
            btn_chat_check.pack(side=tk.LEFT, padx=(6, 0))
            self._bind_btn_tooltip(btn_chat_check, "只检查本地 Web 桥接连通性，不会自动打开网页")

        # Bot identity badge (auto-populated from getMe or seat config)
        lbl_bot_badge = tk.Label(
            r1,
            text="",
            fg=COLOR_INFO,
            bg=COLOR_SURFACE,
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
        r3 = tk.Frame(card_left, bg=COLOR_SURFACE)
        r3.pack(fill=tk.X, pady=4)
        r3.bind("<Button-1>", lambda e, rk=role_key: self._select_seat(rk))

        lbl_role_tag = tk.Label(r3, text="席位职能:", fg=COLOR_TEXT, bg=COLOR_SURFACE, font=self.font_sub, anchor="w")
        lbl_role_tag.pack(side=tk.LEFT, padx=(0, 2))
        lbl_role_tag.bind("<Button-1>", lambda e, rk=role_key: self._select_seat(rk))

        lbl_role_badge = tk.Label(
            r3,
            text="【职能配置】",
            bg=COLOR_SURFACE,
            fg=COLOR_INFO,
            font=self.font_bold,
            relief=tk.FLAT,
            padx=2,
            pady=2,
        )
        lbl_role_badge.pack(side=tk.LEFT, padx=(0, 8))
        lbl_role_badge.bind("<Button-1>", lambda e, rk=role_key: self._select_seat(rk))

        entry_custom = tk.Entry(
            r3,
            bg=COLOR_SURFACE_ALT,
            fg=COLOR_TEXT_SECONDARY,
            insertbackground=COLOR_TEXT_SECONDARY,
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
        drawer_collapsed = tk.Frame(card_right, bg=COLOR_SURFACE)
        drawer_collapsed.pack(fill=tk.BOTH, expand=True)

        btn_tg_config = tk.Button(
            drawer_collapsed,
            text="✈️ TG设置",
            bg=COLOR_PRIMARY,
            fg="#ffffff",
            activebackground=COLOR_PRIMARY_HOVER,
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
            fg=COLOR_TEXT_MUTED,
            bg=COLOR_SURFACE,
            font=self.font_sub,
            cursor="hand2",
        )
        lbl_tg_status.pack(anchor="center", pady=(4, 0))
        lbl_tg_status.bind("<Button-1>", lambda e, rk=role_key: self._expand_tg_drawer(rk))
        self._bind_btn_tooltip(lbl_tg_status, "点击展开右侧 Telegram Bot Token 与战队群快捷设置")

        # State B: Expanded (in-card quick binding panel, +100% width)
        drawer_expanded = tk.Frame(card_right, bg=COLOR_SURFACE_ALT, bd=1, relief=tk.SOLID, padx=12, pady=6)
        # Initially hidden (pack_forget)

        # Drawer Row 1: Header
        dr_head = tk.Frame(drawer_expanded, bg=COLOR_SURFACE_ALT)
        dr_head.pack(fill=tk.X, pady=(0, 4))
        lbl_dr_title = tk.Label(dr_head, text="✈️ TG设置", fg=COLOR_INFO, bg=COLOR_SURFACE_ALT, font=self.font_bold)
        lbl_dr_title.pack(side=tk.LEFT)
        btn_dr_close = tk.Button(
            dr_head,
            text="✖ 收起",
            bg=COLOR_SURFACE,
            fg=COLOR_TEXT_MUTED,
            activebackground=COLOR_CONTROL,
            activeforeground=COLOR_TEXT,
            font=self.font_sub,
            relief=tk.FLAT,
            padx=8,
            pady=1,
            cursor="hand2",
            command=lambda rk=role_key: self._collapse_tg_drawer(rk),
        )
        btn_dr_close.pack(side=tk.RIGHT)

        # Drawer Row 2: Token Input with Eye Toggle (+100% width: width=38)
        dr_tok_row = tk.Frame(drawer_expanded, bg=COLOR_SURFACE_ALT)
        dr_tok_row.pack(fill=tk.X, pady=(0, 4))
        lbl_dr_tok = tk.Label(dr_tok_row, text="Token:", fg=COLOR_TEXT_SECONDARY, bg=COLOR_SURFACE_ALT, font=self.font_sub)
        lbl_dr_tok.pack(side=tk.LEFT, padx=(0, 4))
        entry_dr_tok = tk.Entry(
            dr_tok_row,
            bg=COLOR_SURFACE,
            fg=COLOR_TEXT,
            insertbackground=COLOR_TEXT,
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
            bg=COLOR_CONTROL,
            fg=COLOR_TEXT,
            activebackground=COLOR_CONTROL_HOVER,
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
        dr_act_row = tk.Frame(drawer_expanded, bg=COLOR_SURFACE_ALT)
        dr_act_row.pack(fill=tk.X)
        btn_dr_join = tk.Button(
            dr_act_row,
            text="🚀 加入TG群",
            bg=COLOR_SUCCESS,
            fg="#ffffff",
            activebackground=COLOR_SUCCESS_HOVER,
            activeforeground="#ffffff",
            font=self.font_bold,
            relief=tk.FLAT,
            padx=12,
            pady=3,
            cursor="hand2",
            command=lambda rk=role_key: self._drawer_join_tg(rk),
        )
        btn_dr_join.pack(side=tk.LEFT, padx=(0, 8))
        lbl_dr_status = tk.Label(dr_act_row, text="⚪ 待配置", fg=COLOR_TEXT_MUTED, bg=COLOR_SURFACE_ALT, font=self.font_sub)
        lbl_dr_status.pack(side=tk.LEFT)

        self.widgets[role_key] = {
            "name": entry_name,
            "lbl_bot_badge": lbl_bot_badge,
            "engine": engine_adapter,
            "btn_engine": btn_engine,
            "btn_track": btn_track,
            "btn_open_web": None,
            "btn_ext_guide": btn_ext_guide,
            "btn_chat_check": btn_chat_check,
            "btn_tg_config": btn_tg_config,
            "btn_tg_capsule": btn_tg_config,
            "lbl_tg_status": lbl_tg_status,
            "lbl_env_tag": lbl_env_tag,
            "env": entry_env,
            "token": entry_token,
            "hint": lbl_hint,
            "user": entry_user,
            "role_badge": lbl_role_badge,
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
            self.btn_tab_code.config(bg=COLOR_PRIMARY, fg="#ffffff", highlightbackground=COLOR_PRIMARY)
            self.btn_tab_chat.config(bg=COLOR_TAB_IDLE, fg=COLOR_TEXT, highlightbackground=COLOR_TAB_BORDER)
            self.tab_frame_chat.pack_forget()
            self.tab_frame_code.pack(fill=tk.BOTH, expand=True)
            if self.selected_role not in ("lead", "builder"):
                self._select_seat("lead")
            else:
                self._select_seat(self.selected_role)
        else:
            self.btn_tab_code.config(bg=COLOR_TAB_IDLE, fg=COLOR_TEXT, highlightbackground=COLOR_TAB_BORDER)
            self.btn_tab_chat.config(bg=COLOR_PRIMARY, fg="#ffffff", highlightbackground=COLOR_PRIMARY)
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
        tw.configure(bg=COLOR_SURFACE_ALT, bd=1, relief=tk.SOLID)
        tw.geometry(f"+{x + 12}+{y + 16}")

        frame = tk.Frame(tw, bg=COLOR_SURFACE_ALT, padx=10, pady=8)
        frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(
            frame,
            text=text,
            fg=COLOR_INFO,
            bg=COLOR_SURFACE_ALT,
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
                    highlightbackground=COLOR_INFO,
                    highlightcolor=COLOR_INFO,
                    highlightthickness=2,
                    text=f" {base_title} [⭐ 当前选中编排] ",
                )
            else:
                card.config(
                    highlightbackground=COLOR_CONTROL,
                    highlightcolor=COLOR_CONTROL,
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
            w["role_badge"].config(text="【✏️ 自定义】", bg=COLOR_SURFACE, fg=COLOR_TEXT_MUTED)
            w["custom_entry"].pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
            w["custom_entry"].focus_set()
        else:
            # Presets 1-3 are strictly mutually exclusive: if another seat already holds this preset, downgrade it to custom
            for other_role, other_w in self.widgets.items():
                if other_role != target_role:
                    if not other_w["is_custom"] and other_w["desc_var"].get() == preset["desc_text"]:
                        other_w["is_custom"] = True
                        other_w["desc_var"].set("")
                        other_w["role_badge"].config(text="【✏️ 自定义】", bg=COLOR_SURFACE, fg=COLOR_TEXT_MUTED)
                        other_w["custom_entry"].delete(0, tk.END)
                        other_w["custom_entry"].pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

            w = self.widgets[target_role]
            w["is_custom"] = False
            w["desc_var"].set(preset["desc_text"])
            w["role_badge"].config(text=f"【{preset['btn_label']}】", bg=COLOR_SURFACE, fg=COLOR_INFO)
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
            self.entry_cw.config(state="normal", bg=COLOR_SURFACE, fg=COLOR_TEXT)
        else:
            self.entry_cw.config(state="disabled", bg=COLOR_SURFACE_ALT, fg=COLOR_TEXT_SOFT)

    def _open_chat_ai_webpage(self):
        """Open the webpage for the currently selected Chat AI engine (Item 2)."""
        w = self.widgets.get("chat")
        eng = (w["engine"].get().strip().lower() if w else "") or "chatgpt"
        urls = {
            "chatgpt": "https://chatgpt.com",
            "gemini": "https://gemini.google.com",
            "claude": "https://claude.ai",
            "deepseek": "https://chat.deepseek.com",
            "kimi": "https://kimi.moonshot.cn",
            "qwen": "https://chat.qwen.ai",
        }
        url = urls.get(eng, "https://chatgpt.com")
        try:
            webbrowser.open(url)
            show_floating_toast(
                parent=self,
                title="已打开 AI 网页",
                message=f"🌐 已在浏览器中打开 {eng.upper()} 对话页面，请确保处于登录状态！",
                duration_ms=2200,
            )
        except Exception as e:
            messagebox.showerror("打开网页失败", f"无法打开浏览器: {e}", parent=self)

    def _ensure_and_seed_extension_token(self, ext_path: Path | None = None) -> str:
        """Ensure a valid 18765 bridge bearer token exists and seed seed_token.json using unified SSOT helper."""
        from pocketfleet.bridge_server import seed_extension_token
        return seed_extension_token(repo_root=REPO_ROOT, ext_path=ext_path)

    def _open_browser_extension_guide(self):
        """Locate and open the browser extension directory and guide the user through installation (Item 1)."""
        # Resolve project root dynamically across frozen exe and source runtime
        candidates = []
        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).resolve().parent
            candidates.extend([
                exe_dir.parent / "browser-extension",
                exe_dir / "browser-extension",
                exe_dir / "assets" / "browser-extension",
                Path(getattr(sys, "_MEIPASS", "")) / "assets" / "browser-extension",
            ])
        candidates.extend([
            REPO_ROOT / "browser-extension",
            REPO_ROOT / "assets" / "browser-extension",
            Path.cwd() / "browser-extension",
            Path(__file__).resolve().parent.parent.parent / "browser-extension",
        ])

        ext_path = None
        for c in candidates:
            if c and c.is_dir() and (c / "manifest.json").is_file():
                ext_path = c.resolve()
                break

        if not ext_path:
            ext_path = (REPO_ROOT / "browser-extension").resolve()
            ext_path.mkdir(parents=True, exist_ok=True)

        # Automatically seed the token into extension directory so user never needs manual entry
        self._ensure_and_seed_extension_token(ext_path)

        try:
            self.clipboard_clear()
            self.clipboard_append(str(ext_path))
        except Exception:
            pass

        try:
            os.startfile(str(ext_path))
        except Exception:
            pass

        msg = (
            "【🧩 PocketFleet 浏览器扩展安装向导】\n\n"
            f"已为您在文件资源管理器中打开扩展所在目录（路径已自动复制到剪贴板）：\n"
            f"👉 {ext_path}\n\n"
            "请按以下 4 步在 Chrome / Edge 中完成加载：\n"
            "1. 打开 Chrome 浏览器，在地址栏输入：chrome://extensions\n"
            "2. 开启右上角【开发者模式】(Developer Mode) 开关；\n"
            "3. 点击左上角【加载已解压的扩展程序】(Load unpacked)；\n"
            "4. 选择已打开的文件夹即可完成安装！\n\n"
            "✨ 零配置免填：安装包已自动植入鉴权令牌 (seed_token.json)，插件加载后自动握手连接，无需手动配置！"
        )
        messagebox.showinfo("浏览器扩展安装指引", msg, parent=self)

    def _verify_chat_ai_bridge(self):
        """Verify the Chat AI bridge without producing browser side effects."""
        w = self.widgets.get("chat")
        eng_raw = (w["engine"].get().strip().lower() if w else "") or "chatgpt"
        eng = eng_raw.upper()

        # Refresh the extension token before checking local bridge state.
        self._ensure_and_seed_extension_token()

        # Check ports and auto-start the local bridge daemon if needed.
        is_bridge_listening = is_port_listening(18765)
        if not is_bridge_listening:
            try:
                from pocketfleet.bridge_server import ensure_bridge_server_running
                ensure_bridge_server_running()
                import time
                time.sleep(0.08)
                is_bridge_listening = is_port_listening(18765)
            except Exception:
                pass

        is_cockpit_listening = is_port_listening(8765)

        if is_bridge_listening:
            active_info = ""
            has_active_sessions = False
            try:
                import urllib.request
                import json
                req = urllib.request.Request("http://127.0.0.1:18765/api/v1/sessions", headers={"User-Agent": "PocketFleet"})
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        sessions = data.get("active_clients") or data.get("sessions") or []
                        if sessions:
                            has_active_sessions = True
                            active_info = f"\n• 检测到活跃会话: {', '.join(str(s) for s in sessions)}"
            except Exception:
                pass

            if has_active_sessions:
                status_title = "对话席位自检: 全链路就绪"
                status_body = (
                    f"✅ 本地网关与浏览器会话全链路连接成功 (Port 18765 已连接)！\n\n"
                    f"• 本地 Web 网关: Port 18765 运行中{active_info}\n"
                    f"• 鉴权令牌: 已自动植入扩展并握手验证\n"
                    f"• 当前席位引擎: {eng}\n"
                    f"• 您可在 TG 战队群中 @Bot 发送测试消息验证端到端回传。"
                )
            else:
                status_title = "对话席位自检: 本地网关已就绪 (等待页面接入)"
                status_body = (
                    f"ℹ️ 本地网关运行正常 (Port 18765 已连接)。\n\n"
                    f"• 状态分层: 本机网关已就绪，当前正在等待浏览器扩展或 AI 对话页面接入；\n"
                    f"• 请确认：\n"
                    f"  1. 已在 Chrome / Edge 中安装加载扩展；\n"
                    f"  2. 已打开的 {eng} 对话页面处于登录状态；\n"
                    f"• 扩展将在检测到 AI 页面后自动建立通信链路，无需手动配置！"
                )

            messagebox.showinfo(status_title, status_body, parent=self)
        elif is_cockpit_listening:
            messagebox.showinfo(
                "Web Cockpit 就绪 (未检测到 18765 扩展网关)",
                f"ℹ️ PocketFleet 本地 Cockpit (Port 8765) 运行正常。\n\n"
                f"若对话席位需使用浏览器 Web 会话（如 ChatGPT/Gemini）：\n"
                f"1. 请确认已点击【🧩 浏览器扩展】安装 Chrome 扩展；\n"
                f"2. 请在已打开的 {eng} 页面中保持登录；\n"
                f"3. 扩展将自动与本地服务建立连接。",
                parent=self,
            )
        else:
            messagebox.showwarning(
                "本地服务尚未启动",
                "⚠️ 本地网关服务尚未启动。\n\n"
                "请在 PocketFleet 主控制面板中点击【🚀 Start All Services】启动服务，"
                "启动后服务将自动就绪。",
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
                    w["drawer_status_lbl"].config(text="🟢 Token已就位", fg=COLOR_SUCCESS)
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
            w["drawer_status_lbl"].config(text="❌ 请输入Token", fg=COLOR_DANGER)
            messagebox.showerror("缺少 Token", "请先填入 Telegram Bot Token！", parent=self)
            return

        w["drawer_status_lbl"].config(text="🔄 验证Token...", fg=COLOR_WARNING)
        self.update_idletasks()

        ver_res = verify_bot_token(token)
        ok, bot_id, uname, err = ver_res[0], ver_res[1], ver_res[2], ver_res[3]
        if not ok or not bot_id:
            w["drawer_status_lbl"].config(text=f"❌ {err or '无效'}", fg=COLOR_DANGER)
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
                    w["drawer_status_lbl"].config(text="🟢 已连接战队群", fg=COLOR_SUCCESS)
                    self._update_seat_tg_capsule(role_key)
                    self._update_global_group_banner()
                    self._collapse_tg_drawer(role_key)
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

        w["drawer_status_lbl"].config(text="📡 正在监听加群...", fg=COLOR_WARNING)
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
                    w["drawer_status_lbl"].config(text="🟢 已连接战队群", fg=COLOR_SUCCESS)
                broker.stop_temporary_poller()
                self._collapse_tg_drawer(role_key)
                if self.on_save_callback:
                    self.on_save_callback()
                return
            elif ev_type == "CONFLICT_409":
                w = self.widgets.get(role_key, {})
                if "drawer_status_lbl" in w:
                    w["drawer_status_lbl"].config(text="⚠️ 端口/Bot冲突(409)", fg=COLOR_DANGER)
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
            w["lbl_tg_status"].config(text="🟢 已就位", fg=COLOR_SUCCESS)
            if "drawer_status_lbl" in w:
                w["drawer_status_lbl"].config(text="🟢 已就位", fg=COLOR_SUCCESS)
            bot_name = w["name"].get().strip()
            clean_u = user if (user.startswith("@") or not user) else f"@{user}"
            if bot_name:
                badge_text = f"🏷️ {bot_name} ({clean_u})" if clean_u else f"🏷️ {bot_name}"
            else:
                badge_text = f"🏷️ {clean_u}" if clean_u else ""
            if "lbl_bot_badge" in w:
                w["lbl_bot_badge"].config(text=badge_text, fg=COLOR_INFO)
        else:
            w["lbl_tg_status"].config(text="⚪ 待配置", fg=COLOR_TEXT_MUTED)
            if "drawer_status_lbl" in w:
                w["drawer_status_lbl"].config(text="⚪ 待配置", fg=COLOR_TEXT_MUTED)
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
                fg=COLOR_INFO,
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
                    fg=COLOR_WARNING,
                )
            else:
                self.lbl_global_group.config(
                    text="📢 Telegram 协同战队群: 尚未配置 (点击任一席位的 TG 状态按钮进行配置/打卡)",
                    fg=COLOR_TEXT_MUTED,
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
        self.current_cfg = cfg
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
                    w["role_badge"].config(text=f"【{p['btn_label']}】", bg=COLOR_SURFACE, fg=COLOR_INFO)
                    w["custom_entry"].pack_forget()
                    used_preset_texts.add(p["desc_text"])
                    matched = True
                    break
            if not matched:
                w["desc_var"].set(desc)
                w["is_custom"] = True
                w["role_badge"].config(text="【✏️ 自定义】", bg=COLOR_SURFACE, fg=COLOR_TEXT_MUTED)
                w["custom_entry"].delete(0, tk.END)
                w["custom_entry"].insert(0, desc)
                w["custom_entry"].pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

            # Masked token and last 4 characters hint (never full plaintext echo)
            env_val = (seat.get_token() or os.environ.get(seat.bot_token_env) or "").strip()
            w["token"].delete(0, tk.END)
            if env_val:
                masked = "••••••••" + (env_val[-4:] if len(env_val) >= 4 else env_val)
                w["token"].insert(0, masked)
                hint_str = f"末4位: ...{env_val[-4:]}" if len(env_val) >= 4 else "已设置"
                w["hint"].config(text=hint_str, fg=COLOR_SUCCESS)
                if "drawer_token_entry" in w:
                    w["drawer_token_entry"].delete(0, tk.END)
                    w["drawer_token_entry"].insert(0, env_val)
                if "drawer_status_lbl" in w:
                    w["drawer_status_lbl"].config(text="🟢 Token已就位", fg=COLOR_SUCCESS)
            else:
                w["hint"].config(text="未配置", fg=COLOR_DANGER)
                if "drawer_status_lbl" in w:
                    w["drawer_status_lbl"].config(text="⚪ 待配置", fg=COLOR_TEXT_MUTED)

            self._update_seat_tg_capsule(role_key)

        if hasattr(self, "sentinel_enabled_var"):
            self.sentinel_enabled_var.set(getattr(cfg, "chat_sentinel_enabled", False))
        if hasattr(self, "sentinel_interval_var"):
            self.sentinel_interval_var.set(str(getattr(cfg, "chat_sentinel_interval", 15)))

        self._update_global_group_banner()
        self._refresh_memo()
        self._auto_detect_human_nickname()

    def _copy_memo_to_clipboard(self):
        text = self.txt_memo.get("1.0", tk.END).strip()
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()

        orig_text = "📋 一键复制 Memo (Copy)"
        self.btn_copy_memo.configure(text="✅ 已复制到剪贴板！", bg=COLOR_SUCCESS)
        self.after(2000, lambda: self.btn_copy_memo.configure(text=orig_text, bg=COLOR_PRIMARY))

        show_floating_toast(
            parent=self,
            title="Memo 已复制",
            message="📋 战队协同交互 Memo 已成功复制到剪贴板！",
            duration_ms=1800,
        )

    def _refresh_memo(self):
        human = getattr(self, "human_name", "ENTJ指挥官")
        if not human:
            human = "人类用户"

        participants = []
        for r_key in ["lead", "builder", "chat"]:
            w = self.widgets.get(r_key)
            if not w:
                continue
            bot_tag = w["user"].get().strip() if "user" in w else ""
            if bot_tag:
                if not bot_tag.startswith("@"):
                    bot_tag = "@" + bot_tag
                if bot_tag not in participants:
                    participants.append(bot_tag)
            else:
                name = w["name"].get().strip()
                if name and name not in participants:
                    participants.append(name)

        if not participants:
            participants = ["@AiSoulJudgeBot", "@AiSoulMudSnakeBot", "@AiSoulAlphaSandboxBot"]

        p_str = ";".join(participants)
        memo_line1 = f"[来自TG多AI协作];[人类用户:{human}];参与者:[{p_str}]"
        memo_line2 = "回复格式要求:以 [Telegram]re:{someone} 或 [Telegram][mailto:{someone}] 为开头（指明单一收件人）。"
        memo_content = f"{memo_line1}\n{memo_line2}"

        if hasattr(self, "txt_memo"):
            self.txt_memo.configure(state=tk.NORMAL)
            self.txt_memo.delete("1.0", tk.END)
            self.txt_memo.insert("1.0", memo_content)
            self.txt_memo.configure(state=tk.DISABLED)

    def _auto_detect_human_nickname(self):
        # 1. StateStore cached sender name
        if hasattr(self, "mgr") and hasattr(self.mgr, "state_store") and self.mgr.state_store:
            cached = self.mgr.state_store.get_meta("last_human_sender_name", "")
            if cached.strip():
                self._apply_detected_human_name(cached.strip(), "从本地历史同步")
                return

        # 2. Environment
        env_human = (os.environ.get("POCKETFLEET_HUMAN_NAME") or "").strip()
        if env_human:
            self._apply_detected_human_name(env_human, "环境变量指定")
            return

        # 3. Query group administrators via Telegram API in background thread
        chat_id = self.global_chat_id or (os.environ.get("TELEGRAM_GROUP_ID") or "").strip()
        tok = None
        for r_key in ["lead", "builder", "chat"]:
            w = self.widgets.get(r_key)
            if w and w["token"].get().strip():
                t = w["token"].get().strip()
                if not t.startswith("••••"):
                    tok = t
                    break

        if not tok:
            for env_var in ["TELEGRAM_BOT_JUDGE_TOKEN", "TELEGRAM_BOT_LEAD_TOKEN", "TELEGRAM_BOT_MUDSNAKE_TOKEN", "POCKETFLEET_BOT_TOKEN"]:
                val = (os.environ.get(env_var) or "").strip()
                if val:
                    tok = val
                    break

        if tok and chat_id:
            def _fetch():
                try:
                    from .transport.telegram import fetch_group_human_nickname
                    fname = fetch_group_human_nickname(tok, chat_id)
                    if fname:
                        self.after(0, lambda: self._apply_detected_human_name(fname, "自动从TG群组获取"))
                        if hasattr(self, "mgr") and hasattr(self.mgr, "state_store") and self.mgr.state_store:
                            self.mgr.state_store.set_meta("last_human_sender_name", fname)
                except Exception:
                    pass

            threading.Thread(target=_fetch, daemon=True).start()

    def _apply_detected_human_name(self, name: str, source_label: str = ""):
        if name:
            self.human_name = name.strip()
            if hasattr(self, "lbl_human_display"):
                self.lbl_human_display.configure(text=f"👤 关联人类: {self.human_name} (从TG群自动获取)")
            self._refresh_memo()

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

            # Token and ENV resolution: allow direct token or auto-fill env_var
            tok_dr = ""
            if "drawer_token_entry" in w:
                tok_dr = w["drawer_token_entry"].get().strip()
            effective_tok = tok_dr or token_val

            if ":" in env_var:
                if not effective_tok:
                    effective_tok = env_var
                env_var = f"TELEGRAM_BOT_{role_key.upper()}_TOKEN"
                w["env"].delete(0, tk.END)
                w["env"].insert(0, env_var)
            elif not env_var:
                env_var = f"TELEGRAM_BOT_{role_key.upper()}_TOKEN"

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

            # Direct token persistence: store token into seat config, sync to process env, best-effort .env
            seat_bot_token = ""
            if effective_tok and not effective_tok.startswith("•") and not effective_tok.endswith("••••"):
                seat_bot_token = effective_tok
                if env_var:
                    os.environ[env_var] = effective_tok
                try:
                    env_file = REPO_ROOT / ".env"
                    save_token_to_env(effective_tok, env_path=env_file, var_name=env_var)
                except Exception:
                    pass
            else:
                old_seat = self.current_cfg.seats.get(role_key) if getattr(self, "current_cfg", None) else None
                seat_bot_token = old_seat.bot_token if old_seat else ""

            seat_cfg = SeatConfig(
                role=role_key,
                name=name,
                engine=engine,
                bot_token_env=env_var,
                bot_username=user,
                description=desc,
                command=cmd,
                read_watermark=0,
                bot_token=seat_bot_token,
            )
            seats_dict[role_key] = seat_cfg

        sentinel_enabled = self.sentinel_enabled_var.get() if hasattr(self, "sentinel_enabled_var") else False
        try:
            sentinel_interval = int(self.sentinel_interval_var.get().strip()) if hasattr(self, "sentinel_interval_var") else 15
        except ValueError:
            sentinel_interval = 15

        new_fleet_cfg = FleetSeatsConfig(
            seats=seats_dict,
            context_window=cw_val,
            no_situ=True,
            telegram_chat_id=self.global_chat_id,
            telegram_group_name=self.global_group_name,
            sync_context_window=self.var_sync_context.get(),
            chat_sentinel_enabled=sentinel_enabled,
            chat_sentinel_interval=sentinel_interval,
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

        if self.is_dialog:
            self._close_panel()
        else:
            show_floating_toast(
                parent=self,
                title="席位配置保存成功！",
                message="✅ AI 席位与 Telegram 接入配置已成功持久化并热应用生效！\n现在可启动下方网关服务开始协同。",
                icon="💾",
                duration_ms=2800,
            )
        if self.on_save_callback:
            self.on_save_callback()

    def _close_panel(self):
        if self.is_dialog and hasattr(self.parent, "destroy"):
            self.parent.destroy()
        else:
            self.destroy()


class ThreeSeatsConfigDialog(tk.Toplevel):
    """Modal dialog wrapper around ThreeSeatsConfigPanel for backward compatibility and tests."""
    def __init__(self, parent, fleet_mgr, on_save_callback=None):
        super().__init__(parent)
        self.parent = parent
        self.mgr = fleet_mgr
        self.on_save_callback = on_save_callback

        self.title("Fleet Triad Seats Configuration (席位战队编排) — PocketFleet")
        self.geometry("880x840")
        self.configure(bg=COLOR_APP_BG)
        apply_windows_titlebar_theme(self)
        self.transient(parent)
        self.grab_set()

        try:
            self.update_idletasks()
            pw = parent.winfo_width()
            ph = parent.winfo_height()
            px = parent.winfo_rootx()
            py = parent.winfo_rooty()
            cx = max(0, px + (pw - 880) // 2)
            cy = max(0, py + (ph - 840) // 2)
            self.geometry(f"+{cx}+{cy}")
        except Exception:
            pass

        self.panel = ThreeSeatsConfigPanel(
            self,
            fleet_mgr,
            on_save_callback=on_save_callback,
            is_dialog=True,
            show_header=True,
        )
        self.panel.pack(fill=tk.BOTH, expand=True)

    def __getattr__(self, name):
        return getattr(self.panel, name)

    def __setattr__(self, name, value):
        if name in ("parent", "mgr", "on_save_callback", "panel") or "panel" not in self.__dict__:
            super().__setattr__(name, value)
        elif hasattr(self.panel, name):
            setattr(self.panel, name, value)
        else:
            super().__setattr__(name, value)


# ==============================================================================
# Modern Dialogs for Aligned UI (Group Config & AI Connector Management)
# ==============================================================================
class TelegramGroupConfigDialog(tk.Toplevel):
    """Modern modal dialog to configure collaborative Telegram WarRoom group."""
    def __init__(self, parent, fleet_mgr, on_save_callback=None):
        super().__init__(parent)
        self.parent = parent
        self.mgr = fleet_mgr
        self.on_save_callback = on_save_callback

        self.title("配置 Telegram 协同战队群 (Telegram Group Settings)")
        self.geometry("520x360")
        self.resizable(False, False)
        self.configure(bg=COLOR_MODERN_BG)
        apply_windows_titlebar_theme(self)
        self.transient(parent)
        self.grab_set()

        try:
            self.update_idletasks()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            self.geometry(f"+{max(0, px + (pw - 520) // 2)}+{max(0, py + (ph - 360) // 2)}")
        except Exception:
            pass

        ui_fam = get_ui_font_family()
        font_h = tkfont.Font(family=ui_fam, size=12, weight="bold")
        font_lbl = tkfont.Font(family=ui_fam, size=9, weight="bold")
        font_reg = tkfont.Font(family=ui_fam, size=9)
        font_sub = tkfont.Font(family=ui_fam, size=8)

        card = tk.Frame(self, bg=COLOR_CARD_BG, bd=1, relief=tk.SOLID, padx=20, pady=18)
        card.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

        tk.Label(
            card,
            text="📢 配置 Telegram 协同战队群 (WarRoom)",
            fg=COLOR_TEXT_MAIN,
            bg=COLOR_CARD_BG,
            font=font_h,
            anchor="w",
        ).pack(fill=tk.X)

        tk.Label(
            card,
            text="所有席位将在此群组内接收指令并开展协同会议 (/meet)。",
            fg=COLOR_TEXT_MUTED,
            bg=COLOR_CARD_BG,
            font=font_sub,
            anchor="w",
        ).pack(fill=tk.X, pady=(2, 14))

        seats_cfg = self.mgr.load_seats_config()
        cur_name = seats_cfg.telegram_group_name or "Fleet WarRoom"
        cur_chat_id = seats_cfg.telegram_chat_id or (os.environ.get("TELEGRAM_GROUP_ID") or "")

        # Field 1: Group Name
        tk.Label(card, text="群组显示名称 (Group Name):", fg=COLOR_TEXT_MAIN, bg=COLOR_CARD_BG, font=font_lbl, anchor="w").pack(fill=tk.X)
        self.ent_name = tk.Entry(card, bg=COLOR_INPUT_BG, fg=COLOR_TEXT_MAIN, font=font_reg, relief=tk.FLAT)
        self.ent_name.insert(0, cur_name)
        self.ent_name.pack(fill=tk.X, pady=(3, 10), ipady=3)

        # Field 2: Chat ID
        tk.Label(card, text="群组 Chat ID (通常为负数，如 -1004309197838):", fg=COLOR_TEXT_MAIN, bg=COLOR_CARD_BG, font=font_lbl, anchor="w").pack(fill=tk.X)
        self.ent_chat_id = tk.Entry(card, bg=COLOR_INPUT_BG, fg=COLOR_TEXT_MAIN, font=font_reg, relief=tk.FLAT)
        self.ent_chat_id.insert(0, cur_chat_id)
        self.ent_chat_id.pack(fill=tk.X, pady=(3, 16), ipady=3)

        # Actions
        btn_bar = tk.Frame(card, bg=COLOR_CARD_BG)
        btn_bar.pack(fill=tk.X, side=tk.BOTTOM)

        btn_cancel = tk.Button(
            btn_bar,
            text="取消",
            bg=COLOR_CONTROL,
            fg=COLOR_TEXT_MAIN,
            font=font_reg,
            relief=tk.FLAT,
            padx=14,
            pady=4,
            cursor="hand2",
            command=self.destroy,
        )
        btn_cancel.pack(side=tk.RIGHT, padx=(6, 0))

        btn_save = tk.Button(
            btn_bar,
            text="保存并应用",
            bg=COLOR_ACCENT_GREEN,
            fg="#ffffff",
            activebackground=COLOR_ACCENT_GREEN_HOVER,
            activeforeground="#ffffff",
            font=font_lbl,
            relief=tk.FLAT,
            padx=18,
            pady=4,
            cursor="hand2",
            command=self._save,
        )
        btn_save.pack(side=tk.RIGHT)

    def _save(self):
        new_name = self.ent_name.get().strip() or "Fleet WarRoom"
        new_chat_id = self.ent_chat_id.get().strip()
        try:
            seats_cfg = self.mgr.load_seats_config()
            seats_cfg.telegram_group_name = new_name
            seats_cfg.telegram_chat_id = new_chat_id
            self.mgr.save_seats_config(seats_cfg)

            if new_chat_id:
                try:
                    save_token_to_env(new_chat_id, env_path=REPO_ROOT / ".env", var_name="TELEGRAM_GROUP_ID")
                    os.environ["TELEGRAM_GROUP_ID"] = new_chat_id
                except Exception as env_e:
                    logger.warning("Failed saving TELEGRAM_GROUP_ID: %s", env_e)

            show_floating_toast(self.parent, "群组设置已更新", f"协同战队群已配置为: {new_name} ({new_chat_id})")
            if self.on_save_callback:
                self.on_save_callback()
            self.destroy()
        except Exception as e:
            messagebox.showerror("保存失败", f"无法保存群组配置:\n{e}", parent=self)


class AddOrEditAiDialog(tk.Toplevel):
    """Modern modal dialog to add or edit an AI connector (Code or Chat)."""
    def __init__(
        self,
        parent,
        fleet_mgr,
        role_key: Optional[str] = None,
        default_category: str = "code",
        preset_engine: Optional[str] = None,
        preset_name: Optional[str] = None,
        on_save_callback=None,
    ):
        super().__init__(parent)
        self.parent = parent
        self.mgr = fleet_mgr
        self.role_key = role_key
        self.category = default_category
        self.preset_engine = preset_engine
        self.preset_name = preset_name
        self.on_save_callback = on_save_callback

        is_edit = bool(role_key)
        self.title(f"{'管理' if is_edit else '接入'} AI 席位 ({'Edit' if is_edit else 'Add'} AI Connector) — PocketFleet")
        self.geometry("580x570")
        self.resizable(False, False)
        self.configure(bg=COLOR_MODERN_BG)
        apply_windows_titlebar_theme(self)
        self.transient(parent)
        self.grab_set()

        try:
            self.update_idletasks()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            self.geometry(f"+{max(0, px + (pw - 580) // 2)}+{max(0, py + (ph - 570) // 2)}")
        except Exception:
            pass

        ui_fam = get_ui_font_family()
        font_h = tkfont.Font(family=ui_fam, size=12, weight="bold")
        font_lbl = tkfont.Font(family=ui_fam, size=9, weight="bold")
        font_reg = tkfont.Font(family=ui_fam, size=9)
        font_sub = tkfont.Font(family=ui_fam, size=8)
        font_mono = tkfont.Font(family="Consolas", size=9)

        card = tk.Frame(self, bg=COLOR_CARD_BG, bd=1, relief=tk.SOLID, padx=20, pady=16)
        card.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

        title_text = f"⚙️ 管理 AI 席位" if is_edit else f"➕ 接入新 AI 席位 ({'代码 AI' if self.category == 'code' else '对话 AI'})"
        tk.Label(card, text=title_text, fg=COLOR_TEXT_MAIN, bg=COLOR_CARD_BG, font=font_h, anchor="w").pack(fill=tk.X)

        tk.Label(
            card,
            text="配置该席位的专属 Telegram Bot 接入信息与本地/远程执行引擎。",
            fg=COLOR_TEXT_MUTED,
            bg=COLOR_CARD_BG,
            font=font_sub,
            anchor="w",
        ).pack(fill=tk.X, pady=(2, 12))

        # Load existing seat if edit mode
        seats_cfg = self.mgr.load_seats_config()
        seat = seats_cfg.seats.get(role_key) if (is_edit and role_key) else None

        default_name = preset_name or ("TechLead" if self.category == "code" else "Advisor")
        init_name = seat.name if seat else default_name
        init_user = seat.bot_username if seat else ""
        init_token = (seat.get_token() if seat else "") or ""
        default_engine = preset_engine or ("antigravity" if self.category == "code" else "openai")
        init_engine = seat.engine if seat else default_engine

        # 1. AI Name
        tk.Label(card, text="代号 / 昵称 (Name):", fg=COLOR_TEXT_MAIN, bg=COLOR_CARD_BG, font=font_lbl, anchor="w").pack(fill=tk.X)
        self.ent_name = tk.Entry(card, bg=COLOR_INPUT_BG, fg=COLOR_TEXT_MAIN, font=font_reg, relief=tk.FLAT)
        self.ent_name.insert(0, init_name)
        self.ent_name.pack(fill=tk.X, pady=(2, 8), ipady=3)

        # 2. Category selection (if new)
        cat_frame = tk.Frame(card, bg=COLOR_CARD_BG)
        cat_frame.pack(fill=tk.X, pady=(0, 8))
        tk.Label(cat_frame, text="AI 类别:", fg=COLOR_TEXT_MAIN, bg=COLOR_CARD_BG, font=font_lbl).pack(side=tk.LEFT, padx=(0, 10))

        self.cat_var = tk.StringVar(value=self.category)
        rb_code = tk.Radiobutton(cat_frame, text="</> 代码 AI (Code AI)", variable=self.cat_var, value="code", bg=COLOR_CARD_BG, fg=COLOR_TEXT_MAIN, font=font_reg, command=self._on_category_changed)
        rb_chat = tk.Radiobutton(cat_frame, text="💬 对话 AI (Chat AI)", variable=self.cat_var, value="chat", bg=COLOR_CARD_BG, fg=COLOR_TEXT_MAIN, font=font_reg, command=self._on_category_changed)
        rb_code.pack(side=tk.LEFT, padx=4)
        rb_chat.pack(side=tk.LEFT, padx=4)

        # 3. Execution Engine
        tk.Label(card, text="执行引擎 (Engine):", fg=COLOR_TEXT_MAIN, bg=COLOR_CARD_BG, font=font_lbl, anchor="w").pack(fill=tk.X)
        self.cbo_engine = ttk.Combobox(card, state="readonly", font=font_reg)
        self._populate_engines(init_engine)
        self.cbo_engine.pack(fill=tk.X, pady=(2, 8), ipady=2)

        # 4. Telegram Bot Username
        tk.Label(card, text="Telegram 用户名 (@Username):", fg=COLOR_TEXT_MAIN, bg=COLOR_CARD_BG, font=font_lbl, anchor="w").pack(fill=tk.X)
        self.ent_user = tk.Entry(card, bg=COLOR_INPUT_BG, fg=COLOR_TEXT_MAIN, font=font_reg, relief=tk.FLAT)
        self.ent_user.insert(0, init_user)
        self.ent_user.pack(fill=tk.X, pady=(2, 8), ipady=3)

        # 5. Telegram Bot Token
        tok_bar = tk.Frame(card, bg=COLOR_CARD_BG)
        tok_bar.pack(fill=tk.X)
        tk.Label(tok_bar, text="Bot Token (HTTP API):", fg=COLOR_TEXT_MAIN, bg=COLOR_CARD_BG, font=font_lbl).pack(side=tk.LEFT)

        btn_eye = tk.Button(tok_bar, text="👁️ 显示", bg=COLOR_CARD_BG, fg=COLOR_ACCENT_BLUE, font=font_sub, relief=tk.FLAT, padx=2, cursor="hand2")
        btn_eye.pack(side=tk.RIGHT)

        self.ent_token = tk.Entry(card, bg=COLOR_INPUT_BG, fg=COLOR_TEXT_MAIN, font=font_mono, relief=tk.FLAT, show="•")
        self.ent_token.insert(0, init_token)
        self.ent_token.pack(fill=tk.X, pady=(2, 6), ipady=3)

        def _toggle_token():
            if self.ent_token.cget("show") == "•":
                self.ent_token.config(show="")
                btn_eye.config(text="🙈 隐藏")
            else:
                self.ent_token.config(show="•")
                btn_eye.config(text="👁️ 显示")
        btn_eye.config(command=_toggle_token)

        # Token Verification Button & Status Label
        test_bar = tk.Frame(card, bg=COLOR_CARD_BG)
        test_bar.pack(fill=tk.X, pady=(2, 14))

        self.btn_test = tk.Button(
            test_bar,
            text="🔗 测试连接 (Verify Token)",
            bg=COLOR_ACCENT_BLUE_BG,
            fg=COLOR_ACCENT_BLUE,
            font=font_reg,
            relief=tk.FLAT,
            padx=10,
            pady=2,
            cursor="hand2",
            command=self._test_connection,
        )
        self.btn_test.pack(side=tk.LEFT)

        self.lbl_test_result = tk.Label(test_bar, text="", bg=COLOR_CARD_BG, font=font_sub)
        self.lbl_test_result.pack(side=tk.LEFT, padx=10)

        # Bottom Action Bar
        btn_bar = tk.Frame(card, bg=COLOR_CARD_BG)
        btn_bar.pack(fill=tk.X, side=tk.BOTTOM)

        btn_cancel = tk.Button(
            btn_bar,
            text="取消",
            bg=COLOR_CONTROL,
            fg=COLOR_TEXT_MAIN,
            font=font_reg,
            relief=tk.FLAT,
            padx=14,
            pady=4,
            cursor="hand2",
            command=self.destroy,
        )
        btn_cancel.pack(side=tk.RIGHT, padx=(6, 0))

        btn_save = tk.Button(
            btn_bar,
            text="保存并生效",
            bg=COLOR_ACCENT_GREEN,
            fg="#ffffff",
            activebackground=COLOR_ACCENT_GREEN_HOVER,
            activeforeground="#ffffff",
            font=font_lbl,
            relief=tk.FLAT,
            padx=18,
            pady=4,
            cursor="hand2",
            command=self._save,
        )
        btn_save.pack(side=tk.RIGHT)

    def _populate_engines(self, selected_engine: str = ""):
        cur_cat = self.cat_var.get()
        if cur_cat == "code":
            engines = ["antigravity", "codex", "claude_code", "cursor", "opencode"]
        else:
            engines = ["openai", "gemini", "claude", "deepseek", "groq"]
        self.cbo_engine["values"] = engines
        if selected_engine in engines:
            self.cbo_engine.set(selected_engine)
        else:
            self.cbo_engine.set(engines[0])

    def _on_category_changed(self):
        self._populate_engines()

    def _test_connection(self):
        token = self.ent_token.get().strip()
        if not token:
            self.lbl_test_result.config(text="🔴 请先填写 Token", fg=COLOR_DANGER)
            return

        self.lbl_test_result.config(text="⏳ 正在探测 Telegram API...", fg=COLOR_TEXT_MUTED)
        self.btn_test.config(state=tk.DISABLED)

        def _do_test():
            valid, info = verify_bot_token(token)
            def _update():
                self.btn_test.config(state=tk.NORMAL)
                if valid:
                    u = info.get("username", "")
                    self.lbl_test_result.config(text=f"🟢 验证成功: @{u}", fg=COLOR_ACCENT_GREEN)
                    if u and not self.ent_user.get().strip():
                        self.ent_user.delete(0, tk.END)
                        self.ent_user.insert(0, f"@{u}")
                else:
                    err = info.get("error", "连接失败")
                    self.lbl_test_result.config(text=f"🔴 {err}", fg=COLOR_DANGER)
            self.after(0, _update)

        threading.Thread(target=_do_test, daemon=True).start()

    def _save(self):
        name = self.ent_name.get().strip()
        user = self.ent_user.get().strip()
        token = self.ent_token.get().strip()
        engine = self.cbo_engine.get().strip()
        cat = self.cat_var.get()

        if not name:
            messagebox.showwarning("提示", "AI 席位名称不能为空！", parent=self)
            return

        if user and not user.startswith("@"):
            user = "@" + user

        try:
            seats_cfg = self.mgr.load_seats_config()
            target_key = self.role_key

            if not target_key:
                # Generate a unique role_key
                import re
                base_slug = re.sub(r"[^a-zA-Z0-9_]+", "", name.lower()) or cat
                target_key = base_slug
                idx = 1
                while target_key in seats_cfg.seats:
                    target_key = f"{base_slug}_{idx}"
                    idx += 1

            env_var = f"TELEGRAM_BOT_{target_key.upper()}_TOKEN"
            seat_obj = seats_cfg.seats.get(target_key)
            if seat_obj:
                seat_obj.name = name
                seat_obj.engine = engine
                seat_obj.bot_username = user
                seat_obj.bot_token = token
            else:
                seats_cfg.seats[target_key] = SeatConfig(
                    role=target_key,
                    name=name,
                    engine=engine,
                    bot_token_env=env_var,
                    bot_username=user,
                    description=f"{name} 席位",
                    command=get_default_command_for_engine(engine) or "auto",
                    read_watermark=0,
                    bot_token=token,
                )

            if token:
                try:
                    save_token_to_env(token, env_path=REPO_ROOT / ".env", var_name=env_var)
                    os.environ[env_var] = token
                except Exception as env_err:
                    logger.warning("Failed saving token to .env for %s: %s", target_key, env_err)

            validate_seats_config(seats_cfg)
            self.mgr.save_seats_config(seats_cfg)

            show_floating_toast(self.parent, "席位已保存", f"AI 席位 [{name}] 已就绪并生效。")
            if self.on_save_callback:
                self.on_save_callback()
            self.destroy()
        except Exception as e:
            messagebox.showerror("保存失败", f"无法保存席位配置:\n{e}", parent=self)


# ==============================================================================
# Modern Floating Toast Notification
# ==============================================================================
def show_floating_toast(
    parent: tk.Misc,
    title: str,
    message: str = "",
    icon: str = "🎉",
    duration_ms: int = 2800,
    bg_color: str = COLOR_SURFACE_ALT,
    border_color: str = COLOR_SUCCESS,
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
            fg=COLOR_TEXT_SECONDARY,
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
        self.configure(bg=COLOR_APP_BG)
        apply_windows_titlebar_theme(self)
        self.transient(parent)
        self.grab_set()

        ui_fam = get_ui_font_family()
        self.font_title = tkfont.Font(family=ui_fam, size=13, weight="bold")
        self.font_sub = tkfont.Font(family=ui_fam, size=9)
        self.font_bold = tkfont.Font(family=ui_fam, size=9, weight="bold")
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
        header = tk.Frame(self, bg=COLOR_TITLE_BAR, padx=20, pady=12)
        header.pack(fill=tk.X)
        tk.Label(
            header,
            text="🧭 Antigravity 对话轨发现与一键绑定 (Track Selector)",
            fg=COLOR_TITLE_TEXT,
            bg=COLOR_TITLE_BAR,
            font=self.font_title,
        ).pack(anchor="w")

        self.lbl_bound_status = tk.Label(
            header,
            text="当前绑定的对话轨: 正在检查...",
            fg=COLOR_TITLE_SUBTEXT,
            bg=COLOR_TITLE_BAR,
            font=self.font_sub,
        )
        self.lbl_bound_status.pack(anchor="w", pady=(3, 0))

        # Compact instruction bar (replaces cumbersome 3-step guide box)
        tip_bar = tk.Frame(self, bg=COLOR_SURFACE, padx=16, pady=6)
        tip_bar.pack(fill=tk.X, padx=16, pady=(10, 6))
        tk.Label(
            tip_bar,
            text="💡 提示：鼠标悬停在任意行上可预览完整末轮对话。选中目标会话后（包括 IDE 正在进行的对话），直接点击下方【🔗 绑定为对话轨】即可一键秒级绑定！",
            fg=COLOR_INFO,
            bg=COLOR_SURFACE,
            font=self.font_sub,
        ).pack(anchor="w")

        # Treeview table Frame
        table_frame = tk.Frame(self, bg=COLOR_APP_BG, padx=16, pady=4)
        table_frame.pack(fill=tk.BOTH, expand=True)

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Tracks.Treeview",
            background=COLOR_SURFACE,
            foreground=COLOR_TEXT,
            fieldbackground=COLOR_SURFACE,
            rowheight=26,
            font=("Segoe UI", 9),
        )
        style.configure(
            "Tracks.Treeview.Heading",
            background=COLOR_CONTROL,
            foreground=COLOR_TEXT,
            font=("Segoe UI", 9, "bold"),
        )
        style.map(
            "Tracks.Treeview",
            background=[("selected", COLOR_PRIMARY)],
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
        bar = tk.Frame(self, bg=COLOR_APP_BG, padx=16, pady=12)
        bar.pack(fill=tk.X)

        btn_refresh = tk.Button(
            bar,
            text="🔄 刷新列表",
            bg=COLOR_CONTROL,
            fg=COLOR_TEXT,
            activebackground=COLOR_CONTROL_HOVER,
            activeforeground=COLOR_TEXT,
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
            bg=COLOR_APP_BG,
            fg=COLOR_TEXT_MUTED,
            activebackground=COLOR_APP_BG,
            activeforeground=COLOR_TEXT,
            selectcolor=COLOR_SURFACE,
            font=self.font_sub,
        )
        chk_show_all.pack(side=tk.LEFT, padx=(0, 20))

        # Unified single smart action button
        self.btn_bind = tk.Button(
            bar,
            text="🔗 绑定为对话轨 (Bind Track)",
            bg=COLOR_SUCCESS,
            fg="#ffffff",
            activebackground=COLOR_SUCCESS_HOVER,
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
            bg=COLOR_CONTROL,
            fg=COLOR_TEXT,
            activebackground=COLOR_CONTROL_HOVER,
            activeforeground=COLOR_TEXT,
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
        tw.configure(bg=COLOR_SURFACE_ALT, bd=1, relief=tk.SOLID)
        # Position slightly offset from cursor
        tw.geometry(f"+{x + 18}+{y + 12}")

        frame = tk.Frame(tw, bg=COLOR_SURFACE_ALT, padx=12, pady=10)
        frame.pack(fill=tk.BOTH, expand=True)

        # Header tag
        src_tag = "💬 IDE 对话空间 (可一键直连绑定)" if cand.source == "ide" else "⚡ CLI 对话空间 (可直接绑定)"
        tk.Label(
            frame,
            text=f"📌 {src_tag}",
            fg=COLOR_INFO,
            bg=COLOR_SURFACE_ALT,
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
                fg=COLOR_TEXT,
                bg=COLOR_SURFACE_ALT,
                font=self.font_bold,
                anchor="w",
            ).pack(fill=tk.X, pady=(2, 0))
            tk.Label(
                frame,
                text=f"“{clean_user}”",
                fg=COLOR_INFO,
                bg=COLOR_SURFACE_ALT,
                font=self.font_sub,
                justify=tk.LEFT,
                wraplength=480,
                anchor="w",
            ).pack(fill=tk.X, pady=(1, 6))
        elif clean_resp:
            tk.Label(
                frame,
                text="💡 提示: 此会话未检测到人类独立提问，仅包含助手执行:",
                fg=COLOR_WARNING,
                bg=COLOR_SURFACE_ALT,
                font=self.font_sub,
                anchor="w",
            ).pack(fill=tk.X, pady=(2, 4))
        else:
            tk.Label(
                frame,
                text="📌 空白新会话 (暂无任何交互记录)",
                fg=COLOR_TEXT_MUTED,
                bg=COLOR_SURFACE_ALT,
                font=self.font_sub,
                anchor="w",
            ).pack(fill=tk.X, pady=(2, 6))

        # Model response preview if available
        if clean_resp:
            tk.Label(
                frame,
                text="🤖 助手最新回复:",
                fg=COLOR_TEXT_MUTED,
                bg=COLOR_SURFACE_ALT,
                font=self.font_bold,
                anchor="w",
            ).pack(fill=tk.X, pady=(2, 0))
            tk.Label(
                frame,
                text=f"{clean_resp}",
                fg=COLOR_TEXT_SECONDARY,
                bg=COLOR_SURFACE_ALT,
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
            fg=COLOR_TEXT_SOFT,
            bg=COLOR_SURFACE_ALT,
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
                fg=COLOR_INFO,
            )
        else:
            self.lbl_bound_status.config(
                text="当前未绑定持久轨 (默认以全新独立会话启动)",
                fg=COLOR_TEXT_MUTED,
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
                    f"✅ 已完成数据原子克隆。在 Telegram 发送指令，对应席位的 AI 将直接在此会话施工！"
                )
            else:
                toast_msg = (
                    f"已锁定 CLI 会话为施工续轨：\n“{clean_snip}”\n(UUID: {cid_short})\n\n"
                    f"在 Telegram 发送指令，对应席位的 AI 将直接在此轨道继续施工！"
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
        self.bridge_port = 18765
        self.bridge_server = None
        self.session_hub: SessionHub | None = None
        self.lead_worker: SessionWorker | None = None

    def is_daemon_running(self) -> bool:
        return bool(self.dispatch_loop and getattr(self.dispatch_loop, "running", False))

    def is_cockpit_running(self) -> bool:
        return is_port_listening(self.cockpit_port)

    def is_bridge_running(self) -> bool:
        return is_port_listening(self.bridge_port)

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

    def start_bridge(self) -> bool:
        if self.is_bridge_running():
            self.log(f"[BRIDGE] Web Bridge already listening on port {self.bridge_port}")
            return True

        self.log(f"[BRIDGE] Starting Local Web Bridge on port {self.bridge_port}...")
        try:
            from pocketfleet.bridge_server import ensure_bridge_server_running, get_global_bridge_server

            def _on_telegram_post(payload: dict) -> None:
                text = payload.get("text", "")
                sender = payload.get("sender", "web_chat")
                principal = (payload.get("principal") or payload.get("client_principal") or "").strip().lower()
                if text and self.dispatch_loop and getattr(self.dispatch_loop, "running", False):
                    try:
                        self.log(f"📨 [BRIDGE] Outbound from {sender} (principal={principal or 'unspecified'}): {text[:60]}...")

                        # Security check: if principal is explicitly unauthorized for chat seat, reject outbound
                        if principal and "doubao" in principal:
                            seats_cfg = self.load_seats_config()
                            chat_engine = (getattr(seats_cfg.seats.get("chat", None), "engine", "") or "").lower() if hasattr(seats_cfg, "seats") else ""
                            if "doubao" not in chat_engine:
                                self.log(f"🛡️ [SECURITY] Blocked unauthorized outbound post from unconfigured principal '{principal}'")
                                return

                        # If message contains [NoReply] / 【免回】, apply defanged mention transformation
                        # so recipients are visible to Human Commander in Telegram without triggering a bot reply
                        t_lower = text.lower()
                        if "[noreply]" in t_lower or "【免回】" in text or "mode:[noreply]" in t_lower:
                            from pocketfleet.loop import defang_telegram_mentions
                            text = defang_telegram_mentions(text)
                            self.log("🛡️ [ANTI-ECHO] Applied defanged read-only mention transformation for [NoReply]")

                        chats = getattr(self.dispatch_loop, "allowed_chat_ids", None)
                        if chats:
                            from pocketfleet.core import OutboundMessage
                            target_chat = list(chats)[0]
                            trans = (
                                self.dispatch_loop.secondary_transports.get("chat")
                                if getattr(self.dispatch_loop, "secondary_transports", None) and "chat" in self.dispatch_loop.secondary_transports
                                else getattr(self.dispatch_loop, "transport", None)
                            )
                            if trans:
                                trans.send_message(OutboundMessage(chat_id=target_chat, text=text))
                    except Exception as ex:
                        self.log(f"[WARN] Failed to forward web bridge message: {ex}")

            def _on_meet_kickoff(payload: dict) -> None:
                topic = payload.get("topic", "").strip()
                host = payload.get("host", None)
                watchdog_minutes = int(payload.get("watchdog_minutes", 15))
                participants = payload.get("participants", None)
                self.log(f"🏛️ [MEET] Direct kickoff from Mini App: host={host}, topic={topic[:50]}")
                # Resolve target chat
                target_chat = None
                if self.dispatch_loop and getattr(self.dispatch_loop, "allowed_chat_ids", None):
                    target_chat = list(self.dispatch_loop.allowed_chat_ids)[0]
                if not target_chat:
                    try:
                        seats_cfg = self.load_seats_config()
                        if getattr(seats_cfg, "telegram_chat_id", None):
                            target_chat = int(seats_cfg.telegram_chat_id)
                    except Exception:
                        pass
                if not target_chat:
                    target_chat = -1004309197838

                from pocketfleet.core import InboundMessage
                # 1. Path 2: If Commander has authorized personal MTProto client, send directly as Commander!
                try:
                    from pocketfleet.user_client import UserClientManager
                    u_mgr = UserClientManager.get_instance()
                    if u_mgr.is_authorized():
                        meet_cmd = f"/meet {host or ''} {topic}".strip()
                        if u_mgr.send_message_as_user(chat_id=int(target_chat), text=meet_cmd):
                            self.log("👤 [MEET] Convened Starfleet meeting directly as Commander personal account (MTProto).")
                            return
                except Exception as u_ex:
                    self.log(f"[WARN] MTProto dispatch fallback to bot: {u_ex}")

                fake_msg = InboundMessage(
                    message_id=int(time.time()),
                    chat_id=int(target_chat),
                    sender_id=0,
                    sender_name="人类指挥官",
                    text=f"/meet {host or ''} {topic}".strip(),
                    is_bot=False,
                )

                if self.dispatch_loop and getattr(self.dispatch_loop, "running", False):
                    self.dispatch_loop._handle_fleet_meeting(
                        msg=fake_msg,
                        meet_arg=topic,
                        host_override=host,
                        watchdog_minutes=watchdog_minutes,
                        participants=participants,
                    )
                else:
                    # Daemon loop not active in this process; use standalone transport to publish directly to Telegram
                    try:
                        token, _, _ = self._load_credentials()
                        if token:
                            from pocketfleet.transport.telegram import TelegramTransport
                            from pocketfleet.loop import DispatchLoop
                            from pocketfleet.core import WorkerType
                            trans = TelegramTransport(token=token, allowed_chat_ids={int(target_chat)})
                            seats_cfg = self.load_seats_config()
                            standalone_loop = DispatchLoop(transport=trans, default_worker=WorkerType.ANTIGRAVITY, seats_config=seats_cfg)
                            standalone_loop._handle_fleet_meeting(
                                msg=fake_msg,
                                meet_arg=topic,
                                host_override=host,
                                watchdog_minutes=watchdog_minutes,
                                participants=participants,
                            )
                            self.log("🏛️ [MEET] Published Starfleet Council Briefing via standalone transport.")
                    except Exception as ex:
                        self.log(f"❌ [MEET] Failed to dispatch meeting: {ex}")

            ok = ensure_bridge_server_running(on_telegram_post=_on_telegram_post, on_meet_kickoff=_on_meet_kickoff)
            self.bridge_server = get_global_bridge_server()
            if ok:
                self.log(f"[BRIDGE] Local Web Bridge active at http://127.0.0.1:{self.bridge_port}")
            else:
                self.log(f"[WARN] Failed to start Local Web Bridge on port {self.bridge_port}")
            return ok
        except Exception as e:
            self.log(f"[ERROR] Failed to start Local Web Bridge: {e}")
            return False

    def stop_bridge(self) -> None:
        from pocketfleet.bridge_server import stop_global_bridge_server
        self.log("[BRIDGE] Stopping Local Web Bridge...")
        stop_global_bridge_server()
        self.bridge_server = None
        self.log("[BRIDGE] Local Web Bridge stopped.")

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
        seats_cfg = self.load_seats_config()
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

            # Ensure Port 18765 Web Bridge is running in background
            try:
                self.start_bridge()
            except Exception:
                pass

            seats_cfg = self.load_seats_config()
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
                pure_gateway_mode=True,
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
        tok = None
        if lead_seat:
            tok = lead_seat.get_token() or None
        if not tok:
            token_env = lead_seat.bot_token_env if lead_seat else "TELEGRAM_BOT_JUDGE_TOKEN"
            tok = (
                os.environ.get(token_env)
                or os.environ.get("TELEGRAM_BOT_LEAD_TOKEN")
                or os.environ.get("POCKETFLEET_BOT_TOKEN")
                or None
            )

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
        self.root.title("PocketFleet Control Panel")
        self.root.geometry("1060x880")
        self.root.minsize(980, 780)

        self.root.configure(bg=COLOR_MODERN_BG)
        apply_windows_titlebar_theme(self.root)

        # Thread-safe log queue
        self.log_queue = queue.Queue()

        # Fonts
        ui_fam = get_ui_font_family()
        self.font_title = tkfont.Font(family=ui_fam, size=15, weight="bold")
        self.font_section = tkfont.Font(family=ui_fam, size=11, weight="bold")
        self.font_sub = tkfont.Font(family=ui_fam, size=9)
        self.font_bold = tkfont.Font(family=ui_fam, size=10, weight="bold")
        self.font_regular = tkfont.Font(family=ui_fam, size=9)
        self.font_mono = tkfont.Font(family="Consolas", size=9)
        self.font_pill = tkfont.Font(family=ui_fam, size=9, weight="bold")

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
        # 1. Top Immersive Header (Dark Blue)
        self._build_modern_header()

        # 2. Main Workspace (Sidebar + Scrollable Content Canvas)
        self.workspace_frame = tk.Frame(self.root, bg=COLOR_MODERN_BG)
        self.workspace_frame.pack(fill=tk.BOTH, expand=True)

        # 2.1 Left Navigation Sidebar
        self.sidebar_frame = tk.Frame(
            self.workspace_frame,
            bg="#ffffff",
            width=220,
            highlightbackground=COLOR_CARD_BORDER,
            highlightthickness=1,
            bd=0,
        )
        self.sidebar_frame.pack(side=tk.LEFT, fill=tk.Y)
        self.sidebar_frame.pack_propagate(False)
        self._build_sidebar(self.sidebar_frame)

        # 2.2 Right Content Canvas (Scrollable)
        self.content_container = tk.Frame(self.workspace_frame, bg=COLOR_MODERN_BG)
        self.content_container.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        self.main_canvas = tk.Canvas(self.content_container, bg=COLOR_MODERN_BG, highlightthickness=0)
        self.scrollbar = tk.Scrollbar(self.content_container, orient="vertical", command=self.main_canvas.yview)
        self.scroll_content = tk.Frame(self.main_canvas, bg=COLOR_MODERN_BG)

        self.scroll_content.bind(
            "<Configure>",
            lambda e: self.main_canvas.configure(scrollregion=self.main_canvas.bbox("all")),
        )

        self.canvas_win_id = self.main_canvas.create_window((0, 0), window=self.scroll_content, anchor="nw")
        self.main_canvas.configure(yscrollcommand=self.scrollbar.set)

        self.main_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.main_canvas.bind("<Configure>", lambda e: self.main_canvas.itemconfig(self.canvas_win_id, width=e.width))

        def _on_mousewheel(event):
            try:
                self.main_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            except Exception:
                pass
        self.root.bind_all("<MouseWheel>", _on_mousewheel)

        # Build inside right content canvas
        self._build_content_canvas(self.scroll_content)

        # 3. Bottom Modern Status Bar & Drawer
        self._build_bottom_status_bar()

        # Compatibility panel reference for historical test suites
        self.seats_panel = ThreeSeatsConfigPanel(
            self.root,
            fleet_mgr=self.mgr,
            on_save_callback=self._on_seats_saved,
            is_dialog=False,
            show_header=False,
        )

        self._setup_tray()
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        self.append_log("🚀 PocketFleet Modern Control Panel initialized. Ready to command.")

        # Cached probe status for zero-latency UI responsiveness
        self._status_cache = {
            "is_d": False,
            "is_c": False,
            "is_b": False,
            "detail_d": "Stopped",
            "detail_c": "Offline",
            "detail_b": "Offline",
            "detail_a": "Triad: Initializing...",
        }
        self._tray_status_color = None
        threading.Thread(target=self._background_status_loop, daemon=True).start()

        # Start drain log loop on main thread (low priority, 1s interval)
        self.root.after(1000, self._drain_log_queue)

        # Start periodic status refresh on main thread (reads from cache only)
        self.root.after(500, self._refresh_status)

        # Auto-start Web Cockpit & Web Bridge after mainloop starts
        self.root.after(600, lambda: threading.Thread(target=self._auto_start_local_servers, daemon=True).start())

    def _auto_start_local_servers(self) -> None:
        """Silently auto-launch local web bridge (18765) and cockpit (8765) in background."""
        self.mgr.start_bridge()
        self.mgr.start_cockpit(8765, open_browser=False)

    def append_log(self, text: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] {text}\n")

    def _drain_log_queue(self) -> None:
        has_items = False
        drained = 0
        while drained < 80 and not self.log_queue.empty():
            try:
                msg = self.log_queue.get_nowait()
                if not has_items and hasattr(self, "log_text"):
                    self.log_text.config(state=tk.NORMAL)
                    has_items = True
                if hasattr(self, "log_text"):
                    self.log_text.insert(tk.END, msg)
                drained += 1
            except Exception:
                break
        if has_items and hasattr(self, "log_text"):
            line_count = int(self.log_text.index("end-1c").split(".", 1)[0])
            if line_count > 1200:
                self.log_text.delete("1.0", f"{line_count - 1000}.0")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)

        if not self.is_quitting:
            delay_ms = 80 if not self.log_queue.empty() else 750
            self.root.after(delay_ms, self._drain_log_queue)

    def _background_status_loop(self) -> None:
        """Background worker performing asynchronous socket/disk probes to prevent UI thread lag."""
        while not self.is_quitting:
            try:
                is_d = self.mgr.is_daemon_running()
                is_c = self.mgr.is_cockpit_running()
                is_b = self.mgr.is_bridge_running()
                self._status_cache = {
                    "is_d": is_d,
                    "is_c": is_c,
                    "is_b": is_b,
                    "detail_d": "Active (Polling Telegram)" if is_d else "Stopped",
                    "detail_c": "Listening on http://127.0.0.1:8765" if is_c else "Offline",
                    "detail_b": "Listening on http://127.0.0.1:18765" if is_b else "Offline",
                }
            except Exception:
                pass
            time.sleep(1.5)

    def _refresh_status(self) -> None:
        """Ultra-fast UI update reading purely from memory cache without blocking sockets/disk."""
        try:
            cache = getattr(self, "_status_cache", None) or {}
            is_d = cache.get("is_d", False)
            is_c = cache.get("is_c", False)
            is_b = cache.get("is_b", False)

            # Update diagnostics rows
            if hasattr(self, "row_daemon"):
                self._update_row(self.row_daemon, is_running=is_d, detail=cache.get("detail_d", "Stopped"))
            if hasattr(self, "row_cockpit"):
                self._update_row(self.row_cockpit, is_running=is_c, detail=cache.get("detail_c", "Offline"))
            if hasattr(self, "row_bridge"):
                self._update_row(self.row_bridge, is_running=is_b, detail=cache.get("detail_b", "Offline"))

            # Update bottom status indicators
            if hasattr(self, "dot_tg_status"):
                self.dot_tg_status.config(image=get_icon("dot_green" if is_d else "dot_gray", (8, 8)))
                self.lbl_tg_sub.config(text="已连接" if is_d else "未连接")
            if hasattr(self, "dot_web_status"):
                self.dot_web_status.config(image=get_icon("dot_green" if is_c else "dot_gray", (8, 8)))
                self.lbl_web_sub.config(text="运行中" if is_c else "未启动")
            if hasattr(self, "dot_ext_status"):
                self.dot_ext_status.config(image=get_icon("dot_green" if is_b else "dot_gray", (8, 8)))
                self.lbl_ext_sub.config(text="已就绪" if is_b else "未就绪")

            # Calculate connected AI count
            seats_cfg = self.mgr.load_seats_config()
            connected_ai_count = 0
            for s in seats_cfg.seats.values():
                if s.get_token() and s.bot_username:
                    connected_ai_count += 1

            # Update Modern Header Pills (compatible references)
            if hasattr(self, "lbl_pill_tg"):
                self.lbl_pill_tg.config(text="Telegram 已连接" if is_d else "Telegram 未连接")
                self.dot_pill_tg.config(image=get_icon("dot_green" if is_d else "dot_gray", (8, 8)))

            if hasattr(self, "lbl_pill_ai"):
                self.lbl_pill_ai.config(text=f"{connected_ai_count} 个 AI 已接入")
                self.dot_pill_ai.config(image=get_icon("dot_green" if connected_ai_count > 0 else "dot_gray", (8, 8)))

            if hasattr(self, "lbl_pill_meet"):
                can_meet = is_d and (connected_ai_count >= 2)
                self.lbl_pill_meet.config(text="可以开会" if can_meet else "待配齐开会")
                self.dot_pill_meet.config(image=get_icon("dot_green" if can_meet else "dot_gray", (8, 8)))

            # Update TG Group Card info
            if hasattr(self, "lbl_group_title"):
                grp_name = seats_cfg.telegram_group_name or "Fleet WarRoom"
                self.lbl_group_title.config(text=grp_name)
            if hasattr(self, "lbl_group_chat_id"):
                cid = seats_cfg.telegram_chat_id or (os.environ.get("TELEGRAM_GROUP_ID") or "未配置")
                self.lbl_group_chat_id.config(text=f"Chat ID: {cid} (全席位共用)")

            color = "green" if (is_d and is_c and is_b) else ("cyan" if (is_d or is_c or is_b) else "yellow")
            if self.tray_icon and color != self._tray_status_color:
                self.tray_icon.icon = create_tray_image(color)
                self._tray_status_color = color
        except Exception:
            pass

        if not self.is_quitting:
            self.root.after(1000, self._refresh_status)

    def _build_modern_header(self) -> None:
        """Top Header with immersive dark navy background and embedded tip banner."""
        header_outer = tk.Frame(self.root, bg="#0e3c5d", height=70)
        header_outer.pack(fill=tk.X)

        header = tk.Frame(header_outer, bg="#0e3c5d", padx=20, pady=10)
        header.pack(fill=tk.X)

        # Left Branding
        brand_frame = tk.Frame(header, bg="#0e3c5d")
        brand_frame.pack(side=tk.LEFT)

        logo_lbl = tk.Label(brand_frame, image=get_icon("logo_rocket", (38, 38)), bg="#0e3c5d")
        logo_lbl.pack(side=tk.LEFT, padx=(0, 10))

        text_box = tk.Frame(brand_frame, bg="#0e3c5d")
        text_box.pack(side=tk.LEFT)

        title_row = tk.Frame(text_box, bg="#0e3c5d")
        title_row.pack(anchor="w")
        tk.Label(title_row, text="PocketFleet Control Panel", fg="#ffffff", bg="#0e3c5d", font=self.font_title).pack(side=tk.LEFT)

        tk.Label(
            text_box,
            text="AI STARFLEET COMMUNICATION HUB  |  XAMPP-STYLE TRAY CONTROLLER  |  v0.2.0",
            fg="#8cb4d2",
            bg="#0e3c5d",
            font=tkfont.Font(family=get_ui_font_family(), size=8),
        ).pack(anchor="w", pady=(1, 0))

        # Right Embedded Tip Banner
        banner_box = tk.Frame(
            header,
            bg="#092d47",
            highlightbackground="#18527a",
            highlightthickness=1,
            bd=0,
            padx=14,
            pady=6,
        )
        banner_box.pack(side=tk.RIGHT)

        tk.Label(banner_box, image=get_icon("lightbulb", (20, 20)), bg="#092d47").pack(side=tk.LEFT, padx=(0, 8))

        b_text_box = tk.Frame(banner_box, bg="#092d47")
        b_text_box.pack(side=tk.LEFT)

        tk.Label(
            b_text_box,
            text="桌面端负责 AI 接入与连接管理；会议参与者与主持由 Telegram /meet 面板决定。",
            fg="#ffffff",
            bg="#092d47",
            font=tkfont.Font(family=get_ui_font_family(), size=8, weight="bold"),
            anchor="w",
        ).pack(anchor="w")

        tk.Label(
            b_text_box,
            text="在这里连接和管理你的 AI 工具，然后前往 Telegram 使用 /meet 组建今天的会议，指定参与者并分配任务。",
            fg="#a5c9e8",
            bg="#092d47",
            font=tkfont.Font(family=get_ui_font_family(), size=8),
            anchor="w",
        ).pack(anchor="w")

        # Hidden pills references to preserve test suite compatibility
        self.lbl_pill_tg = tk.Label(header_outer, text="Telegram 已连接")
        self.dot_pill_tg = tk.Label(header_outer)
        self.lbl_pill_ai = tk.Label(header_outer, text="3 个 AI 已接入")
        self.dot_pill_ai = tk.Label(header_outer)
        self.lbl_pill_meet = tk.Label(header_outer, text="可以开会")
        self.dot_pill_meet = tk.Label(header_outer)

    def _build_sidebar(self, parent) -> None:
        """Left navigation sidebar containing AI Binding, Collaboration Modes, and TG Config."""
        # Top padding
        pad_top = tk.Frame(parent, bg="#ffffff", height=12)
        pad_top.pack(fill=tk.X)

        # ---------------- Section 1: 🔗 绑定AI ----------------
        sec1 = tk.Frame(parent, bg="#ffffff", padx=12, pady=6)
        sec1.pack(fill=tk.X)

        sec1_hdr = tk.Frame(sec1, bg="#ffffff")
        sec1_hdr.pack(fill=tk.X, pady=(0, 6))
        tk.Label(sec1_hdr, text="🔗 绑定AI", fg="#1e293b", bg="#ffffff", font=self.font_bold).pack(side=tk.LEFT)
        tk.Label(sec1_hdr, text="∧", fg="#94a3b8", bg="#ffffff", font=self.font_sub).pack(side=tk.RIGHT)

        self.btn_nav_code = tk.Button(
            sec1,
            text="  </>  + 代码AI",
            bg="#e0f2fe",
            fg="#0284c7",
            activebackground="#e0f2fe",
            activeforeground="#0284c7",
            font=self.font_bold,
            relief=tk.FLAT,
            bd=0,
            padx=10,
            pady=8,
            anchor="w",
            cursor="hand2",
            command=lambda: self._switch_nav_tab("code"),
        )
        self.btn_nav_code.pack(fill=tk.X, pady=2)

        self.btn_nav_chat = tk.Button(
            sec1,
            text="  💬  + 对话AI",
            bg="#ffffff",
            fg="#475569",
            activebackground="#f1f5f9",
            activeforeground="#1e293b",
            font=self.font_regular,
            relief=tk.FLAT,
            bd=0,
            padx=10,
            pady=8,
            anchor="w",
            cursor="hand2",
            command=lambda: self._switch_nav_tab("chat"),
        )
        self.btn_nav_chat.pack(fill=tk.X, pady=2)

        # ---------------- Section 2: 👥 协作方式 ----------------
        sec2 = tk.Frame(parent, bg="#ffffff", padx=12, pady=10)
        sec2.pack(fill=tk.X)

        sec2_hdr = tk.Frame(sec2, bg="#ffffff")
        sec2_hdr.pack(fill=tk.X, pady=(0, 6))
        tk.Label(sec2_hdr, text="👥 协作方式", fg="#1e293b", bg="#ffffff", font=self.font_bold).pack(side=tk.LEFT)
        tk.Label(sec2_hdr, text="∧", fg="#94a3b8", bg="#ffffff", font=self.font_sub).pack(side=tk.RIGHT)

        self.btn_collab_2ai = tk.Button(
            sec2,
            text="  👥  Human Root + 2 AI",
            bg="#dcfce7",
            fg="#047857",
            activebackground="#dcfce7",
            activeforeground="#047857",
            font=self.font_bold,
            relief=tk.FLAT,
            bd=0,
            padx=8,
            pady=7,
            anchor="w",
            cursor="hand2",
            command=lambda: self._select_collab_template("2ai"),
        )
        self.btn_collab_2ai.pack(fill=tk.X, pady=2)

        # Dropdown Toggle for More
        self.collab_expanded = False
        self.btn_more_collab = tk.Button(
            sec2,
            text="更多 ∨",
            bg="#ffffff",
            fg="#64748b",
            activebackground="#f1f5f9",
            activeforeground="#1e293b",
            font=self.font_sub,
            relief=tk.FLAT,
            bd=0,
            pady=3,
            cursor="hand2",
            command=self._toggle_collab_more,
        )
        self.btn_more_collab.pack(fill=tk.X)

        self.frame_collab_more = tk.Frame(sec2, bg="#ffffff")
        # Hidden initially

        self.btn_collab_3ai = tk.Button(
            self.frame_collab_more,
            text="Human Root + 3 AI",
            bg="#ffffff",
            fg="#64748b",
            font=self.font_sub,
            relief=tk.FLAT,
            bd=0,
            padx=12,
            pady=5,
            anchor="w",
            cursor="hand2",
            command=lambda: self._select_collab_template("3ai"),
        )
        self.btn_collab_3ai.pack(fill=tk.X, pady=1)

        self.btn_collab_nai = tk.Button(
            self.frame_collab_more,
            text="Human Root + N AI",
            bg="#ffffff",
            fg="#64748b",
            font=self.font_sub,
            relief=tk.FLAT,
            bd=0,
            padx=12,
            pady=5,
            anchor="w",
            cursor="hand2",
            command=lambda: self._select_collab_template("nai"),
        )
        self.btn_collab_nai.pack(fill=tk.X, pady=1)

        # ---------------- Section 3: ✈️ Telegram设置 ----------------
        sec3 = tk.Frame(parent, bg="#ffffff", padx=12, pady=10)
        sec3.pack(fill=tk.X)

        sec3_hdr = tk.Frame(sec3, bg="#ffffff")
        sec3_hdr.pack(fill=tk.X, pady=(0, 6))
        tk.Label(sec3_hdr, text="✈️ Telegram设置", fg="#1e293b", bg="#ffffff", font=self.font_bold).pack(side=tk.LEFT)
        tk.Label(sec3_hdr, text="∧", fg="#94a3b8", bg="#ffffff", font=self.font_sub).pack(side=tk.RIGHT)

        btn_set_tg = tk.Button(
            sec3,
            text="  ⚙  设置TG群组",
            bg="#ffffff",
            fg="#475569",
            activebackground="#f1f5f9",
            font=self.font_regular,
            relief=tk.FLAT,
            bd=0,
            padx=10,
            pady=6,
            anchor="w",
            cursor="hand2",
            command=self.open_telegram_group_dialog,
        )
        btn_set_tg.pack(fill=tk.X, pady=2)

        # ---------------- Bottom Fixed CTA ----------------
        cta_frame = tk.Frame(parent, bg="#ffffff", padx=12, pady=16)
        cta_frame.pack(side=tk.BOTTOM, fill=tk.X)

        btn_go_tg = tk.Button(
            cta_frame,
            text="  前往Telegram开会 ❯",
            image=get_icon("white_plane", (16, 16)),
            compound=tk.LEFT,
            bg=COLOR_ACCENT_GREEN,
            fg="#ffffff",
            activebackground=COLOR_ACCENT_GREEN_HOVER,
            activeforeground="#ffffff",
            font=tkfont.Font(family=get_ui_font_family(), size=10, weight="bold"),
            relief=tk.FLAT,
            bd=0,
            padx=12,
            pady=10,
            cursor="hand2",
            command=self._action_open_tg,
        )
        btn_go_tg.pack(fill=tk.X)

        tk.Label(
            cta_frame,
            text="在 Telegram 使用 /meet 组建会议",
            fg="#94a3b8",
            bg="#ffffff",
            font=tkfont.Font(family=get_ui_font_family(), size=8),
        ).pack(fill=tk.X, pady=(6, 0))

    def _toggle_collab_more(self) -> None:
        self.collab_expanded = not self.collab_expanded
        if self.collab_expanded:
            self.frame_collab_more.pack(fill=tk.X, pady=(2, 0))
            self.btn_more_collab.config(text="收起 ∧")
        else:
            self.frame_collab_more.pack_forget()
            self.btn_more_collab.config(text="更多 ∨")

    def _select_collab_template(self, mode: str) -> None:
        if mode == "2ai":
            self.btn_collab_2ai.config(bg="#dcfce7", fg="#047857", font=self.font_bold)
            self.btn_collab_3ai.config(bg="#ffffff", fg="#64748b", font=self.font_sub)
            self.btn_collab_nai.config(bg="#ffffff", fg="#64748b", font=self.font_sub)
            show_floating_toast(self.root, "协作模式", "已切换至 Human Root + 2 AI 编队模式。")
        elif mode == "3ai":
            self.btn_collab_2ai.config(bg="#ffffff", fg="#64748b", font=self.font_sub)
            self.btn_collab_3ai.config(bg="#dcfce7", fg="#047857", font=self.font_bold)
            self.btn_collab_nai.config(bg="#ffffff", fg="#64748b", font=self.font_sub)
            show_floating_toast(self.root, "协作模式", "已切换至 Human Root + 3 AI (主持/审计/施工) 模式。")
        else:
            self.btn_collab_2ai.config(bg="#ffffff", fg="#64748b", font=self.font_sub)
            self.btn_collab_3ai.config(bg="#ffffff", fg="#64748b", font=self.font_sub)
            self.btn_collab_nai.config(bg="#dcfce7", fg="#047857", font=self.font_bold)
            show_floating_toast(self.root, "协作模式", "已切换至 Human Root + N AI 自由扩展模式。")

    def _switch_nav_tab(self, category: str) -> None:
        self.current_ai_category = category
        if category == "code":
            self.btn_nav_code.config(bg="#e0f2fe", fg="#0284c7", font=self.font_bold)
            self.btn_nav_chat.config(bg="#ffffff", fg="#475569", font=self.font_regular)
            self.lbl_shelf_title.config(text="</> 添加代码AI")
            self.lbl_shelf_desc.config(text="选择并连接你常用的代码 AI 工具。连接后，它们将可以在 Telegram 会议中被你召唤和使用。")
            self.lbl_connected_title.config(text="已连接的代码AI")
        else:
            self.btn_nav_code.config(bg="#ffffff", fg="#475569", font=self.font_regular)
            self.btn_nav_chat.config(bg="#e0f2fe", fg="#0284c7", font=self.font_bold)
            self.lbl_shelf_title.config(text="💬 添加对话AI")
            self.lbl_shelf_desc.config(text="选择并连接你常用的对话与分析 AI 工具。连接后，它们将在 Telegram 会议中协助多轮推理。")
            self.lbl_connected_title.config(text="已连接的对话AI")

        self._render_shelf_cards()
        self._render_connected_cards()

    def _build_content_canvas(self, parent) -> None:
        """Right Main Content Viewport: Header Dual Cards, Marketplace Shelf, Connected List."""
        canvas_box = tk.Frame(parent, bg=COLOR_MODERN_BG, padx=20, pady=16)
        canvas_box.pack(fill=tk.BOTH, expand=True)

        # 1. Top Dual Cards
        top_cards = tk.Frame(canvas_box, bg=COLOR_MODERN_BG)
        top_cards.pack(fill=tk.X, pady=(0, 14))
        top_cards.columnconfigure(0, weight=3)
        top_cards.columnconfigure(1, weight=2)

        # Left: Human Root Card
        card_root = tk.Frame(
            top_cards,
            bg="#ffffff",
            highlightbackground=COLOR_CARD_BORDER,
            highlightthickness=1,
            bd=0,
            padx=14,
            pady=10,
        )
        card_root.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        tk.Label(card_root, image=get_icon("human_circle", (38, 38)), bg="#ffffff").pack(side=tk.LEFT, padx=(0, 12))

        root_text_box = tk.Frame(card_root, bg="#ffffff")
        root_text_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        head_line = tk.Frame(root_text_box, bg="#ffffff")
        head_line.pack(anchor="w")
        tk.Label(head_line, text="Human Root / 人类指挥官", fg=COLOR_TEXT_TITLE, bg="#ffffff", font=self.font_bold).pack(side=tk.LEFT)
        tag = tk.Label(
            head_line,
            text=" 核心控制中心 ",
            fg="#ffffff",
            bg=COLOR_ACCENT_GREEN,
            font=tkfont.Font(family=get_ui_font_family(), size=8, weight="bold"),
        )
        tag.pack(side=tk.LEFT, padx=8)

        tk.Label(
            root_text_box,
            text="你是编队的指挥官，负责在 Telegram 中组建会议、指定参与者并分配任务。",
            fg=COLOR_TEXT_MUTED,
            bg="#ffffff",
            font=self.font_sub,
            anchor="w",
        ).pack(anchor="w", pady=(2, 0))

        # Right: Quick Telegram Meet Card
        card_tg_quick = tk.Frame(
            top_cards,
            bg="#ffffff",
            highlightbackground=COLOR_CARD_BORDER,
            highlightthickness=1,
            bd=0,
            padx=14,
            pady=10,
            cursor="hand2",
        )
        card_tg_quick.grid(row=0, column=1, sticky="nsew")
        card_tg_quick.bind("<Button-1>", lambda e: self._action_open_tg())

        tk.Label(card_tg_quick, image=get_icon("tg_circle", (36, 36)), bg="#ffffff").pack(side=tk.LEFT, padx=(0, 10))

        q_box = tk.Frame(card_tg_quick, bg="#ffffff")
        q_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        q_box.bind("<Button-1>", lambda e: self._action_open_tg())

        lbl_tg_link = tk.Label(
            q_box,
            text="前往 Telegram 使用 /meet  ❯",
            fg="#0369a1",
            bg="#ffffff",
            font=self.font_bold,
            cursor="hand2",
        )
        lbl_tg_link.pack(anchor="w")
        lbl_tg_link.bind("<Button-1>", lambda e: self._action_open_tg())

        tk.Label(
            q_box,
            text="邀请 AI 参与会议并开始协作",
            fg=COLOR_TEXT_MUTED,
            bg="#ffffff",
            font=self.font_sub,
        ).pack(anchor="w", pady=(2, 0))

        # 2. Marketplace Shelf Section
        shelf_card = tk.Frame(
            canvas_box,
            bg="#ffffff",
            highlightbackground=COLOR_CARD_BORDER,
            highlightthickness=1,
            bd=0,
            padx=16,
            pady=14,
        )
        shelf_card.pack(fill=tk.X, pady=(0, 14))

        shelf_hdr = tk.Frame(shelf_card, bg="#ffffff")
        shelf_hdr.pack(fill=tk.X)

        self.lbl_shelf_title = tk.Label(
            shelf_hdr,
            text="</> 添加代码AI",
            fg=COLOR_TEXT_TITLE,
            bg="#ffffff",
            font=self.font_section,
        )
        self.lbl_shelf_title.pack(side=tk.LEFT)

        btn_how = tk.Label(
            shelf_hdr,
            text="❓ 如何选择?",
            fg="#0284c7",
            bg="#ffffff",
            font=self.font_sub,
            cursor="hand2",
        )
        btn_how.pack(side=tk.RIGHT)
        btn_how.bind(
            "<Button-1>",
            lambda e: show_floating_toast(
                self.root,
                "如何选择 AI",
                "代码 AI 负责编程与工程实现 (如 Codex/Claude/Antigravity)；\n对话 AI 负责多轮推理、需求拆解与架构讨论 (如 ChatGPT)。",
            ),
        )

        self.lbl_shelf_desc = tk.Label(
            shelf_card,
            text="选择并连接你常用的代码 AI 工具。连接后，它们将可以在 Telegram 会议中被你召唤和使用。",
            fg=COLOR_TEXT_MUTED,
            bg="#ffffff",
            font=self.font_sub,
        )
        self.lbl_shelf_desc.pack(anchor="w", pady=(2, 12))

        # 5-Column Shelf Cards Container
        self.shelf_frame = tk.Frame(shelf_card, bg="#ffffff")
        self.shelf_frame.pack(fill=tk.X)
        for c in range(5):
            self.shelf_frame.columnconfigure(c, weight=1)

        self._render_shelf_cards()

        # 3. Connected AI List Section
        conn_outer = tk.Frame(
            canvas_box,
            bg="#ffffff",
            highlightbackground=COLOR_CARD_BORDER,
            highlightthickness=1,
            bd=0,
            padx=16,
            pady=14,
        )
        conn_outer.pack(fill=tk.X, pady=(0, 14))

        conn_hdr = tk.Frame(conn_outer, bg="#ffffff")
        conn_hdr.pack(fill=tk.X, pady=(0, 10))

        tk.Label(conn_hdr, image=get_icon("code_circle", (20, 20)), bg="#ffffff").pack(side=tk.LEFT, padx=(0, 6))
        self.lbl_connected_title = tk.Label(
            conn_hdr,
            text="已连接的代码AI (1)",
            fg=COLOR_TEXT_TITLE,
            bg="#ffffff",
            font=self.font_section,
        )
        self.lbl_connected_title.pack(side=tk.LEFT)

        btn_refresh = tk.Button(
            conn_hdr,
            text=" 🔄 刷新",
            bg="#ffffff",
            fg=COLOR_BTN_OUTLINE_TEXT,
            activebackground=COLOR_BTN_OUTLINE_HOVER,
            highlightbackground=COLOR_BTN_OUTLINE_BORDER,
            highlightthickness=1,
            bd=0,
            relief=tk.FLAT,
            font=self.font_sub,
            padx=10,
            pady=3,
            cursor="hand2",
            command=self._render_connected_cards,
        )
        btn_refresh.pack(side=tk.RIGHT)

        self.connected_cards_container = tk.Frame(conn_outer, bg="#ffffff")
        self.connected_cards_container.pack(fill=tk.X)

        self._render_connected_cards()

    def _render_shelf_cards(self) -> None:
        """Render 5 marketplace AI cards depending on current category."""
        for w in self.shelf_frame.winfo_children():
            w.destroy()

        cat = getattr(self, "current_ai_category", "code")
        if cat == "code":
            presets = [
                ("OpenAI Codex", "icon_openai", "强大的代码能力\n由 OpenAI 提供", "codex", True),
                ("Antigravity", "icon_antigravity", "专注工程的\nAI 编程助手", "antigravity", False),
                ("Claude Code", "icon_claude", "Anthropic 的\n代码智能助手", "claude_code", False),
                ("Aider", "icon_aider", "开源的\nAI 编程伙伴", "aider", False),
                ("GitHub Copilot", "icon_github", "你熟悉的\nAI 编程助手", "copilot", False),
            ]
        else:
            presets = [
                ("ChatGPT", "icon_openai", "OpenAI 旗舰\n多轮思考分析助手", "chatgpt", True),
                ("Claude 3.7", "icon_claude", "Anthropic 对话\n长文本多轮推理", "claude", False),
                ("Gemini", "bot_purple", "Google 多模态\n大模型智能体", "gemini", False),
                ("DeepSeek", "bot_green", "国产开源强推理\n深度思考模型", "deepseek", False),
                ("OpenHands", "bot_orange", "全自主智能体\n多轮交互助手", "openhands", False),
            ]

        for idx, (name, icon_name, desc, engine_key, is_featured) in enumerate(presets):
            border_col = "#22c55e" if is_featured else COLOR_CARD_BORDER
            thick = 2 if is_featured else 1
            card = tk.Frame(
                self.shelf_frame,
                bg="#ffffff",
                highlightbackground=border_col,
                highlightthickness=thick,
                bd=0,
                padx=10,
                pady=12,
            )
            card.grid(row=0, column=idx, sticky="nsew", padx=4)

            # Icon
            tk.Label(card, image=get_icon(icon_name, (36, 36)), bg="#ffffff").pack(pady=(2, 6))

            # Name
            tk.Label(card, text=name, fg=COLOR_TEXT_MAIN, bg="#ffffff", font=self.font_bold).pack()

            # Description
            tk.Label(card, text=desc, fg=COLOR_TEXT_MUTED, bg="#ffffff", font=self.font_sub, justify="center").pack(pady=(4, 10))

            # Connect Button
            btn_conn = tk.Button(
                card,
                text="＋ 连接此 AI",
                bg="#f0fdf4" if is_featured else "#f8fafc",
                fg="#059669" if is_featured else "#334155",
                activebackground="#dcfce7",
                highlightbackground="#86efac" if is_featured else "#e2e8f0",
                highlightthickness=1,
                bd=0,
                relief=tk.FLAT,
                font=self.font_sub,
                padx=8,
                pady=4,
                cursor="hand2",
                command=lambda e_key=engine_key, n=name: self._action_connect_preset(e_key, n),
            )
            btn_conn.pack(fill=tk.X, padx=4)

    def _action_connect_preset(self, engine_key: str, name: str) -> None:
        cat = getattr(self, "current_ai_category", "code")
        AddOrEditAiDialog(
            self.root,
            fleet_mgr=self.mgr,
            role_key=None,
            default_category=cat,
            preset_engine=engine_key,
            preset_name=name,
            on_save_callback=self._on_seats_saved,
        )

    def _render_connected_cards(self) -> None:
        """Render discrete connected AI cards in horizontal tabular format with copy & menu."""
        for w in self.connected_cards_container.winfo_children():
            w.destroy()

        seats_cfg = self.mgr.load_seats_config()
        cat = getattr(self, "current_ai_category", "code")

        items = []
        for r_key, seat in seats_cfg.seats.items():
            is_code_seat = (seat.engine in ALLOWED_CODE_ENGINES or r_key in ("lead", "builder"))
            if (cat == "code" and is_code_seat) or (cat == "chat" and not is_code_seat):
                items.append((r_key, seat, is_code_seat))

        # Update counter title
        title_prefix = "已连接的代码AI" if cat == "code" else "已连接的对话AI"
        self.lbl_connected_title.config(text=f"{title_prefix} ({len(items)})")

        if not items:
            empty_box = tk.Frame(self.connected_cards_container, bg="#ffffff", pady=20)
            empty_box.pack(fill=tk.X)
            tk.Label(empty_box, text="暂无已连接的席位，请从上方选择工具进行连接。", fg=COLOR_TEXT_MUTED, bg="#ffffff", font=self.font_sub).pack()
            return

        for r_key, seat, is_code in items:
            card = tk.Frame(
                self.connected_cards_container,
                bg="#ffffff",
                highlightbackground="#e2e8f0",
                highlightthickness=1,
                bd=0,
                padx=14,
                pady=10,
            )
            card.pack(fill=tk.X, pady=4)

            # Left AI Brand Icon
            if "antigravity" in (seat.engine or "").lower():
                ic = "icon_antigravity"
            elif "claude" in (seat.engine or "").lower():
                ic = "icon_claude"
            elif "codex" in (seat.engine or "").lower() or "chatgpt" in (seat.engine or "").lower():
                ic = "icon_openai"
            elif "aider" in (seat.engine or "").lower():
                ic = "icon_aider"
            elif "copilot" in (seat.engine or "").lower():
                ic = "icon_github"
            else:
                ic = "bot_green" if is_code else "bot_purple"

            tk.Label(card, image=get_icon(ic, (36, 36)), bg="#ffffff").pack(side=tk.LEFT, padx=(0, 12))

            # Main info column
            info_col = tk.Frame(card, bg="#ffffff")
            info_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

            name_line = tk.Frame(info_col, bg="#ffffff")
            name_line.pack(anchor="w")
            tk.Label(name_line, text=seat.name, fg=COLOR_TEXT_TITLE, bg="#ffffff", font=self.font_bold).pack(side=tk.LEFT)

            token_val = seat.get_token() or (os.environ.get(seat.bot_token_env) or "").strip()
            is_ready = bool(token_val and seat.bot_username)
            status_text = " ● 已连接 " if is_ready else " ● 待配置 "
            status_fg = "#059669" if is_ready else COLOR_TEXT_MUTED
            status_bg = "#dcfce7" if is_ready else "#f1f5f9"
            tk.Label(
                name_line,
                text=status_text,
                fg=status_fg,
                bg=status_bg,
                font=tkfont.Font(family=get_ui_font_family(), size=8, weight="bold"),
            ).pack(side=tk.LEFT, padx=6)

            # Subtitle / Role description
            role_disp = "代码 AI · 主力程序员" if is_code else "对话 AI · 策略分析师"
            tk.Label(info_col, text=f"{role_disp}  擅长代码编写、架构设计与技术实现。", fg=COLOR_TEXT_MUTED, bg="#ffffff", font=self.font_sub).pack(anchor="w", pady=(2, 0))

            # Right Meta Attributes (Engine, TG username, Status dot, More menu)
            meta_box = tk.Frame(card, bg="#ffffff")
            meta_box.pack(side=tk.RIGHT)

            # 1. AI Engine column
            eng_box = tk.Frame(meta_box, bg="#ffffff", padx=10)
            eng_box.pack(side=tk.LEFT)
            tk.Label(eng_box, text="AI 引擎", fg=COLOR_TEXT_MUTED, bg="#ffffff", font=self.font_sub).pack(anchor="w")
            eng_name = (seat.engine or "AUTO").upper()
            tk.Label(eng_box, text=eng_name, fg="#1e293b", bg="#ffffff", font=self.font_bold).pack(anchor="w")

            # 2. TG Account column with copy button
            tg_box = tk.Frame(meta_box, bg="#ffffff", padx=10)
            tg_box.pack(side=tk.LEFT)
            tk.Label(tg_box, text="Telegram 账号", fg=COLOR_TEXT_MUTED, bg="#ffffff", font=self.font_sub).pack(anchor="w")

            raw_u = (seat.bot_username or "").strip()
            clean_u = raw_u.lstrip("@")
            tg_user = f"@{clean_u}" if clean_u else "未配置"
            tg_line = tk.Frame(tg_box, bg="#ffffff")
            tg_line.pack(anchor="w")
            tk.Label(tg_line, text=tg_user, fg="#2563eb", bg="#ffffff", font=self.font_sub).pack(side=tk.LEFT)

            if clean_u:
                btn_cp = tk.Button(
                    tg_line,
                    image=get_icon("icon_copy", (14, 14)),
                    bg="#ffffff",
                    activebackground="#f1f5f9",
                    bd=0,
                    relief=tk.FLAT,
                    cursor="hand2",
                    command=lambda u=clean_u: self._copy_to_clipboard(f"@{u}"),
                )
                btn_cp.pack(side=tk.LEFT, padx=3)

            # 3. Status dot
            st_box = tk.Frame(meta_box, bg="#ffffff", padx=10)
            st_box.pack(side=tk.LEFT)
            tk.Label(st_box, text="状态", fg=COLOR_TEXT_MUTED, bg="#ffffff", font=self.font_sub).pack(anchor="w")
            st_line = tk.Frame(st_box, bg="#ffffff")
            st_line.pack(anchor="w")
            dot_icon = "dot_green" if is_ready else "dot_gray"
            tk.Label(st_line, image=get_icon(dot_icon, (8, 8)), bg="#ffffff").pack(side=tk.LEFT, padx=(0, 4))
            tk.Label(st_line, text="在线" if is_ready else "离线", fg="#059669" if is_ready else COLOR_TEXT_MUTED, bg="#ffffff", font=self.font_sub).pack(side=tk.LEFT)

            # 4. Context Menu Button (⋮)
            btn_more = tk.Button(
                meta_box,
                image=get_icon("icon_more", (14, 14)),
                bg="#ffffff",
                activebackground="#f1f5f9",
                bd=0,
                relief=tk.FLAT,
                cursor="hand2",
                command=lambda rk=r_key, s=seat: self._show_seat_context_menu(rk, s),
            )
            btn_more.pack(side=tk.LEFT, padx=(6, 0))

    def _copy_to_clipboard(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        show_floating_toast(self.root, "已复制", f"已将 {text} 复制到剪贴板。")

    def _show_seat_context_menu(self, role_key: str, seat: SeatConfig) -> None:
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="🔗 测试连接", command=lambda: self._test_seat_connection(seat))
        menu.add_command(label="⚙ 管理配置", command=lambda: self.open_edit_ai_dialog(role_key))
        menu.add_separator()
        menu.add_command(label="🗑 移除席位", command=lambda: self._remove_seat(role_key, seat.name))

        # Position menu at mouse cursor
        x = self.root.winfo_pointerx()
        y = self.root.winfo_pointery()
        menu.tk_popup(x, y)

    def _build_bottom_status_bar(self) -> None:
        """Bottom compact status bar with 4 live services indicators and log drawer."""
        self.bottom_bar_frame = tk.Frame(
            self.root,
            bg="#ffffff",
            highlightbackground=COLOR_CARD_BORDER,
            highlightthickness=1,
            bd=0,
            padx=16,
            pady=8,
        )
        self.bottom_bar_frame.pack(side=tk.BOTTOM, fill=tk.X)

        # Left: Diagnostics expand toggle
        left_diag = tk.Frame(self.bottom_bar_frame, bg="#ffffff", cursor="hand2")
        left_diag.pack(side=tk.LEFT, padx=(0, 20))
        left_diag.bind("<Button-1>", lambda e: self._toggle_log_drawer())

        tk.Label(left_diag, image=get_icon("service_tg", (16, 16)), bg="#ffffff").pack(side=tk.LEFT, padx=(0, 6))
        tk.Label(left_diag, text="运行状态 / 高级诊断", fg=COLOR_TEXT_TITLE, bg="#ffffff", font=self.font_bold).pack(side=tk.LEFT)
        self.lbl_drawer_arrow = tk.Label(left_diag, text=" ∧", fg="#94a3b8", bg="#ffffff", font=self.font_bold)
        self.lbl_drawer_arrow.pack(side=tk.LEFT)

        # Center: 4 Micro Service Indicators
        micro_box = tk.Frame(self.bottom_bar_frame, bg="#ffffff")
        micro_box.pack(side=tk.LEFT, fill=tk.X, expand=True)

        def _make_micro_indicator(parent, name: str, default_sub: str, default_desc: str):
            f = tk.Frame(parent, bg="#ffffff", padx=8)
            f.pack(side=tk.LEFT, expand=True)

            top = tk.Frame(f, bg="#ffffff")
            top.pack(anchor="w")
            dot = tk.Label(top, image=get_icon("dot_green", (8, 8)), bg="#ffffff")
            dot.pack(side=tk.LEFT, padx=(0, 4))
            tk.Label(top, text=name, fg=COLOR_TEXT_MAIN, bg="#ffffff", font=self.font_bold).pack(side=tk.LEFT)

            sub_line = tk.Frame(f, bg="#ffffff")
            sub_line.pack(anchor="w")
            sub_lbl = tk.Label(sub_line, text=default_sub, fg="#059669", bg="#ffffff", font=self.font_sub)
            sub_lbl.pack(side=tk.LEFT, padx=(12, 2))
            tk.Label(sub_line, text=default_desc, fg=COLOR_TEXT_MUTED, bg="#ffffff", font=self.font_sub).pack(side=tk.LEFT)

            return dot, sub_lbl

        self.dot_tg_status, self.lbl_tg_sub = _make_micro_indicator(micro_box, "Telegram", "已连接", "(Telegram Bridge)")
        self.dot_web_status, self.lbl_web_sub = _make_micro_indicator(micro_box, "Web Bridge", "运行中", "(本地 Web 服务)")
        self.dot_ext_status, self.lbl_ext_sub = _make_micro_indicator(micro_box, "浏览器扩展", "已就绪", "(Web Extension)")
        self.dot_agent_status, self.lbl_agent_sub = _make_micro_indicator(micro_box, "本地运行环境", "正常", "(Local Runtime)")

        # Right: View Logs Button
        btn_view_logs = tk.Button(
            self.bottom_bar_frame,
            text="  查看日志",
            image=get_icon("icon_doc", (14, 14)),
            compound=tk.LEFT,
            bg="#ffffff",
            fg=COLOR_BTN_OUTLINE_TEXT,
            activebackground=COLOR_BTN_OUTLINE_HOVER,
            highlightbackground=COLOR_BTN_OUTLINE_BORDER,
            highlightthickness=1,
            bd=0,
            relief=tk.FLAT,
            font=self.font_sub,
            padx=10,
            pady=4,
            cursor="hand2",
            command=self._toggle_log_drawer,
        )
        btn_view_logs.pack(side=tk.RIGHT)

        # Drawer Log Frame (Collapsible above bottom bar)
        self.drawer_log_frame = tk.Frame(self.root, bg="#ffffff", highlightbackground=COLOR_CARD_BORDER, highlightthickness=1, bd=0)
        self.is_drawer_open = False
        self._build_log_console(self.drawer_log_frame)

        # Compatible references for old tests
        dummy = tk.Canvas(self.root, width=1, height=1)
        lt = dummy.create_oval(0, 0, 1, 1, fill=COLOR_ACCENT_GREEN)
        detail = tk.Label(self.root, text="")
        btn = tk.Button(self.root, text="")
        self.row_daemon = {"canvas": dummy, "light": lt, "detail": detail, "button": btn, "on_start": self._action_start_daemon, "on_stop": self._action_start_daemon}
        self.row_cockpit = {"canvas": dummy, "light": lt, "detail": detail, "button": btn, "on_start": lambda: None, "on_stop": lambda: None}
        self.row_bridge = {"canvas": dummy, "light": lt, "detail": detail, "button": btn, "on_start": lambda: None, "on_stop": lambda: None}
        self.row_agent = {"canvas": dummy, "light": lt, "detail": detail, "button": btn, "on_start": lambda: None, "on_stop": lambda: None}

    def _toggle_log_drawer(self) -> None:
        self.is_drawer_open = not self.is_drawer_open
        if self.is_drawer_open:
            self.drawer_log_frame.pack(side=tk.BOTTOM, fill=tk.X, before=self.bottom_bar_frame)
            self.lbl_drawer_arrow.config(text=" ∨")
        else:
            self.drawer_log_frame.pack_forget()
            self.lbl_drawer_arrow.config(text=" ∧")

    def _build_log_console(self, parent=None) -> None:
        p = parent if parent is not None else self.root
        console_frame = tk.Frame(p, bg=COLOR_CARD_BG, padx=12, pady=8)
        console_frame.pack(fill=tk.BOTH, expand=True)

        hdr = tk.Frame(console_frame, bg=COLOR_CARD_BG, height=22)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="实时控制台输出 (Live Logs):", fg=COLOR_TEXT_MUTED, bg=COLOR_CARD_BG, font=self.font_sub).pack(side=tk.LEFT)

        btn_clear = tk.Button(
            hdr,
            text="清屏",
            bg="#ffffff",
            fg=COLOR_BTN_OUTLINE_TEXT,
            activebackground=COLOR_BTN_OUTLINE_HOVER,
            highlightbackground=COLOR_BTN_OUTLINE_BORDER,
            highlightthickness=1,
            bd=0,
            relief=tk.FLAT,
            font=self.font_sub,
            padx=6,
            pady=1,
            command=self._clear_log,
        )
        btn_clear.pack(side=tk.RIGHT)

        self.log_text = tk.Text(
            console_frame,
            bg=COLOR_INPUT_BG,
            fg=COLOR_TEXT_MAIN,
            font=self.font_mono,
            height=6,
            relief=tk.FLAT,
            bd=2,
            state=tk.DISABLED,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

    def _clear_log(self) -> None:
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _render_ai_cards(self) -> None:
        """Compatibility wrapper for seat refresh."""
        self._render_connected_cards()

    def _test_seat_connection(self, seat: SeatConfig):
        token = seat.get_token() or (os.environ.get(seat.bot_token_env) or "").strip()
        if not token:
            messagebox.showinfo("提示", f"席位 [{seat.name}] 尚未配置 Bot Token，请点击管理进行配置。")
            return

        def _do_probe():
            valid, info = verify_bot_token(token)
            if valid:
                show_floating_toast(
                    self.root,
                    "连接正常",
                    f"席位 [{seat.name}] 成功连通 Telegram!\nBot: @{info.get('username')}",
                    icon="🟢",
                )
            else:
                err = info.get("error", "连接失败")
                show_floating_toast(
                    self.root,
                    "连接失败",
                    f"席位 [{seat.name}] Token 验证未通过:\n{err}",
                    icon="🔴",
                    border_color=COLOR_DANGER,
                )
        threading.Thread(target=_do_probe, daemon=True).start()

    def _remove_seat(self, role_key: str, name: str):
        if messagebox.askyesno("确认移除", f"确定要移除 AI 席位 [{name}] 吗？", parent=self.root):
            seats_cfg = self.mgr.load_seats_config()
            if role_key in seats_cfg.seats:
                del seats_cfg.seats[role_key]
                self.mgr.save_seats_config(seats_cfg)
                show_floating_toast(self.root, "席位已移除", f"席位 [{name}] 已从编队配置中移除。")
                self._render_connected_cards()

    def _build_log_console(self, parent=None) -> None:
        p = parent if parent is not None else self.root
        console_frame = tk.Frame(p, bg=COLOR_CARD_BG)
        console_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        hdr = tk.Frame(console_frame, bg=COLOR_CARD_BG, height=22)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="实时控制台输出 (Live Logs):", fg=COLOR_TEXT_MUTED, bg=COLOR_CARD_BG, font=self.font_sub).pack(side=tk.LEFT)

        btn_clear = tk.Button(
            hdr,
            text="清屏",
            bg=COLOR_CONTROL,
            fg=COLOR_TEXT_MAIN,
            font=self.font_sub,
            relief=tk.FLAT,
            padx=4,
            pady=0,
            command=self._clear_log,
        )
        btn_clear.pack(side=tk.RIGHT)

        self.log_text = tk.Text(
            console_frame,
            bg=COLOR_INPUT_BG,
            fg=COLOR_TEXT_MAIN,
            font=self.font_mono,
            height=4,
            relief=tk.FLAT,
            bd=2,
            state=tk.DISABLED,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

    def _clear_log(self) -> None:
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)

    def open_telegram_group_dialog(self) -> None:
        TelegramGroupConfigDialog(self.root, fleet_mgr=self.mgr, on_save_callback=self._on_seats_saved)

    def action_add_ai(self, category: str = "code") -> None:
        AddOrEditAiDialog(self.root, fleet_mgr=self.mgr, role_key=None, default_category=category, on_save_callback=self._on_seats_saved)

    def open_edit_ai_dialog(self, role_key: str) -> None:
        seats_cfg = self.mgr.load_seats_config()
        seat = seats_cfg.seats.get(role_key)
        cat = "code" if (seat and seat.engine in ALLOWED_CODE_ENGINES) else "chat"
        AddOrEditAiDialog(self.root, fleet_mgr=self.mgr, role_key=role_key, default_category=cat, on_save_callback=self._on_seats_saved)

    def open_three_seats_dialog(self) -> None:
        ThreeSeatsConfigDialog(self.root, fleet_mgr=self.mgr, on_save_callback=self._on_seats_saved)

    def _on_seats_saved(self) -> None:
        self.append_log("👥 [SEATS] 席位与群组配置已保存并同步。")
        self._render_ai_cards()
        self._refresh_status()

    def open_antigravity_tracks_dialog(self) -> None:
        AntigravityTracksDialog(self.root, fleet_mgr=self.mgr, on_bind_callback=self._on_track_bound)

    def _on_track_bound(self) -> None:
        self.append_log("🧭 [TRACK] Antigravity 轨道已绑定。")
        self._render_ai_cards()

    def open_setup_wizard(self) -> None:
        self.open_three_seats_dialog()

    def _action_open_browser(self) -> None:
        if not self.mgr.is_cockpit_running():
            self.mgr.start_cockpit(8765, open_browser=True)
        else:
            webbrowser.open("http://127.0.0.1:8765")

    def _action_open_tg(self) -> None:
        """Open Telegram group, client or bot."""
        cfg = self.mgr.load_seats_config()
        if cfg.telegram_chat_id:
            # If standard negative chat ID, attempt Telegram deep link or web
            cid = cfg.telegram_chat_id.replace("-100", "").replace("-", "")
            webbrowser.open(f"https://t.me/c/{cid}")
        else:
            lead = cfg.seats.get("lead")
            if lead and lead.bot_username:
                uname = lead.bot_username.lstrip("@")
                webbrowser.open(f"https://t.me/{uname}")
            else:
                webbrowser.open("https://web.telegram.org/")

    def _action_install_ext(self) -> None:
        ext_dir = REPO_ROOT / "web-extension"
        if ext_dir.exists():
            webbrowser.open(f"file:///{ext_dir.as_posix()}")
        webbrowser.open("chrome://extensions")

    def _action_start_daemon(self) -> None:
        def _run():
            started = self.mgr.start_daemon()
            if not started:
                self.append_log("🔴 [ERROR] 无法启动 Telegram 守护网关，请先检查 Bot Token 与白名单配置。")
            else:
                self.append_log("🟢 [DAEMON] Telegram 守护网关已成功启动。")
        threading.Thread(target=_run, daemon=True).start()

    def action_start_all(self) -> None:
        threading.Thread(target=self._action_start_daemon, daemon=True).start()
        threading.Thread(target=self.mgr.start_bridge, daemon=True).start()
        threading.Thread(target=lambda: self.mgr.start_cockpit(8765, False), daemon=True).start()
        show_floating_toast(self.root, "正在启动所有服务", "已发送全部网关服务启动指令。")

    def action_stop_all(self) -> None:
        threading.Thread(target=self.mgr.stop_daemon, daemon=True).start()
        threading.Thread(target=self.mgr.stop_bridge, daemon=True).start()
        threading.Thread(target=self.mgr.stop_cockpit, daemon=True).start()
        show_floating_toast(self.root, "正在停止服务", "已发送网关停止指令。")

    def _update_row(self, row: dict, is_running: bool, detail: str) -> None:
        color = COLOR_ACCENT_GREEN if is_running else COLOR_DANGER
        row["canvas"].itemconfig(row["light"], fill=color)
        row["detail"].config(text=detail)
        if "button" in row:
            if is_running:
                row["button"].config(
                    text="Stop",
                    bg=COLOR_DANGER,
                    command=row["on_stop"],
                )
            else:
                row["button"].config(
                    text="Start",
                    bg=COLOR_ACCENT_GREEN,
                    command=row["on_start"],
                )

    def _setup_tray(self) -> None:
        if not HAS_TRAY or pystray is None:
            return

        icon_img = create_tray_image("cyan")
        if not icon_img:
            return

        menu = pystray.Menu(
            pystray.MenuItem("Restore Window", lambda: self.root.after(0, self._restore_window)),
            pystray.MenuItem("Start All", lambda: self.root.after(0, self.action_start_all)),
            pystray.MenuItem("Stop All", lambda: self.root.after(0, self.action_stop_all)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", lambda: self.root.after(0, self.quit_app)),
        )

        self.tray_icon = pystray.Icon("pocketfleet", icon_img, "PocketFleet Control", menu)
        threading.Thread(target=self.tray_icon.run, daemon=True, name="SystemTrayThread").start()

    def hide_to_tray(self) -> None:
        if self.tray_icon:
            self.root.withdraw()
        else:
            self.quit_app()

    def _restore_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit_app(self) -> None:
        self.is_quitting = True
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        self.mgr.stop_daemon()
        self.mgr.stop_bridge()
        self.root.destroy()


_SINGLE_INSTANCE_SOCKET = None



def acquire_single_instance_lock(port: int = 18766) -> bool:
    global _SINGLE_INSTANCE_SOCKET
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(5)
        _SINGLE_INSTANCE_SOCKET = s
        return True
    except Exception:
        return False


def notify_existing_instance_or_takeover(port: int = 18766) -> bool:
    """Send RESTORE_WINDOW to the existing instance to bring it to front.
    If responsive, return True. If unresponsive/stale, kill it and return False.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        s.connect(("127.0.0.1", port))
        s.sendall(b"RESTORE_WINDOW\n")
        resp = s.recv(1024)
        s.close()
        if b"ACK" in resp:
            logger.info("Existing PocketFleet window notified and restored to front.")
            return True
    except Exception as e:
        logger.debug("Could not notify existing instance: %s", e)

    # If socket did not respond with ACK, it's a stale/frozen zombie. Kill it!
    try:
        import subprocess
        creation_flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        res = subprocess.run(
            'netstat -ano | findstr "18766"',
            shell=True,
            capture_output=True,
            text=True,
            creationflags=creation_flags,
        )
        if res.returncode == 0 and res.stdout:
            for line in res.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) >= 5 and "LISTENING" in parts:
                    pid = parts[-1]
                    if pid != str(os.getpid()):
                        subprocess.run(
                            f"taskkill /F /PID {pid}",
                            shell=True,
                            capture_output=True,
                            creationflags=creation_flags,
                        )
        time.sleep(0.5)
    except Exception as ex:
        logger.error("Failed to kill stale instance: %s", ex)
    return False


def start_single_instance_listener(app: "PocketFleetControlApp", port: int = 18766) -> None:
    """Background listener to restore window when another instance tries to launch."""
    global _SINGLE_INSTANCE_SOCKET
    if not _SINGLE_INSTANCE_SOCKET:
        return

    def _listen():
        while not getattr(app, "is_quitting", False):
            try:
                conn, _ = _SINGLE_INSTANCE_SOCKET.accept()
                data = conn.recv(1024)
                if b"RESTORE_WINDOW" in data:
                    app.root.after(0, app._restore_window)
                    conn.sendall(b"ACK\n")
                conn.close()
            except Exception:
                break

    t = threading.Thread(target=_listen, daemon=True, name="SingleInstanceListener")
    t.start()


def main():
    multiprocessing.freeze_support()
    acquired = acquire_single_instance_lock(18766)
    if not acquired:
        # Another instance is already bound. Ask it to bring its window to the front!
        if notify_existing_instance_or_takeover(18766):
            # Existing instance has brought its window to front. Exit cleanly.
            sys.exit(0)
        # Old zombie was killed, try to acquire lock again
        acquired = acquire_single_instance_lock(18766)
        # Even if lock wasn't re-acquired (e.g. socket in TIME_WAIT), do NOT exit;
        # proceed to launch the UI so the user always sees their application!

    root = tk.Tk()
    app = PocketFleetControlApp(root)
    if acquired:
        start_single_instance_listener(app, 18766)
    root.mainloop()


if __name__ == "__main__":
    main()
