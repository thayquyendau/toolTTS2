export const API_BASE_URL = (globalThis.API_BASE_URL || "").replace(/\/+$/, "");
export const POLL_INTERVAL_MS = 2000;

let appConfigPromise;

export function defaultAppConfig() {
  return {
    use_blob_upload: false,
    blob_upload_url: null
  };
}

export async function getAppConfig() {
  if (!appConfigPromise) {
    const appConfigUrl = API_BASE_URL ? `${API_BASE_URL}/app-config` : "/app-config";
    appConfigPromise = fetch(appConfigUrl)
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(`Request failed: ${response.status}`);
        }
        return response.json();
      })
      .catch(() => defaultAppConfig());
  }
  return appConfigPromise;
}
