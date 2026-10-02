const endpoint = document.querySelector("#endpoint");
const token = document.querySelector("#token");
const thetaToken = document.querySelector("#thetaToken");
const doubaoToken = document.querySelector("#doubaoToken");
const xingtuToken = document.querySelector("#xingtuToken");
const motinToken = document.querySelector("#motinToken");
const readRoots = document.querySelector("#readRoots");
const writeRoots = document.querySelector("#writeRoots");
const meetingDir = document.querySelector("#meetingDir");
const status = document.querySelector("#status");
const executionControl = document.querySelector("#executionControl");
const startCollaborationProtocol = document.querySelector("#startCollaborationProtocol");
const CONTENT_PROTOCOL_VERSION = "unified-telegram-v1";


async function ensureContentBridge(tab) {
  let statusResponse;
  try {
    statusResponse = await chrome.tabs.sendMessage(tab.id, { type: "bridge-content-status" });
  } catch {
    // A freshly reloaded unpacked extension is not present in already-open tabs.
  }
  if (statusResponse?.ok) {
    if (statusResponse.protocolVersion !== CONTENT_PROTOCOL_VERSION) {
      throw new Error("当前聊天页仍在运行旧版发信脚本。请刷新这个聊天页签，再点击“加载发信说明”；无需重启浏览器。");
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
    throw new Error("页面适配器未更新到简版发信协议；请刷新当前聊天页签后重试。");
  }
  return injected;
}

async function load() {
  const stored = await chrome.storage.local.get({
    endpoint: "http://127.0.0.1:18765",
    token: "",
    thetaToken: "",
    doubaoToken: "",
    xingtuToken: "",
    motinToken: "",
    readRoots: "",
    writeRoots: "",
    meetingDir: "",
  });
  endpoint.value = stored.endpoint;
  token.value = stored.token;
  if (thetaToken) thetaToken.value = stored.thetaToken || "";
  if (doubaoToken) doubaoToken.value = stored.doubaoToken || "";
  if (xingtuToken) xingtuToken.value = stored.xingtuToken || "";
  if (motinToken) motinToken.value = stored.motinToken || "";
  readRoots.value = stored.readRoots || "";
  writeRoots.value = stored.writeRoots || "";
  meetingDir.value = stored.meetingDir || "";
}

function parseRoots(value) {
  return [...new Set(value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean))];
}

async function save() {
  const normalized = endpoint.value.replace(/\/$/, "");
  if (normalized !== "http://127.0.0.1:18765") {
    throw new Error("首期端点锁定为 http://127.0.0.1:18765");
  }
  await chrome.storage.local.set({
    endpoint: normalized,
    token: token.value,
    thetaToken: thetaToken ? thetaToken.value : "",
    doubaoToken: doubaoToken ? doubaoToken.value : "",
    xingtuToken: xingtuToken ? xingtuToken.value : "",
    motinToken: motinToken ? motinToken.value : "",
    readRoots: readRoots.value,
    writeRoots: writeRoots.value,
    meetingDir: meetingDir.value.trim(),
  });
  const configuredReadRoots = parseRoots(readRoots.value);
  if (configuredReadRoots.length) {
    const response = await chrome.runtime.sendMessage({
      type: "bridge-access-roots-update",
      readRoots: configuredReadRoots,
      writeRoots: parseRoots(writeRoots.value),
      meetingDir: meetingDir.value.trim(),
    });
    if (!response.ok) throw new Error(response.error);
    if (typeof response.data?.meeting_dir === "string") {
      meetingDir.value = response.data.meeting_dir;
      await chrome.storage.local.set({ meetingDir: response.data.meeting_dir });
    }
  }
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
  if (Array.isArray(response.data.read_roots)) {
    readRoots.value = response.data.read_roots.join("\n");
  }
  if (Array.isArray(response.data.write_roots)) {
    writeRoots.value = response.data.write_roots.join("\n");
  }
  if (typeof response.data.meeting_dir === "string") {
    meetingDir.value = response.data.meeting_dir;
  }
  renderExecutionControl(response.data.kill_switch_active);
  return response.data;
}

document.querySelector("#save").addEventListener("click", () => {
  save().catch((error) => (status.textContent = error.message));
});

document.querySelector("#test").addEventListener("click", async () => {
  try {
    await save();
    const reports = [];
    if (token.value.trim()) {
      try {
        const bridgeStatus = await refreshBridgeStatus();
        reports.push(bridgeStatus.kill_switch_active
          ? "星舰全舰主令牌已连接，但 Human Root 熔断已开启"
          : `星舰全舰主令牌已连接：${bridgeStatus.principal}`);
      } catch (bridgeErr) {
        reports.push(`全舰主令牌连接失败: ${bridgeErr.message}`);
      }
    } else {
      reports.push("全舰主令牌未配置");
    }

    // 检查高级独立覆盖席位
    const activeOverrides = [];
    if (xingtuToken?.value.trim()) activeOverrides.push("星图(独立)");
    if (motinToken?.value.trim()) activeOverrides.push("墨汀(独立)");
    if (thetaToken?.value.trim()) activeOverrides.push("Theta(独立)");
    if (doubaoToken?.value.trim()) activeOverrides.push("心机姝(独立)");

    if (activeOverrides.length) {
      reports.push(`独立覆盖生效: ${activeOverrides.join(", ")}`);
    } else if (token.value.trim()) {
      reports.push("对话组全员（平视/心机姝/星图/墨汀/地球/灰阶/余数）已就绪自动继承主令牌");
    }

    status.textContent = reports.join(" | ");
  } catch (error) {
    status.textContent = `失败：${error.message}`;
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
      ? "执行已暂停；控制链仍在线"
      : "执行已恢复；Policy Gate 继续生效";
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
      throw new Error("当前页面不是已授权的 AI 会话。");
    }
    const response = await chrome.tabs.sendMessage(tab.id, {
      type: "bridge-start-collaboration-protocol",
      humanGesture: true,
    });
    if (!response?.ok) throw new Error(response?.error || "协议启动失败。");
    status.textContent = "星舰统一电报发信说明（方案 1 标准头版）已提交到当前 Chat，等待 AI 确认。";
  } catch (error) {
    status.textContent = `启动失败：${error.message}`;
  } finally {
    startCollaborationProtocol.disabled = false;
  }
});



load()
  .then(() => {
    if (token.value.trim()) return refreshBridgeStatus();
    executionControl.disabled = true;
    executionControl.textContent = "全舰主桥未配置";
    status.textContent = "请在上方填入星舰全舰通信令牌（主桥）。";
    return null;
  })
  .catch((error) => {
    executionControl.disabled = true;
    executionControl.textContent = "桥未连接";
    status.textContent = `状态检测失败：${error.message}`;
  });
