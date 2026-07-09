import { apiFetch, getJson } from "./api.js";
import { dom } from "./dom.js";

const STORAGE_KEY = "ttsworker.modalToken.v1";
const DEFAULT_TOKEN_SETTINGS = { modal_profile: "" };

let currentTokenSettings = normalizeTokenSettings(loadStoredSettings());
let tokenJobId = null;
let tokenPollTimer = null;

function normalizeTokenSettings(raw = {}) {
  const merged = { ...DEFAULT_TOKEN_SETTINGS, ...(raw || {}) };
  return { modal_profile: String(merged.modal_profile || "").trim() };
}

function loadStoredSettings() {
  try {
    const raw = globalThis.localStorage?.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function saveStoredSettings() {
  try {
    globalThis.localStorage?.setItem(STORAGE_KEY, JSON.stringify(currentTokenSettings));
  } catch {
    // Ignore storage failures.
  }
}

function applyTokenSettingsToDom() {
  dom.modalProfileInput.value = currentTokenSettings.modal_profile;
}

function syncTokenSettingsFromDom() {
  currentTokenSettings = normalizeTokenSettings({ modal_profile: dom.modalProfileInput.value });
  applyTokenSettingsToDom();
  saveStoredSettings();
}

function setTokenStatus(message, tone = "") {
  dom.modalTokenStatus.className = tone ? `status-console ${tone}` : "status-console";
  dom.modalTokenStatus.textContent = message;
}

function setTokenButtonDisabled(disabled) {
  dom.modalTokenButton.disabled = disabled;
  dom.modalTokenButton.textContent = disabled ? "Switching..." : "Create Modal token";
}

function stopTokenPolling() {
  if (tokenPollTimer) {
    clearInterval(tokenPollTimer);
    tokenPollTimer = null;
  }
}

function renderTokenJob(job) {
  const lines = [job.message || "Token flow running."];
  if (job.profile) lines.push(`Profile: ${job.profile}`);
  const logs = Array.isArray(job.logs) ? job.logs.slice(-5) : [];
  if (logs.length) lines.push("", ...logs);
  const tone = job.status === "success" ? "success" : job.status === "failed" ? "failure" : job.status === "running" ? "running" : "";
  setTokenStatus(lines.join("\n"), tone);
  setTokenButtonDisabled(job.status === "running" || job.status === "pending");
  if (job.status === "success" || job.status === "failed") stopTokenPolling();
}

async function refreshTokenStatus() {
  if (!tokenJobId) return;
  const job = await getJson(`/modal/token/${tokenJobId}`);
  renderTokenJob(job);
}

async function startTokenSwitch() {
  const profile = currentTokenSettings.modal_profile || dom.modalProfileInput.value || "";
  if (!profile) {
    setTokenStatus("Enter a Modal profile first.", "failure");
    return;
  }
  setTokenButtonDisabled(true);
  setTokenStatus(`Starting Modal token flow for ${profile}...`, "running");
  const response = await apiFetch("/modal/token/new", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ profile, activate: true, verify: true })
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || "Failed to start Modal token flow");
  }
  const job = await response.json();
  tokenJobId = job.job_id;
  renderTokenJob(job);
  stopTokenPolling();
  tokenPollTimer = setInterval(async () => {
    try {
      await refreshTokenStatus();
    } catch (error) {
      setTokenStatus(`Token status error: ${error.message}`, "failure");
    }
  }, 2000);
  await refreshTokenStatus();
}

export function initModalTokenPanel() {
  applyTokenSettingsToDom();
  dom.modalProfileInput.addEventListener("input", syncTokenSettingsFromDom);
  dom.modalProfileInput.addEventListener("change", syncTokenSettingsFromDom);
  setTokenStatus("Ready to switch token.");
  dom.modalTokenButton.addEventListener("click", async () => {
    try {
      await startTokenSwitch();
    } catch (error) {
      setTokenStatus(`Token flow error: ${error.message}`, "failure");
      setTokenButtonDisabled(false);
      stopTokenPolling();
    }
  });
}
