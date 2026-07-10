# TTS Worker

`TTS_worker` is a self-contained Vercel app for transcript extraction, manual script approval, XTTS audio generation, Modal step 3 deployment, and Modal token creation.

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
- Modal token creation panel

## Included API

- `POST /generate`
- `GET /app-config`
- `GET /job/{job_id}`
- `GET /job/{job_id}/logs`
- `GET /job/{job_id}/result`
- `GET|PUT|POST /job/{job_id}/transcript`
- `GET|PUT|POST /job/{job_id}/script`
- `GET|POST /job/{job_id}/audio`
- `POST /modal/deploy`
- `GET /modal/deploy/{job_id}`
- `POST /modal/token/new`
- `GET /modal/token/{job_id}`

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
   - set `USE_BLOB_UPLOAD=1`
   - ensure `BLOB_READ_WRITE_TOKEN` is available in the project
4. In the Vercel dashboard, enable `Fluid Compute` for the project.
5. The repo config sets `maxDuration: 60` for the Python entrypoint in `vercel.json`.
6. Set the required Modal environment variables:
   - `MODAL_APP_NAME`
   - `MODAL_TTS_GPU`
   - `MODAL_XTTS_ARTIFACT_VOLUME`
   - `MODAL_XTTS_ARTIFACT_PREFIX`
7. Redeploy the Modal step 3 app after code changes so the deployed app includes:
   - `run_pipeline_step_3`

Optional overrides:

- `MODAL_TTS_JOB_VOLUME`
  - defaults to `tooltucode-tts-jobs`
- `TTS_JOB_BACKEND`
  - only needed if you want to force `local` or `modal`

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

Local mode defaults to multipart upload. If you want local behavior closer to Vercel, set:

- `USE_BLOB_UPLOAD=1`

If you keep `gpu_backend=modal`, local run only needs the web stack plus Modal CLI access. If you switch to `gpu_backend=local`, the packages in `requirements-local.txt` are required.

## Notes

- The operator UI is intentionally border-led and cardless.
- Deploy support is limited to `step_3` to match the reduced worker scope.
- Local XTTS still requires the optional local inference stack:
  - `torch`
  - `numpy`
  - `TTS`
  - `ffmpeg` on PATH
