import { dom } from "./dom.js";

const STORAGE_KEY = "tts-worker.blob-upload-mode";
const VALID_MODES = new Set(["auto", "on", "off"]);

function normalizeMode(value) {
  return VALID_MODES.has(value) ? value : "auto";
}

function readStoredMode() {
  try {
    return normalizeMode(globalThis.localStorage?.getItem(STORAGE_KEY) || "auto");
  } catch {
    return "auto";
  }
}

function writeStoredMode(value) {
  try {
    globalThis.localStorage?.setItem(STORAGE_KEY, value);
  } catch {
    // Ignore storage failures and keep runtime selection only.
  }
}

export function getBlobUploadMode() {
  return normalizeMode(dom.blobUploadModeSelect?.value || readStoredMode());
}

export function shouldUseBlobUpload(appConfig) {
  const mode = getBlobUploadMode();
  if (mode === "on") {
    return true;
  }
  if (mode === "off") {
    return false;
  }
  return Boolean(appConfig?.use_blob_upload);
}

function updateBlobUploadNote() {
  const mode = getBlobUploadMode();
  if (!dom.blobUploadNote) {
    return;
  }
  if (mode === "on") {
    dom.blobUploadNote.textContent = "Force Blob upload before /generate. Use this on Vercel.";
    return;
  }
  if (mode === "off") {
    dom.blobUploadNote.textContent = "Force direct multipart upload to /generate. Use this for local only.";
    return;
  }
  dom.blobUploadNote.textContent = "Auto follows backend app-config.";
}

export function initUploadModeControl() {
  if (!dom.blobUploadModeSelect) {
    return;
  }
  dom.blobUploadModeSelect.value = readStoredMode();
  updateBlobUploadNote();
  dom.blobUploadModeSelect.addEventListener("change", () => {
    const mode = getBlobUploadMode();
    writeStoredMode(mode);
    updateBlobUploadNote();
  });
}
