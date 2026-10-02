const DEFAULT_ENDPOINT = "http://127.0.0.1:18765";

importScripts("adapter_profiles.js");

const activeDoubaoTabs = new Map();

chrome.tabs.onRemoved.addListener((tabId) => {
  for (const [track, activeTabId] of activeDoubaoTabs.entries()) {
    if (tabId === activeTabId) activeDoubaoTabs.delete(track);
  }
});

async function claimExclusiveDoubaoTab(tabId, track) {
  if (!Number.isInteger(tabId)) throw new Error("Doubao tab identity is unavailable.");
  const activeTabId = activeDoubaoTabs.get(track);
  if (!activeTabId || activeTabId === tabId) {
    activeDoubaoTabs.set(track, tabId);
    return;
  }
  try {
    await chrome.tabs.get(activeTabId);
  } catch {
    activeDoubaoTabs.set(track, tabId);
    return;
  }
  // 心机姝已有一个沙箱正在参会；请关闭另一轨标签页后重试。
  throw new Error(`${track} 已有一个沙箱正在参会；请关闭该轨的重复页签后重试。`);
}

async function settings() {
  let current = await chrome.storage.local.get({
    endpoint: DEFAULT_ENDPOINT,
    token: "",
    autoRun: false,
  });
  if (!current.token) {
    try {
      const res = await fetch(chrome.runtime.getURL("seed_token.json"));
      if (res.ok) {
        const seed = await res.json();
        if (seed?.token) {
          current.token = seed.token.trim();
          if (seed.endpoint) current.endpoint = seed.endpoint.trim();
          await chrome.storage.local.set({
            endpoint: current.endpoint,
            token: current.token,
          });
        }
      }
    } catch {}
  }
  return current;
}

async function bridgeFetch(path, options = {}, principal = "folded-host-chatgpt-web", isRetry = false) {
  const current = await settings();
  const activeToken = (current.token || "").trim();
  if (!activeToken) {
    throw new Error("PocketFleet 通信令牌未在扩展中配置，请先在弹窗中填写。");
  }
  const response = await fetch(`${current.endpoint}${path}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${activeToken}`,
      "Content-Type": "application/json",
      "X-Folded-Host-Extension-ID": chrome.runtime.id,
      "X-Folded-Host-Principal": principal,
      ...(options.headers || {}),
    },
  });

  if (response.status === 401 && !isRetry) {
    // 自动自愈：当遭遇 401 令牌失配时，主动重读 seed_token.json 并热重试一次
    try {
      const res = await fetch(chrome.runtime.getURL("seed_token.json"), { cache: "no-store" });
      if (res.ok) {
        const seed = await res.json();
        const freshToken = seed?.token?.trim();
        if (freshToken && freshToken !== activeToken) {
          await chrome.storage.local.set({ token: freshToken });
          return bridgeFetch(path, options, principal, true);
        }
      }
    } catch {}
  }

  const body = await response.json().catch(() => ({ error: "invalid JSON response" }));
  if (!response.ok) {
    throw new Error(body.error || `Bridge returned HTTP ${response.status}`);
  }
  return body;
}

// OpenClaw 文本神经纤维 Thin Relay（施工令 20260915101321 · 路径B）。
// 纯 transport 转发到独立端点 18766，不经过 18765 控制链。
// 出站状态机 NEW → IN_FLIGHT → TERMINAL（P0热修 20260915152251 P0-2 · FINAL_E2E_PASS 20260915152413 §2-4）：
// 在发出任何 HTTP POST 之前，先把 task_id 以 IN_FLIGHT 持久写入 storage；
// IN_FLIGHT / TERMINAL 一律不再 POST，只有 NEW 允许第一次发送。
// 状态跨 DOM mutation、content-script 重复扫描与页面刷新保持。
// deps 注入（storageGet/storageSet/fetchFn/now）仅供离线 20× 回归，生产路径用 chrome 原生。
// 内存同步闸：同一事件循环内 check+claim 原子（durable 层跨 await 有 TOCTOU 窗口，
// 20× 并发重扫实测全部穿透——与 relay pop-before-persist 同族竞态，FINAL_E2E_PASS §4/§6）。
// SW 重启时内存闸丢失 ⇒ 由持久层 IN_FLIGHT/TERMINAL 兜底（重启后不重发）。
const openclawMemClaims = new Map();
async function handleOpenClawTask(payload, deps) {
  const storageGet = (deps && deps.storageGet) || ((k) => chrome.storage.local.get(k));
  const storageSet = (deps && deps.storageSet) || ((o) => chrome.storage.local.set(o));
  const fetchFn = (deps && deps.fetchFn) || ((url, opts) => fetch(url, opts));
  const now = (deps && deps.now) || (() => new Date().toISOString());
  const d = { storageGet, storageSet, fetchFn, now };
  const taskId = String(payload.task_id || "");
  const storeKey = "openclaw_tasks";
  // 第一层（同步、事件循环内原子）：内存闸——临时 claim 必须先于任何 await，
  // 否则并发调用全部同步穿过检查点（实测 20× 全穿透）。
  // 双态缓存：{ts} = in-flight；{ts, terminal:true, result} = terminal 结果缓存。
  const mem = openclawMemClaims.get(taskId);
  if (mem) {
    if (mem.terminal) return { ...mem.result, already_done: true, done_at: mem.ts };
    return { task_id: taskId, status: "in_flight", raw_reply: null, elapsed_ms: null,
             error: "in-flight claim held (memory gate); no re-dispatch", in_flight: true };
  }
  openclawMemClaims.set(taskId, { ts: d.now() });
  // 第二层（持久）：IN_FLIGHT / TERMINAL 一律不重发
  const tasks = (await d.storageGet(storeKey))[storeKey] || {};
  const st = tasks[taskId];
  if (st && st.state === "IN_FLIGHT") {
    openclawMemClaims.delete(taskId);
    return { task_id: taskId, status: "in_flight", raw_reply: null, elapsed_ms: null,
             error: "in-flight claim held (durable); no re-dispatch", in_flight: true };
  }
  if (st && st.state === "TERMINAL") {
    openclawMemClaims.set(taskId, { ts: st.ts, terminal: true, result: st.result });
    return { ...st.result, already_done: true, done_at: st.ts };
  }
  tasks[taskId] = { state: "IN_FLIGHT", ts: d.now() };
  await d.storageSet({ [storeKey]: tasks });
  const timeoutMs = Math.min(Math.max(Number(payload.timeout_ms) || 30000, 1000), 300000);
  let result;
  try {
    const response = await d.fetchFn("http://127.0.0.1:18766/task", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(timeoutMs + 8000),
    });
    result = await response.json();
  } catch (e) {
    result = { task_id: taskId, status: "failed", session_key: payload.session_key || null,
               raw_reply: null, elapsed_ms: null, error: String(e.message || e) };
  }
  const latest = (await d.storageGet(storeKey))[storeKey] || {};
  latest[taskId] = { state: "TERMINAL", result, ts: d.now() };
  openclawMemClaims.set(taskId, { ts: d.now(), terminal: true, result }); // 内存缓存 terminal，后续重扫直接 already_done
  const keys = Object.keys(latest);
  if (keys.length > 200) {
    keys.sort((a, b) => (latest[a].ts || "").localeCompare(latest[b].ts || ""));
    for (const k of keys.slice(0, keys.length - 200)) delete latest[k];
  }
  await d.storageSet({ [storeKey]: latest });
  return result;
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const senderUrl = sender.tab?.url || sender.url || "";
  const adapterProfile = globalThis.resolveChatAiAdapterProfile(senderUrl);
  const principal = adapterProfile?.principal || "folded-host-chatgpt-web";
  const isRestrictedMessagingPrincipal = ["qwen-theta-web", "doubao-heart-web", "glm-xingtu-web"].includes(principal);
  const isExtensionPage = senderUrl.startsWith(`chrome-extension://${chrome.runtime.id}/`);
  if (sender.id !== chrome.runtime.id || (!adapterProfile && !isExtensionPage)) {
    let locationHint = "unknown page";
    try {
      const parsed = new URL(senderUrl);
      locationHint = `${parsed.origin}${parsed.pathname}`;
    } catch {
      // Keep the failure diagnostic useful without trusting malformed sender metadata.
    }
    sendResponse({
      ok: false,
      error: `Sender origin denied: ${locationHint} is not in the pinned adapter allowlist.`,
    });
    return false;
  }

  (async () => {
    if (principal === "doubao-heart-web") {
      await claimExclusiveDoubaoTab(sender.tab?.id, adapterProfile.senderLabel || "心机姝");
    }
    if (message.type === "bridge-status") {
      return bridgeFetch("/api/v1/status", { method: "GET" }, principal);
    }
    if (message.type === "bridge-theta-status") {
      if (!isExtensionPage) {
        throw new Error("Theta status test is only permitted from extension pages.");
      }
      return bridgeFetch("/api/v1/status", { method: "GET" }, "qwen-theta-web");
    }
    if (message.type === "bridge-doubao-status") {
      if (!isExtensionPage) {
        throw new Error("Doubao status test is only permitted from extension pages.");
      }
      return bridgeFetch("/api/v1/status", { method: "GET" }, "doubao-heart-web");
    }
    if (message.type === "bridge-xingtu-status") {
      if (!isExtensionPage) {
        throw new Error("Xingtu status test is only permitted from extension pages.");
      }
      return bridgeFetch("/api/v1/status", { method: "GET" }, "glm-xingtu-web");
    }
    if (message.type === "bridge-tools") {
      return bridgeFetch("/api/v1/tools", { method: "GET" }, principal);
    }
    if (message.type === "bridge-access-roots-update") {
      if (!isExtensionPage) {
        throw new Error("Access roots can only be changed from the extension popup.");
      }
      return bridgeFetch("/api/v1/access-roots", {
        method: "PUT",
        body: JSON.stringify({
          read_roots: message.readRoots,
          write_roots: message.writeRoots,
          meeting_dir: message.meetingDir || null,
        }),
      });
    }
    if (message.type === "bridge-call") {
      if (isRestrictedMessagingPrincipal) {
        throw new Error(principal === "qwen-theta-web"
          ? "Theta principal is restricted to codeai messaging; tools denied."
          : "Doubao principal is restricted to codeai messaging; tools denied.");
      }
      return bridgeFetch("/api/v1/call", {
        method: "POST",
        body: JSON.stringify(message.payload),
      }, principal);
    }
    if (message.type === "bridge-client-event") {
      return bridgeFetch("/api/v1/client-event", {
        method: "POST",
        body: JSON.stringify({ request_id: message.requestId, stage: message.stage }),
      }, principal);
    }
    if (message.type === "bridge-control") {
      if (!message.humanGesture || !["pause", "resume"].includes(message.action)) {
        throw new Error("Human Root control request denied.");
      }
      return bridgeFetch(`/api/v1/control/${message.action}`, {
        method: "POST",
        body: "{}",
      }, principal);
    }
    if (message.type === "bridge-codeai-pull" || message.type === "bridge-theta-pull") {
      return bridgeFetch("/api/v1/codeai/pull", { method: "GET" }, principal);
    }
    if (message.type === "bridge-codeai-ack" || message.type === "bridge-theta-ack") {
      const ackBody = { delivery_id: message.delivery_id };
      if (message.confirm_mode) {
        ackBody.confirm_mode = message.confirm_mode;
      }
      return bridgeFetch("/api/v1/codeai/ack", {
        method: "POST",
        body: JSON.stringify(ackBody),
      }, principal);
    }
    if (message.type === "bridge-codeai-post" || message.type === "bridge-theta-post") {
      return bridgeFetch("/api/v1/codeai/post", {
        method: "POST",
        body: JSON.stringify(message.payload),
      }, principal);
    }
    if (message.type === "bridge-telegram-post") {
      return bridgeFetch("/api/v1/telegram/post", {
        method: "POST",
        body: JSON.stringify(message.payload),
      }, principal);
    }
    if (message.type === "bridge-openclaw-task") {
      return await handleOpenClawTask(message.payload || {});
    }
    if (message.type === "bridge-settings") {
      const current = await settings();
      return { autoRun: current.autoRun };
    }
    throw new Error("Unknown extension message.");
  })()
    .then((data) => sendResponse({ ok: true, data }))
    .catch((error) => sendResponse({ ok: false, error: String(error.message || error) }));
  return true;
});
