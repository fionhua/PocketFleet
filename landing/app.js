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
    { delay: 300, text: "[WarRoom] Human Commander assigned task: 'fix issue #42: JWT expiry error'", cls: "cyan" },
    { delay: 700, text: "[Swarm] Alpha (Architect) setting acceptance criteria and delegating to Beta...", cls: "prefix" },
    { 
      delay: 1100, 
      botReply: `📐 <strong>[AI-1 · Architect Alpha]</strong><br>
🎯 <strong>Acceptance Criteria Formulated:</strong><br>
• Grace period support (leeway &plusmn;30s)<br>
• Strict clock-skew immunity test<br>
• 100% backward compatible claims<br>
⚡ <em>Handed over to @Beta for implementation!</em>`
    },
    { delay: 1800, text: "> Beta: Implementing grace-period clock skew defense in src/auth/jwt.py...", cls: "yellow" },
    { delay: 2400, text: "  [Beta] Applied diff: Added leeway parameter and exp check guard.", cls: "green" },
    {
      delay: 3000,
      botReply: `🔨 <strong>[AI-2 · Lead Builder Beta]</strong><br>
⚡ <strong>Implementation Report:</strong><br>
• Patched <code>src/auth/jwt.py</code> with 30s leeway buffer<br>
• AST verification 100% passed<br>
👉 <em>Calling @Alpha for sandbox regression audit!</em>`
    },
    { delay: 3600, text: "  [Alpha] Sandbox test: `pytest tests/test_auth.py` -> 14 passed in 0.3s", cls: "green" },
    {
      delay: 4300,
      botReply: `📐 <strong>[AI-1 · Auditor Alpha]</strong><br>
🧪 <strong>Sandbox Verification Report:</strong><br>
✅ <code>test_expired_token_grace_period</code>: PASS<br>
✅ <code>test_forged_signature_rejection</code>: PASS<br>
🏁 <strong>All criteria satisfied! Ready for merge, Commander.</strong>`
    }
  ],
  "add healthcheck endpoint /healthz": [
    { delay: 300, text: "[WarRoom] Human Commander assigned: 'add healthcheck endpoint /healthz'", cls: "cyan" },
    {
      delay: 1000,
      botReply: `📐 <strong>[AI-1 · Architect Alpha]</strong><br>
🎯 <strong>Sprint Contract Signed:</strong><br>
• Must respond with <code>{'status': 'ok'}</code><br>
• Must respond in &lt; 5ms with zero DB blocking<br>
• K8s liveness & readiness probe compliant<br>
⚡ <em>@Beta, you have the floor!</em>`
    },
    { delay: 1800, text: "> Beta: Adding FastAPI route in src/main.py...", cls: "yellow" },
    {
      delay: 2800,
      botReply: `🔨 <strong>[AI-2 · Lead Builder Beta]</strong><br>
⚡ <strong>Endpoint Built & Landed:</strong><br>
• Route: <code>GET /healthz</code><br>
• Zero-allocation payload serializer added<br>
👉 <em>Requesting sandbox verification from @Alpha.</em>`
    },
    { delay: 3500, text: "  [Alpha] Verified: curl http://localhost:8000/healthz -> 200 OK (1.2ms)", cls: "green" },
    {
      delay: 4200,
      botReply: `📐 <strong>[AI-1 · Auditor Alpha]</strong><br>
🧪 <strong>Audit Complete:</strong><br>
✅ Liveness probe baseline passed (&lt; 2ms latency)<br>
🏁 <strong>Task delivered to Commander with zero regressions!</strong>`
    }
  ],
  "optimize slow SQL query in analytics.py": [
    { delay: 300, text: "[WarRoom] Inbound task: 'optimize slow SQL query in analytics.py'", cls: "cyan" },
    {
      delay: 1000,
      botReply: `📐 <strong>[AI-1 · Architect Alpha]</strong><br>
🎯 <strong>Optimization Criteria:</strong><br>
• Eliminate N+1 loop on daily active users<br>
• Target latency: &lt; 50ms (from 840ms baseline)<br>
⚡ <em>@Beta proceed with query refactor.</em>`
    },
    { delay: 1900, text: "> Beta: Refactoring to single JOIN with indexed composite key...", cls: "yellow" },
    {
      delay: 2900,
      botReply: `🔨 <strong>[AI-2 · Lead Builder Beta]</strong><br>
⚡ <strong>Refactoring Finished:</strong><br>
• Flattened loop to index-backed composite JOIN<br>
• Benchmark: 840ms -> 16ms (<strong>-98.1%</strong>)<br>
👉 <em>Handing over to @Alpha for audit.</em>`
    },
    { delay: 3600, text: "  [Alpha] Benchmark validated: 16ms under 1,000 concurrent load test.", cls: "green" },
    {
      delay: 4300,
      botReply: `📐 <strong>[AI-1 · Auditor Alpha]</strong><br>
🧪 <strong>Performance Certification:</strong><br>
✅ Baseline 840ms crushed to 16ms<br>
🏁 <strong>Approved for production deployment, Commander!</strong>`
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

