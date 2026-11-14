const AUTH_KEY = "mindbridge.auth";
const APP_BASE = window.location.pathname.startsWith("/mindbridge") ? "/mindbridge" : "";

const els = {
  serviceState: document.querySelector("#serviceState"),
  demoForm: document.querySelector("#demoForm"),
  accessCode: document.querySelector("#accessCode"),
  demoState: document.querySelector("#demoState")
};

function appPath(path = "/") {
  return `${APP_BASE}${path}`;
}

function setPill(el, text, tone = "ok") {
  if (!el) return;
  el.textContent = text;
  el.className = `pill ${tone}`;
}

async function api(path, options = {}) {
  const response = await fetch(appPath(path), options);
  if (!response.ok) {
    let message = await response.text();
    try {
      message = JSON.parse(message).detail || message;
    } catch {}
    throw new Error(message || `${response.status} ${response.statusText}`);
  }
  return response;
}

function saveAuth(token, scheme, profile) {
  sessionStorage.setItem(AUTH_KEY, JSON.stringify({
    token,
    scheme,
    username: profile.username,
    displayName: profile.displayName,
    roles: profile.roles || []
  }));
}

function readAuth() {
  try {
    return JSON.parse(sessionStorage.getItem(AUTH_KEY) || "null");
  } catch {
    return null;
  }
}

function routeProfile() {
  window.location.assign(appPath("/student.html"));
}

async function profileFor(token, scheme) {
  const response = await api("/api/profile", {
    headers: { Authorization: `${scheme} ${token}` }
  });
  return response.json();
}

async function checkHealth() {
  try {
    const response = await api("/actuator/health");
    const body = await response.json();
    setPill(els.serviceState, body.status === "UP" ? "服务正常" : `服务 ${body.status}`, body.status === "UP" ? "ok" : "danger");
  } catch {
    setPill(els.serviceState, "服务 DOWN", "danger");
  }
}

async function resumeExistingLogin() {
  const auth = readAuth();
  if (!auth?.token) return;
  try {
    const profile = await profileFor(auth.token, auth.scheme || "Basic");
    saveAuth(auth.token, auth.scheme || "Basic", profile);
    routeProfile(profile);
  } catch {
    sessionStorage.removeItem(AUTH_KEY);
  }
}

async function openDemo(event) {
  event.preventDefault();
  const accessCode = els.accessCode.value.trim();
  els.demoState.textContent = "正在创建安全体验会话...";
  try {
    const response = await api("/api/demo/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accessCode })
    });
    const session = await response.json();
    const profile = await profileFor(session.token, session.tokenType);
    saveAuth(session.token, session.tokenType, profile);
    els.demoState.textContent = "验证成功，正在进入学生工作台";
    routeProfile(profile);
  } catch (error) {
    sessionStorage.removeItem(AUTH_KEY);
    els.demoState.textContent = `无法进入：${error.message}`;
  }
}

els.demoForm.addEventListener("submit", openDemo);
checkHealth();
resumeExistingLogin();
