#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PocketFleet Control Panel (Solo Hacker Edition)
XAMPP-Style Desktop Tray Controller for PocketFleet

Features:
- Built-in GUI Setup Wizard for 60-second Telegram Bot onboarding
- In-process Web Cockpit server (Port 8765) with 100% reliable zero-delay startup
- In-process DispatchLoop supervisor with Telegram connectivity
- Thread-safe queue architecture avoiding Tkinter mainloop collision
- Multi-service status monitoring with live status lights (Green/Red)
- Windows System Tray resident with right-click menu & notifications
- Real-time embedded console log window
- Direct browser launch & config file editor
"""

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
import tkinter as tk
from tkinter import font as tkfont, messagebox, ttk

from PIL import Image, ImageDraw

import pystray

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
    validate_seats_config,
)
from pocketfleet.loop import DispatchLoop

from pocketfleet.onboard import FleetConfig
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


def is_port_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.close()
        return True
    except OSError:
        return False


def create_tray_image(color: str = "cyan") -> Image.Image:
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


# ==============================================================================
# Three Seats Configuration Dialog (Triad Seats Setup)
# ==============================================================================
class ThreeSeatsConfigDialog(tk.Toplevel):
    def __init__(self, parent, fleet_mgr, on_save_callback=None):
        super().__init__(parent)
        self.parent = parent
        self.mgr = fleet_mgr
        self.on_save_callback = on_save_callback

        self.title("Fleet Triad Seats Configuration (三席位战队独立编排) — PocketFleet")
        self.geometry("680x750")
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
        cx = max(0, px + (pw - 680) // 2)
        cy = max(0, py + (ph - 750) // 2)
        self.geometry(f"+{cx}+{cy}")

        self.widgets = {}
        self._build_ui()
        self._load_values()

    def _build_ui(self):
        # Header banner
        header = tk.Frame(self, bg="#0f172a", padx=20, pady=12)
        header.pack(fill=tk.X)
        tk.Label(
            header,
            text="👥 Fleet Triad Seats Configuration (三席位独立编排)",
            fg="#38bdf8",
            bg="#0f172a",
            font=self.font_title,
        ).pack(anchor="w")
        tk.Label(
            header,
            text="三席位：对话AI (推演对账) + 施工指挥 (验收统筹) + 主力程序员 (核心编码) | 严禁明文Token",
            fg="#94a3b8",
            bg="#0f172a",
            font=self.font_sub,
        ).pack(anchor="w", pady=(2, 0))

        content = tk.Frame(self, bg="#0b0f19", padx=20, pady=12)
        content.pack(fill=tk.BOTH, expand=True)

        # 3 Seat Sections
        seat_defs = [
            ("chat", "💬 席位 1: 对话AI (Chat AI — 推演与宏观对账)", "#38bdf8", list(ALLOWED_CHAT_ENGINES)),
            ("lead", "🎖️ 席位 2: 施工指挥 (Lead — 架构守门与改卷验收)", "#10b981", list(ALLOWED_CODE_ENGINES)),
            ("builder", "🛠️ 席位 3: 主力程序员 (Builder — 核心施工与算法定桩)", "#f59e0b", list(ALLOWED_CODE_ENGINES)),
        ]

        for role_key, title, color, engine_choices in seat_defs:
            card = tk.LabelFrame(
                content,
                text=f" {title} ",
                fg=color,
                bg="#1e293b",
                font=self.font_bold,
                padx=12,
                pady=8,
                relief=tk.GROOVE,
            )
            card.pack(fill=tk.X, pady=(0, 10))

            # Row 1: Name & Engine
            r1 = tk.Frame(card, bg="#1e293b")
            r1.pack(fill=tk.X, pady=2)

            tk.Label(r1, text="席位代号:", fg="#f1f5f9", bg="#1e293b", font=self.font_sub, width=10, anchor="w").pack(side=tk.LEFT)
            entry_name = tk.Entry(r1, bg="#0f172a", fg="#f8fafc", insertbackground="#f8fafc", font=self.font_mono, width=18, relief=tk.FLAT, bd=4)
            entry_name.pack(side=tk.LEFT, padx=(0, 16))

            tk.Label(r1, text="执行引擎:", fg="#f1f5f9", bg="#1e293b", font=self.font_sub, width=10, anchor="w").pack(side=tk.LEFT)
            combo_eng = ttk.Combobox(r1, values=engine_choices, state="readonly", width=16)
            combo_eng.pack(side=tk.LEFT)

            # Row 2: Token Env Var & Username
            r2 = tk.Frame(card, bg="#1e293b")
            r2.pack(fill=tk.X, pady=2)

            tk.Label(r2, text="Token变量:", fg="#f1f5f9", bg="#1e293b", font=self.font_sub, width=10, anchor="w").pack(side=tk.LEFT)
            entry_env = tk.Entry(r2, bg="#0f172a", fg="#38bdf8", insertbackground="#38bdf8", font=self.font_mono, width=22, relief=tk.FLAT, bd=4)
            entry_env.pack(side=tk.LEFT, padx=(0, 8))

            tk.Label(r2, text="Bot用户名:", fg="#94a3b8", bg="#1e293b", font=self.font_sub, width=9, anchor="w").pack(side=tk.LEFT)
            entry_user = tk.Entry(r2, bg="#0f172a", fg="#94a3b8", insertbackground="#94a3b8", font=self.font_mono, width=18, relief=tk.FLAT, bd=4)
            entry_user.pack(side=tk.LEFT)

            # Row 3: Description & Command
            r3 = tk.Frame(card, bg="#1e293b")
            r3.pack(fill=tk.X, pady=2)

            tk.Label(r3, text="职责描述:", fg="#94a3b8", bg="#1e293b", font=self.font_sub, width=10, anchor="w").pack(side=tk.LEFT)
            entry_desc = tk.Entry(r3, bg="#0f172a", fg="#cbd5e1", insertbackground="#cbd5e1", font=self.font_sub, relief=tk.FLAT, bd=4)
            entry_desc.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

            tk.Label(r3, text="启动指令:", fg="#94a3b8", bg="#1e293b", font=self.font_sub, width=8, anchor="w").pack(side=tk.LEFT)
            entry_cmd = tk.Entry(r3, bg="#0f172a", fg="#a7f3d0", insertbackground="#a7f3d0", font=self.font_mono, width=12, relief=tk.FLAT, bd=4)
            entry_cmd.pack(side=tk.LEFT)

            self.widgets[role_key] = {
                "name": entry_name,
                "engine": combo_eng,
                "env": entry_env,
                "user": entry_user,
                "desc": entry_desc,
                "cmd": entry_cmd,
            }

        # Extra options
        opt_frame = tk.Frame(content, bg="#0b0f19")
        opt_frame.pack(fill=tk.X, pady=(4, 0))

        tk.Label(opt_frame, text="最近消息条数 (context_window):", fg="#94a3b8", bg="#0b0f19", font=self.font_sub).pack(side=tk.LEFT)
        self.entry_cw = tk.Entry(opt_frame, bg="#1e293b", fg="#f8fafc", font=self.font_mono, width=6, relief=tk.FLAT, bd=4)
        self.entry_cw.insert(0, "20")
        self.entry_cw.pack(side=tk.LEFT, padx=(4, 20))

        self.var_nositu = tk.BooleanVar(value=True)
        cb_nositu = tk.Checkbutton(
            opt_frame,
            text="不启用司柝中继",
            variable=self.var_nositu,
            bg="#0b0f19",
            fg="#38bdf8",
            selectcolor="#1e293b",
            activebackground="#0b0f19",
            activeforeground="#38bdf8",
            font=self.font_sub,
        )
        cb_nositu.pack(side=tk.LEFT)

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

    def _load_values(self):
        cfg = self.mgr.load_seats_config()
        self._populate_fields(cfg)

    def _populate_fields(self, cfg: FleetSeatsConfig):
        self.entry_cw.delete(0, tk.END)
        self.entry_cw.insert(0, str(cfg.context_window))
        self.var_nositu.set(cfg.no_situ)

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
            w["desc"].delete(0, tk.END)
            w["desc"].insert(0, seat.description)
            w["cmd"].delete(0, tk.END)
            w["cmd"].insert(0, seat.command)

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
            desc = w["desc"].get().strip()
            cmd = w["cmd"].get().strip()

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
                w["env"].focus_set()
                return

            if not user:
                messagebox.showerror("缺失配置", f"席位 '{role_key}' 必须指定 Bot 用户名！", parent=self)
                w["user"].focus_set()
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
            no_situ=self.var_nositu.get(),
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

        self.destroy()
        if self.on_save_callback:
            self.on_save_callback()


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
            "authorized_user_ids": [6801810539],
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
            return get_default_seats_config()
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValueError(f"配置文件 {CONFIG_FILE.name} 损坏 (JSON解析失败): {e}") from e

        if "seats" not in data:
            raise ValueError(f"配置文件 {CONFIG_FILE.name} 缺少 'seats' 三席位定义，配置非法！")

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

    def start_daemon(self, executor: str | None = None) -> bool:
        if self.is_daemon_running():
            self.log("[WARN] Telegram Daemon is already active.")
            return True

        cfg = self.load_fleet_config()
        roles = cfg.get("role_assignment", {"lead": "antigravity", "builder": "codex"})
        token, allowed_ids, configured_executor = self._load_credentials()

        if not token or token == "YOUR_TELEGRAM_BOT_TOKEN":
            self.log("[CONFIG] No valid Bot Token found! Wizard prompt triggered.")
            return False

        active_executor = executor or configured_executor or cfg.get("executor") or "fleet_triad"
        self.log("[DAEMON] Initializing evidence-backed dispatch loop...")
        try:
            state_store = StateStore()
            transport = TelegramTransport(bot_token=token, state_store=state_store)
            self.dispatch_loop = DispatchLoop(
                transport=transport,
                workspace_cwd=str(REPO_ROOT),
                default_worker=WorkerType(active_executor),
                allowed_chat_ids=allowed_ids,
                state_store=state_store,
                bots_config=cfg.get("bots", {}),
                role_assignment=RoleAssignment(lead=roles.get("lead", "antigravity"), builder=roles.get("builder", "codex")),
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
        if not self.is_daemon_running():
            self.log("[DAEMON] Daemon is not running.")
            return
        self.log("[DAEMON] Stopping Telegram Bridge Daemon...")
        if self.dispatch_loop:
            self.dispatch_loop.stop()
            self.dispatch_loop = None
        telemetry.telegram_connected = False
        self.log("[DAEMON] Daemon stopped.")

    def _load_credentials(self) -> tuple[str | None, set[int] | None, str | None]:
        seats_cfg = self.load_seats_config()
        lead_seat = seats_cfg.seats.get("lead")
        token_env = lead_seat.bot_token_env if lead_seat else "TELEGRAM_BOT_JUDGE_TOKEN"
        tok = os.environ.get(token_env) or os.environ.get("POCKETFLEET_BOT_TOKEN")
        ids = set(seats_cfg.authorized_user_ids) if seats_cfg.authorized_user_ids else None
        exec_type = lead_seat.engine if lead_seat else "fleet_triad"
        return tok, ids, exec_type



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
            padx=10,
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
        seat_roles = [
            ("chat", "💬 席位 1: 对话AI (Chat)", "#38bdf8"),
            ("lead", "🎖️ 席位 2: 施工指挥 (Lead)", "#10b981"),
            ("builder", "🛠️ 席位 3: 主力程序员 (Builder)", "#f59e0b"),
        ]

        for col, (role_key, role_label, accent_color) in enumerate(seat_roles):
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

            tk.Label(
                card_sub,
                text=f"Token变量: {token_env}",
                fg="#94a3b8",
                bg="#1e293b",
                font=self.font_mono,
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
