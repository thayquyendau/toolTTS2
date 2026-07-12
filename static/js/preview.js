import { apiUrl } from "./api.js";
import { dom } from "./dom.js";
import { escapeHtml, getStatusTone } from "./format.js";
import { state } from "./state.js";

export function updateStatusBadge(status) {
  const normalized = (status || "pending").toLowerCase();
  dom.jobStatusBadge.className = `tone-label ${getStatusTone(normalized)}`;
  dom.jobStatusBadge.textContent = normalized.replace(/_/g, " ");
}

export function updateResultLink(statusInput) {
  const status = typeof statusInput === "string" ? { status: statusInput } : (statusInput || {});
  if (status.status === "completed" && state.currentJobId) {
    dom.resultLink.href = apiUrl(`/job/${state.currentJobId}/result`);
    dom.resultLink.hidden = false;
    dom.resultPlaceholder.hidden = true;
  } else {
    dom.resultLink.hidden = true;
    dom.resultPlaceholder.hidden = false;
    dom.resultPlaceholder.textContent = status.status === "failed" ? "Failed" : "Pending";
  }
}

export function renderPreviewSummary(status) {
  const lines = [
    ["Current status", status.status || "pending"],
    ["Message", status.message || "Processing not started."],
  ];
  if (status.current_step) lines.push(["Current step", status.current_step]);
  if (status.step_1_duration_seconds !== null && status.step_1_duration_seconds !== undefined) {
    lines.push(["Step 1 duration", `${status.step_1_duration_seconds}s`]);
  }
  if (status.step_1_spawn_status && status.step_1_spawn_status !== "idle") {
    lines.push(["Step 1 dispatch", status.step_1_spawn_status]);
  }
  if (status.step_1_modal_call_id) {
    lines.push(["Step 1 call", status.step_1_modal_call_id]);
  }
  if (status.step_3_spawn_status && status.step_3_spawn_status !== "idle") {
    lines.push(["Step 3 spawn", status.step_3_spawn_status]);
  }
  if (status.step_3_modal_call_id) {
    lines.push(["Modal call", status.step_3_modal_call_id]);
  }
  if (status.render_config) {
    lines.push(["Runtime", `${status.render_config.gpu_backend || "modal"} / TTS x${status.render_config.tts_concurrency || 4}`]);
  }
  dom.previewSummary.innerHTML = lines.map(([label, value]) => (
    `<div class="summary-line"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`
  )).join("");
}

export function renderStepProgress(steps) {
  if (!steps || !steps.length) {
    dom.stepProgress.textContent = "Waiting for pipeline start.";
    return;
  }
  dom.stepProgress.innerHTML = steps.map((step, index) => (
    `<div class="progress-row ${getStatusTone(step.status)}"><span>${index + 1}. ${escapeHtml(step.name || step.key)}</span><strong>${escapeHtml(step.status || "pending")}</strong></div>`
  )).join("");
}

export function renderSegmentProgress(segments) {
  if (!segments || !segments.length) {
    dom.segmentProgress.textContent = "No segments yet.";
    return;
  }
  dom.segmentProgress.innerHTML = segments.map((segment) => (
    `<div class="progress-row ${getStatusTone(segment.status)}"><span>${escapeHtml(segment.name || `segment_${segment.index}`)}</span><strong>${escapeHtml(segment.phase || segment.status || "pending")}</strong></div>`
  )).join("");
}
