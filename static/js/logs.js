import { apiFetch } from "./api.js";
import { dom } from "./dom.js";
import { state } from "./state.js";

export function addLog(message, type = "info", preserveLine = false) {
  const line = preserveLine ? String(message) : `[${new Date().toLocaleTimeString()}] ${message}`;
  const logLine = document.createElement("div");
  logLine.className = `log-line ${type}`;
  logLine.textContent = line;
  dom.logOutput.appendChild(logLine);
  dom.logOutput.scrollTop = dom.logOutput.scrollHeight;
}

export function clearLogs() {
  dom.logOutput.innerHTML = '<div class="log-line info">Job logs will appear here.</div>';
  state.seenRemoteLogCount = 0;
}

export async function fetchRemoteLogs() {
  if (!state.currentJobId) return;
  try {
    const res = await apiFetch(`/job/${state.currentJobId}/logs`);
    if (!res.ok) return;
    const text = await res.text();
    const lines = text.split(/\r?\n/).filter((line) => line.trim());
    const freshLines = lines.slice(state.seenRemoteLogCount);
    freshLines.forEach((line) => addLog(line, "info", true));
    state.seenRemoteLogCount = lines.length;
  } catch {
    // Logging must stay resilient.
  }
}
