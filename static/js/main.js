import { approveAudioPreview } from "./audio.js";
import { approveScript, approveTranscript, resetScript, resetTranscript } from "./approvals.js";
import { dom } from "./dom.js";
import { initModalDeployPanel } from "./deploy.js";
import { initJobForm, resetWorkspaceState } from "./job-form.js";
import { initSettingsModal } from "./settings-modal.js";
import { clearPollInterval } from "./state.js";
import { initTabs } from "./tabs.js";

initTabs();
initSettingsModal();
initModalDeployPanel();
initJobForm();

dom.transcriptText.addEventListener("input", () => {
  import("./state.js").then(({ state }) => {
    state.transcriptDirty = dom.transcriptText.value !== state.currentTranscriptSnapshot;
  });
});
dom.scriptText.addEventListener("input", () => {
  import("./state.js").then(({ state }) => {
    state.scriptDirty = dom.scriptText.value !== state.currentScriptSnapshot;
  });
});

dom.btnTranscriptReset.addEventListener("click", resetTranscript);
dom.btnTranscriptApprove.addEventListener("click", approveTranscript);
dom.btnScriptReset.addEventListener("click", resetScript);
dom.btnScriptApprove.addEventListener("click", approveScript);
dom.btnAudioApprove.addEventListener("click", approveAudioPreview);

window.addEventListener("beforeunload", () => {
  clearPollInterval();
});

resetWorkspaceState();
