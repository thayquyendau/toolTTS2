function sanitizeBlobPathSegment(filename) {
  return String(filename || "voice-sample")
    .trim()
    .replace(/[^a-zA-Z0-9._-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "") || "voice-sample";
}

export async function uploadVoiceSampleToBlob(file, handleUploadUrl) {
  if (!file) {
    throw new Error("No voice sample selected.");
  }
  if (!handleUploadUrl) {
    throw new Error("Blob upload endpoint is not configured.");
  }
  const { upload } = await import("/static/vendor/vercel-blob-client.js");
  const pathname = `voice-samples/${Date.now()}-${sanitizeBlobPathSegment(file.name)}`;
  return upload(pathname, file, {
    access: "public",
    handleUploadUrl
  });
}
