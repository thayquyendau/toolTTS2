import { dom } from "./dom.js";

function setModalOpen(isOpen) {
  dom.settingsModal.hidden = !isOpen;
  document.body.style.overflow = isOpen ? "hidden" : "";
}

function closeOnEscape(event) {
  if (event.key === "Escape" && !dom.settingsModal.hidden) {
    setModalOpen(false);
    dom.settingsOpen.focus();
  }
}

export function initSettingsModal() {
  dom.settingsOpen.addEventListener("click", () => setModalOpen(true));
  dom.settingsClose.addEventListener("click", () => setModalOpen(false));
  dom.settingsBackdrop.addEventListener("click", () => setModalOpen(false));
  document.addEventListener("keydown", closeOnEscape);
}
