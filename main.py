import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

import requests
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from job_status import (
    append_job_log,
    approve_script,
    approve_transcript,
    approve_voice,
    init_job_status,
    job_log_path,
    load_job_status,
    save_job_status,
    set_overall_status,
    update_step_status,
    update_script_data,
    update_transcript_data,
)
from modal_auth import get_modal_token_job, start_modal_token_new
from modal_deploy import get_modal_deploy_job, start_modal_deploy
from modal_job_store import (
    create_modal_job_id,
    fail_step_3_spawn,
    init_modal_job,
    is_modal_job_backend,
    job_exists as modal_job_exists,
    load_status as modal_load_status,
    append_log as modal_append_log,
    prepare_step_3_spawn,
    put_text as modal_put_text,
    read_bytes as modal_read_bytes,
    read_text as modal_read_text,
    record_step_3_call,
    spawn_modal_job_function,
    update_text_field as modal_update_text_field,
    upload_local_file as modal_upload_local_file,
    approve_field as modal_approve_field,
)
from pipeline_steps.common import ProcessingError
from pipeline_steps.orchestrator import run_tts_pipeline
from pipeline_steps.step_1_transcript import step_1_get_transcript
from utils import create_job_dir, get_job_dir, save_upload_file


def _load_repo_env() -> None:
    candidate_paths = [
        Path(__file__).resolve().parent / ".env",
        Path(__file__).resolve().parent.parent / ".env",
    ]
    for env_path in candidate_paths:
        if not env_path.exists():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key:
                os.environ.setdefault(key, value.strip())


_load_repo_env()

BASE_DIR = os.path.dirname(__file__)
app = FastAPI(title="TTS Worker", version="1.0")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
app.state.executor = ThreadPoolExecutor(max_workers=2)


def _get_cors_origins() -> list[str]:
    origins = {
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    }
    configured = os.getenv("FRONTEND_ORIGIN", "")
    for origin in configured.split(","):
        origin = origin.strip()
        if origin:
            origins.add(origin)
    return sorted(origins)


app.add_middleware(
    CORSMiddleware,
    allow_origins=_get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class TextUpdateRequest(BaseModel):
    content: str


class ModalDeployRequest(BaseModel):
    target: str = "step_3"
    base_app_name: str = "tooltucode-gpu-v1"
    strategy: str = "rolling"


class ModalTokenRequest(BaseModel):
    profile: str = "default"
    activate: bool = True
    verify: bool = True


class GenerateJobRequest(BaseModel):
    youtube_url: str
    voice_sample_url: str | None = None
    voice_sample_filename: str | None = None
    xtts_segment_max_chars: int = 250
    xtts_segment_min_chars: int = 80
    gpu_backend: str = "modal"
    modal_app_name: str = "tooltucode-gpu-v1"
    modal_tts_gpu: str = "L4"
    tts_concurrency: int = 4
    tts_parallel_backend: str = "process"
    modal_xtts_dispatch: str = "spawn"
    modal_xtts_artifact_volume: str = "tooltucode-xtts-artifacts"
    modal_xtts_artifact_prefix: str = "xtts-jobs"
    modal_xtts_download_workers: int = 8


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


def _build_render_config(payload: dict) -> dict:
    return {
        "xtts_segment_max_chars": int(payload.get("xtts_segment_max_chars", 250)),
        "xtts_segment_min_chars": int(payload.get("xtts_segment_min_chars", 80)),
        "gpu_backend": str(payload.get("gpu_backend", "modal")),
        "modal_app_name": str(payload.get("modal_app_name", "tooltucode-gpu-v1")),
        "modal_tts_gpu": str(payload.get("modal_tts_gpu", "L4")),
        "tts_concurrency": int(payload.get("tts_concurrency", 4)),
        "tts_parallel_backend": str(payload.get("tts_parallel_backend", "process")),
        "modal_xtts_dispatch": str(payload.get("modal_xtts_dispatch", "spawn")),
        "modal_xtts_artifact_volume": str(payload.get("modal_xtts_artifact_volume", "tooltucode-xtts-artifacts")),
        "modal_xtts_artifact_prefix": str(payload.get("modal_xtts_artifact_prefix", "xtts-jobs")),
        "modal_xtts_download_workers": int(payload.get("modal_xtts_download_workers", 8)),
    }


def _safe_voice_extension(filename: str | None, source_url: str | None = None) -> str:
    candidates = [filename or ""]
    if source_url:
        candidates.append(urlparse(source_url).path)
    for candidate in candidates:
        suffix = Path(candidate).suffix.strip()
        if suffix:
            return suffix[:12]
    return ".mp3"


def _download_voice_sample(voice_sample_url: str, output_path: str) -> None:
    parsed = urlparse(voice_sample_url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="voice_sample_url must use http or https.")
    try:
        with requests.get(voice_sample_url, stream=True, timeout=(10, 120)) as response:
            response.raise_for_status()
            with open(output_path, "wb") as file_obj:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        file_obj.write(chunk)
    except HTTPException:
        raise
    except requests.RequestException as exc:
        raise HTTPException(status_code=400, detail=f"Could not download voice_sample_url: {exc}") from exc


def _prepare_voice_sample_file(
    job_dir: str,
    voice_sample: UploadFile | None = None,
    voice_sample_url: str | None = None,
    voice_sample_filename: str | None = None,
) -> str:
    voice_ext = _safe_voice_extension(voice_sample_filename or getattr(voice_sample, "filename", None), voice_sample_url)
    voice_path = os.path.join(job_dir, f"input_voice{voice_ext}")
    if voice_sample_url:
        append_job_log(job_dir, f"Downloading voice sample from URL: {voice_sample_url}")
        _download_voice_sample(voice_sample_url, voice_path)
        append_job_log(job_dir, f"Voice sample downloaded to {os.path.basename(voice_path)}.")
        return voice_path
    if voice_sample is None:
        raise HTTPException(status_code=400, detail="voice_sample or voice_sample_url is required.")
    append_job_log(job_dir, "Saving uploaded voice sample.")
    save_upload_file(voice_sample, voice_path)
    return voice_path


def _resolve_modal_job_app_name(render_config: dict) -> str:
    base_name = str(render_config.get("modal_app_name") or os.getenv("MODAL_APP_NAME", "tooltucode-gpu-v1")).strip()
    return f"{base_name}-step3"


def _prime_script_waiting_state_local(job_dir: str) -> None:
    status_data = load_job_status(job_dir)
    if status_data is None:
        raise FileNotFoundError(f"Status file not found: {job_dir}")
    if status_data.get("script_data") is None:
        status_data["script_data"] = ""
        status_data["script_approved"] = False
        save_job_status(job_dir, status_data)
    update_step_status(
        job_dir,
        "step_2_manual_script",
        "waiting_approval",
        "Waiting for user approval: manual script before TTS.",
    )


def _upload_job_dir_to_modal(job_id: str, job_dir: str) -> None:
    for entry in os.scandir(job_dir):
        if entry.is_file():
            modal_upload_local_file(job_id, entry.path, entry.name)


def _init_modal_job_with_voice(
    youtube_url: str,
    voice_sample: UploadFile | None,
    voice_sample_url: str | None,
    voice_sample_filename: str | None,
    render_config: dict,
) -> dict:
    job_id = create_modal_job_id()
    temp_dir = tempfile.mkdtemp(prefix="tooltucode_modal_job_")
    try:
        init_job_status(temp_dir, job_id)
        temp_voice_path = _prepare_voice_sample_file(
            temp_dir,
            voice_sample=voice_sample,
            voice_sample_url=voice_sample_url,
            voice_sample_filename=voice_sample_filename,
        )
        set_overall_status(temp_dir, "running", "Fetching transcript from YouTube.")
        step_1_get_transcript(youtube_url, temp_dir)
        _prime_script_waiting_state_local(temp_dir)
        status_data = load_job_status(temp_dir) or {}
        status_data["render_config"] = render_config
        save_job_status(temp_dir, status_data)
        init_modal_job(job_id, render_config)
        _upload_job_dir_to_modal(job_id, temp_dir)
        modal_append_log(job_id, "Job prepared on API server; waiting for script approval.")
        return {
            "job_id": job_id,
            "status_url": f"/job/{job_id}",
            "logs_url": f"/job/{job_id}/logs",
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.get("/app-config")
def get_app_config():
    use_blob_upload = _env_flag("USE_BLOB_UPLOAD", False)
    return {
        "use_blob_upload": use_blob_upload,
        "blob_upload_url": "/api/blob/upload" if use_blob_upload else None,
    }


@app.post("/modal/deploy")
def modal_deploy_app(request: ModalDeployRequest):
    return start_modal_deploy("step_3", request.base_app_name, request.strategy)


@app.get("/modal/deploy/{job_id}")
def get_modal_deploy_app(job_id: str):
    try:
        return get_modal_deploy_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Deployment job not found.") from exc


@app.post("/modal/token/new")
def modal_token_new_app(request: ModalTokenRequest):
    return start_modal_token_new(request.profile, request.activate, request.verify)


@app.get("/modal/token/{job_id}")
def get_modal_token_app(job_id: str):
    try:
        return get_modal_token_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Token job not found.") from exc


@app.get("/")
def read_root():
    return RedirectResponse(url="/static/index.html")


@app.post("/generate")
async def generate_tts(request: Request):
    content_type = request.headers.get("content-type", "")
    job_dir = None
    voice_sample = None
    voice_sample_url = None
    voice_sample_filename = None

    if "application/json" in content_type:
        try:
            payload = GenerateJobRequest.model_validate(await request.json())
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON request body: {exc}") from exc
        youtube_url = payload.youtube_url
        voice_sample_url = payload.voice_sample_url
        voice_sample_filename = payload.voice_sample_filename
        render_config = _build_render_config(payload.model_dump())
    else:
        form = await request.form()
        youtube_url = str(form.get("youtube_url") or "").strip()
        voice_sample = form.get("voice_sample")
        voice_sample_url = str(form.get("voice_sample_url") or "").strip() or None
        voice_sample_filename = str(form.get("voice_sample_filename") or "").strip() or None
        render_config = _build_render_config(dict(form))

    if not youtube_url:
        raise HTTPException(status_code=400, detail="youtube_url is required.")
    if not voice_sample_url and not voice_sample:
        raise HTTPException(status_code=400, detail="voice_sample or voice_sample_url is required.")

    try:
        if is_modal_job_backend():
            return _init_modal_job_with_voice(
                youtube_url=youtube_url,
                voice_sample=voice_sample if hasattr(voice_sample, "file") else None,
                voice_sample_url=voice_sample_url,
                voice_sample_filename=voice_sample_filename,
                render_config=render_config,
            )
        job_dir = create_job_dir()
        job_id = os.path.basename(job_dir)
        init_job_status(job_dir, job_id)
        voice_path = _prepare_voice_sample_file(
            job_dir,
            voice_sample=voice_sample if hasattr(voice_sample, "file") else None,
            voice_sample_url=voice_sample_url,
            voice_sample_filename=voice_sample_filename,
        )
        set_overall_status(job_dir, "pending", "Files received. Preparing TTS pipeline.")
        app.state.executor.submit(run_tts_pipeline_job, job_dir, youtube_url, voice_path, render_config)
        return {
            "job_id": job_id,
            "status_url": f"/job/{job_id}",
            "logs_url": f"/job/{job_id}/logs",
        }
    except Exception as exc:
        if job_dir:
            set_overall_status(job_dir, "failed", "Could not create TTS job.", error_detail=str(exc))
        raise HTTPException(status_code=500, detail=f"Error creating TTS job: {exc}") from exc


def run_tts_pipeline_job(job_dir: str, youtube_url: str, voice_path: str, render_config: dict | None = None):
    append_job_log(job_dir, "Background TTS job started.")
    try:
        set_overall_status(job_dir, "running", "TTS pipeline is running.")
        voice_output_path = run_tts_pipeline(
            youtube_url=youtube_url,
            voice_sample_path=voice_path,
            job_dir=job_dir,
            render_config=render_config,
        )
        set_overall_status(
            job_dir,
            "completed",
            "TTS pipeline completed.",
            result_file=os.path.basename(voice_output_path),
        )
        append_job_log(job_dir, "TTS job completed successfully.")
    except ProcessingError as exc:
        set_overall_status(job_dir, "failed", str(exc), error_detail=str(exc))
        append_job_log(job_dir, f"ProcessingError: {exc}")
    except Exception as exc:
        set_overall_status(job_dir, "failed", "System error while running TTS job.", error_detail=str(exc))
        append_job_log(job_dir, f"System error: {exc}")


def _modal_status(job_id: str) -> dict:
    try:
        return modal_load_status(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job status not found.") from exc


def _modal_update_text(job_id: str, field: str, content: str, *, approval_field: str | None = None, artifact_name: str | None = None):
    if artifact_name:
        modal_put_text(job_id, artifact_name, content)
    modal_update_text_field(job_id, field, content, approval_field)


def _modal_spawn_step_3(job_id: str) -> None:
    status = _modal_status(job_id)
    render_config = status.get("render_config") or {}
    app_name = _resolve_modal_job_app_name(render_config)
    prepare_step_3_spawn(job_id)
    try:
        call = spawn_modal_job_function(app_name, "run_pipeline_step_3", job_id, render_config)
        record_step_3_call(job_id, call.object_id)
        modal_append_log(job_id, "Modal step 3 job spawned.")
    except Exception as exc:
        fail_step_3_spawn(job_id, str(exc))
        raise


@app.get("/job/{job_id}")
def get_job_status(job_id: str):
    if is_modal_job_backend():
        return _modal_status(job_id)
    status = _load_existing_status(job_id)
    return status


@app.get("/job/{job_id}/logs")
def get_job_logs(job_id: str):
    if is_modal_job_backend():
        try:
            log_text = modal_read_text(job_id, "job.log")
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Job log not found.") from exc
        return Response(content=log_text, media_type="text/plain")
    job_dir = _existing_job_dir(job_id)
    log_path = job_log_path(job_dir)
    if not os.path.exists(log_path):
        raise HTTPException(status_code=404, detail="Job log not found.")
    return FileResponse(path=log_path, media_type="text/plain", filename="job.log")


@app.get("/job/{job_id}/result")
def get_job_result(job_id: str):
    if is_modal_job_backend():
        status = _modal_status(job_id)
        if status.get("status") != "completed" or not status.get("result_file"):
            raise HTTPException(status_code=400, detail="Audio is not ready.")
        try:
            audio_bytes = modal_read_bytes(job_id, status["result_file"])
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Audio result not found.") from exc
        return Response(content=audio_bytes, media_type="audio/wav", headers={"Content-Disposition": 'attachment; filename="voice.wav"'})
    job_dir = _existing_job_dir(job_id)
    status = _load_existing_status(job_id)
    if status.get("status") != "completed" or not status.get("result_file"):
        raise HTTPException(status_code=400, detail="Audio is not ready.")
    audio_path = os.path.join(job_dir, status["result_file"])
    if not os.path.exists(audio_path):
        raise HTTPException(status_code=404, detail="Audio result not found.")
    return FileResponse(path=audio_path, media_type="audio/wav", filename="voice.wav")


@app.get("/job/{job_id}/transcript")
def get_transcript(job_id: str):
    status = _load_existing_status(job_id)
    return {
        "job_id": job_id,
        "transcript": status.get("transcript_data"),
        "approved": status.get("transcript_approved", False),
    }


@app.put("/job/{job_id}/transcript")
def update_transcript(job_id: str, request: TextUpdateRequest):
    if is_modal_job_backend():
        try:
            _modal_update_text(
                job_id,
                "transcript_data",
                request.content,
                approval_field="transcript_approved",
                artifact_name="transcript.txt",
            )
            return {"success": True, "message": "Transcript updated."}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Error updating transcript: {exc}") from exc
    job_dir = _existing_job_dir(job_id)
    try:
        update_transcript_data(job_dir, request.content)
        return {"success": True, "message": "Transcript updated."}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error updating transcript: {exc}") from exc


@app.post("/job/{job_id}/transcript/approve")
def approve_transcript_endpoint(job_id: str):
    if is_modal_job_backend():
        try:
            modal_approve_field(job_id, "transcript_approved", "Transcript approved by user.")
            return {"success": True, "message": "Transcript approved."}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Error approving transcript: {exc}") from exc
    job_dir = _existing_job_dir(job_id)
    try:
        approve_transcript(job_dir)
        return {"success": True, "message": "Transcript approved."}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error approving transcript: {exc}") from exc


@app.get("/job/{job_id}/script")
def get_script(job_id: str):
    status = _load_existing_status(job_id)
    return {
        "job_id": job_id,
        "script": status.get("script_data"),
        "approved": status.get("script_approved", False),
    }


@app.put("/job/{job_id}/script")
def update_script(job_id: str, request: TextUpdateRequest):
    if is_modal_job_backend():
        try:
            _modal_update_text(
                job_id,
                "script_data",
                request.content,
                approval_field="script_approved",
                artifact_name="script.txt",
            )
            return {"success": True, "message": "Script updated."}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Error updating script: {exc}") from exc
    job_dir = _existing_job_dir(job_id)
    try:
        update_script_data(job_dir, request.content)
        return {"success": True, "message": "Script updated."}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error updating script: {exc}") from exc


@app.post("/job/{job_id}/script/approve")
def approve_script_endpoint(job_id: str):
    if is_modal_job_backend():
        try:
            status = _modal_status(job_id)
            if not str(status.get("script_data") or "").strip():
                raise HTTPException(status_code=400, detail="Manual script is empty. Paste script content before approving.")
            spawn_status = str(status.get("step_3_spawn_status") or "idle")
            existing_call_id = status.get("step_3_modal_call_id")
            if spawn_status in {"queued", "running"}:
                return {
                    "success": True,
                    "message": "Step 3 is already running.",
                    "modal_call_id": existing_call_id,
                }
            if spawn_status == "completed":
                return {
                    "success": True,
                    "message": "Step 3 has already completed for the approved script.",
                    "modal_call_id": existing_call_id,
                }
            modal_approve_field(job_id, "script_approved", "Script approved by user.")
            _modal_spawn_step_3(job_id)
            refreshed = _modal_status(job_id)
            return {
                "success": True,
                "message": "Script approved.",
                "modal_call_id": refreshed.get("step_3_modal_call_id"),
            }
        except HTTPException:
            raise
        except RuntimeError as exc:
            if "step_3 already" in str(exc):
                refreshed = _modal_status(job_id)
                return {
                    "success": True,
                    "message": "Step 3 is already queued or running.",
                    "modal_call_id": refreshed.get("step_3_modal_call_id"),
                }
            raise HTTPException(status_code=500, detail=f"Error approving script: {exc}") from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Error approving script: {exc}") from exc
    job_dir = _existing_job_dir(job_id)
    try:
        approve_script(job_dir)
        return {"success": True, "message": "Script approved."}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error approving script: {exc}") from exc


@app.get("/job/{job_id}/audio")
def get_audio_preview(job_id: str):
    status = _load_existing_status(job_id)
    return {
        "job_id": job_id,
        "audio_preview": status.get("audio_preview"),
        "approved": status.get("voice_approved", False),
    }


@app.get("/job/{job_id}/audio/file")
def get_audio_file(job_id: str):
    if is_modal_job_backend():
        try:
            audio_bytes = modal_read_bytes(job_id, "voice.wav")
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Audio preview is not available.") from exc
        return Response(content=audio_bytes, media_type="audio/wav", headers={"Content-Disposition": 'inline; filename="voice.wav"'})
    job_dir = _existing_job_dir(job_id)
    audio_path = os.path.join(job_dir, "voice.wav")
    if not os.path.exists(audio_path):
        raise HTTPException(status_code=404, detail="Audio preview is not available.")
    return FileResponse(path=audio_path, media_type="audio/wav", filename="voice.wav")


@app.post("/job/{job_id}/audio/approve")
def approve_audio_endpoint(job_id: str):
    if is_modal_job_backend():
        try:
            modal_approve_field(job_id, "voice_approved", "Voice preview approved by user.")
            return {"success": True, "message": "Audio preview approved."}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Error approving audio preview: {exc}") from exc
    job_dir = _existing_job_dir(job_id)
    try:
        approve_voice(job_dir)
        return {"success": True, "message": "Audio preview approved."}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error approving audio preview: {exc}") from exc


def _existing_job_dir(job_id: str) -> str:
    if is_modal_job_backend():
        if not modal_job_exists(job_id):
            raise HTTPException(status_code=404, detail="Job does not exist.")
        return job_id
    job_dir = get_job_dir(job_id)
    if not os.path.exists(job_dir):
        raise HTTPException(status_code=404, detail="Job does not exist.")
    return job_dir


def _load_existing_status(job_id: str) -> dict:
    if is_modal_job_backend():
        return _modal_status(job_id)
    job_dir = _existing_job_dir(job_id)
    status = load_job_status(job_dir)
    if not status:
        raise HTTPException(status_code=404, detail="Job status not found.")
    return status


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    reload = os.getenv("UVICORN_RELOAD", "1").strip().lower() not in {"0", "false", "no"}

    uvicorn.run("main:app", host=host, port=port, reload=reload)
