import { apiFetch } from "./api.js";
import { uploadVoiceSampleToBlob } from "./blob-upload.js";
import { getAppConfig } from "./config.js";
import { dom } from "./dom.js";
import { addLog, clearLogs, fetchRemoteLogs } from "./logs.js";
import { updateJobStatus, startPolling } from "./polling.js";
import { updateResultLink, updateStatusBadge } from "./preview.js";
import { state, clearPollInterval, resetRuntimeState } from "./state.js";

export function updateFileLabel(input, labelNode, emptyText) {
  const fileName = input.files[0]?.name;
  labelNode.textContent = fileName ? `Selected: ${fileName}` : emptyText;
}

export function resetWorkspaceState() {
  dom.transcriptText.value = "";
  dom.scriptText.value = "";
  state.currentTranscriptSnapshot = "";
  state.currentScriptSnapshot = "";
  state.transcriptDirty = false;
  state.scriptDirty = false;
  dom.transcriptStatusText.textContent = "Waiting";
  dom.scriptStatusText.textContent = "Waiting";
  dom.audioEmpty.hidden = false;
  dom.audioPanel.hidden = true;
  dom.audioPlayer.removeAttribute("src");
  dom.audioPlayer.load();
  dom.audioStatus.textContent = "Waiting";
  dom.btnAudioApprove.hidden = false;
  dom.previewSummary.innerHTML = '<div class="summary-line">No active job.</div>';
  dom.stepProgress.textContent = "Waiting for pipeline start.";
  dom.segmentProgress.textContent = "No segments yet.";
  updateResultLink("pending");
}

export async function submitJob(event) {
  event.preventDefault();
  dom.submitBtn.disabled = true;
  dom.submitBtn.textContent = "Starting...";
  clearPollInterval();
  clearLogs();
  resetWorkspaceState();
  resetRuntimeState();
  dom.jobIdEl.textContent = "Preparing";
  dom.jobMessageEl.textContent = "Uploading files and creating job.";
  updateStatusBadge("pending");
  updateResultLink("pending");
  addLog("Creating job.", "info");

  try {
    const appConfig = await getAppConfig();
    const voiceFile = dom.voiceSampleInput.files[0];
    let response;

    if (appConfig.use_blob_upload) {
      dom.jobMessageEl.textContent = "Uploading voice sample to storage.";
      addLog("Uploading voice sample to Vercel Blob.", "info");
      const blob = await uploadVoiceSampleToBlob(voiceFile, appConfig.blob_upload_url);
      addLog(`Voice sample uploaded: ${blob.pathname}`, "success");
      dom.jobMessageEl.textContent = "Voice upload complete. Creating job.";
      response = await apiFetch("/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          youtube_url: dom.youtubeUrlInput.value,
          voice_sample_url: blob.url,
          voice_sample_filename: voiceFile?.name || "",
          xtts_segment_max_chars: dom.xttsSegmentMaxCharsInput?.value || 250,
          xtts_segment_min_chars: dom.xttsSegmentMinCharsInput?.value || 80,
          gpu_backend: dom.gpuBackendSelect?.value || "modal",
          modal_app_name: dom.deployModalAppNameInput.value || "tooltucode-gpu-v1",
          modal_tts_gpu: "L4",
          tts_concurrency: dom.ttsConcurrencyInput?.value || 4,
          tts_parallel_backend: "process",
          modal_xtts_dispatch: "spawn",
          modal_xtts_artifact_volume: "tooltucode-xtts-artifacts",
          modal_xtts_artifact_prefix: "xtts-jobs",
          modal_xtts_download_workers: 8
        })
      });
    } else {
      const formData = new FormData(dom.form);
      formData.set("modal_app_name", dom.deployModalAppNameInput.value || "tooltucode-gpu-v1");
      formData.set("modal_xtts_dispatch", "spawn");
      formData.set("modal_xtts_artifact_volume", "tooltucode-xtts-artifacts");
      formData.set("modal_xtts_artifact_prefix", "xtts-jobs");
      formData.set("modal_xtts_download_workers", "8");
      response = await apiFetch("/generate", {
        method: "POST",
        body: formData
      });
    }
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || "Failed to create job");
    }
    const data = await response.json();
    state.currentJobId = data.job_id;
    dom.jobIdEl.textContent = state.currentJobId;
    addLog(`Job created: ${state.currentJobId}`, "success");
    await updateJobStatus();
    await fetchRemoteLogs();
    startPolling();
  } catch (error) {
    updateStatusBadge("failed");
    dom.jobMessageEl.textContent = error.message;
    addLog(`Job creation error: ${error.message}`, "error");
  } finally {
    dom.submitBtn.disabled = false;
    dom.submitBtn.textContent = "Start job";
  }
}

export function initJobForm() {
  dom.voiceSampleInput.addEventListener("change", () => {
    updateFileLabel(dom.voiceSampleInput, dom.voiceFileName, "No file selected.");
  });
  dom.form.addEventListener("submit", submitJob);
}
