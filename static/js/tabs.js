import { dom } from "./dom.js";

export function setActiveTab(tabName) {
  dom.tabButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === tabName);
  });
  dom.tabPanes.forEach((pane) => {
    pane.classList.toggle("active", pane.id === tabName);
  });
}

export function openRelevantTab(status) {
  if (document.activeElement === dom.transcriptText || document.activeElement === dom.scriptText) {
    return;
  }
  if (status.transcript_data && !status.transcript_approved) {
    setActiveTab("transcript");
    return;
  }
  if (status.script_data !== null && status.script_data !== undefined && !status.script_approved) {
    setActiveTab("script");
    return;
  }
  if (status.audio_preview && !status.voice_approved) {
    setActiveTab("audio");
    return;
  }
  if (status.status === "running") {
    setActiveTab("logs");
  }
}

export function initTabs() {
  dom.tabButtons.forEach((button) => {
    button.addEventListener("click", () => setActiveTab(button.dataset.tab));
  });
}
