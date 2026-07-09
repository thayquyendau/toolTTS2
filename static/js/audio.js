import { apiFetch, apiUrl, postJson } from "./api.js";
import { dom } from "./dom.js";
import { addLog } from "./logs.js";
import { state } from "./state.js";

export async function loadAudioPreview() {
  if (!state.currentJobId) return;
  try {
    const res = await apiFetch(`/job/${state.currentJobId}/audio`);
    if (!res.ok) return;
    const data = await res.json();
    const preview = data.audio_preview;
    if (!preview || !preview.url) return;
    dom.audioEmpty.hidden = true;
    dom.audioPanel.hidden = false;
    dom.audioPlayer.src = `${apiUrl(preview.url)}?t=${Date.now()}`;
    dom.audioStatus.textContent = data.approved ? "Approved" : "Preview ready";
    dom.btnAudioApprove.hidden = Boolean(data.approved);
  } catch {
    // Keep polling resilient.
  }
}

export async function approveAudioPreview() {
  if (!state.currentJobId) return;
  try {
    await postJson(`/job/${state.currentJobId}/audio/approve`);
    dom.btnAudioApprove.hidden = true;
    dom.audioStatus.textContent = "Approved";
    addLog("Audio preview approved.", "success");
  } catch (error) {
    addLog(`Audio approval error: ${error.message}`, "error");
  }
}
