export const state = {
  currentJobId: null,
  pollInterval: null,
  seenRemoteLogCount: 0,
  currentTranscriptSnapshot: "",
  currentScriptSnapshot: "",
  transcriptDirty: false,
  scriptDirty: false
};

export function resetRuntimeState() {
  state.currentJobId = null;
  state.seenRemoteLogCount = 0;
  state.currentTranscriptSnapshot = "";
  state.currentScriptSnapshot = "";
  state.transcriptDirty = false;
  state.scriptDirty = false;
}

export function clearPollInterval() {
  if (state.pollInterval) {
    clearInterval(state.pollInterval);
    state.pollInterval = null;
  }
}
