import { apiFetch, getJson } from "./api.js";
import { getAppConfig } from "./config.js";
import { getCurrentDeploySettings } from "./deploy.js";
import { dom } from "./dom.js";

let tokenJobId = null;
let tokenPollTimer = null;

function setTokenStatus(message, tone = "") {
  dom.modalTokenStatus.className = tone ? `status-console ${tone}` : "status-console";
  dom.modalTokenStatus.textContent = message;
}

function setTokenButtonDisabled(disabled) {
  dom.modalTokenButton.disabled = disabled;
  dom.modalTokenButton.textContent = disabled ? "Linking..." : "Link Modal account";
}

function stopTokenPolling() {
  if (tokenPollTimer) {
    clearInterval(tokenPollTimer);
    tokenPollTimer = null;
  }
}

function renderTokenJob(job) {
  const lines = [job.message || "Modal account link flow running."];
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

async function startTokenLink() {
  const profile = getCurrentDeploySettings().modal_profile_key;
  if (!profile) {
    setTokenStatus("Select a Modal profile first.", "failure");
    return;
  }
  setTokenButtonDisabled(true);
  setTokenStatus(`Starting Modal account link flow for ${profile}...`, "running");
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

export async function initModalTokenPanel() {
  const appConfig = await getAppConfig();
  if (!appConfig.modal_token_linking_enabled) {
    dom.modalTokenButton.hidden = true;
    setTokenStatus("Production uses configured Modal secrets.");
    return;
  }
  setTokenStatus("Link flow idle.");
  dom.modalTokenButton.addEventListener("click", async () => {
    try {
      await startTokenLink();
    } catch (error) {
      setTokenStatus(`Token flow error: ${error.message}`, "failure");
      setTokenButtonDisabled(false);
      stopTokenPolling();
    }
  });
}
