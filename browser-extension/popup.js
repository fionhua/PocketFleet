const endpoint = document.querySelector("#endpoint");
const token = document.querySelector("#token");
const toggleTokenVisibility = document.querySelector("#toggleTokenVisibility");
const status = document.querySelector("#status");
const executionControl = document.querySelector("#executionControl");
const startCollaborationProtocol = document.querySelector("#startCollaborationProtocol");
const CONTENT_PROTOCOL_VERSION = "unified-telegram-v1";

if (toggleTokenVisibility) {
  toggleTokenVisibility.addEventListener("click", () => {
    const isPassword = token.type === "password";
    token.type = isPassword ? "text" : "password";
    const eyeSlash = toggleTokenVisibility.querySelector(".eye-closed");
    if (eyeSlash) {
      eyeSlash.style.display = isPassword ? "inline" : "none";
    }
    toggleTokenVisibility.setAttribute("title", isPassword ? "隐藏令牌" : "显示令牌");
  });
}

async function ensureContentBridge(tab) {
  let statusResponse;
  try {
    statusResponse = await chrome.tabs.sendMessage(tab.id, { type: "bridge-content-status" });
  } catch {
    // Freshly loaded unpacked extension is not present in already-open tabs.
  }
  if (statusResponse?.ok) {
    if (statusResponse.protocolVersion !== CONTENT_PROTOCOL_VERSION) {
      throw new Error("当前聊天页运行旧版发信脚本，请刷新标签页后再试。");
    }
    return statusResponse;
  }
  await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    files: ["adapter_profiles.js", "content.js"],
  });
  await chrome.scripting.insertCSS({
    target: { tabId: tab.id },
    files: ["content.css"],
  });
  const injected = await chrome.tabs.sendMessage(tab.id, { type: "bridge-content-status" });
  if (!injected?.ok || injected.protocolVersion !== CONTENT_PROTOCOL_VERSION) {
    throw new Error("页面适配器未就绪，请刷新当前聊天页签后重试。");
  }
  return injected;
}

async function load() {
  let stored = await chrome.storage.local.get({
    endpoint: "http://127.0.0.1:18765",
    token: "",
  });
  if (!stored.token) {
    try {
      const res = await fetch(chrome.runtime.getURL("seed_token.json"));
      if (res.ok) {
        const seed = await res.json();
        if (seed?.token) {
          stored.token = seed.token.trim();
          if (seed.endpoint) stored.endpoint = seed.endpoint.trim();
          await chrome.storage.local.set({
            endpoint: stored.endpoint,
            token: stored.token,
          });
        }
      }
    } catch {}
  }
  endpoint.value = stored.endpoint;
  token.value = stored.token;
}

async function save() {
  const normalized = endpoint.value.replace(/\/$/, "");
  if (normalized !== "http://127.0.0.1:18765") {
    throw new Error("当前本地桥端点锁定为 http://127.0.0.1:18765");
  }
  await chrome.storage.local.set({
    endpoint: normalized,
    token: token.value.trim(),
  });
  status.textContent = "设置已保存";
}

function renderExecutionControl(killSwitchActive) {
  executionControl.disabled = false;
  executionControl.dataset.paused = String(killSwitchActive);
  executionControl.textContent = killSwitchActive ? "恢复执行" : "暂停执行";
}

async function refreshBridgeStatus() {
  const response = await chrome.runtime.sendMessage({ type: "bridge-status" });
  if (!response.ok) throw new Error(response.error);
  renderExecutionControl(response.data.kill_switch_active);
  return response.data;
}

document.querySelector("#save").addEventListener("click", () => {
  save().catch((error) => (status.textContent = error.message));
});

document.querySelector("#test").addEventListener("click", async () => {
  try {
    await save();
    if (!token.value.trim()) {
      status.textContent = "请先填入 PocketFleet 通信鉴权令牌";
      return;
    }
    const bridgeStatus = await refreshBridgeStatus();
    const stateText = bridgeStatus.kill_switch_active
      ? "PocketFleet 桥接已连接（当前熔断暂停中）"
      : `PocketFleet 桥接正常在线：${bridgeStatus.principal || "OK"}`;
    status.textContent = stateText;
  } catch (error) {
    status.textContent = `连接失败：${error.message}`;
  }
});

executionControl.addEventListener("click", async (event) => {
  if (!event.isTrusted) return;
  executionControl.disabled = true;
  try {
    await save();
    const before = await refreshBridgeStatus();
    const action = before.kill_switch_active ? "resume" : "pause";
    const response = await chrome.runtime.sendMessage({
      type: "bridge-control",
      action,
      humanGesture: true,
    });
    if (!response.ok) throw new Error(response.error);
    const after = await refreshBridgeStatus();
    status.textContent = after.kill_switch_active
      ? "执行已暂停；出站队列已临时冻结"
      : "执行已恢复；出站通信正常流转";
  } catch (error) {
    executionControl.disabled = false;
    status.textContent = `切换失败：${error.message}`;
  }
});

startCollaborationProtocol.addEventListener("click", async (event) => {
  if (!event.isTrusted) return;
  startCollaborationProtocol.disabled = true;
  try {
    await save();
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id) throw new Error("未找到当前浏览器标签页。");
    const contentStatus = await ensureContentBridge(tab);
    if (!contentStatus?.adapter) {
      throw new Error("当前页面不是支持的 AI 聊天会话页。");
    }
    const response = await chrome.tabs.sendMessage(tab.id, {
      type: "bridge-start-collaboration-protocol",
      humanGesture: true,
    });
    if (!response?.ok) throw new Error(response?.error || "协议说明注入失败。");
    status.textContent = "PocketFleet 多AI协同交互规范已写入输入框，可按回车发送给当前 AI！";
  } catch (error) {
    status.textContent = `注入失败：${error.message}`;
  } finally {
    startCollaborationProtocol.disabled = false;
  }
});

load()
  .then(() => {
    if (token.value.trim()) return refreshBridgeStatus();
    executionControl.disabled = true;
    executionControl.textContent = "桥未连接";
    status.textContent = "请在上方填入 PocketFleet 通信鉴权令牌";
    return null;
  })
  .catch((error) => {
    executionControl.disabled = true;
    executionControl.textContent = "桥未连接";
    status.textContent = `状态检测失败：${error.message}`;
  });
