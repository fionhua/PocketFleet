#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🛸 PocketFleet Control Panel (Solo Hacker Edition)
XAMPP-Style Desktop Tray Controller for PocketFleet

Features:
- Multi-service status monitoring (Telegram Bridge Daemon, AI Executor, Local Web Cockpit)
- Millisecond-level alive probing & process tree supervision
- 1-Click Start All / Stop All
- Windows System Tray resident ("Tony" Icon) with right-click menu & notifications
- Real-time embedded console log window
- Web Cockpit browser launcher
"""

import ctypes
import json
import os
import socket
import subprocess
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

# ==============================================================================
# Environment & Paths
# ==============================================================================
if getattr(sys, "frozen", False):
    REPO_ROOT = Path(sys.executable).resolve().parent
    PYTHON_EXE = sys.executable
else:
    REPO_ROOT = Path(__file__).resolve().parent.parent.parent
    PYTHON_EXE = sys.executable

SYSTEM_PYTHON = r"C:\Python312\python.exe" if Path(r"C:\Python312\python.exe").is_file() else PYTHON_EXE
CONFIG_FILE = REPO_ROOT / "pocketfleet.json"
PID_FILE_DAEMON = REPO_ROOT / ".pocketfleet_daemon.pid"
PID_FILE_COCKPIT = REPO_ROOT / ".pocketfleet_cockpit.pid"


# ==============================================================================
# Process & Win32 Helpers
# ==============================================================================
def is_pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h_proc = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h_proc:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetExitCodeProcess(h_proc, ctypes.byref(exit_code)):
            return exit_code.value == 259  # STILL_ACTIVE
        return False
    finally:
        ctypes.windll.kernel32.CloseHandle(h_proc)


def kill_pid_tree(pid: int | None) -> None:
    if not pid or pid <= 0:
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


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
    # Draw rocket / starfleet badge shape
    draw.rounded_rectangle([4, 4, 60, 60], radius=16, fill="#0f172a", outline=hex_color, width=3)
    draw.polygon([(32, 12), (48, 48), (32, 40), (16, 48)], fill=hex_color)
    draw.ellipse([28, 24, 36, 32], fill="#ffffff")
    return img


# ==============================================================================
# Process Manager
# ==============================================================================
class FleetManager:
    def __init__(self, log_cb):
        self.log = log_cb
        self.daemon_proc = None
        self.cockpit_proc = None

    def get_daemon_pid(self) -> int | None:
        if self.daemon_proc and self.daemon_proc.poll() is None:
            return self.daemon_proc.pid
        if PID_FILE_DAEMON.is_file():
            try:
                pid = int(PID_FILE_DAEMON.read_text().strip())
                if is_pid_alive(pid):
                    return pid
            except Exception:
                pass
        return None

    def get_cockpit_pid(self) -> int | None:
        if self.cockpit_proc and self.cockpit_proc.poll() is None:
            return self.cockpit_proc.pid
        if PID_FILE_COCKPIT.is_file():
            try:
                pid = int(PID_FILE_COCKPIT.read_text().strip())
                if is_pid_alive(pid):
                    return pid
            except Exception:
                pass
        return None

    def start_daemon(self, executor: str = "claude_code") -> None:
        pid = self.get_daemon_pid()
        if pid:
            self.log(f"[WARN] PocketFleet Daemon is already running (PID: {pid})")
            return
        
        self.log(f"[DAEMON] Launching PocketFleet Bridge Daemon (Executor: {executor})...")
        cmd = [SYSTEM_PYTHON, "-m", "pocketfleet.launcher", "run", "--worker", executor]
        try:
            p = subprocess.Popen(
                cmd,
                cwd=str(REPO_ROOT),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.daemon_proc = p
            PID_FILE_DAEMON.write_text(str(p.pid))
            self.log(f"[DAEMON] Started successfully (PID: {p.pid})")
        except Exception as e:
            self.log(f"[ERROR] Failed to start daemon: {e}")

    def stop_daemon(self) -> None:
        pid = self.get_daemon_pid()
        if not pid:
            self.log("[DAEMON] Daemon is not running.")
            return
        self.log(f"[DAEMON] Stopping daemon (PID: {pid})...")
        kill_pid_tree(pid)
        if PID_FILE_DAEMON.is_file():
            try:
                PID_FILE_DAEMON.unlink()
            except Exception:
                pass
        self.daemon_proc = None
        self.log("[DAEMON] Daemon stopped.")

    def start_cockpit(self, port: int = 8765) -> None:
        if is_port_listening(port):
            self.log(f"[COCKPIT] Web Cockpit already listening on port {port}")
            webbrowser.open(f"http://127.0.0.1:{port}")
            return
        
        self.log(f"[COCKPIT] Launching Web Cockpit on port {port}...")
        cmd = [SYSTEM_PYTHON, "-m", "pocketfleet.launcher", "--ui", "--port", str(port)]
        try:
            p = subprocess.Popen(
                cmd,
                cwd=str(REPO_ROOT),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.cockpit_proc = p
            PID_FILE_COCKPIT.write_text(str(p.pid))
            self.log(f"[COCKPIT] Started on port {port} (PID: {p.pid})")
            time.sleep(0.8)
            webbrowser.open(f"http://127.0.0.1:{port}")
        except Exception as e:
            self.log(f"[ERROR] Failed to start cockpit: {e}")

    def stop_cockpit(self) -> None:
        pid = self.get_cockpit_pid()
        if not pid:
            self.log("[COCKPIT] Web Cockpit is not running.")
            return
        self.log(f"[COCKPIT] Stopping Web Cockpit (PID: {pid})...")
        kill_pid_tree(pid)
        if PID_FILE_COCKPIT.is_file():
            try:
                PID_FILE_COCKPIT.unlink()
            except Exception:
                pass
        self.cockpit_proc = None
        self.log("[COCKPIT] Cockpit stopped.")


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

        # Handle window close (minimize to tray instead of destroy)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        # Polling thread for status updates
        self.poll_thread = threading.Thread(target=self._status_poll_loop, daemon=True)
        self.poll_thread.start()

        self.append_log("🚀 PocketFleet Control Panel initialized. Ready to command.")

    def append_log(self, text: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        msg = f"[{ts}] {text}\n"

        def _insert():
            try:
                self.log_text.config(state=tk.NORMAL)
                self.log_text.insert(tk.END, msg)
                self.log_text.see(tk.END)
                self.log_text.config(state=tk.DISABLED)
            except Exception:
                pass
        self.root.after(0, _insert)

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
            on_start=lambda: threading.Thread(target=self.mgr.start_daemon, daemon=True).start(),
            on_stop=lambda: threading.Thread(target=self.mgr.stop_daemon, daemon=True).start(),
            aux_text="Edit Config",
            aux_cmd=self._open_config,
        )

        # Row 2: Local Web Cockpit
        self.row_cockpit = self._create_service_row(
            table_card,
            name="2. Local Web Cockpit (UI)",
            on_start=lambda: threading.Thread(target=self.mgr.start_cockpit, daemon=True).start(),
            on_stop=lambda: threading.Thread(target=self.mgr.stop_cockpit, daemon=True).start(),
            aux_text="Open Browser",
            aux_cmd=lambda: webbrowser.open("http://127.0.0.1:8765"),
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

    def _open_config(self) -> None:
        if not CONFIG_FILE.is_file():
            # Create template
            template = {
                "bot_token": "YOUR_TELEGRAM_BOT_TOKEN",
                "authorized_user_ids": [12345678],
                "executor": "claude_code"
            }
            CONFIG_FILE.write_text(json.dumps(template, indent=2))
        os.startfile(str(CONFIG_FILE))

    def action_start_all(self) -> None:
        self.append_log("Starting all PocketFleet services...")
        def _run():
            self.mgr.start_daemon()
            time.sleep(0.5)
            self.mgr.start_cockpit()
            self.append_log("All services started.")
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
            pystray.MenuItem("🌐 Open Web Cockpit (Port 8765)", lambda: webbrowser.open("http://127.0.0.1:8765")),
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
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.after(0, self.root.destroy)

    # ---------------- Polling Loop ----------------
    def _status_poll_loop(self) -> None:
        while not self.is_quitting:
            try:
                # 1. Daemon
                d_pid = self.mgr.get_daemon_pid()
                self._update_row(self.row_daemon, is_running=bool(d_pid), detail=f"Active (PID: {d_pid})" if d_pid else "Stopped")

                # 2. Cockpit
                c_pid = self.mgr.get_cockpit_pid()
                c_port = 8765
                is_c_active = is_port_listening(c_port)
                self._update_row(self.row_cockpit, is_running=is_c_active, detail=f"Listening on :8765" if is_c_active else "Offline")

                # 3. Agent
                self._update_row(self.row_agent, is_running=True, detail="Ready (Claude Code / Aider)")

                # Update tray icon color
                if self.tray_icon:
                    color = "green" if (d_pid or is_c_active) else "cyan"
                    self.tray_icon.icon = create_tray_image(color)
            except Exception:
                pass
            time.sleep(1.0)

    def _update_row(self, row: dict, is_running: bool, detail: str) -> None:
        def _ui():
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
        self.root.after(0, _ui)


def main():
    root = tk.Tk()
    app = PocketFleetControlApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
