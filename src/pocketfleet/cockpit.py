"""PocketFleet Local Web Cockpit (通信中枢控制台)

A lightweight, zero-dependency, local-only web dashboard for inspecting
real-time Telegram tasks, worker statuses, and system diagnostics.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

# Single-Page Embedded Web Cockpit
COCKPIT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>PocketFleet Cockpit — Communication & Telemetry Hub</title>
  <style>
    :root {
      --bg: #07090e;
      --card-bg: rgba(18, 23, 34, 0.85);
      --border: rgba(255, 255, 255, 0.08);
      --cyan: #00f0ff;
      --green: #10b981;
      --yellow: #f59e0b;
      --red: #ef4444;
      --text: #f0f4f8;
      --sub: #8a99ad;
      --mono: 'Fira Code', 'Courier New', monospace;
      --sans: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: var(--sans);
      padding: 24px;
      line-height: 1.5;
      background-image: radial-gradient(circle at 50% 0%, rgba(0, 240, 255, 0.08) 0%, transparent 60%);
      min-height: 100vh;
    }
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 24px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--border);
    }
    .logo {
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 1.4rem;
      font-weight: 700;
      letter-spacing: -0.02em;
    }
    .logo span { color: var(--cyan); }
    .badge-live {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: rgba(16, 185, 129, 0.15);
      border: 1px solid var(--green);
      color: var(--green);
      padding: 4px 10px;
      border-radius: 20px;
      font-size: 0.75rem;
      font-weight: 600;
    }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; animation: pulse 1.5s infinite; }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

    /* Grid layout */
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }
    .card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px 20px;
      backdrop-filter: blur(8px);
    }
    .card-label { font-size: 0.78rem; color: var(--sub); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
    .card-val { font-size: 1.25rem; font-weight: 700; color: #fff; font-family: var(--mono); }
    .card-sub { font-size: 0.8rem; color: var(--cyan); margin-top: 4px; }

    /* Sections */
    .section-title {
      font-size: 1rem;
      font-weight: 600;
      margin-bottom: 12px;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .table-card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      overflow: hidden;
      margin-bottom: 24px;
    }
    table { width: 100%; border-collapse: collapse; font-size: 0.88rem; text-align: left; }
    th {
      background: rgba(255, 255, 255, 0.03);
      padding: 12px 16px;
      color: var(--sub);
      font-weight: 600;
      border-bottom: 1px solid var(--border);
    }
    td { padding: 12px 16px; border-bottom: 1px solid var(--border); vertical-align: top; }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: rgba(255, 255, 255, 0.02); }
    .pill {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 0.75rem;
      font-weight: 600;
      font-family: var(--mono);
    }
    .pill-success { background: rgba(16, 185, 129, 0.2); color: var(--green); }
    .pill-running { background: rgba(0, 240, 255, 0.2); color: var(--cyan); }
    .pill-failed { background: rgba(239, 68, 68, 0.2); color: var(--red); }
    .pill-pending { background: rgba(245, 158, 11, 0.2); color: var(--yellow); }

    /* Quick dispatch tester */
    .dispatch-box {
      display: flex;
      gap: 10px;
      margin-bottom: 24px;
    }
    .dispatch-box input {
      flex: 1;
      background: #0d111a;
      border: 1px solid var(--border);
      color: #fff;
      padding: 10px 14px;
      border-radius: 8px;
      font-size: 0.9rem;
      outline: none;
    }
    .dispatch-box input:focus { border-color: var(--cyan); }
    .btn {
      background: linear-gradient(135deg, #00f0ff 0%, #3b82f6 100%);
      color: #000;
      font-weight: 700;
      border: none;
      padding: 10px 18px;
      border-radius: 8px;
      cursor: pointer;
      font-size: 0.9rem;
      transition: opacity 0.2s;
    }
    .btn:hover { opacity: 0.9; }

    /* Terminal output */
    pre.log-viewer {
      background: #05070a;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px;
      font-family: var(--mono);
      font-size: 0.82rem;
      color: #a5b4fc;
      max-height: 240px;
      overflow-y: auto;
      white-space: pre-wrap;
    }
  </style>
</head>
<body>
  <div class="header">
    <div class="logo">
      <span>🚀</span> PocketFleet <span>Cockpit</span>
    </div>
    <div class="badge-live">
      <div class="dot"></div> DAEMON ONLINE
    </div>
  </div>

  <!-- Telemetry HUD Cards -->
  <div class="grid">
    <div class="card">
      <div class="card-label">Telegram Transport</div>
      <div class="card-val" id="valTelegram">Connected</div>
      <div class="card-sub" id="valBotName">@PocketFleetBot</div>
    </div>
    <div class="card">
      <div class="card-label">Security Whitelist</div>
      <div class="card-val" id="valWhitelist">Guarded</div>
      <div class="card-sub" id="valChatId">Allowed Chat ID Locked</div>
    </div>
    <div class="card">
      <div class="card-label">Available Agents</div>
      <div class="card-val" id="valWorkers">Claude Code</div>
      <div class="card-sub" id="valWorkersSub">Zero Relay Servers</div>
    </div>
    <div class="card">
      <div class="card-label">Workspace Root</div>
      <div class="card-val" style="font-size: 0.95rem; word-break: break-all;" id="valWorkspace">/workspace</div>
      <div class="card-sub">Active Mutex Guarded</div>
    </div>
  </div>

  <!-- Quick Dispatch Tester -->
  <div class="section-title">⚡ Local Testing Dispatcher</div>
  <div class="dispatch-box">
    <input type="text" id="testPrompt" placeholder="Type prompt to simulate inbound mobile dispatch (e.g. 'check git status')...">
    <button class="btn" onclick="triggerTestTask()">Dispatch Task</button>
  </div>

  <!-- Real-time Tasks Table -->
  <div class="section-title">📡 Real-Time Telemetry Bus (Tasks & Logs)</div>
  <div class="table-card">
    <table>
      <thead>
        <tr>
          <th>Time</th>
          <th>Prompt</th>
          <th>Worker</th>
          <th>Status</th>
          <th>Duration</th>
          <th>Output Preview</th>
        </tr>
      </thead>
      <tbody id="taskTableBody">
        <tr><td colspan="6" style="text-align: center; color: var(--sub);">No tasks executed yet. Send a prompt from Telegram or use the tester above.</td></tr>
      </tbody>
    </table>
  </div>

  <!-- Console Logs -->
  <div class="section-title">📋 Live Telemetry Logs</div>
  <pre class="log-viewer" id="liveLogs">[PocketFleet Cockpit] Initializing local telemetry stream...
[PocketFleet Cockpit] Echo-Proof DAG Guard Active. Listening on port 8765.
</pre>

  <script>
    async function refreshStatus() {
      try {
        const res = await fetch('/api/status');
        if (!res.ok) return;
        const data = await res.json();
        
        document.getElementById('valTelegram').textContent = data.telegram_connected ? 'Connected' : 'Listening';
        document.getElementById('valBotName').textContent = data.bot_username || 'Bot Configured';
        document.getElementById('valWhitelist').textContent = data.whitelist_active ? 'Guarded' : 'Open (Dev)';
        document.getElementById('valChatId').textContent = data.allowed_chat_ids ? ('ID: ' + data.allowed_chat_ids) : 'Warning: No Whitelist';
        document.getElementById('valWorkers').textContent = data.workers.join(', ') || 'None';
        document.getElementById('valWorkspace').textContent = data.workspace || 'Default';

        renderTasks(data.tasks || []);
      } catch (e) {
        console.error('Failed to poll status', e);
      }
    }

    function renderTasks(tasks) {
      const tbody = document.getElementById('taskTableBody');
      if (!tasks.length) return;
      tbody.innerHTML = tasks.map(t => {
        let pillClass = 'pill-pending';
        if (t.status === 'COMPLETED') pillClass = 'pill-success';
        if (t.status === 'RUNNING') pillClass = 'pill-running';
        if (t.status === 'FAILED') pillClass = 'pill-failed';

        return `<tr>
          <td>${t.created_at || 'Just now'}</td>
          <td><strong>${escapeHtml(t.prompt)}</strong></td>
          <td><code>${t.worker}</code></td>
          <td><span class="pill ${pillClass}">${t.status}</span></td>
          <td>${t.duration_sec ? t.duration_sec + 's' : '-'}</td>
          <td><code style="font-size: 0.78rem;">${escapeHtml(t.preview || '-')}</code></td>
        </tr>`;
      }).join('');
    }

    async function triggerTestTask() {
      const input = document.getElementById('testPrompt');
      const val = input.value.trim();
      if (!val) return;
      
      const logViewer = document.getElementById('liveLogs');
      logViewer.textContent += `\\n>> [Cockpit Dispatch] "${val}" sent to local worker...`;
      logViewer.scrollTop = logViewer.scrollHeight;

      try {
        const res = await fetch('/api/dispatch', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ prompt: val })
        });
        const json = await res.json();
        logViewer.textContent += `\\n<< [Cockpit Response] Status: ${json.status}. Result: ${json.result || json.error || 'OK'}`;
        logViewer.scrollTop = logViewer.scrollHeight;
        input.value = '';
        refreshStatus();
      } catch (err) {
        logViewer.textContent += `\\n[!] Dispatch error: ${err}`;
      }
    }

    function escapeHtml(str) {
      if (!str) return '';
      return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    // Auto refresh every 2.5 seconds
    setInterval(refreshStatus, 2500);
    refreshStatus();
  </script>
</body>
</html>
"""


class CockpitTelemetry:
    """Thread-safe telemetry state container."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.workspace: str = ""
        self.bot_username: str = ""
        self.allowed_chat_ids: list[int] = []
        self.available_workers: list[str] = []
        self.telegram_connected: bool = True
        self.tasks: list[dict[str, Any]] = []

    def record_task(
        self,
        prompt: str,
        worker: str,
        status: str,
        duration_sec: float = 0.0,
        preview: str = "",
    ) -> None:
        with self.lock:
            entry = {
                "created_at": time.strftime("%H:%M:%S"),
                "prompt": prompt,
                "worker": worker,
                "status": status,
                "duration_sec": round(duration_sec, 2),
                "preview": preview[:120] if preview else "",
            }
            self.tasks.insert(0, entry)
            if len(self.tasks) > 50:
                self.tasks = self.tasks[:50]

    def to_dict(self) -> dict[str, Any]:
        with self.lock:
            return {
                "workspace": self.workspace,
                "bot_username": self.bot_username,
                "whitelist_active": bool(self.allowed_chat_ids),
                "allowed_chat_ids": ", ".join(map(str, self.allowed_chat_ids)),
                "workers": self.available_workers,
                "telegram_connected": self.telegram_connected,
                "tasks": list(self.tasks),
            }


telemetry = CockpitTelemetry()


class CockpitRequestHandler(BaseHTTPRequestHandler):
    """Zero-dependency HTTP Handler for Local Cockpit."""

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress noisy HTTP request logging
        pass

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(COCKPIT_HTML.encode("utf-8"))
            return

        if parsed.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            data = telemetry.to_dict()
            self.wfile.write(json.dumps(data).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/dispatch":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                payload = json.loads(body.decode("utf-8"))
                prompt = payload.get("prompt", "").strip()
                if not prompt:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b'{"error": "Empty prompt"}')
                    return

                # Record test task in telemetry
                telemetry.record_task(
                    prompt=prompt,
                    worker="local_cockpit",
                    status="COMPLETED",
                    duration_sec=0.1,
                    preview="[Test Simulation Executed Successfully via Cockpit]",
                )

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": "COMPLETED", "result": "Dispatched to local loop"}')
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()


class CockpitServer:
    """Embedded Cockpit daemon running in a background daemon thread."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.host = host
        self.port = port
        self.server: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self, auto_open: bool = False) -> bool:
        try:
            self.server = ThreadingHTTPServer((self.host, self.port), CockpitRequestHandler)
        except OSError:
            # If default port is taken, try port + 1
            try:
                self.port += 1
                self.server = ThreadingHTTPServer((self.host, self.port), CockpitRequestHandler)
            except OSError as err:
                logger.warning("Could not bind Cockpit Web Server: %s", err)
                return False

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        logger.info("PocketFleet Web Cockpit active at http://%s:%d", self.host, self.port)

        if auto_open:
            webbrowser.open(f"http://{self.host}:{self.port}")
        return True

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
