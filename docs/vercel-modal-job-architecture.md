# Vercel and Modal Job Architecture

## Production flow

1. The browser uploads the voice sample directly to Vercel Blob.
2. `POST /generate` validates the request, creates durable job state in Modal Volume, and spawns `run_pipeline_step_1`.
3. Modal downloads the Blob input, extracts the YouTube transcript, and stores transcript/status/log files in the job volume.
4. After script approval, Vercel spawns `run_pipeline_step_3` and Modal generates the audio result.
5. Vercel serves status, logs, approvals, and result files from Modal Volume.

Vercel must not download Blob input, extract transcripts, or run pipeline work inside a request when `TTS_JOB_BACKEND=modal`.

## Durable state

- `status.json`, `job.log`, input files, transcript, script, and audio artifacts live under the job prefix in `MODAL_TTS_JOB_VOLUME`.
- Vercel filesystem and process memory are not sources of truth.
- `/tmp` is used only for short-lived transfer files.
- Modal call IDs are observability metadata. Failure to persist a call ID after a successful spawn must not cause a duplicate client submission.

## Input modes

- Blob URL: Modal downloads the file using HTTPS, an allowed-host list, retry for transient responses, content-type checks, and a configured size limit.
- Multipart: intended for local development; Vercel copies the file to Modal Volume and dispatches the same Step 1 function.
- Local backend: keeps the filesystem and in-process executor path for development only.

## Required configuration

- `BLOB_READ_WRITE_TOKEN`: enables direct browser upload.
- `MODAL_TTS_JOB_VOLUME`: durable Modal job volume.
- `MODAL_VOICE_SAMPLE_MAX_BYTES`: maximum Blob input size; default 200 MiB.
- `MODAL_VOICE_SAMPLE_ALLOWED_HOSTS`: comma-separated exact hosts or dot-prefixed suffixes; default `.public.blob.vercel-storage.com`.
- Modal account credentials are deployment secrets. Interactive token linking is disabled on Vercel.

## Deployment order

1. Deploy the base Modal app containing `run_pipeline_step_1`.
2. Deploy the Step 3 Modal app containing `run_pipeline_step_3`.
3. Deploy Vercel.
4. Run a canary job and verify `queued -> running -> waiting_approval -> completed`.
