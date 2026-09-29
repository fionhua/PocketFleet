#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PocketFleet Control Panel (Solo Hacker Edition)
XAMPP-Style Desktop Tray Controller for PocketFleet

Features:
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
from tkinter import font as tkfont, messagebox

from PIL import Image, ImageDraw
import pystray

from pocketfleet.cockpit import CockpitServer, telemetry
from pocketfleet.core import WorkerType
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

    def start_daemon(self, executor: str = "claude_code") -> bool:
        if self.is_daemon_running():
            self.log("[WARN] Telegram Daemon is already active.")
            return True

        token, allowed_ids = self._load_credentials()
        if not token or token == "YOUR_TELEGRAM_BOT_TOKEN":
            self.log("[CONFIG] No valid Bot Token found! Please edit pocketfleet.json first.")
            return False

        self.log(f"[DAEMON] Initializing Telegram Dispatch Loop (Worker: {executor})...")
        try:
            state_store = StateStore()
            transport = TelegramTransport(bot_token=token, state_store=state_store)
            self.dispatch_loop = DispatchLoop(
                transport=transport,
                workspace_cwd=str(REPO_ROOT),
                default_worker=WorkerType(executor),
                allowed_chat_ids=allowed_ids,
                state_store=state_store,
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
            self.log("[DAEMON] Telegram Bridge Daemon running. Listening for tasks...")
            return True
        except Exception as e:
            self.log(f"[ERROR] Failed to start daemon: {e}")
            return False

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

    def _load_credentials(self) -> tuple[str | None, set[int] | None]:
        if CONFIG_FILE.is_file():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                tok = data.get("bot_token")
                ids = data.get("authorized_user_ids")
                set_ids = set(ids) if ids else None
                return tok, set_ids
            except Exception:
                pass

        saved = FleetConfig.load()
        if saved and saved.bot_token:
            return saved.bot_token, {saved.allowed_chat_id} if saved.allowed_chat_id else None

        env_tok = os.environ.get("POCKETFLEET_BOT_TOKEN")
        if env_tok:
            return env_tok, None

        return None, None


# ==============================================================================
# Main GUI Window (Tkinter)
# ==============================================================================
class PocketFleetControlApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PocketFleet Control Panel (Solo Hacker Edition)")
        self.root.geometry("820x680")
        self.root.minsize(760, 600)
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

        self._build_header()
        self._build_table()
        self._build_toolbar()
        self._build_log_console()

        self._setup_tray()
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        self._ensure_config_exists()

        self.append_log("🚀 PocketFleet Control Panel initialized. Ready to command.")

        # Start drain log loop on main thread
        self.root.after(100, self._drain_log_queue)

        # Start periodic status refresh on main thread
        self.root.after(500, self._refresh_status)

        # Auto-start Web Cockpit after mainloop starts
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
            # 1. Daemon
            is_d = self.mgr.is_daemon_running()
            self._update_row(
                self.row_daemon,
                is_running=is_d,
                detail="Active (Polling Telegram)" if is_d else "Stopped",
            )

            # 2. Cockpit
            is_c = self.mgr.is_cockpit_running()
            self._update_row(
                self.row_cockpit,
                is_running=is_c,
                detail="Listening on http://127.0.0.1:8765" if is_c else "Offline",
            )

            # 3. Agent
            self._update_row(self.row_agent, is_running=True, detail="Ready (Claude Code / Aider)")

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
            aux_text="Edit Config",
            aux_cmd=self._open_config,
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
            width=10,
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

    def _ensure_config_exists(self) -> None:
        if not CONFIG_FILE.is_file():
            template = {
                "bot_token": "YOUR_TELEGRAM_BOT_TOKEN",
                "authorized_user_ids": [12345678],
                "executor": "claude_code"
            }
            try:
                CONFIG_FILE.write_text(json.dumps(template, indent=2), encoding="utf-8")
            except Exception:
                pass

    def _open_config(self) -> None:
        self._ensure_config_exists()
        os.startfile(str(CONFIG_FILE))

    def _action_open_browser(self) -> None:
        if not self.mgr.is_cockpit_running():
            self.mgr.start_cockpit(8765, open_browser=True)
        else:
            webbrowser.open("http://127.0.0.1:8765")

    def _action_start_daemon(self) -> None:
        def _task():
            ok = self.mgr.start_daemon()
            if not ok:
                self.root.after(0, lambda: messagebox.showinfo(
                    "Telegram Bot Token Required",
                    "Please configure your Telegram Bot Token in pocketfleet.json first (the file will now open).\n\n"
                    "1. Get your Bot Token from @BotFather\n"
                    "2. Paste it into pocketfleet.json and save\n"
                    "3. Click Start again."
                ))
                self.root.after(200, self._open_config)
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
