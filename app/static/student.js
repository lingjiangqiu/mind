const AUTH_KEY = "mindbridge.auth";
const APP_BASE = window.location.pathname.startsWith("/mindbridge") ? "/mindbridge" : "";
const state = { busy: false, sessionId: null, turns: 0, maxTurns: 10, timer: null };
const $ = (id) => document.getElementById(id);
const appPath = (path) => `${APP_BASE}${path}`;
function auth() { try { return JSON.parse(sessionStorage.getItem(AUTH_KEY) || "null"); } catch { return null; } }
function leave() { sessionStorage.removeItem(AUTH_KEY); window.location.replace(appPath("/")); }
async function api(path, options = {}) {
  const saved = auth();
  if (!saved?.token) { leave(); throw new Error("请先进入体验。"); }
  const response = await fetch(appPath(path), { ...options, headers: { ...(options.headers || {}), Authorization: `${saved.scheme || "Bearer"} ${saved.token}` } });
  if (response.status === 401) { leave(); throw new Error("体验已到期，请重新进入。"); }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : "这次请求没有完成，请稍后重试。");
  }
  return response;
}
function message(role, text) {
  const article = document.createElement("article");
  article.className = `chat-message ${role}`;
  const label = document.createElement("span"); label.className = "speaker"; label.textContent = role === "user" ? "你" : "校园心理助手";
  const content = document.createElement("div"); content.className = "message-text"; content.textContent = text;
  article.append(label, content); $("chatMessages").append(article);
  return { article, content };
}
function showTrace(trace) {
  const descriptions = { model: "本轮由 AI 生成", fallback: "本轮使用预设安全回应", safety: "本轮优先现实安全支持" };
  $("traceMeta").textContent = `${descriptions[trace.source] || "处理完成"} · ${(trace.elapsedMs / 1000).toFixed(1)} 秒`;
  const rows = trace.steps.map((step) => {
    const li = document.createElement("li"); const title = document.createElement("strong"); const detail = document.createElement("p");
    title.textContent = step.label; detail.textContent = step.detail; li.append(title, detail); return li;
  });
  $("traceSteps").replaceChildren(...rows);
  if (trace.lens) { const li = document.createElement("li"); li.className = "trace-lens"; li.textContent = `本轮使用的表达视角：${trace.lens}`; $("traceSteps").append(li); }
  document.querySelector(".trace-panel").open = true;
}
function setBusy(busy) {
  state.busy = busy;
  $("sendChat").disabled = busy || state.turns >= state.maxTurns;
  $("resetChat").disabled = busy;
  $("chatInput").disabled = busy;
  document.querySelectorAll("[data-example]").forEach((button) => { button.disabled = busy; });
}
function updateTurns() { $("turnState").textContent = `已用 ${state.turns} / ${state.maxTurns} 轮 · 会话到期即结束`; }
function acceptEvent(raw, reply) {
  const lines = raw.split("\n").filter((line) => line.startsWith("data:"));
  if (!lines.length) return;
  const data = JSON.parse(lines.map((line) => line.slice(5).trimStart()).join("\n"));
  if (data.type === "meta") {
    state.sessionId = data.sessionId; state.turns = data.turnsUsed; state.maxTurns = data.maxTurns; updateTurns();
  } else if (data.type === "status") { $("chatState").textContent = data.message;
  } else if (data.type === "trace") {
    reply.trace = data; showTrace(data);
  } else if (data.type === "token") {
    if (!reply.started) { reply.content.textContent = ""; reply.started = true; }
    reply.content.textContent += data.content;
  } else if (data.type === "done") {
    reply.done = true;
    const button = document.createElement("button"); button.type = "button"; button.className = "trace-trigger"; button.textContent = "查看这轮过程";
    button.addEventListener("click", () => { showTrace(reply.trace); $("traceMeta").scrollIntoView({ behavior: "smooth", block: "nearest" }); });
    reply.article.append(button);
    $("chatState").textContent = data.source === "fallback" ? "本轮采用预设安全回应。可以继续说说你的具体情况。" : data.source === "safety" ? "请优先联系现实中的支持。这里没有真人跟进。" : "回应完成。可以接着聊。";
  } else if (data.type === "error") { throw new Error(data.message); }
}
async function send(event) {
  event.preventDefault();
  if (state.busy || state.turns >= state.maxTurns) return;
  const text = $("chatInput").value.trim(); if (!text) return;
  message("user", text); const reply = message("assistant", "正在听你说…");
  $("chatInput").value = ""; setBusy(true); $("chatState").textContent = "正在准备回应，可能需要几十秒…";
  const abort = new AbortController(); const timeout = setTimeout(() => abort.abort(), 130000);
  try {
    const response = await api("/api/chat/stream", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: text, sessionId: state.sessionId }), signal: abort.signal });
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = "";
    while (true) {
      const part = await reader.read(); if (part.done) break;
      buffer += decoder.decode(part.value, { stream: true }).replace(/\r\n/g, "\n");
      let end;
      while ((end = buffer.indexOf("\n\n")) !== -1) { const raw = buffer.slice(0, end); buffer = buffer.slice(end + 2); acceptEvent(raw, reply); }
    }
    if (!reply.done) throw new Error("连接中断了，这轮回应没有完成。可以稍后重试。");
  } catch (error) {
    const text = error.name === "AbortError" ? "等待时间较长，这次连接已结束，请稍后再试。" : error.message;
    if (!reply.started) reply.content.textContent = text;
    $("chatState").textContent = text;
  } finally { clearTimeout(timeout); setBusy(false); if (state.turns < state.maxTurns) $("chatInput").focus(); }
}
async function reset() {
  if (state.busy) return;
  try {
    const body = await (await api("/api/demo/conversation", { method: "DELETE" })).json();
    state.sessionId = null; state.turns = body.turnsUsed; $("chatMessages").replaceChildren();
    message("assistant", "这段对话已清除。我们可以从另一件事开始聊。");
    $("traceSteps").replaceChildren(); $("traceMeta").textContent = "等待新的回应";
    $("chatState").textContent = "已清除临时上下文；体验次数和到期时间保持原样。"; updateTurns(); setBusy(false);
  } catch (error) { $("chatState").textContent = error.message; }
}
async function init() {
  try {
    await api("/api/profile");
    const info = await (await api("/api/agent/status")).json();
    $("modelState").textContent = info.realModelEnabled && info.modelConfigured ? "AI 已配置" : "预设回应模式";
    $("modelState").className = `pill ${info.realModelEnabled && info.modelConfigured ? "ok" : "warn"}`;
    state.maxTurns = info.publicDemo.maxTurns;
    const token = auth()?.token;
    if (token) {
      const base64 = token.split(".")[0].replace(/-/g, "+").replace(/_/g, "/");
      const payload = JSON.parse(atob(base64.padEnd(Math.ceil(base64.length / 4) * 4, "=")));
      const expiresIn = payload.exp * 1000 - Date.now();
      state.timer = setTimeout(() => { $("chatMessages").replaceChildren(); $("traceSteps").replaceChildren(); leave(); }, Math.max(0, expiresIn));
    }
  } catch (error) { $("chatState").textContent = error.message; }
}
$("chatForm").addEventListener("submit", send);
$("resetChat").addEventListener("click", reset);
$("switchAccount").addEventListener("click", leave);
document.querySelectorAll("[data-example]").forEach((button) => button.addEventListener("click", () => { $("chatInput").value = button.dataset.example; $("chatInput").focus(); }));
init();
