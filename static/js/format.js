export function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function getStatusTone(status) {
  const normalized = String(status || "").toLowerCase();
  if (normalized === "success" || normalized === "completed") return "success";
  if (normalized === "failure" || normalized === "failed") return "failure";
  if (normalized === "running" || normalized === "waiting_approval") return "running";
  return "pending";
}
