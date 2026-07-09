import { apiFetch, getJson } from "./api.js";
import { dom } from "./dom.js";

const STORAGE_KEY = "ttsworker.modalDeploy.v1";
const DEFAULT_DEPLOY_SETTINGS = {
  modal_app_name: "tooltucode-gpu-v1",
  modal_deploy_target: "step_3"
};

let currentDeploySettings = normalizeDeploySettings(loadStoredSettings());
let deployJobId = null;
let deployPollTimer = null;

function normalizeDeploySettings(raw = {}) {
  const merged = { ...DEFAULT_DEPLOY_SETTINGS, ...(raw || {}) };
  return {
    modal_app_name: String(merged.modal_app_name || DEFAULT_DEPLOY_SETTINGS.modal_app_name).trim() || DEFAULT_DEPLOY_SETTINGS.modal_app_name,
    modal_deploy_target: "step_3"
  };
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
    globalThis.localStorage?.setItem(STORAGE_KEY, JSON.stringify(currentDeploySettings));
  } catch {
    // Ignore localStorage failures.
  }
}

function applyDeploySettingsToDom() {
  dom.deployModalAppNameInput.value = currentDeploySettings.modal_app_name;
  dom.deployModalDeployTargetSelect.value = "step_3";
  dom.deploySummary.textContent = `${currentDeploySettings.modal_app_name}-step3`;
}

function syncDeploySettingsFromDom() {
  currentDeploySettings = normalizeDeploySettings({
    modal_app_name: dom.deployModalAppNameInput.value,
    modal_deploy_target: "step_3"
  });
  applyDeploySettingsToDom();
  saveStoredSettings();
}

function setDeployStatus(message, tone = "") {
  dom.modalDeployStatus.className = tone ? `status-console ${tone}` : "status-console";
  dom.modalDeployStatus.textContent = message;
}

function setDeployButtonDisabled(disabled) {
  dom.modalDeployButton.disabled = disabled;
  dom.modalDeployButton.textContent = disabled ? "Deploying..." : "Deploy step 3";
}

function stopDeployPolling() {
  if (deployPollTimer) {
    clearInterval(deployPollTimer);
    deployPollTimer = null;
  }
}

function renderDeployJob(job) {
  const lines = [job.message || "Deployment running."];
  if (job.app_name) lines.push(`App: ${job.app_name}`);
  const logs = Array.isArray(job.logs) ? job.logs.slice(-5) : [];
  if (logs.length) lines.push("", ...logs);
  const tone = job.status === "success" ? "success" : job.status === "failed" ? "failure" : job.status === "running" ? "running" : "";
  setDeployStatus(lines.join("\n"), tone);
  setDeployButtonDisabled(job.status === "running" || job.status === "pending");
  if (job.status === "success" || job.status === "failed") stopDeployPolling();
}

async function refreshDeployStatus() {
  if (!deployJobId) return;
  const job = await getJson(`/modal/deploy/${deployJobId}`);
  renderDeployJob(job);
}

async function startDeploy() {
  setDeployButtonDisabled(true);
  setDeployStatus("Starting Modal deploy for step_3...", "running");
  const response = await apiFetch("/modal/deploy", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      target: "step_3",
      base_app_name: currentDeploySettings.modal_app_name,
      strategy: "rolling"
    })
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || "Failed to start Modal deploy");
  }
  const job = await response.json();
  deployJobId = job.job_id;
  renderDeployJob(job);
  stopDeployPolling();
  deployPollTimer = setInterval(async () => {
    try {
      await refreshDeployStatus();
    } catch (error) {
      setDeployStatus(`Deployment status error: ${error.message}`, "failure");
    }
  }, 2000);
  await refreshDeployStatus();
}

export function initModalDeployPanel() {
  applyDeploySettingsToDom();
  dom.deployModalAppNameInput.addEventListener("input", syncDeploySettingsFromDom);
  dom.deployModalAppNameInput.addEventListener("change", syncDeploySettingsFromDom);
  dom.deployToggle.addEventListener("click", () => {
    dom.deployPanel.hidden = !dom.deployPanel.hidden;
  });
  setDeployStatus("Ready to deploy.");
  dom.modalDeployButton.addEventListener("click", async () => {
    try {
      await startDeploy();
    } catch (error) {
      setDeployStatus(`Deployment error: ${error.message}`, "failure");
      setDeployButtonDisabled(false);
      stopDeployPolling();
    }
  });
}
