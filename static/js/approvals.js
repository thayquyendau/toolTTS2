import { apiFetch, postJson, putJson } from "./api.js";
import { dom } from "./dom.js";
import { addLog } from "./logs.js";
import { state } from "./state.js";
import { setActiveTab } from "./tabs.js";

function canReplaceEditor(textarea, dirty) {
  return !dirty && document.activeElement !== textarea;
}

export async function loadTranscript(options = {}) {
  if (!state.currentJobId) return;
  try {
    const res = await apiFetch(`/job/${state.currentJobId}/transcript`);
    if (!res.ok) return;
    const data = await res.json();
    if (data.transcript !== null && data.transcript !== undefined) {
      const force = Boolean(options.force);
      if (force || canReplaceEditor(dom.transcriptText, state.transcriptDirty) || dom.transcriptText.value === "") {
        dom.transcriptText.value = data.transcript;
        state.transcriptDirty = false;
      }
      state.currentTranscriptSnapshot = data.transcript || "";
      dom.transcriptStatusText.textContent = data.approved ? "Approved" : "Ready for review";
    }
  } catch {
    // Keep polling resilient.
  }
}

export async function resetTranscript() {
  await loadTranscript({ force: true });
}

export async function approveTranscript() {
  if (!state.currentJobId) return;
  try {
    if (dom.transcriptText.value !== state.currentTranscriptSnapshot) {
      await putJson(`/job/${state.currentJobId}/transcript`, { content: dom.transcriptText.value });
      state.currentTranscriptSnapshot = dom.transcriptText.value;
    }
    await postJson(`/job/${state.currentJobId}/transcript/approve`);
    state.transcriptDirty = false;
    dom.transcriptStatusText.textContent = "Approved";
    addLog("Transcript approved.", "success");
    setActiveTab("script");
  } catch (error) {
    addLog(`Transcript approval error: ${error.message}`, "error");
  }
}

export async function loadScript(options = {}) {
  if (!state.currentJobId) return;
  try {
    const res = await apiFetch(`/job/${state.currentJobId}/script`);
    if (!res.ok) return;
    const data = await res.json();
    if (data.script !== null && data.script !== undefined) {
      const force = Boolean(options.force);
      if (force || canReplaceEditor(dom.scriptText, state.scriptDirty) || dom.scriptText.value === "") {
        dom.scriptText.value = data.script;
        state.scriptDirty = false;
      }
      state.currentScriptSnapshot = data.script || "";
      dom.scriptStatusText.textContent = data.approved ? "Approved" : "Ready for review";
    }
  } catch {
    // Keep polling resilient.
  }
}

export async function resetScript() {
  await loadScript({ force: true });
}

export async function approveScript() {
  if (!state.currentJobId) return;
  dom.btnScriptApprove.disabled = true;
  try {
    if (dom.scriptText.value !== state.currentScriptSnapshot) {
      await putJson(`/job/${state.currentJobId}/script`, { content: dom.scriptText.value });
      state.currentScriptSnapshot = dom.scriptText.value;
    }
    const result = await postJson(`/job/${state.currentJobId}/script/approve`);
    state.scriptDirty = false;
    dom.scriptStatusText.textContent = "Approved";
    if (result?.modal_call_id) {
      addLog(`Script approved. Modal call: ${result.modal_call_id}`, "success");
    } else {
      addLog(result?.message || "Script approved.", "success");
    }
    setActiveTab("audio");
  } catch (error) {
    addLog(`Script approval error: ${error.message}`, "error");
  } finally {
    dom.btnScriptApprove.disabled = false;
  }
}
