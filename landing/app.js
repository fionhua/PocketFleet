/**
 * PocketFleet Live Dual Simulator Engine
 * Synchronizes mobile Telegram mockup with remote VPS terminal output.
 */

const tgChatBody = document.getElementById("tgChatBody");
const tgInput = document.getElementById("tgInput");
const termBody = document.getElementById("termBody");
const termCursorLine = document.getElementById("termCursorLine");

let isSimulating = false;

function getTimeString() {
  const d = new Date();
  const h = String(d.getHours()).padStart(2, "0");
  const m = String(d.getMinutes()).padStart(2, "0");
  return `${h}:${m}`;
}

function appendUserMessage(text) {
  const msgEl = document.createElement("div");
  msgEl.className = "chat-msg user-msg";
  msgEl.innerHTML = `
    <div class="msg-content">${text}</div>
    <div class="msg-time">${getTimeString()}</div>
  `;
  tgChatBody.appendChild(msgEl);
  tgChatBody.scrollTop = tgChatBody.scrollHeight;
}

function appendBotMessage(html) {
  const msgEl = document.createElement("div");
  msgEl.className = "chat-msg bot-msg";
  msgEl.innerHTML = `
    <div class="msg-content">${html}</div>
    <div class="msg-time">${getTimeString()}</div>
  `;
  tgChatBody.appendChild(msgEl);
  tgChatBody.scrollTop = tgChatBody.scrollHeight;
}

function appendTerminalLine(text, className = "") {
  const line = document.createElement("div");
  line.className = `term-line ${className}`;
  line.textContent = text;
  termBody.insertBefore(line, termCursorLine);
  termBody.scrollTop = termBody.scrollHeight;
}

const SIMULATION_SCRIPTS = {
  "fix issue #42: JWT expiry error": [
    { delay: 400, text: "[Telegram] Inbound task received: 'fix issue #42: JWT expiry error'", cls: "cyan" },
    { delay: 700, text: "[Dispatch] Waking worker: Claude Code (CLI subagent)...", cls: "prefix" },
    { delay: 1200, text: "> claude 'fix issue #42: JWT expiry error' --cwd /app/backend-api", cls: "yellow" },
    { delay: 1800, text: "  [Claude] Scanning codebase for JWT expiration token handlers...", cls: "" },
    { delay: 2400, text: "  [Claude] Found src/auth/jwt.py: line 84 `if token.exp < now:`", cls: "" },
    { delay: 3000, text: "  [Claude] Applied diff: Added grace period & refreshed signature verification.", cls: "green" },
    { delay: 3600, text: "  [Claude] Running tests: `pytest tests/test_auth.py` -> 12 passed in 0.4s", cls: "green" },
    { delay: 4200, text: "  [Claude] Git commit: 'fix(auth): handle expired token edge-case (#42)'", cls: "cyan" },
    { delay: 4700, text: "[PocketFleet] Task finished with exit code 0. Dispatching Telegram response...", cls: "prefix" },
    { 
      delay: 5200, 
      botReply: `✅ <strong>Issue #42 Resolved</strong><br>
Worker: <strong>Claude Code</strong><br>
Files modified: <code>src/auth/jwt.py</code><br>
Tests: 12/12 passed (0.4s)<br>
Commit: <code>a7f89b2</code> pushed to branch <code>fix/jwt-expiry</code><br>
<a href="#" style="color:#00f0ff;">Review PR #43</a>`
    }
  ],
  "add healthcheck endpoint /healthz": [
    { delay: 400, text: "[Telegram] Inbound task received: 'add healthcheck endpoint /healthz'", cls: "cyan" },
    { delay: 700, text: "[Dispatch] Waking worker: Aider (CLI pair programmer)...", cls: "prefix" },
    { delay: 1200, text: "> aider --message 'add healthcheck endpoint /healthz' src/main.py", cls: "yellow" },
    { delay: 1900, text: "  [Aider] Adding FastAPI route `@app.get('/healthz')` returning `{'status': 'ok'}`", cls: "" },
    { delay: 2600, text: "  [Aider] Created test: `tests/test_health.py`", cls: "" },
    { delay: 3200, text: "  [Aider] Verified: curl http://localhost:8000/healthz -> 200 OK", cls: "green" },
    { delay: 3800, text: "  [Aider] Commit: 'feat: add /healthz endpoint for kubernetes liveness probe'", cls: "cyan" },
    { delay: 4400, text: "[PocketFleet] Task finished with exit code 0. Outbound sent.", cls: "prefix" },
    {
      delay: 4900,
      botReply: `🚀 <strong>Healthcheck Endpoint Added</strong><br>
Worker: <strong>Aider</strong><br>
Endpoint: <code>GET /healthz</code><br>
Tests: 1 new test passed<br>
Commit: <code>c4e810a</code> ready for merge.`
    }
  ],
  "optimize slow SQL query in analytics.py": [
    { delay: 400, text: "[Telegram] Inbound task received: 'optimize slow SQL query in analytics.py'", cls: "cyan" },
    { delay: 700, text: "[Dispatch] Waking worker: Claude Code...", cls: "prefix" },
    { delay: 1300, text: "> claude 'optimize slow SQL query in analytics.py'", cls: "yellow" },
    { delay: 2000, text: "  [Claude] Analyzing `get_daily_active_users()`: Found N+1 query issue.", cls: "" },
    { delay: 2700, text: "  [Claude] Refactored to single `JOIN` with indexed composite key.", cls: "green" },
    { delay: 3400, text: "  [Claude] Benchmark: Execution time dropped from 840ms -> 18ms (-97.8%)", cls: "green" },
    { delay: 4100, text: "  [Claude] Git commit: 'perf(analytics): optimize DAU calculation query'", cls: "cyan" },
    { delay: 4600, text: "[PocketFleet] Dispatching diff summary to Telegram...", cls: "prefix" },
    {
      delay: 5100,
      botReply: `⚡ <strong>Query Optimization Complete</strong><br>
Worker: <strong>Claude Code</strong><br>
Latency: <strong>840ms ➔ 18ms (-97.8%)</strong><br>
Commit: <code>d9a04f1</code> ready.`
    }
  ]
};

window.sendSimPrompt = function(promptText) {
  if (isSimulating) return;
  isSimulating = true;

  appendUserMessage(promptText);
  tgInput.value = promptText;

  const script = SIMULATION_SCRIPTS[promptText];
  if (!script) {
    isSimulating = false;
    return;
  }

  script.forEach(step => {
    setTimeout(() => {
      if (step.text) {
        appendTerminalLine(step.text, step.cls || "");
      }
      if (step.botReply) {
        appendBotMessage(step.botReply);
        isSimulating = false;
        tgInput.value = "";
      }
    }, step.delay);
  });
};

// Deployment Tabs Logic
document.querySelectorAll(".deploy-tab").forEach(tab => {
  tab.addEventListener("click", () => {
    const target = tab.getAttribute("data-tab");
    document.querySelectorAll(".deploy-tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".deploy-panel").forEach(p => p.classList.remove("active"));

    tab.classList.add("active");
    const activePanel = document.getElementById(`tab-${target}`);
    if (activePanel) {
      activePanel.classList.add("active");
    }
  });
});

// Copy Deployment Code Function
window.copyDeployCode = function(elementId) {
  const el = document.getElementById(elementId);
  if (!el) return;
  const text = el.innerText || el.textContent;
  navigator.clipboard.writeText(text).then(() => {
    const btn = event?.target;
    if (btn) {
      const origText = btn.textContent;
      btn.textContent = "✓ Copied!";
      btn.style.background = "rgba(16, 185, 129, 0.3)";
      btn.style.borderColor = "#10b981";
      setTimeout(() => {
        btn.textContent = origText;
        btn.style.background = "";
        btn.style.borderColor = "";
      }, 2000);
    }
  }).catch(err => {
    console.error("Clipboard copy failed:", err);
  });
};

