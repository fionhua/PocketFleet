(() => {
const PANEL_ID = "folded-host-bridge-panel";
const CALL_KEY = "folded_host_tool_call";
const adapterProfile = globalThis.resolveChatAiAdapterProfile?.(
  typeof window !== "undefined" ? window.location.href : "https://chatgpt.com/",
) || globalThis.CHAT_AI_ADAPTER_PROFILES?.[0];
const BRIDGE_HEADER = adapterProfile?.id === "chatgpt-folded-host" ? adapterProfile.macros.action : "[#bridge:v1]";
const THETA_HEADER = adapterProfile?.id === "qwen-theta" ? adapterProfile.macros.action : "[#theta:v1]";
const DELIVERY_SUBMIT_TIMEOUT_MS = adapterProfile?.submitTimeoutMs || 15000;
const DELIVERY_WAKE_TIMEOUT_MS = adapterProfile?.wakeTimeoutMs || 30000;
const CONTENT_INSTANCE_ID = globalThis.crypto?.randomUUID?.()
  || `bridge-${Date.now()}-${Math.random().toString(16).slice(2)}`;
const handled = new Set();
const executedMessages = new WeakSet();
const messageStates = new WeakMap();

function persistHandledCodeAiId(id) {
  if (!id) return;
  handled.add(id);
  try {
    if (typeof chrome !== "undefined" && chrome.storage?.local?.get) {
      chrome.storage.local.get(["codeai_handled_ids"], (res) => {
        const list = Array.isArray(res?.codeai_handled_ids) ? res.codeai_handled_ids : [];
        if (!list.includes(id)) {
          list.push(id);
          if (list.length > 300) list.splice(0, list.length - 300);
          chrome.storage.local.set({ codeai_handled_ids: list });
        }
      });
    }
  } catch {}
}

try {
  if (typeof chrome !== "undefined" && chrome.storage?.local?.get) {
    chrome.storage.local.get(["codeai_handled_ids"], (res) => {
      const list = res?.codeai_handled_ids;
      if (Array.isArray(list)) {
        for (const id of list) handled.add(id);
      }
    });
  }
} catch {}
const STABILITY_WINDOW_MS = adapterProfile?.stabilityWindowMs || 600;
const DOUBAO_RESPONSE_STABILITY_MS = adapterProfile?.responseStabilityMs || 5000;
const PAGE_SCAN_DEBOUNCE_MS = 3000;
const CONTENT_PROTOCOL_VERSION = "unified-telegram-v1";
const ROUTE_BASELINE_QUIET_MS = 2000;
let isScanningTheta = false;
const isQwen = adapterProfile?.id === "qwen-theta";
const isDoubao = adapterProfile?.id?.startsWith("doubao-heart");
let connectionMessage = "正在检测本地桥...";
let connectionState = "checking";
let activityMessage = isQwen
  ? "Qwen 适配器已注入，未检测到新行动宏"
  : isDoubao
    ? "心机姝适配器已注入，等待会议来信"
    : "尚无工具活动";
let activityWarning = false;
let killSwitchActive = null;
let pendingDelivery = null;
let bridgeInstanceActive = true;
let panelClaimed = false;
let pageObserver = null;
let visibilityHandler = null;
let pageScanTimer = null;
let routeBaselineTimer = null;
let routeBaselinePending = false;
let activePageRoute = null;
const managedIntervals = [];

function ownsPanel(root = null) {
  if (typeof document === "undefined") return true;
  root ||= document.getElementById(PANEL_ID);
  return root?.dataset?.bridgeOwner === CONTENT_INSTANCE_ID;
}

function deactivateBridgeInstance() {
  if (!bridgeInstanceActive) return;
  bridgeInstanceActive = false;
  pageObserver?.disconnect();
  if (pageScanTimer) clearTimeout(pageScanTimer);
  if (routeBaselineTimer) clearTimeout(routeBaselineTimer);
  pageScanTimer = null;
  routeBaselineTimer = null;
  for (const intervalId of managedIntervals) clearInterval(intervalId);
  managedIntervals.length = 0;
  if (visibilityHandler && typeof document !== "undefined") {
    document.removeEventListener("visibilitychange", visibilityHandler);
  }
}

function retireIfSuperseded() {
  if (!bridgeInstanceActive || (panelClaimed && !ownsPanel())) {
    deactivateBridgeInstance();
    return true;
  }
  return false;
}

function isInvalidatedContextError(error) {
  const msg = String(error?.message || error);
  return msg.includes("Extension context invalidated")
    || msg.includes("message port closed before a response was received")
    || msg.includes("Receiving end does not exist");
}

let isPanelCollapsed = false;
try {
  isPanelCollapsed = typeof localStorage !== "undefined" && localStorage.getItem("folded_host_panel_collapsed") === "true";
} catch {}

function setPanelCollapsed(collapsed) {
  isPanelCollapsed = !!collapsed;
  try {
    if (typeof localStorage !== "undefined") {
      localStorage.setItem("folded_host_panel_collapsed", String(isPanelCollapsed));
    }
  } catch {}
  const root = typeof document !== "undefined" ? document.getElementById(PANEL_ID) : null;
  if (root) {
    root.dataset.collapsed = String(isPanelCollapsed);
  }
}

function panel() {
  let root = document.getElementById(PANEL_ID);
  if (!root) {
    root = document.createElement("section");
    root.id = PANEL_ID;
    root.dataset.collapsed = String(isPanelCollapsed);
    root.innerHTML = `
    <div class="bridge-heading">
      <span class="bridge-mark" aria-hidden="true"><span class="bridge-light"></span>PF</span>
      <div>
        <strong>PocketFleet Bridge</strong>
        <span class="bridge-kicker">AI STARFLEET LINK</span>
      </div>
      <button class="bridge-collapse-toggle" type="button" data-action="collapse" title="收起浮层" aria-label="收起浮层">收起</button>
    </div>
    <div class="bridge-connection"><span class="bridge-status-dot"></span><span data-connection></span></div>
    <div class="bridge-activity"><span>最近动作</span><p data-activity></p></div>
    <div class="bridge-actions">
      <button class="bridge-primary" type="button" data-action="control" data-control>暂停执行</button>
      <button type="button" data-action="retry" data-retry disabled>重试回传</button>
      <button type="button" data-action="check">重新检测</button>
      <button type="button" data-action="instructions">协同说明</button>
    </div>
    <div class="bridge-collapsed-bar" data-action="expand" title="展开 PocketFleet Bridge 浮层">
      <span class="bridge-status-dot"></span>
      <span class="bridge-collapsed-label">PF 桥</span>
      <span class="bridge-expand-icon">◀</span>
    </div>
    `;
    document.body.appendChild(root);
  }
  if (!panelClaimed) {
    root.dataset.bridgeOwner = CONTENT_INSTANCE_ID;
    panelClaimed = true;
    root.addEventListener("click", async (event) => {
      if (!bridgeInstanceActive || !ownsPanel(root) || !event.isTrusted) return;
      const action = event.target?.dataset?.action || event.target?.closest("[data-action]")?.dataset?.action;
      if (action === "collapse") { setPanelCollapsed(true); return; }
      if (action === "expand") { setPanelCollapsed(false); return; }
      if (root.dataset.collapsed === "true" && (event.target === root || event.target.closest(".bridge-collapsed-bar"))) {
        setPanelCollapsed(false);
        return;
      }
      if (action === "check") {
        await refreshStatus();
        const latest = messagesByRole("assistant").slice(-1)[0];
        if (latest) telegramExecutedElements.delete(latest);
        runPageScan();
      }
      if (action === "instructions") await insertInstructions();
      if (action === "control") await toggleExecution();
      if (action === "retry") await retryDelivery();
    });
  }
  renderStatus(root);
  return root;
}

function renderStatus(root = null) {
  if (!root) {
    if (typeof document === "undefined") return;
    root = panel();
  }
  if (!root || !bridgeInstanceActive || !ownsPanel(root)) return;
  const renderSignature = JSON.stringify({
    connectionState,
    connectionMessage,
    activityMessage,
    activityWarning,
    killSwitchActive,
    hasPendingDelivery: pendingDelivery !== null,
  });
  if (root.dataset.renderSignature === renderSignature) return;
  root.dataset.renderSignature = renderSignature;
  root.dataset.state = connectionState;
  const conn = root.querySelector("[data-connection]");
  if (conn) conn.textContent = connectionMessage;
  const act = root.querySelector("[data-activity]");
  if (act) {
    act.textContent = activityMessage;
    act.style.color = activityWarning ? "#b45309" : "";
    act.style.fontWeight = activityWarning ? "700" : "";
  }
  const control = root.querySelector("[data-control]");
  const retry = root.querySelector("[data-retry]");
  if (control) {
    control.textContent = killSwitchActive ? "恢复执行" : "暂停执行";
    control.disabled = killSwitchActive === null;
  }
  if (retry) {
    retry.disabled = pendingDelivery === null;
  }
}

function setConnection(message, state = "checking") {
  connectionMessage = message;
  connectionState = state;
  renderStatus();
}

function setActivity(message, { warning = false } = {}) {
  activityMessage = warning ? `⚠ ${message}` : message;
  activityWarning = warning;
  renderStatus();
}

async function send(message) {
  if (retireIfSuperseded()) throw new Error("Bridge content instance superseded.");
  try {
    const response = await chrome.runtime.sendMessage(message);
    if (!response?.ok) throw new Error(response?.error || "Bridge request failed.");
    return response.data;
  } catch (error) {
    if (isInvalidatedContextError(error)) deactivateBridgeInstance();
    throw error;
  }
}

async function refreshStatus() {
  setConnection("正在检测本地桥...", "checking");
  try {
    const current = await send({ type: "bridge-status" });
    killSwitchActive = current.kill_switch_active;
    if (current.kill_switch_active) {
      setConnection("控制链在线，工具执行已暂停", "warning");
    } else {
      setConnection("控制链在线，协同网桥已就绪", "connected");
    }
  } catch (error) {
    killSwitchActive = null;
    setConnection(`未连接：${error.message}`, "disconnected");
  }
}

async function toggleExecution() {
  if (killSwitchActive === null) return;
  const action = killSwitchActive ? "resume" : "pause";
  setActivity(action === "pause" ? "正在暂停执行..." : "正在恢复执行...");
  try {
    await send({ type: "bridge-control", action, humanGesture: true });
    await refreshStatus();
    setActivity(action === "pause" ? "执行已暂停" : "执行已恢复");
  } catch (error) {
    setActivity(`控制失败：${error.message}`);
  }
}

function promptEditor() {
  return queryFirst(adapterProfile?.selectors.editor);
}

function joinedSelectors(selectors) {
  return (selectors || []).join(", ");
}

function queryFirst(selectors, scope = document) {
  const selector = joinedSelectors(selectors);
  return selector ? scope.querySelector(selector) : null;
}

function queryAll(selectors, scope = document) {
  const selector = joinedSelectors(selectors);
  return selector ? [...scope.querySelectorAll(selector)] : [];
}

function insertIntoEditor(text) {
  const editor = promptEditor();
  if (!editor) throw new Error("未找到当前 AI 输入框，页面适配器需要更新。");
  if (editorText(editor)) throw new Error("输入框非空，为避免覆盖已停止自动回填。");
  editor.focus();
  if (editor instanceof HTMLTextAreaElement || editor instanceof HTMLInputElement) {
    const prototype = editor instanceof HTMLTextAreaElement
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    const nativeSetter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
    if (nativeSetter) nativeSetter.call(editor, text);
    else editor.value = text;
    editor.dispatchEvent(new Event("input", { bubbles: true }));
    editor.dispatchEvent(new Event("change", { bubbles: true }));
    return editor;
  }
  const inserted = document.execCommand("insertText", false, text);
  if (!inserted) {
    editor.textContent = text;
    editor.dispatchEvent(
      new InputEvent("input", { bubbles: true, composed: true, inputType: "insertText", data: text }),
    );
  }
  return editor;
}

function editorText(editor) {
  if (!editor) return "";
  if ("value" in editor && typeof editor.value === "string") {
    return editor.value.trim();
  }
  if (editor.isContentEditable || editor.getAttribute("contenteditable") === "true") {
    const clone = editor.cloneNode(true);
    const placeholders = clone.querySelectorAll(
      ".placeholder, [data-placeholder], .ProseMirror-placeholder, [class*='placeholder'], [aria-hidden='true']"
    );
    placeholders.forEach((el) => el.remove());
    return (clone.textContent || "").trim();
  }
  return String(editor.textContent || "").trim();
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitFor(predicate, timeoutMilliseconds) {
  const deadline = Date.now() + timeoutMilliseconds;
  while (Date.now() < deadline) {
    const value = predicate();
    if (value) return value;
    await sleep(100);
  }
  return null;
}

function messagesByRole(role) {
  const selectors = role === "assistant"
    ? adapterProfile?.selectors.assistantMessages
    : adapterProfile?.selectors.userMessages;
  const messages = queryAll(selectors);
  const userMessageSelector = joinedSelectors(adapterProfile?.selectors.userMessages);
  const safeMessages = messages.filter((message) => (
    (userMessageSelector ? !message.closest(userMessageSelector) : true)
    && !message.closest("[contenteditable='true']")
    && !message.closest(`#${PANEL_ID}`)
  ));
  // 通用父子嵌套去重：若 A 包含了 B，剔除内层子节点，确保每个回复仅对应一个 DOM 根节点
  return safeMessages.filter((message) => (
    !safeMessages.some((other) => other !== message && other.contains(message))
  ));
}

function generationIsVisible() {
  return queryAll(adapterProfile?.selectors.generation).some(
    (candidate) => candidate.getClientRects().length > 0,
  );
}

function doubaoQueueIsPaused() {
  if (!isDoubao) return false;
  if (document.querySelector("[data-item-id^='queue-item-'][data-item-status='Pending']")) {
    return true;
  }
  const pageText = document.body?.innerText || "";
  return pageText.includes("队列已暂停")
    || pageText.includes("按 Enter 发送队列中首条消息");
}

function doubaoConversationBusyReason(now = Date.now()) {
  if (!isDoubao) return null;
  if (doubaoQueueIsPaused()) return "queue_paused";
  if (generationIsVisible()) return "generating";
  const users = messagesByRole("user");
  const assistants = messagesByRole("assistant");
  const latestUser = users[users.length - 1] || null;
  const latestAssistant = assistants[assistants.length - 1] || null;
  if (latestUser && (!latestAssistant || (
    latestAssistant.compareDocumentPosition(latestUser) & Node.DOCUMENT_POSITION_FOLLOWING
  ))) return "awaiting_reply";
  if (latestAssistant) {
    const state = getOrCreateMessageState(latestAssistant, now);
    if (now - state.lastChangedAt < DOUBAO_RESPONSE_STABILITY_MS) return "reply_unstable";
  }
  return null;
}

async function reportDeliveryStage(requestId, stage) {
  try {
    await send({ type: "bridge-client-event", requestId, stage });
  } catch {
    // Delivery telemetry must never replace the visible recovery path.
  }
}

function activeSubmitButton(editor) {
  const selector = joinedSelectors(adapterProfile?.selectors.submit);
  const isReady = (candidate) => candidate
    && candidate.getClientRects().length > 0
    && !candidate.disabled
    && candidate.getAttribute("aria-disabled") !== "true"
    && candidate.getAttribute("data-disabled") !== "true";

  let scope = editor.parentElement;
  for (let depth = 0; scope && depth < 12; depth += 1) {
    const nearby = [...scope.querySelectorAll(selector)].find(isReady);
    if (nearby) return nearby;
    scope = scope.parentElement;
  }
  return [...document.querySelectorAll(selector)].find(isReady) || null;
}

function dispatchPointerActivation(button) {
  for (const [type, buttons] of [["pointerdown", 1], ["pointerup", 0]]) {
    button.dispatchEvent(new PointerEvent(type, {
      bubbles: true,
      cancelable: true,
      composed: true,
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
      button: 0,
      buttons,
    }));
  }
  for (const type of ["mousedown", "mouseup", "click"]) {
    button.dispatchEvent(new MouseEvent(type, {
      bubbles: true,
      cancelable: true,
      composed: true,
      button: 0,
      buttons: type === "mousedown" ? 1 : 0,
    }));
  }
}

function dispatchEnter(editor) {
  editor.focus();
  for (const type of ["keydown", "keypress", "keyup"]) {
    editor.dispatchEvent(new KeyboardEvent(type, {
      key: "Enter",
      code: "Enter",
      keyCode: 13,
      which: 13,
      bubbles: true,
      cancelable: true,
      composed: true,
    }));
  }
}

async function submitDoubaoEditor(editor, requestId, onSubmitConfirmed = null) {
  const existingUserMessages = new Set(messagesByRole("user"));
  const existingAssistantMessages = new Set(messagesByRole("assistant"));
  const submittedMessage = () => messagesByRole("user").find(
    (message) => !existingUserMessages.has(message) && message.textContent.includes(requestId),
  );
  let sendButton = await waitFor(() => activeSubmitButton(editor), 5000);
  if (!sendButton) {
    throw new Error("SUBMIT_UNCONFIRMED：豆包发送按钮未就绪，未发送 ACK。");
  }
  const submittedText = editorText(editor);
  const busyBeforeSubmit = doubaoConversationBusyReason();
  if (busyBeforeSubmit) {
    throw new Error(`DOUBAO_BUSY: ${busyBeforeSubmit}; 自动投递已停止，未发送 ACK。`);
  }
  await reportDeliveryStage(requestId, "submit_click_attempted");
  sendButton.click();

  let confirmedMessage = await waitFor(submittedMessage, 1500);
  if (!confirmedMessage) {
    if (doubaoQueueIsPaused() || editorText(editor) !== submittedText) {
      throw new Error("SUBMIT_QUEUED_OR_UNCONFIRMED: 豆包已接管草稿；禁止重复点击，未发送 ACK。");
    }
    sendButton = activeSubmitButton(editor);
    if (sendButton) {
      await reportDeliveryStage(requestId, "submit_pointer_fallback");
      dispatchPointerActivation(sendButton);
      confirmedMessage = await waitFor(submittedMessage, 1500);
    }
  }
  if (!confirmedMessage) {
    if (doubaoQueueIsPaused() || editorText(editor) !== submittedText) {
      throw new Error("SUBMIT_QUEUED_OR_UNCONFIRMED: 豆包已接管草稿；禁止 Enter 回退，未发送 ACK。");
    }
    await reportDeliveryStage(requestId, "submit_keyboard_fallback");
    dispatchEnter(editor);
    confirmedMessage = await waitFor(
      submittedMessage,
      Math.max(1000, DELIVERY_SUBMIT_TIMEOUT_MS - 3000),
    );
  }
  if (!confirmedMessage) {
    throw new Error("SUBMIT_UNCONFIRMED：豆包未出现真实用户消息，未发送 ACK。");
  }
  pendingDelivery = null;
  renderStatus();
  await reportDeliveryStage(requestId, "submit_confirmed");
  if (typeof onSubmitConfirmed === "function") {
    const callbackConfirmed = await onSubmitConfirmed();
    if (callbackConfirmed === false) {
      throw new Error("CALLBACK_UNCONFIRMED: post-submit callback did not close");
    }
  }
  setActivity("结果已提交，等待心机姝响应...");

  const awakened = await waitFor(
    () => generationIsVisible()
      || messagesByRole("assistant").some((message) => !existingAssistantMessages.has(message)),
    DELIVERY_WAKE_TIMEOUT_MS,
  );
  if (!awakened) {
    await reportDeliveryStage(requestId, "wake_unconfirmed");
    throw new Error("WAKE_UNCONFIRMED：消息已进入豆包，但未观察到心机姝响应启动。");
  }
  await reportDeliveryStage(requestId, "wake_confirmed");
}

function evaluateSubmitConfirmation({
  submittedMessage = null,
  generationVisibleBefore = false,
  submittedText = "",
  requestId = "",
  editorCleared = false,
  generationVisibleAfter = false,
}) {
  if (submittedMessage) {
    return { confirmed: true, path: "user_message" };
  }
  const draftMatched = Boolean(submittedText && requestId && submittedText.includes(requestId));
  if (!generationVisibleBefore && draftMatched && editorCleared && generationVisibleAfter) {
    return { confirmed: true, path: "rising_edge_fallback" };
  }
  return { confirmed: false, path: null };
}

async function submitEditor(editor, requestId, onSubmitConfirmed = null) {
  if (isDoubao) {
    return submitDoubaoEditor(editor, requestId, onSubmitConfirmed);
  }
  const selector = joinedSelectors(adapterProfile?.selectors.submit);
  const existingUserMessages = new Set(messagesByRole("user"));
  const existingAssistantMessages = new Set(messagesByRole("assistant"));
  let button = null;
  for (let attempt = 0; attempt < 6000; attempt += 1) {
    let scope = editor.parentElement;
    for (let depth = 0; scope && depth < 12 && !button; depth += 1) {
      button = [...scope.querySelectorAll(selector)].find(
        (candidate) => candidate.getClientRects().length > 0,
      );
      scope = scope.parentElement;
    }
    if (!button) {
      button = [...document.querySelectorAll(selector)].find(
        (candidate) => candidate.getClientRects().length > 0,
      );
    }
    if (button && !button.disabled && button.getAttribute("aria-disabled") !== "true") break;
    button = null;
    await sleep(100);
  }
  if (!button || button.disabled || button.getAttribute("aria-disabled") === "true") {
    throw new Error("当前 AI 输入框的发送按钮在 600 秒内未就绪。");
  }
  const submittedText = editorText(editor);
  const generationVisibleBefore = generationIsVisible();
  button.click();
  for (let attempt = 0; attempt < 10 && editorText(editor); attempt += 1) {
    await sleep(100);
  }
  if (editorText(editor) === submittedText) {
    editor.focus();
    editor.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Enter",
        code: "Enter",
        bubbles: true,
        cancelable: true,
      }),
    );
  }
  for (let attempt = 0; attempt < 40 && editorText(editor); attempt += 1) {
    await sleep(100);
  }

  const submittedMessage = await waitFor(
    () => messagesByRole("user").find(
      (message) => !existingUserMessages.has(message) && (
        (requestId && message.textContent.includes(requestId))
        || (submittedText && message.textContent.includes(submittedText.trim().slice(0, 20)))
      ),
    ),
    DELIVERY_SUBMIT_TIMEOUT_MS,
  );

  let submitConfirmed = false;
  let confirmMode = null;
  if (submittedMessage) {
    submitConfirmed = true;
    confirmMode = "user_message_match";
  } else {
    const editorCleared = (editorText(editor) || "").trim().length === 0;
    const risingEdgeObserved = await waitFor(
      () => generationIsVisible(),
      Math.min(DELIVERY_SUBMIT_TIMEOUT_MS, 10000),
    );
    const evaluation = evaluateSubmitConfirmation({
      submittedMessage: null,
      generationVisibleBefore,
      submittedText,
      requestId,
      editorCleared,
      generationVisibleAfter: Boolean(risingEdgeObserved),
    });
    submitConfirmed = evaluation.confirmed;
    if (submitConfirmed) {
      confirmMode = "fallback_generation_rising_edge";
    }
  }

  if (!submitConfirmed) {
    throw new Error("SUBMIT_UNCONFIRMED：未观察到真实用户消息或生成上升沿；可点击“重试回传”，工具不会重跑。");
  }
  pendingDelivery = null;
  renderStatus();
  await reportDeliveryStage(requestId, "submit_confirmed");
  if (typeof onSubmitConfirmed === "function") {
    const callbackConfirmed = await onSubmitConfirmed(confirmMode);
    if (callbackConfirmed === false) {
      throw new Error("CALLBACK_UNCONFIRMED: post-submit callback did not close");
    }
  }
  setActivity("结果已提交，等待折叠主机响应...");

  const awakened = await waitFor(
    () => generationIsVisible()
      || messagesByRole("assistant").some((message) => !existingAssistantMessages.has(message)),
    DELIVERY_WAKE_TIMEOUT_MS,
  );
  if (!awakened) {
    await reportDeliveryStage(requestId, "wake_unconfirmed");
    throw new Error("WAKE_UNCONFIRMED：结果已进入对话，但未观察到折叠主机响应启动；请检查 ChatGPT 状态。");
  }
  await reportDeliveryStage(requestId, "wake_confirmed");
}

async function retryDelivery() {
  if (!pendingDelivery) return;
  const delivery = pendingDelivery;
  if (messagesByRole("user").some((message) => message.textContent.includes(delivery.requestId))) {
    pendingDelivery = null;
    renderStatus();
    setActivity("结果已存在于对话中，已取消重复回传");
    return;
  }
  try {
    setActivity("正在重试结果回传，不重复执行工具...");
    const editor = insertIntoEditor(delivery.text);
    await reportDeliveryStage(delivery.requestId, "retry_inserted");
    await submitEditor(editor, delivery.requestId);
    setActivity("回传已确认，折叠主机已唤醒");
  } catch (error) {
    setActivity(`NEEDS_HUMAN：${error.message}`);
  }
}

async function insertInstructions() {
  const instructions = [
    "【PocketFleet 多AI协同出站规范】",
    "• 当你在对话中向星舰战队其它席位或 Telegram 发信时，请在回复首行以规范头开头：",
    "  [Telegram]re:{收件席位} 回复内容...",
    "• 示例：",
    "  [Telegram]re:@AiSoulJudgeBot 代码审计已完成，基线无异常。",
    "• 浏览器扩展将自动识别发信指令并通过本地网桥无感转发至战队群！",
  ].join("\n");
  try {
    insertIntoEditor(instructions);
    setActivity("协同发信规范说明已放入输入框");
  } catch (error) {
    setActivity(error.message);
  }
}

function parseCall(code) {
  return parseCallText(code.textContent);
}

function normalizeJsonTypography(text) {
  if (typeof text !== "string") return "";
  return text
    .replace(/[\u201c\u201d\u201e\u201f\u00ab\u00bb]/g, '"')
    .replace(/[\u2018\u2019\u201a\u201b]/g, "'")
    .replace(/[\u2013\u2014\u2015]/g, "-");
}

function normalizeStructuralJsonQuotes(text) {
  if (typeof text !== "string") return "";
  let s = text.replace(/｛/g, "{").replace(/｝/g, "}");
  s = s.replace(/([{\[,:]\s*)[“\u201c\u201d\u201e\u201f]/g, '$1"');
  s = s.replace(/[“\u201c\u201d\u201e\u201f](\s*[:,\}\]])/g, '"$1');
  return s;
}

function safeParseJson(raw) {
  if (!raw || typeof raw !== "string") return null;
  const trimmed = raw.trim();
  try {
    return JSON.parse(trimmed);
  } catch {}

  try {
    return JSON.parse(normalizeStructuralJsonQuotes(trimmed));
  } catch {}

  try {
    return JSON.parse(normalizeJsonTypography(trimmed));
  } catch {}

  return null;
}

function parseCallText(text) {
  try {
    const parsed = safeParseJson(text);
    const call = parsed?.[CALL_KEY];
    if (!call || typeof call.request_id !== "string" || typeof call.tool !== "string") return null;
    if (!call.arguments || typeof call.arguments !== "object" || Array.isArray(call.arguments)) return null;
    return call;
  } catch {
    return null;
  }
}

function inspectJsonObjects(text) {
  const objects = [];
  let start = -1;
  let depth = 0;
  let inString = false;
  let quoteChar = null;
  let escaped = false;
  let hasLeadingText = false;
  let hasTrailingText = false;

  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    if (start < 0) {
      if (/\s/.test(character)) continue;
      if (character !== "{" && character !== "｛") {
        if (objects.length === 0) {
          hasLeadingText = true;
        } else {
          hasTrailingText = true;
        }
        continue;
      }
      start = index;
      depth = 1;
      continue;
    }

    if (inString) {
      if (escaped) {
        escaped = false;
      } else if (character === "\\") {
        escaped = true;
      } else if (
        character === quoteChar ||
        (quoteChar === '“' && character === '”') ||
        (quoteChar === '”' && character === '“')
      ) {
        inString = false;
        quoteChar = null;
      }
      continue;
    }

    if (character === '"' || character === '“' || character === '”') {
      inString = true;
      quoteChar = character;
    } else if (character === "{" || character === "｛") {
      depth += 1;
    } else if (character === "}" || character === "｝") {
      depth -= 1;
      if (depth === 0) {
        objects.push(text.slice(start, index + 1));
        start = -1;
      }
    }
  }

  const isIncomplete = (start >= 0 && depth > 0) || (objects.length === 0 && !hasLeadingText && text.trim().length === 0);
  return {
    objects,
    isIncomplete,
    hasLeadingText,
    hasTrailingText,
    hasNonWhitespaceNonBrace: hasLeadingText || hasTrailingText,
  };
}

function extractJsonObjects(text) {
  return inspectJsonObjects(text).objects;
}

function hasBridgeHeader(message) {
  const content = message.textContent.trimStart();
  if (!content.startsWith(BRIDGE_HEADER)) return false;
  const nextCharacter = content.slice(BRIDGE_HEADER.length, BRIDGE_HEADER.length + 1);
  return !nextCharacter || /\s/.test(nextCharacter);
}

function callsFromMessage(message) {
  const codeCalls = [...message.querySelectorAll("pre code")].map(parseCall).filter(Boolean);
  if (codeCalls.length > 0) return codeCalls;
  const content = message.textContent.trimStart();
  const body = content.slice(BRIDGE_HEADER.length);
  return extractJsonObjects(body).map(parseCallText).filter(Boolean);
}

async function executeCall(call) {
  setActivity(`执行 ${call.tool}...`);
  try {
    const result = await send({
      type: "bridge-call",
      payload: {
        request_id: call.request_id,
        timestamp: Math.floor(Date.now() / 1000),
        tool: call.tool,
        arguments: call.arguments,
      },
    });
    const resultPrompt = [
      "[FOLDED_HOST_TOOL_RESULT]",
      JSON.stringify(result),
      "[/FOLDED_HOST_TOOL_RESULT]",
      "请基于真实工具结果继续；不要重复相同 request_id。",
    ].join("\n");
    pendingDelivery = { requestId: call.request_id, text: resultPrompt };
    renderStatus();
    await reportDeliveryStage(call.request_id, "result_received");
    const editor = insertIntoEditor(resultPrompt);
    await reportDeliveryStage(call.request_id, "result_inserted");
    await submitEditor(editor, call.request_id);
    setActivity(`${call.tool} 已执行、提交并唤醒`);
  } catch (error) {
    await reportDeliveryStage(call.request_id, "delivery_failed");
    setActivity(`NEEDS_HUMAN：${error.message}`);
    await refreshStatus();
  }
}

async function scanCalls() {
  for (const message of messagesByRole("assistant")) {
    if (executedMessages.has(message) || !hasBridgeHeader(message)) continue;
    const calls = callsFromMessage(message);
    if (calls.length === 0) continue;
    if (calls.length > 1) {
      executedMessages.add(message);
      setActivity("已拒绝：一条 bridge 消息只能包含一个工具调用");
      continue;
    }
    const [call] = calls;
    if (handled.has(call.request_id)) continue;
    handled.add(call.request_id);
    executedMessages.add(message);
    executeCall(call);
  }
}

function getOrCreateMessageState(message, now = Date.now()) {
  let state = messageStates.get(message);
  const signature = message.textContent || "";
  if (!state) {
    state = {
      signature,
      lastChangedAt: now,
      timer: null,
      finalized: false,
    };
    messageStates.set(message, state);
  } else if (state.signature !== signature) {
    state.signature = signature;
    state.lastChangedAt = now;
    if (state.timer) {
      clearTimeout(state.timer);
      state.timer = null;
    }
    if (!executedMessages.has(message)) {
      state.finalized = false;
    }
  }
  return state;
}

function evaluateMessageFinalization(message, { now = Date.now(), isGenerating = false, stabilityWindowMs = STABILITY_WINDOW_MS } = {}) {
  const state = getOrCreateMessageState(message, now);
  if (state.finalized || executedMessages.has(message)) {
    return { canFinalize: false, state, isStable: true, remainingMs: 0, reason: "already_finalized" };
  }
  if (isGenerating) {
    return { canFinalize: false, state, isStable: false, remainingMs: stabilityWindowMs, reason: "generating" };
  }
  const elapsed = now - state.lastChangedAt;
  if (elapsed < stabilityWindowMs) {
    return { canFinalize: false, state, isStable: false, remainingMs: stabilityWindowMs - elapsed, reason: "within_stability_window" };
  }
  return { canFinalize: true, state, isStable: true, remainingMs: 0, reason: null };
}

function markExistingCallsHandled(customMessages = null, { recoverLatestDoubao = true } = {}) {
  if (customMessages) {
    for (const message of customMessages) {
      executedMessages.add(message);
      const state = getOrCreateMessageState(message);
      state.finalized = true;
      const inspection = inspectThetaMessage(message, { isGenerating: false });
      if (inspection.payload && inspection.payload.message_id) {
        handled.add(inspection.payload.message_id);
      }
    }
    return;
  }
  const assistantMessages = messagesByRole("assistant");
  const recoverableDoubaoMessage = isDoubao && recoverLatestDoubao
    ? [...assistantMessages].reverse().find((message) => hasCodeAiHeader(message))
    : null;
  for (const message of assistantMessages) {
    if (hasBridgeHeader(message)) {
      for (const call of callsFromMessage(message)) handled.add(call.request_id);
      executedMessages.add(message);
    }
    if (hasCodeAiHeader(message)) {
      if (message === recoverableDoubaoMessage) continue;
      const codeaiMsg = parseCodeAiMessage(message);
      if (codeaiMsg && codeaiMsg.message_id) {
        handled.add(codeaiMsg.message_id);
        persistHandledCodeAiId(codeaiMsg.message_id);
      }
      executedMessages.add(message);
    }
    // 关键基线防护：页面加载/刷新时，页面上已有的历史电报消息全部打标已处理，严禁历史重放！
    // 唯有当前处于活跃流式生成中（正在打字）的最后一条消息才被豁免，等待生成结束后出站。
    if (typeof isTelegramOutbound === "function" && isTelegramOutbound(message.textContent)) {
      if (typeof isGenerationActiveNow === "function" && isGenerationActiveNow() && message === assistantMessages[assistantMessages.length - 1]) {
        // 正在流式打字中，不锁死，等待结束
      } else {
        telegramExecutedElements.add(message);
      }
    }
  }
  if (typeof document !== "undefined") {
    const pageNodes = document.querySelectorAll(
      ".markdown-body, [class*='markdown'], [class*='conversation'], [class*='message'], [class*='bubble'], [class*='content'], article, pre"
    );
    for (const node of pageNodes) {
      if (node.closest(`#${PANEL_ID}`) || node.closest("textarea, [contenteditable='true']")) continue;
      if (hasCodeAiHeader(node)) {
        executedMessages.add(node);
        const codeaiMsg = parseCodeAiMessage(node);
        if (codeaiMsg && codeaiMsg.message_id) {
          handled.add(codeaiMsg.message_id);
          persistHandledCodeAiId(codeaiMsg.message_id);
        }
      }
    }
  }
  if (isQwen) {
    for (const message of qwenAssistantMessages()) {
      executedMessages.add(message);
      const state = getOrCreateMessageState(message);
      state.finalized = true;
      const inspection = inspectThetaMessage(message, { isGenerating: false });
      if (inspection.payload && inspection.payload.message_id) {
        handled.add(inspection.payload.message_id);
      }
    }
  }
}

const CODEAI_HEADER = "[#codeai:v1]";
let isPullingCodeAi = false;

function hasCodeAiHeader(message) {
  if (!message) return false;
  const content = (message.textContent || "").trimStart();
  if (!content.includes(CODEAI_HEADER)) return false;
  const idx = content.indexOf(CODEAI_HEADER);
  const tail = content.slice(idx + CODEAI_HEADER.length).trimStart();
  if (!tail || tail.startsWith("{") || tail.startsWith("｛") || tail.startsWith("<") || /^\s/.test(content.slice(idx + CODEAI_HEADER.length, idx + CODEAI_HEADER.length + 1))) {
    return true;
  }
  return tail.includes("codeai_message") || tail.includes("theta_codeai_message");
}

function codeaiMsgFromParsed(obj) {
  // 优先信封格式 {"codeai_message": {...}}；兜底裸格式 {..., message_id, content}。
  // 背景：结算主机主会话恢复后曾输出裸 JSON（2026-09-15 实测），旧解析恒 null ⇒ 出站静默丢失。
  if (obj?.codeai_message?.message_id) return obj.codeai_message;
  if (typeof obj?.message_id === "string" && obj.message_id && typeof obj?.content === "string") return obj;
  return null;
}

function parseCodeAiMessage(message) {
  if (!message) return null;
  const codeBlocks = message.querySelectorAll ? [...message.querySelectorAll("pre code, pre, code")] : [];
  for (const block of codeBlocks) {
    const blockText = (block.textContent || "").trimStart();
    if (blockText.includes(CODEAI_HEADER)) {
      const idx = blockText.indexOf(CODEAI_HEADER);
      const body = blockText.slice(idx + CODEAI_HEADER.length);
      const jsonStrs = extractJsonObjects(body);
      for (const s of jsonStrs) {
        try {
          const obj = safeParseJson(s);
          if (codeaiMsgFromParsed(obj)) return codeaiMsgFromParsed(obj);
        } catch {}
      }
    }
    const jsonStrs = extractJsonObjects(blockText);
    for (const s of jsonStrs) {
      try {
        const obj = safeParseJson(s);
        if (codeaiMsgFromParsed(obj)) return codeaiMsgFromParsed(obj);
      } catch {}
    }
  }

  const content = (message.textContent || "").trimStart();
  const idx = content.indexOf(CODEAI_HEADER);
  if (idx >= 0) {
    const body = content.slice(idx + CODEAI_HEADER.length);
    const jsonStrs = extractJsonObjects(body);
    for (const s of jsonStrs) {
      try {
        const obj = safeParseJson(s);
        if (codeaiMsgFromParsed(obj)) return codeaiMsgFromParsed(obj);
      } catch {}
    }
  }
  return null;
}

// ── C：出站失败的「回程」（2026-09-16 指挥官批准）────────────────────────────
// 结构性缺陷：发信被桥拒绝时，扩展只把原因写进页面浮层（给人看）；写信的
// 那个节点自己收不到任何反馈 ⇒ 只能靠人每次去提醒它。
// 补丁：失败时把桥的拒绝原因**注入该节点自己的聊天窗**，节点读到后可自纠重发。
// 三条硬边界（缺一条都会出问题）：
//   1. 只在**节点可自纠的载荷类拒绝**时注入（白名单式判定）。404 / 503 / PAUSED
//      属控制链故障，节点改了也没用 ⇒ 不注入，不白烧它的 token。
//   2. 注入文本**不含** [#codeai:v1] / [#theta:v1] 宏头，故不会被 scanCodeAiCalls
//      当成新信件再投递一次 ⇒ 无死循环。
//   3. 成功路径**保持不注入**：实测成功回执会诱发 AI 寒暄轮（白烧一轮，
//      见 pollThetaIncoming 注释）。只有失败才值得烧这一轮。
const LOCAL_NOTICE_PREFIX = "[#bridge_local_notice]";
const injectedNoticeKeys = new Set();

function persistInjectedNoticeKey(key) {
  if (!key) return;
  injectedNoticeKeys.add(key);
  try {
    if (typeof chrome !== "undefined" && chrome.storage?.local?.get) {
      chrome.storage.local.get(["injected_notice_keys"], (res) => {
        const list = Array.isArray(res?.injected_notice_keys) ? res.injected_notice_keys : [];
        if (!list.includes(key)) {
          list.push(key);
          if (list.length > 200) list.splice(0, list.length - 200);
          chrome.storage.local.set({ injected_notice_keys: list });
        }
      });
    }
  } catch {}
}

try {
  if (typeof chrome !== "undefined" && chrome.storage?.local?.get) {
    chrome.storage.local.get(["injected_notice_keys"], (res) => {
      const list = res?.injected_notice_keys;
      if (Array.isArray(list)) {
        for (const k of list) injectedNoticeKeys.add(k);
      }
    });
  }
} catch {}

function isSelfCorrectableRejection(reason) {
  // 只注入「写信节点自己改得动」的载荷类拒绝。
  // cc is not supported ＝ CTO Gate 20260916 §4 的 FAIL-CLOSED 拒绝，
  // 节点删掉 cc / 改写 recipient 即可通过 ⇒ 属可自纠，必须注入（否则是第二种静默）。
  return /not in allowed (recipients|types)|cc is not supported|sensitive data detected|does not match the authenticated principal|message_id must be a valid UUID|exceeds maximum length|invalid codeai envelope/i
    .test(String(reason || ""));
}

async function injectOutboundFailureNotice(reason, dedupeKey) {
  const key = String(dedupeKey || reason || "").slice(0, 160);
  if (!key || injectedNoticeKeys.has(key)) return false;
  const probe = isQwen ? qwenEditor() : promptEditor();
  if (!probe) return false;
  if (String(probe.value ?? probe.textContent ?? "").trim()) return false;
  if (generationIsVisible() || (isQwen && qwenGenerationIsVisible())) return false;
  injectedNoticeKeys.add(key);
  persistInjectedNoticeKey(key);
  const ref = `bridge-local-notice-${crypto.randomUUID()}`;
  const text = [
    `${LOCAL_NOTICE_PREFIX} 本地系统提示（不是信件；请勿回复、勿引用、勿当协作件处理）：`,
    "你刚才发出的 CodeAi 信件【未落盘】，被本地桥拒绝了。",
    `桥的原始回执：${reason}`,
    "请按回执修正后重发。要点：recipient 必须是白名单节点名（拿不准就只写一个）；type 整行省略即合法（桥端默认 CUSTOM），ping / TEXT 之类自造标签会被 400 拒收；本通道没有 cc/bcc —— 若写了 cc，请删掉整行并把全部收件人写进 recipient 的 _收件人A_收件人B_ 名单。",
    `ref ${ref}`,
  ].join("\n");
  try {
    const filled = isQwen ? insertIntoQwenEditor(text) : insertIntoEditor(text);
    await (isQwen ? submitQwenEditor(filled, ref) : submitEditor(filled, ref));
    setActivity("桥的拒绝原因已注入聊天窗（写信节点可见，可自纠重发）", { warning: true });
    return true;
  } catch (error) {
    setActivity(`注入拒绝原因失败（仅页面浮层可见）：${error.message}`, { warning: true });
    return false;
  }
}

async function scanCodeAiCalls() {
  if (routeBaselinePending || killSwitchActive) return;
  const scanned = new Set();
  const candidates = [];
  const assistantList = messagesByRole("assistant");
  for (const message of assistantList.slice(-3)) {
    candidates.push(message);
    scanned.add(message);
  }
  if (typeof document !== "undefined") {
    const pageNodes = document.querySelectorAll(
      ".markdown-body, [class*='markdown'], [class*='conversation'], [class*='message'], [class*='bubble'], [class*='content'], pre"
    );
    const recentNodes = Array.from(pageNodes).slice(-5);
    for (const node of recentNodes) {
      if (node.closest(`#${PANEL_ID}`) || node.closest("textarea, [contenteditable='true']")) continue;
      if (!scanned.has(node) && hasCodeAiHeader(node)) {
        candidates.push(node);
        scanned.add(node);
      }
    }
  }

  for (const message of candidates) {
    if (executedMessages.has(message) || !hasCodeAiHeader(message)) continue;
    const codeaiMsg = parseCodeAiMessage(message);
    if (!codeaiMsg || !codeaiMsg.message_id) continue;
    if (handled.has(codeaiMsg.message_id)) continue;
    handled.add(codeaiMsg.message_id);
    persistHandledCodeAiId(codeaiMsg.message_id);
    executedMessages.add(message);

    setActivity(`正在发送 CodeAi 消息到会议: ${codeaiMsg.title || "消息"}...`);
    try {
      const resp = await send({ type: "bridge-codeai-post", payload: { codeai_message: codeaiMsg } });
      if (resp && resp.ok) {
        setActivity(`CodeAi 消息已写入会议: ${resp.filename || "成功"}`);
      } else {
        setActivity(`CodeAi 写入失败: ${resp?.error || "未知错误"}`);
      }
    } catch (e) {
      const reason = String(e.message || e);
      if (reason.includes("404")) {
        setConnection("控制链在线，工具可以执行 | VERSION_MISMATCH / RESTART_REQUIRED", "warning");
        setActivity("CodeAi 发信失败 (HTTP 404)：VERSION_MISMATCH / RESTART_REQUIRED");
      } else if (reason.includes("503") || reason.includes("PAUSED")) {
        setActivity("CodeAi 发信失败：控制链已暂停 (PAUSED)");
      } else {
        setActivity(`CodeAi 发信失败：${reason}`, { warning: true });
        // C：把拒绝原因注入本节点聊天窗，让它自己看到并修正重发（不 await，不阻塞轮询）
        if (isSelfCorrectableRejection(reason)) {
          injectOutboundFailureNotice(reason, codeaiMsg.message_id);
        }
      }
    }
  }
}

const OPENCLAW_HEADER = "[#openclaw:v1]";
const OPENCLAW_RESULT_HEADER = "[#openclaw_result:v1]";
let isOpenClawBusy = false;
const openclawInFlight = new Set();

function hasOpenClawHeader(message) {
  if (!message) return false;
  const content = (message.textContent || "").trimStart();
  if (!content.includes(OPENCLAW_HEADER)) return false;
  const idx = content.indexOf(OPENCLAW_HEADER);
  const tail = content.slice(idx + OPENCLAW_HEADER.length).trimStart();
  return !tail || tail.startsWith("{") || tail.startsWith("｛") || tail.startsWith("<");
}

function parseOpenClawTask(message) {
  if (!message) return null;
  const extract = (body) => {
    for (const s of extractJsonObjects(body)) {
      try {
        const obj = safeParseJson(s);
        // 信封优先 {"openclaw_task": {...}}；裸格式兜底（同 codeai 裸 JSON 血泪，2026-09-15）。
        if (obj?.openclaw_task?.task_id && obj.openclaw_task.instruction) return obj.openclaw_task;
        if (typeof obj?.task_id === "string" && typeof obj?.instruction === "string") return obj;
      } catch {}
    }
    return null;
  };
  const codeBlocks = message.querySelectorAll ? [...message.querySelectorAll("pre code, pre, code")] : [];
  for (const block of codeBlocks) {
    const blockText = (block.textContent || "").trimStart();
    if (blockText.includes(OPENCLAW_HEADER)) {
      const idx = blockText.indexOf(OPENCLAW_HEADER);
      const task = extract(blockText.slice(idx + OPENCLAW_HEADER.length));
      if (task) return task;
    }
  }
  const content = (message.textContent || "").trimStart();
  const idx = content.indexOf(OPENCLAW_HEADER);
  if (idx >= 0) {
    return extract(content.slice(idx + OPENCLAW_HEADER.length));
  }
  return null;
}

// VOID 任务判定（补偿施工令 20260915144810 §二）：task 体带 "void": true 或
// task_id 以 void- / void_ 前缀 ⇒ 排除、标记已执行、不派发、不回注。
function isOpenClawVoidTask(task) {
  if (!task) return false;
  if (task.void === true) return true;
  return /^void[-_]/i.test(String(task.task_id || ""));
}

async function scanOpenClawTasks() {
  if (isOpenClawBusy) return;
  const candidates = messagesByRole("assistant").filter(
    (message) => !executedMessages.has(message) && hasOpenClawHeader(message),
  );
  if (!candidates.length) return;
  isOpenClawBusy = true;
  try {
    for (const message of candidates) {
      const task = parseOpenClawTask(message);
      if (!task || !task.task_id || openclawInFlight.has(task.task_id)) continue;
      if (isOpenClawVoidTask(task)) {
        executedMessages.add(message);
        setActivity(`OpenClaw VOID 任务已排除: ${task.task_id}`, { warning: true });
        continue;
      }
      openclawInFlight.add(task.task_id);
      executedMessages.add(message);
      setActivity(`OpenClaw 任务转发中: ${task.task_id} ...`);
      try {
        // P0 架构纠偏（20260915145619）：插件在 OpenClaw 链路只做出站识别与发送，
        // 不做结果回填。结果由 Relay 写入既有 CodeAI 会议邮件总线，不经浏览器。
        await send({
          type: "bridge-openclaw-task",
          payload: {
            task_id: task.task_id,
            session_key: task.session_key || "agent:bridgewatch:main",
            instruction: task.instruction,
            timeout_ms: task.timeout_ms,
          },
        });
        setActivity(`OpenClaw 任务已出站: ${task.task_id}（结果走会议邮件总线）`);
      } catch (e) {
        // 传输失败：本消息不再重扫（防重复派发循环），task 锁释放；
        // 页面刷新后允许再试（relay 可能已恢复）。
        openclawInFlight.delete(task.task_id);
        executedMessages.add(message);
        setActivity(`OpenClaw 出站失败: ${task.task_id} — ${e.message}`, { warning: true });
      }
    }
  } finally {
    isOpenClawBusy = false;
  }
}

const CODEAI_MEETING_NOTICE_HEADER = "[#codeai_meeting_notice:v1]";

function isManagedCodeAiDraft(text) {
  return text.trimStart().startsWith(CODEAI_MEETING_NOTICE_HEADER)
    && text.includes("来源文件:")
    && text.includes("哈希校验:");
}

function managedDraftMatchesDelivery(text, delivery) {
  return isManagedCodeAiDraft(text)
    && text.includes(delivery.filename)
    && text.includes(delivery.sha256);
}

async function pollCodeAiIncoming() {
  if (isQwen || isPullingCodeAi || killSwitchActive || routeBaselinePending || generationIsVisible()) return;
  if (doubaoConversationBusyReason()) return;
  const editor = promptEditor();
  if (!editor) {
    if (activityMessage === "尚无工具活动" || activityMessage.includes("未找到输入框")) {
      setActivity("未找到输入框，页面适配器需要更新", true);
    }
    return;
  }
  const existingDraft = editorText(editor);
  const canRecoverManagedDraft = isDoubao && isManagedCodeAiDraft(existingDraft);
  if (existingDraft && !canRecoverManagedDraft) {
    if (activityMessage === "尚无工具活动" || activityMessage.includes("输入框非空")) {
      setActivity("输入框非空，已暂停自动拉取");
    }
    return;
  }

  isPullingCodeAi = true;
  try {
    const resp = await send({ type: "bridge-codeai-pull" });
    if (resp && resp.ok && resp.pending && resp.content) {
      // 对话组免疫硬门禁：若内容包含免回/抄送声明，绝不注入输入框与自动敲回车，直接静默 ACK
      const isNoticeOnly = String(resp.content).includes("kind:[notice|")
        || String(resp.content).includes("【此条无需回复，请阅知】")
        || String(resp.content).includes("Mode:[NoReply]");
      if (isNoticeOnly) {
        if (resp.delivery_id) {
          await send({ type: "bridge-codeai-ack", delivery_id: resp.delivery_id });
        }
        setActivity(`[免回件] 已阅知入账: ${resp.filename || "通知公文"}`);
        return;
      }

      // duty-wake（OpenClaw 门铃勤务位）→ **只注入桥合成的那一行**，不套任何会议件包装。
      // 结算主机按 token 计费，"[#codeai_meeting_notice:v1]/来源文件/哈希校验/请阅读…"
      // 对心跳唤醒是纯开销（20260915 指挥官令：特别通道上只留那一句）。
      // 判据三重兜底：channel 为主；raw 布尔与 .ready.json 文件名防契约漂移。
      const isTelegramCollab = String(resp.filename || "").includes("Telegram");
      const isRawDirect = resp.channel === "duty-wake"
        || resp.raw === true
        || String(resp.filename || "").endsWith(".ready.json")
        || isTelegramCollab;
      const prompt = isRawDirect ? resp.content : [
        CODEAI_MEETING_NOTICE_HEADER,
        `来源文件: ${resp.filename}`,
        `哈希校验: ${resp.sha256}`,
        `----------------------------------------`,
        resp.content,
        `----------------------------------------`,
        `请阅读上述来自会议的最新协作文件并自主处理。`
      ].join("\n");
      let ed = editor;
      if (existingDraft) {
        const currentDraft = editorText(editor);
        if (!managedDraftMatchesDelivery(currentDraft, resp)) {
          throw new Error("受管会议草稿与当前投递租约不一致，拒绝自动发送。");
        }
        setActivity(`正在恢复未闭环会议草稿: ${resp.filename}`);
      } else {
        ed = insertIntoEditor(prompt);
      }
      let ackSent = false;
      const onUserMessageSubmitted = async (confirmMode = null) => {
        if (!resp.delivery_id || ackSent) return true;

        const maxAckAttempts = 5;
        for (let attempt = 1; attempt <= maxAckAttempts; attempt++) {
          try {
            reportDeliveryStage(resp.sha256, `ack_attempt_${attempt}`);
            const ackPayload = { type: "bridge-codeai-ack", delivery_id: resp.delivery_id };
            if (confirmMode) {
              ackPayload.confirm_mode = confirmMode;
            }
            const ackResult = await send(ackPayload);
            if (ackResult && ackResult.ok && ackResult.status === "ACKNOWLEDGED" && ackResult.delivery_id === resp.delivery_id) {
              ackSent = true;
              reportDeliveryStage(resp.sha256, "ack_confirmed");
              setActivity(`会议来信已完成 ACK 闭环: ${resp.filename}`);
              return true;
            }
            const errMsg = ackResult?.error || (typeof ackResult === "object" ? JSON.stringify(ackResult) : "未知错误");
            setActivity(`ACK 尝试 ${attempt}/${maxAckAttempts} 失败: ${errMsg}`);
          } catch (ackErr) {
            setActivity(`ACK 尝试 ${attempt}/${maxAckAttempts} 异常: ${ackErr.message}`);
          }
          if (attempt < maxAckAttempts) {
            await new Promise(r => setTimeout(r, attempt * 1000));
          }
        }
        setActivity(`NEEDS_HUMAN：消息已提交，但 ACK 耗尽重试未闭环: ${resp.filename}`);
        return false;
      };

      const submitToken = isRawDirect ? (prompt.trim().slice(0, 30) || resp.sha256) : resp.sha256;
      try {
        await submitEditor(ed, submitToken, onUserMessageSubmitted);
        setActivity(`会议来信已投递折叠主机并唤醒: ${resp.filename}`);
      } catch (wakeErr) {
        if (ackSent) {
          setActivity(`NEEDS_HUMAN：会议来信已提交但唤醒未确认: ${wakeErr.message}`);
        } else if (wakeErr.message && wakeErr.message.includes("CALLBACK_UNCONFIRMED")) {
          setActivity(`NEEDS_HUMAN：消息已提交，但 ACK 未闭环: ${resp.filename}`);
        } else {
          throw wakeErr;
        }
      }
    } else if (canRecoverManagedDraft) {
      setActivity("检测到未闭环会议草稿，等待投递租约可重试");
    }
  } catch (e) {
    if (e.message && e.message.includes("404")) {
      setConnection("控制链在线，工具可以执行 | VERSION_MISMATCH / RESTART_REQUIRED", "warning");
      setActivity("CodeAi 轮询失败 (HTTP 404)：VERSION_MISMATCH / RESTART_REQUIRED");
    }
  } finally {
    isPullingCodeAi = false;
  }
}

function qwenEditor() {
  return queryFirst(adapterProfile?.selectors.editor);
}

function insertIntoQwenEditor(text) {
  const editor = qwenEditor();
  if (!editor) throw new Error("未找到 Qwen 输入框，页面适配器需要更新。");
  if (editor.value && editor.value.trim()) throw new Error("输入框非空，为避免覆盖已停止自动回填。");
  editor.focus();
  const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value")?.set;
  if (nativeSetter) {
    nativeSetter.call(editor, text);
  } else {
    editor.value = text;
  }
  editor.dispatchEvent(new Event("input", { bubbles: true }));
  editor.dispatchEvent(new Event("change", { bubbles: true }));
  return editor;
}

function qwenUserMessages() {
  if (typeof document === "undefined") return [];
  return queryAll(adapterProfile?.selectors.userMessages);
}

function qwenAssistantMessages() {
  if (typeof document === "undefined") return [];
  return queryAll(adapterProfile?.selectors.assistantMessages);
}

function qwenGenerationIsVisible() {
  if (typeof document === "undefined") return false;
  return queryAll(adapterProfile?.selectors.generation).some(
    (candidate) => candidate.getClientRects().length > 0,
  );
}

async function submitQwenEditor(editor, requestId, onSubmitConfirmed = null) {
  const selector = joinedSelectors(adapterProfile?.selectors.submit);
  const existingUserMessages = new Set(qwenUserMessages());
  const existingAssistantMessages = new Set(qwenAssistantMessages());
  let button = null;
  for (let attempt = 0; attempt < 6000; attempt += 1) {
    let scope = editor.parentElement;
    for (let depth = 0; scope && depth < 12 && !button; depth += 1) {
      button = [...scope.querySelectorAll(selector)].find(
        (candidate) => candidate.getClientRects().length > 0,
      );
      scope = scope.parentElement;
    }
    if (!button) {
      button = [...document.querySelectorAll(selector)].find(
        (candidate) => candidate.getClientRects().length > 0,
      );
    }
    if (button && !button.disabled && button.getAttribute("aria-disabled") !== "true") break;
    button = null;
    await sleep(100);
  }
  if (!button || button.disabled || button.getAttribute("aria-disabled") === "true") {
    throw new Error("当前 Qwen 输入框的发送按钮在 600 秒内未就绪。");
  }
  const submittedText = (editor.value || "").trim();
  button.click();
  for (let attempt = 0; attempt < 10 && (editor.value || "").trim(); attempt += 1) {
    await sleep(100);
  }
  if ((editor.value || "").trim() === submittedText) {
    editor.focus();
    editor.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Enter",
        code: "Enter",
        bubbles: true,
        cancelable: true,
      }),
    );
  }
  for (let attempt = 0; attempt < 40 && (editor.value || "").trim(); attempt += 1) {
    await sleep(100);
  }

  const submittedMessage = await waitFor(
    () => qwenUserMessages().find(
      (message) => !existingUserMessages.has(message) && message.textContent.includes(requestId),
    ),
    DELIVERY_SUBMIT_TIMEOUT_MS,
  );
  if (!submittedMessage) {
    throw new Error("SUBMIT_UNCONFIRMED：未观察到真实用户消息；可点击“重试回传”，消息不会重跑。");
  }
  pendingDelivery = null;
  renderStatus();
  await reportDeliveryStage(requestId, "submit_confirmed");
  if (typeof onSubmitConfirmed === "function") {
    const callbackConfirmed = await onSubmitConfirmed();
    if (callbackConfirmed === false) {
      throw new Error("CALLBACK_UNCONFIRMED: post-submit callback did not close");
    }
  }
  setActivity("结果已提交，等待 Qwen 思想体响应...");

  const awakened = await waitFor(
    () => qwenGenerationIsVisible()
      || qwenAssistantMessages().some((message) => !existingAssistantMessages.has(message)),
    DELIVERY_WAKE_TIMEOUT_MS,
  );
  if (!awakened) {
    await reportDeliveryStage(requestId, "wake_unconfirmed");
    throw new Error("WAKE_UNCONFIRMED：结果已进入对话，但未观察到 Qwen 响应启动；请检查页面状态。");
  }
  await reportDeliveryStage(requestId, "wake_confirmed");
}

function isThinkingElement(el) {
  if (!el || el.nodeType !== 1) return false;
  const tag = (el.tagName || "").toLowerCase();
  if (tag === "details" || tag === "summary") return true;
  const className = typeof el.className === "string" ? el.className.toLowerCase() : "";
  if (/think|thought|reasoning|collapse/.test(className)) return true;
  const testId = (el.getAttribute && el.getAttribute("data-testid") || "").toLowerCase();
  if (/think|thought|reasoning/.test(testId)) return true;
  const role = (el.getAttribute && el.getAttribute("role") || "").toLowerCase();
  if (/think|thought|reasoning/.test(role)) return true;
  return false;
}

function isInsideThinking(node, root) {
  let cur = node.nodeType === 3 ? node.parentElement : node;
  while (cur && cur !== root) {
    if (isThinkingElement(cur)) return true;
    cur = cur.parentElement;
  }
  return false;
}

function isElementVisible(el) {
  if (!el || el.nodeType !== 1) return false;
  if (el.getAttribute && el.getAttribute("aria-hidden") === "true") return false;
  if (el.hidden) return false;
  const style = el.style;
  if (style && (style.display === "none" || style.visibility === "hidden")) return false;
  return true;
}

function isInsideHidden(node, root) {
  let cur = node.nodeType === 3 ? node.parentElement : node;
  while (cur && cur !== root) {
    if (!isElementVisible(cur)) return true;
    cur = cur.parentElement;
  }
  return false;
}

const BLOCK_TAGS = new Set([
  "P", "DIV", "PRE", "BLOCKQUOTE", "LI", "UL", "OL", "H1", "H2", "H3", "H4", "H5", "H6",
  "TABLE", "TR", "TD", "TH", "SECTION", "ARTICLE", "HEADER", "FOOTER", "ASIDE", "NAV", "BR"
]);

function getEnclosingBlock(node, root) {
  let cur = node.nodeType === 3 ? node.parentElement : node;
  while (cur && cur !== root) {
    const tag = (cur.tagName || "").toUpperCase();
    if (BLOCK_TAGS.has(tag)) return cur;
    cur = cur.parentElement;
  }
  return root;
}

function findForbiddenAncestor(node, root) {
  let cur = node.nodeType === 3 ? node.parentElement : node;
  while (cur && cur !== root) {
    const tag = (cur.tagName || "").toLowerCase();
    if (tag === "pre" || tag === "code" || tag === "blockquote" || tag === "details" || tag === "summary") {
      return cur;
    }
    if (isThinkingElement(cur) || !isElementVisible(cur)) {
      return cur;
    }
    cur = cur.parentElement;
  }
  return null;
}

function collectVisibleNonThinkingTextNodes(root) {
  const entries = [];
  function walk(node) {
    if (!node) return;
    if (node.nodeType === 1) {
      if (isThinkingElement(node) || !isElementVisible(node)) return;
      const children = node.childNodes && node.childNodes.length > 0 ? node.childNodes : (node.children || []);
      if (children.length === 0 && node.textContent && (!node.children || !node.children.length)) {
        entries.push({
          node: { nodeType: 3, nodeValue: node.textContent, parentElement: node },
          enclosingBlock: getEnclosingBlock(node, root),
        });
        return;
      }
      for (let i = 0; i < children.length; i++) {
        walk(children[i]);
      }
    } else if (node.nodeType === 3) {
      if (!isInsideThinking(node, root) && !isInsideHidden(node, root)) {
        entries.push({
          node,
          enclosingBlock: getEnclosingBlock(node, root),
        });
      }
    }
  }
  const rootChildren = root.childNodes && root.childNodes.length > 0 ? root.childNodes : (root.children || []);
  if (rootChildren.length === 0 && root.textContent) {
    entries.push({
      node: { nodeType: 3, nodeValue: root.textContent, parentElement: root },
      enclosingBlock: root,
    });
  } else {
    for (let i = 0; i < rootChildren.length; i++) {
      walk(rootChildren[i]);
    }
  }
  return entries;
}

// 兜底提取（P2 健壮性）：宏被 LLM 包进 pre/code 块时，主路径（要求宏顶格）会拒收。
// 此函数镜像 parseCodeAiMessage 的代码块扫描，对 theta_codeai_message 做同样容忍。
// 仅当块可见、不在思考/隐藏区时才提取，防止休眠示例被误执行。
// UUIDv4 闸：兜底路径较主路径宽松，必须更保守 —— 占位/示例 UUID（如 a1b2c3d4-…-7890-…，版本位 7）直接拒收，
// 仅放行符合舰队 message_id 惯例的真随机 UUIDv4。
const THETA_FALLBACK_UUID_V4_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
function parseThetaMessageFromCodeBlocks(message) {
  if (!message || !message.querySelectorAll) return null;
  const blocks = [...message.querySelectorAll("pre code, pre, code")];
  for (const block of blocks) {
    if (isThinkingElement(block) || isInsideThinking(block, message) || isInsideHidden(block, message)) continue;
    const blockText = (block.textContent || "").trimStart();
    if (!blockText.includes(THETA_HEADER)) continue;
    const idx = blockText.indexOf(THETA_HEADER);
    const body = blockText.slice(idx + THETA_HEADER.length);
    const jsonStrs = extractJsonObjects(body);
    for (const s of jsonStrs) {
      try {
        const obj = safeParseJson(s);
        const thetaMsg = obj?.theta_codeai_message;
        if (thetaMsg && thetaMsg.message_id && THETA_FALLBACK_UUID_V4_RE.test(String(thetaMsg.message_id))) return thetaMsg;
      } catch {}
    }
  }
  return null;
}

function inspectThetaMessage(message, { isGenerating = false } = {}) {
  if (!message || message.nodeType !== 1) {
    return { hasHeader: false, valid: false, isIncomplete: false, payload: null, rejectionReason: null };
  }

  const allText = message.textContent || "";
  if (!allText.includes(THETA_HEADER)) {
    return { hasHeader: false, valid: false, isIncomplete: false, payload: null, rejectionReason: null };
  }

  // Collect text nodes belonging strictly to visible, non-thinking areas with block tracking
  const entries = collectVisibleNonThinkingTextNodes(message);
  if (entries.length === 0) {
    return { hasHeader: false, valid: false, isIncomplete: false, payload: null, rejectionReason: "非最终答案块" };
  }

  // Find the first non-empty text node
  let firstNonEmptyIndex = -1;
  for (let i = 0; i < entries.length; i++) {
    const val = entries[i].node.nodeValue || entries[i].node.textContent || "";
    if (val.trim().length > 0) {
      firstNonEmptyIndex = i;
      break;
    }
  }

  if (firstNonEmptyIndex === -1) {
    return { hasHeader: false, valid: false, isIncomplete: false, payload: null, rejectionReason: "非最终答案块" };
  }

  const firstEntry = entries[firstNonEmptyIndex];
  const firstVal = firstEntry.node.nodeValue || firstEntry.node.textContent || "";
  const trimmedFirst = firstVal.trimStart();

  if (!trimmedFirst.startsWith(THETA_HEADER)) {
    // 宏在消息里但不在最前 —— 先尝试代码块兜底提取，再拒收（P2 容错）
    const fallback = parseThetaMessageFromCodeBlocks(message);
    if (fallback) {
      return { hasHeader: true, valid: true, isIncomplete: false, payload: fallback, rejectionReason: null };
    }
    // Macro header was present in message (e.g. in thinking area, or preceded by normal text in answer)
    return { hasHeader: false, valid: false, isIncomplete: false, payload: null, rejectionReason: "宏不在回复最前，且代码块中无有效信件" };
  }

  // Verify that the first entry's text node is NOT inside pre/code/blockquote
  const forbidden = findForbiddenAncestor(firstEntry.node, message);
  if (forbidden) {
    // 宏在代码块/引用块内 —— 尝试代码块兜底提取（P2 容错），失败才拒收
    const fallback = parseThetaMessageFromCodeBlocks(message);
    if (fallback) {
      return { hasHeader: true, valid: true, isIncomplete: false, payload: fallback, rejectionReason: null };
    }
    return { hasHeader: false, valid: false, isIncomplete: false, payload: null, rejectionReason: "宏位于代码块/引用块内，但未解析出有效信件" };
  }

  // Header boundary check within the first node:
  // If there are characters immediately after THETA_HEADER in this node, they MUST start with whitespace!
  // If the node ends right after THETA_HEADER (e.g. <p>[#theta:v1]</p>), that is also completely valid.
  const afterHeaderInFirst = trimmedFirst.slice(THETA_HEADER.length);
  // P2 容错（与 codeai 路径对齐）：允许 [#theta:v1]{...} 紧贴 JSON 的紧凑形态
  const compactBody = afterHeaderInFirst.trimStart();
  if (afterHeaderInFirst.length > 0 && !/\s/.test(afterHeaderInFirst[0]) && !compactBody.startsWith("{") && !compactBody.startsWith("｛")) {
    return { hasHeader: false, valid: false, isIncomplete: false, payload: null, rejectionReason: "格式错误" };
  }

  const hasHeader = true;

  // Construct body:
  // Start with remainder of the first node
  let body = afterHeaderInFirst;
  let prevBlock = firstEntry.enclosingBlock;

  for (let i = firstNonEmptyIndex + 1; i < entries.length; i++) {
    const entry = entries[i];
    const val = entry.node.nodeValue || entry.node.textContent || "";
    if (entry.enclosingBlock !== prevBlock) {
      body += "\n";
      prevBlock = entry.enclosingBlock;
    }
    body += val;
  }

  const { objects, isIncomplete: jsonIncomplete, hasLeadingText, hasTrailingText } = inspectJsonObjects(body);

  if (isGenerating) {
    // While generating, NEVER finalize or permanently reject!
    return { hasHeader: true, valid: false, isIncomplete: true, payload: null, rejectionReason: null };
  }

  if (objects.length > 1) {
    return { hasHeader: true, valid: false, isIncomplete: false, payload: null, rejectionReason: "多 JSON" };
  }

  if (hasLeadingText || hasTrailingText) {
    return { hasHeader: true, valid: false, isIncomplete: false, payload: null, rejectionReason: "格式错误" };
  }

  if (objects.length === 0) {
    if (jsonIncomplete) {
      return { hasHeader: true, valid: false, isIncomplete: true, payload: null, rejectionReason: null };
    }
    return { hasHeader: true, valid: false, isIncomplete: false, payload: null, rejectionReason: "格式错误" };
  }

  if (jsonIncomplete) {
    return { hasHeader: true, valid: false, isIncomplete: true, payload: null, rejectionReason: null };
  }

  try {
    const parsed = safeParseJson(objects[0]);
    const thetaMsg = parsed?.theta_codeai_message;
    if (!thetaMsg || typeof thetaMsg !== "object" || !thetaMsg.message_id) {
      return { hasHeader: true, valid: false, isIncomplete: false, payload: null, rejectionReason: "格式错误" };
    }
    return {
      hasHeader: true,
      valid: true,
      isIncomplete: false,
      payload: thetaMsg,
      rejectionReason: null,
    };
  } catch {
    return { hasHeader: true, valid: false, isIncomplete: false, payload: null, rejectionReason: "格式错误" };
  }
}

function hasThetaHeader(message) {
  const inspection = inspectThetaMessage(message);
  return inspection.hasHeader;
}

function parseThetaMessage(message) {
  const inspection = inspectThetaMessage(message);
  return inspection.payload;
}

const inFlightRequests = new Set();

async function scanThetaCalls({ now = Date.now(), sendFn = send, messages = null, isGenerating = null } = {}) {
  if (!isQwen && !messages) return;
  if (isScanningTheta) return;
  isScanningTheta = true;

  try {
    const isGen = isGenerating !== null ? isGenerating : (typeof qwenGenerationIsVisible === "function" ? qwenGenerationIsVisible() : false);
    let activeMacroObserved = false;
    const targetMessages = messages || (typeof qwenAssistantMessages === "function" ? qwenAssistantMessages() : []);

    for (const message of targetMessages) {
      if (executedMessages.has(message)) continue;

      const evalResult = evaluateMessageFinalization(message, { now, isGenerating: isGen });
      const state = evalResult.state;

      if (!evalResult.canFinalize) {
        if (!isGen && evalResult.remainingMs > 0 && !state.timer) {
          state.timer = setTimeout(() => {
            state.timer = null;
            scanThetaCalls({ sendFn, messages, isGenerating });
          }, evalResult.remainingMs + 10);
        }
        if (message.textContent && message.textContent.includes(THETA_HEADER)) {
          activeMacroObserved = true;
          setActivity("正在接收 Theta 流式消息...");
        }
        continue;
      }

      const inspection = inspectThetaMessage(message, { isGenerating: false });
      if (!inspection.hasHeader && !inspection.rejectionReason) {
        // Message has no macro header yet (e.g. thinking area only); do NOT mark finalized
        continue;
      }

      activeMacroObserved = true;

      if (inspection.isIncomplete) {
        setActivity("正在接收 Theta 流式消息...");
        continue;
      }

      if (inspection.rejectionReason) {
        state.finalized = true;
        executedMessages.add(message);
        setActivity(`检测到宏但拒绝：${inspection.rejectionReason}（本条消息不会投递；修正格式后请让 AI 重新输出完整宏）`, { warning: true });
        continue;
      }

      const thetaMsg = inspection.payload;
      if (!thetaMsg || !thetaMsg.message_id) {
        state.finalized = true;
        executedMessages.add(message);
        setActivity("检测到宏但拒绝：缺少有效 message_id（本条消息不会投递）", { warning: true });
        continue;
      }

      if (handled.has(thetaMsg.message_id)) {
        state.finalized = true;
        executedMessages.add(message);
        setActivity(`检测到宏但拒绝：历史请求 (${thetaMsg.message_id})，未重复投递`, { warning: true });
        continue;
      }

      // In-flight guard: prevent duplicate concurrent posts for the same request
      if (inFlightRequests.has(thetaMsg.message_id)) {
        continue;
      }
      inFlightRequests.add(thetaMsg.message_id);
      handled.add(thetaMsg.message_id);
      state.finalized = true;
      executedMessages.add(message);

      setActivity("Theta 请求已提交 Runtime...");
      try {
        const resp = await sendFn({ type: "bridge-theta-post", payload: { theta_codeai_message: thetaMsg } });
        if (resp && resp.ok) {
          if (resp.status === "ALREADY_PROCESSED") {
            setActivity(`Theta 历史消息已幂等处理，未重复注入回执: ${thetaMsg.message_id}`);
            continue;
          }
          // 投递结果只进页面浮层（给人看），不再向聊天窗注入回执宏：
          // 回执会诱发 AI 寒暄轮（实测 Theta「回执已阅」白烧一轮），幂等由桥侧
          // ALREADY_PROCESSED + 信件 message_id 保证，不依赖回执。
          const sha16 = resp.sha256 ? String(resp.sha256).slice(0, 16) : "";
          setActivity(
            `Theta 信件已落盘: ${resp.filename || "成功"}${sha16 ? ` | SHA-256 前16位 ${sha16}` : ""}（收件方经投递链拉取，AI 无需等待确认、勿自行重发）`
          );
        } else {
          setActivity(`Theta 写入失败: ${resp?.error || "未知错误"}（信件未落盘，请重试）`, { warning: true });
        }
      } catch (e) {
        const reason = String(e.message || e);
        if (reason.includes("404")) {
          setConnection("控制链在线，工具可以执行 | VERSION_MISMATCH / RESTART_REQUIRED", "warning");
          setActivity("Theta 发信失败 (HTTP 404)：VERSION_MISMATCH / RESTART_REQUIRED");
        } else if (reason.includes("503") || reason.includes("PAUSED")) {
          setActivity("Theta 发信失败：控制链已暂停 (PAUSED)");
        } else {
          setActivity(`Theta 发信失败：${reason}`, { warning: true });
          // C：同上，让 Qwen 侧节点自己看到拒绝原因（不 await）
          if (isSelfCorrectableRejection(reason)) {
            injectOutboundFailureNotice(reason, thetaMsg.message_id);
          }
        }
      } finally {
        inFlightRequests.delete(thetaMsg.message_id);
      }
    }

    if (!activeMacroObserved && activityMessage === "尚无工具活动") {
      setActivity("Qwen 适配器已注入，未检测到新行动宏");
    }
  } finally {
    isScanningTheta = false;
  }
}

let isPullingTheta = false;
async function pollThetaIncoming() {
  if (!isQwen || isPullingTheta || killSwitchActive || qwenGenerationIsVisible()) return;
  const editor = qwenEditor();
  if (!editor || (editor.value || "").trim().length > 0) return;

  isPullingTheta = true;
  try {
    const resp = await send({ type: "bridge-theta-pull" });
    if (resp && resp.ok && resp.pending && resp.content) {
      const prompt = [
        `[#theta_meeting_notice:v1]`,
        `来源文件: ${resp.filename}`,
        `哈希校验: ${resp.sha256}`,
        `----------------------------------------`,
        resp.content,
        `----------------------------------------`,
        `请阅读上述来自会议的最新协作文件并自主处理。`
      ].join("\n");
      const ed = insertIntoQwenEditor(prompt);
      let ackSent = false;
      const onUserMessageSubmitted = async () => {
        if (!resp.delivery_id || ackSent) return true;
        const maxAckAttempts = 5;
        for (let attempt = 1; attempt <= maxAckAttempts; attempt++) {
          try {
            reportDeliveryStage(resp.sha256, `ack_attempt_${attempt}`);
            const ackResult = await send({ type: "bridge-theta-ack", delivery_id: resp.delivery_id });
            if (ackResult && ackResult.ok && ackResult.status === "ACKNOWLEDGED" && ackResult.delivery_id === resp.delivery_id) {
              ackSent = true;
              reportDeliveryStage(resp.sha256, "ack_confirmed");
              setActivity(`会议来信已完成 ACK 闭环: ${resp.filename}`);
              return true;
            }
            const errMsg = ackResult?.error || (typeof ackResult === "object" ? JSON.stringify(ackResult) : "未知错误");
            setActivity(`ACK 尝试 ${attempt}/${maxAckAttempts} 失败: ${errMsg}`);
          } catch (ackErr) {
            setActivity(`ACK 尝试 ${attempt}/${maxAckAttempts} 异常: ${ackErr.message}`);
          }
          if (attempt < maxAckAttempts) {
            await new Promise(r => setTimeout(r, attempt * 1000));
          }
        }
        setActivity(`NEEDS_HUMAN：消息已提交，但 ACK 耗尽重试未闭环: ${resp.filename}`);
        return false;
      };

      try {
        await submitQwenEditor(ed, resp.sha256, onUserMessageSubmitted);
        setActivity(`会议来信已投递 Qwen 并唤醒: ${resp.filename}`);
      } catch (wakeErr) {
        if (ackSent) {
          setActivity(`NEEDS_HUMAN：会议来信已提交但唤醒未确认: ${wakeErr.message}`);
        } else if (wakeErr.message && wakeErr.message.includes("CALLBACK_UNCONFIRMED")) {
          setActivity(`NEEDS_HUMAN：消息已提交，但 ACK 未闭环: ${resp.filename}`);
        } else {
          throw wakeErr;
        }
      }
    }
  } catch (e) {
    if (e.message && e.message.includes("404")) {
      setConnection("控制链在线，工具可以执行 | VERSION_MISMATCH / RESTART_REQUIRED", "warning");
      setActivity("Theta 轮询失败 (HTTP 404)：VERSION_MISMATCH / RESTART_REQUIRED");
    }
  } finally {
    isPullingTheta = false;
  }
}

// 白名单真源＝桥端运行态（GET /api/v1/status 的 codeai_allowed_recipients / codeai_allowed_types）。
// 协议文本在注入时**取活值**，不落硬编码副本 ⇒ 桥端或 policy.json 改白名单，协议自动跟随。
// 取不到活值时明确告知「以桥端拒绝回执为准」，绝不写一份可能过期的枚举。
async function fetchCodeAiWhitelist() {
  try {
    const data = await send({ type: "bridge-status" });
    if (!Array.isArray(data?.codeai_allowed_recipients) || !Array.isArray(data?.codeai_allowed_types)) return null;
    return {
      recipients: data.codeai_allowed_recipients.filter(Boolean),
      types: data.codeai_allowed_types.filter(Boolean),
      aliases: data.codeai_recipient_aliases || {},
    };
  } catch {
    return null;
  }
}

function codeaiWhitelistLines(whitelist) {
  if (!whitelist) {
    return [
      "合法 recipient / type 取值：本机桥此刻未应答，取不到活白名单。请勿凭记忆填写；若被拒，桥端拒绝回执会附上完整合法取值，照它改即可。",
    ];
  }
  const lines = [
    `合法 recipient 全集（只认这些，须逐字一致）：${whitelist.recipients.join(" / ")}。`,
    `type 规则（最省事的写法）：整行省略 type 字段即合法（桥端默认按 CUSTOM 处理）。若必须显式填写，只认以下大写枚举：${whitelist.types.join("/")}。小写或自造标签（如 ping、TEXT）会被桥端 400 拒收，整封信不落盘。`,
  ];
  const aliasPairs = Object.entries(whitelist.aliases)
    .filter(([from, to]) => from && to && from !== to)
    .map(([from, to]) => `${from}→${to}`);
  if (aliasPairs.length) lines.push(`常见别名（桥端自动归一）：${aliasPairs.join("；")}。`);
  return lines;
}

function collaborationProtocolBootstrap(requestId, whitelist) {
  return [
    `# PocketFleet 多AI协同交互规范`,
    `启动请求ID: ${requestId}`,
    ``,
    `回复格式要求：以 [Telegram]re:{someone} 或 [Telegram][mailto:{someone}] 为开头（指明单一收件人），随后换行写正文。`,
    `例如：`,
    `[Telegram]re:@SomeoneBot`,
    `协同任务已确认，执行进展如下……`,
    ``,
    `收到本说明后，请仅回复：发信规范已加载`,
  ].join("\n");
}

if (typeof chrome !== "undefined" && chrome.runtime?.onMessage) {
  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (retireIfSuperseded()) return false;
    if (message?.type === "bridge-content-status") {
      sendResponse({ ok: true, adapter: adapterProfile?.id || null, protocolVersion: CONTENT_PROTOCOL_VERSION });
      return false;
    }
    if (message?.type !== "bridge-start-collaboration-protocol") return false;
    if (sender.id !== chrome.runtime.id || !message.humanGesture) {
      sendResponse({ ok: false, error: "启动请求来源被拒绝。" });
      return false;
    }

    (async () => {
      if (killSwitchActive) throw new Error("执行总开关已暂停。");
      if (generationIsVisible() || (isQwen && qwenGenerationIsVisible())) {
        throw new Error("当前 AI 正在生成回复，请等待结束后重试。");
      }
      const requestId = `codeai-bootstrap-${crypto.randomUUID()}`;
      const whitelist = await fetchCodeAiWhitelist();
      const prompt = collaborationProtocolBootstrap(requestId, whitelist);
      if (isQwen) {
        const editor = insertIntoQwenEditor(prompt);
        await submitQwenEditor(editor, requestId);
      } else {
        const editor = insertIntoEditor(prompt);
        await submitEditor(editor, requestId, null, { protocolVersion: CONTENT_PROTOCOL_VERSION });
      }
      return { ok: true, requestId };
    })()
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, error: String(error.message || error) }));
    return true;
  });
}

function pageRouteKey(url = null) {
  try {
    const parsed = new URL(url || window.location.href);
    return `${parsed.origin}${parsed.pathname}${parsed.search}`;
  } catch {
    return String(url || "");
  }
}

// ── PocketFleet [电报] 出站直通门禁（图 3 标准头优先，兼容旧版格式）───
// 图 3 标准格式：以 [Telegram]re:{someone} 或 [Telegram][mailto:{someone}] 为开头
const SCHEME_POCKETFLEET_RE = /^[ \t]{0,3}(?:\*\*|__)?\[(?:telegram|电报)\](?:\*\*|__)?\s*(?:re\s*[:：]|\[mailto\s*[:：])/i;
// 兼容方案 1 标准头：[Telegram];收件人:[...];抄送:[...];Mode:[WaitReply|NoReply]
const SCHEME1_HEAD_RE = /^[ \t]{0,3}(?:\*\*|__)?\[(?:telegram|电报)\](?:\*\*|__)?\s*;\s*(?:收件人|To)\s*[:：]\s*\[/i;
const SCHEME1_ANYWHERE_RE = /(?:\[(?:telegram|电报)\]\s*;|;\s*(?:收件人|To)\s*[:：]\s*\[|(?:收件人|To)\s*[:：]\s*\[)/i;
// 兼容旧版：首行前3字符内的 [telegram]
const TELEGRAM_HEAD_RE = /^[ \t]{0,3}(?:\[(?:telegram|电报)\](?:\s*\[(?:noreply|免回)\])?|【电报】|\*\*\[(?:telegram|电报)\]\*\*)[ \t]*/i;

function findTelegramHeaderIndex(lines) {
  for (let i = 0; i < Math.min(lines.length, 10); i++) {
    const line = lines[i].trim();
    if (SCHEME_POCKETFLEET_RE.test(line) || SCHEME1_HEAD_RE.test(line) || SCHEME1_ANYWHERE_RE.test(line) || TELEGRAM_HEAD_RE.test(line)) {
      return i;
    }
  }
  return -1;
}

function isTelegramOutbound(raw) {
  if (!raw) return false;
  const lines = raw.split(/\r?\n/);
  return findTelegramHeaderIndex(lines) !== -1;
}

function extractTelegramText(raw) {
  if (!raw) return null;
  const sanitized = raw.replace(/<think>[\s\S]*?<\/think>/gi, "").trim();
  const lines = sanitized.split(/\r?\n/);
  const idx = findTelegramHeaderIndex(lines);
  if (idx === -1) return null;

  const headerLine = lines[idx].trim();
  const restLines = lines.slice(idx + 1);

  // 1. 图 3 标准格式：[Telegram]re:... 或 [Telegram][mailto:...]，绝对保留完整头部行
  if (SCHEME_POCKETFLEET_RE.test(headerLine)) {
    return [headerLine, ...restLines].join("\n").trim() || null;
  }

  // 2. 方案 1 标准头：保留完整标准头行
  if (SCHEME1_HEAD_RE.test(headerLine) || SCHEME1_ANYWHERE_RE.test(headerLine)) {
    let normalized = headerLine;
    if (!/^\s*\[(?:telegram|电报)\]/i.test(normalized)) {
      normalized = `[Telegram]${normalized.startsWith(";") ? "" : ";"}${normalized}`;
    }
    return [normalized, ...restLines].join("\n").trim() || null;
  }

  // 3. 仅对旧格式（简易出站 [telegram]@目标人）剥离前置 [telegram] 门禁
  const match = TELEGRAM_HEAD_RE.exec(headerLine);
  if (!match) return null;
  const firstLineTail = headerLine.slice(match[0].length);
  return [firstLineTail, ...restLines].join("\n").trim() || null;
}

const telegramHandled = new Set();
const telegramInFlight = new Set();
const telegramExecutedElements = new WeakSet();

function persistTelegramHandledHash(hash) {
  if (!hash) return;
  telegramHandled.add(hash);
  try {
    if (typeof chrome !== "undefined" && chrome.storage?.local?.get) {
      chrome.storage.local.get(["telegram_handled_hashes"], (res) => {
        const list = Array.isArray(res?.telegram_handled_hashes) ? res.telegram_handled_hashes : [];
        if (!list.includes(hash)) {
          list.push(hash);
          if (list.length > 500) list.splice(0, list.length - 500);
          chrome.storage.local.set({ telegram_handled_hashes: list });
        }
      });
    }
  } catch {}
}

// 页面启动时恢复历史已发送哈希，确保刷新页面时绝不重复发送历史公文
try {
  if (typeof chrome !== "undefined" && chrome.storage?.local?.get) {
    chrome.storage.local.get(["telegram_handled_hashes"], (res) => {
      const list = res?.telegram_handled_hashes;
      if (Array.isArray(list)) {
        for (const h of list) telegramHandled.add(h);
      }
    });
  }
} catch {}

function isGenerationActiveNow() {
  if (generationIsVisible()) return true;
  if (isQwen && typeof qwenGenerationIsVisible === "function" && qwenGenerationIsVisible()) return true;
  if (isDoubao) {
    // 豆包严格流式中判定：只认明确的 true / 1，绝不可写裸 [data-streaming]（结束时属性为 data-streaming="false"）
    const streamingEl = document.querySelector("[data-streaming='true'], [data-streaming='1'], .md-box-root[data-streaming='true']");
    if (streamingEl) return true;
  }
  // 全局可见的停止按钮（严格可见性判定，避免被隐藏 DOM 误判）
  const stopCandidates = document.querySelectorAll("button[data-testid*='stop'], button[aria-label*='停止生成'], button[aria-label*='Stop generating'], button[aria-label*='Stop response']");
  for (const btn of stopCandidates) {
    if (btn.getClientRects().length > 0 && !btn.disabled) {
      return true;
    }
  }
  return false;
}

async function scanTelegramCalls() {
  if (routeBaselinePending || killSwitchActive) return;

  const assistantList = messagesByRole("assistant");
  if (!assistantList || assistantList.length === 0) return;

  // 铁律 1（防重放）：出站消息在逻辑上只能是整个页面当前最新的一条助手消息！绝不回溯扫描历史旧消息！
  const targetMessage = assistantList[assistantList.length - 1];
  if (telegramExecutedElements.has(targetMessage)) return;
  if (targetMessage.closest && targetMessage.closest(".ds-think-content, [class*='think-content'], [class*='think'], [class*='thought'], [class*='reasoning'], details, summary")) return;
  if (isThinkingElement(targetMessage)) return;

  let text = "";
  if (targetMessage.cloneNode) {
    const clone = targetMessage.cloneNode(true);
    clone.querySelectorAll(
      ".sr-only, [class*='sr-only'], [role='toolbar'], [data-testid*='reaction'], button, " +
      ".ds-think-content, [class*='think-content'], [class*='thinking'], [class*='think'], [class*='thought'], [class*='reasoning'], details, summary"
    ).forEach((el) => el.remove());
    text = (clone.textContent || "").trim();
  } else {
    text = (targetMessage.textContent || "").trim();
  }
  if (!isTelegramOutbound(text)) return;
  const targetRaw = text;

  // 铁律 2：严格判定生成态与稳定窗（防流式抽样截断倒灌）
  if (isGenerationActiveNow()) {
    setTimeout(scanTelegramCalls, 1500);
    return;
  }

  // 稳定性追踪：以 cleanText 签名判断 1000ms 内是否无新内容追加
  const cleanText = extractTelegramText(targetRaw);
  if (!cleanText) return;

  const state = getOrCreateMessageState(targetMessage, Date.now());
  if (Date.now() - state.lastChangedAt < 1000) {
    setTimeout(scanTelegramCalls, 1000);
    return;
  }

  // 铁律 3：计算 SHA-256 并严格比对持久化已发哈希池
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(cleanText));
  const key = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");

  // 只要内存已标记、或持久化已发池已记录、或正在飞行中，绝对物理锁死，严禁重复出站！
  if (telegramExecutedElements.has(targetMessage) || telegramHandled.has(key) || telegramInFlight.has(key)) {
    telegramExecutedElements.add(targetMessage);
    return;
  }

  // 锁定该 DOM 元素与内容哈希
  telegramExecutedElements.add(targetMessage);
  telegramInFlight.add(key);

  const sender = adapterProfile?.id === "chatgpt-folded-host" ? "chatgpt" :
                 adapterProfile?.id === "claude-web" ? "claude" :
                 adapterProfile?.id === "gemini-earth-sandbox" ? "gemini" :
                 adapterProfile?.id === "deepseek-remainder" ? "deepseek" :
                 adapterProfile?.id === "copilot-grayfold" ? "copilot" :
                 adapterProfile?.id === "doubao-heart" ? "doubao" :
                 adapterProfile?.id === "qwen-theta" ? "qwen" :
                 adapterProfile?.id === "glm-xingtu" ? "glm" :
                 adapterProfile?.id === "kimi-motin" ? "kimi" : "chatgpt";

  try {
    const resp = await send({
      type: "bridge-telegram-post",
      payload: { text: cleanText, sender: sender, source_key: pageRouteKey() },
    });
    if (!resp?.ok) throw new Error(resp?.error || "Bridge 未确认投递");
    persistTelegramHandledHash(key);
    setActivity(`[电报] 出站已成功排入待发队列（完整报文，单次投递）！`);
  } catch (error) {
    // 若投递失败，解除元素锁以便下轮重试
    telegramExecutedElements.delete(targetMessage);
    setActivity(`[电报] 发送失败：${error.message || error}`, { warning: true });
  } finally {
    telegramInFlight.delete(key);
  }
}

function runPageScan() {
  pageScanTimer = null;
  if (retireIfSuperseded() || routeBaselinePending) return;
  panel();
  scanCalls();
  scanCodeAiCalls();
  scanOpenClawTasks();
  scanTelegramCalls();
  if (isQwen) scanThetaCalls();
}

function schedulePageScan(delayMs = PAGE_SCAN_DEBOUNCE_MS) {
  if (pageScanTimer) clearTimeout(pageScanTimer);
  pageScanTimer = setTimeout(runPageScan, delayMs);
}

function scheduleRouteBaseline() {
  routeBaselinePending = true;
  if (routeBaselineTimer) return; // 关键：已在倒计时中，严禁反复重置饿死！
  routeBaselineTimer = setTimeout(() => {
    routeBaselineTimer = null;
    if (retireIfSuperseded()) return;
    // Hydrated history in a destination conversation is never a new tool call.
    markExistingCallsHandled(null, { recoverLatestDoubao: false });
    routeBaselinePending = false;
    schedulePageScan(0);
  }, ROUTE_BASELINE_QUIET_MS);
}

function syncPageRoute() {
  const nextRoute = pageRouteKey();
  if (nextRoute === activePageRoute) return false;
  activePageRoute = nextRoute;
  scheduleRouteBaseline();
  return true;
}

function handlePageMutation() {
  if (retireIfSuperseded()) return;
  if (syncPageRoute()) {
    panel();
    return;
  }
  if (routeBaselinePending) {
    return; // 关键：静默期内不响应微小 DOM 扰动，让倒计时按时完成
  }
  schedulePageScan();
}

if (typeof window !== "undefined" && typeof document !== "undefined") {
  activePageRoute = pageRouteKey();
  // 必须立即进入路由基线锁定！SPA 异步拉取历史消息水合期间，绝对禁止任何扫描出站！
  routeBaselinePending = true;
  scheduleRouteBaseline();
  pageObserver = new MutationObserver(handlePageMutation);
  pageObserver.observe(document.documentElement, { childList: true, subtree: true });
  panel();
  refreshStatus();
  const schedule = (callback, intervalMs) => managedIntervals.push(setInterval(() => {
    if (retireIfSuperseded()) return;
    callback();
  }, intervalMs));
  schedule(refreshStatus, 5000);
  schedule(runPageScan, 3000);
  if (!isQwen) schedule(pollCodeAiIncoming, adapterProfile?.pollingMs || 60000);
  if (isQwen) schedule(pollThetaIncoming, adapterProfile?.pollingMs || 60000);
  visibilityHandler = () => {
    if (!document.hidden && !retireIfSuperseded()) refreshStatus();
  };
  document.addEventListener("visibilitychange", visibilityHandler);
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    isThinkingElement,
    isElementVisible,
    findForbiddenAncestor,
    BLOCK_TAGS,
    getEnclosingBlock,
    collectVisibleNonThinkingTextNodes,
    evaluateSubmitConfirmation,
    isSelfCorrectableRejection,
    LOCAL_NOTICE_PREFIX,
    safeParseJson,
    normalizeStructuralJsonQuotes,
    normalizeJsonTypography,
    inspectJsonObjects,
    inspectThetaMessage,
    hasThetaHeader,
    parseThetaMessage,
    extractJsonObjects,
    scanThetaCalls,
    markExistingCallsHandled,
    qwenAssistantMessages,
    evaluateMessageFinalization,
    getOrCreateMessageState,
    messageStates,
    handled,
    executedMessages,
    inFlightRequests,
    THETA_HEADER,
    STABILITY_WINDOW_MS,
    PAGE_SCAN_DEBOUNCE_MS,
    ROUTE_BASELINE_QUIET_MS,
    pageRouteKey,
  };
}
})();
