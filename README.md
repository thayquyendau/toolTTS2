# TTS Worker

`TTS_worker` is a self-contained Vercel app for transcript extraction, manual script approval, XTTS audio generation, Modal step 3 deployment, and optional Modal account linking.

## Flow

1. Submit `youtube_url` and `voice_sample`
2. Review and optionally edit transcript
3. Paste or edit the final script
4. Approve script and let XTTS generate `voice.wav`
5. Review audio output

## Included UI

- `/static/index.html` control room
- transcript editor
- script editor
- audio preview
- live logs
- step and segment status
- Modal step 3 deploy panel
- optional `Link Modal account` flow for manual Modal profile authorization

## Included API

- `POST /generate`
- `GET /app-config`
- `GET /modal/profiles`
- `POST /modal/token/new`
- `GET /modal/token/{job_id}`
- `GET /job/{job_id}`
- `GET /job/{job_id}/logs`
- `GET /job/{job_id}/result`
- `GET|PUT|POST /job/{job_id}/transcript`
- `GET|PUT|POST /job/{job_id}/script`
- `GET|POST /job/{job_id}/audio`
- `POST /modal/deploy`
- `GET /modal/deploy/{job_id}`

## Job backends

- `TTS_JOB_BACKEND=local`
  - local filesystem + in-process background executor
- `TTS_JOB_BACKEND=modal`
  - job status/logs/artifacts stored in a Modal Volume
  - Vercel handles lightweight job setup and approvals
  - Modal executes:
    - XTTS phase after script approval

If `TTS_JOB_BACKEND` is not set, the app auto-selects:

- `modal` on Vercel
- `local` outside Vercel

## Deploy

1. Use `TTS_worker/` as the Vercel project root.
2. Vercel entrypoint is `api/index.py`.
3. Enable direct Blob upload in Vercel for large voice samples:
   - create a Vercel Blob store
   - ensure `BLOB_READ_WRITE_TOKEN` is available in the project
4. Blob upload is enabled only when:
   - the app is running on Vercel, or `USE_BLOB_UPLOAD=1`
   - `BLOB_READ_WRITE_TOKEN` is available in the runtime
   If the token is missing, `/app-config` falls back to direct multipart upload.
5. In the Vercel dashboard, enable `Fluid Compute` for the project.
6. `vercel.json` uses the modern `functions` configuration only.
   - `api/index.py` is configured with `maxDuration: 60`
   - `/api/blob/upload` is routed to `api/blob/upload.js`
   - all other paths are routed to `api/index.py`
7. Redeploy the Modal step 3 app after code changes so the deployed app includes:
   - `run_pipeline_step_3`
8. Configure Modal deploy accounts through `config/modal_profiles.json`.
   - Each profile maps to secret env names, not raw tokens.
   - The backend injects `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` from the selected profile at deploy time.

Optional overrides:

- `MODAL_TTS_JOB_VOLUME`
  - defaults to `tooltucode-tts-jobs`
- `TTS_JOB_BACKEND`
  - only needed if you want to force `local` or `modal`

## Modal profiles

The checked-in file [config/modal_profiles.json](/mnt/d/xaykenhYTB/ToolTuCode/TTS_worker/config/modal_profiles.json) stores non-secret deploy mappings:

- profile key and label
- token env variable names
- default `modal_app_name`
- default GPU and XTTS artifact settings

Example:

```json
{
  "default_profile": "default",
  "profiles": {
    "default": {
      "label": "Default account",
      "token_id_env": "MODAL_DEFAULT_TOKEN_ID",
      "token_secret_env": "MODAL_DEFAULT_TOKEN_SECRET",
      "modal_app_name": "tooltucode-gpu-v2"
    }
  }
}
```

What you must configure yourself:

- Add secret envs for each Modal account token pair.
  - Example: `MODAL_DEFAULT_TOKEN_ID`
  - Example: `MODAL_DEFAULT_TOKEN_SECRET`
- Update `config/modal_profiles.json` to map each profile to the matching env names.
- If a different account should deploy to a different app name, set that app name in the same profile entry.

Notes:

- Deploy uses env-mapped account tokens from the selected profile.
- `Link Modal account` is separate and only runs `modal token new` for manual browser-based account authorization.
- fixed production env such as `MODAL_APP_NAME`, `MODAL_TTS_GPU`, `MODAL_XTTS_ARTIFACT_VOLUME`, `MODAL_XTTS_ARTIFACT_PREFIX` are no longer required for ordinary deploy switching

## Requirements

- `requirements.txt`
  - runtime set for Vercel
  - excludes local XTTS stack and `uvicorn`
- `requirements-local.txt`
  - extends `requirements.txt`
  - adds local server and local XTTS packages

## Local run path

1. Install local dependencies:
   - `pip install -r requirements-local.txt`
2. Ensure `ffmpeg` is on `PATH`
3. Start the app locally:
   - `python main.py`
4. Open:
   - `http://127.0.0.1:8000/static/index.html`

Local mode defaults to multipart upload. If you want local behavior closer to Vercel, you can opt in with:

- `USE_BLOB_UPLOAD=1`
  - optional override to force Blob mode on non-Vercel runtimes
- `BLOB_READ_WRITE_TOKEN`
  - required for `/api/blob/upload`; without it the UI stays on multipart fallback

If you keep `gpu_backend=modal`, local run only needs the web stack plus Modal CLI access. If you switch to `gpu_backend=local`, the packages in `requirements-local.txt` are required.

## Notes

- The operator UI is intentionally border-led and cardless.
- Deploy support is limited to `step_3` to match the reduced worker scope.
- Local XTTS still requires the optional local inference stack:
  - `torch`
  - `numpy`
  - `TTS`
  - `ffmpeg` on PATH
