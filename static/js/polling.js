import { apiFetch } from "./api.js";
import { loadAudioPreview } from "./audio.js";
import { loadScript, loadTranscript } from "./approvals.js";
import { POLL_INTERVAL_MS } from "./config.js";
import { dom } from "./dom.js";
import { addLog, fetchRemoteLogs } from "./logs.js";
import { renderPreviewSummary, renderSegmentProgress, renderStepProgress, updateResultLink, updateStatusBadge } from "./preview.js";
import { state, clearPollInterval } from "./state.js";
import { openRelevantTab } from "./tabs.js";

export async function updateJobStatus() {
  if (!state.currentJobId) return;
  try {
    const res = await apiFetch(`/job/${state.currentJobId}`);
    if (!res.ok) throw new Error("Failed to fetch job status");
    const status = await res.json();
    dom.jobMessageEl.textContent = status.message || "Processing";
    updateStatusBadge(status.status || "pending");
    renderPreviewSummary(status);
    renderStepProgress(status.steps);
    renderSegmentProgress(status.segments);
    updateResultLink(status);
    if (status.transcript_data !== null && status.transcript_data !== undefined) await loadTranscript();
    if (status.script_data !== null && status.script_data !== undefined) await loadScript();
    if (status.audio_preview) await loadAudioPreview();
    openRelevantTab(status);
    if (status.status === "completed" || status.status === "failed") {
      clearPollInterval();
      addLog(`Job ${status.status.toUpperCase()}.`, status.status === "completed" ? "success" : "error");
    }
  } catch (error) {
    addLog(`Status polling error: ${error.message}`, "error");
  }
}

export function startPolling() {
  clearPollInterval();
  state.pollInterval = setInterval(() => {
    updateJobStatus();
    fetchRemoteLogs();
  }, POLL_INTERVAL_MS);
}
