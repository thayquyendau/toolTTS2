import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
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
    set_overall_status,
    update_script_data,
    update_transcript_data,
)
from modal_auth import get_modal_token_job, start_modal_token_new
from modal_deploy import get_modal_deploy_job, start_modal_deploy
from pipeline_steps.common import ProcessingError
from pipeline_steps.orchestrator import run_tts_pipeline
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
def generate_tts(
    youtube_url: str = Form(..., description="YouTube URL used to fetch transcript."),
    voice_sample: UploadFile = File(..., description="Voice sample file, mp3 or wav."),
    xtts_segment_max_chars: int = Form(250),
    xtts_segment_min_chars: int = Form(80),
    gpu_backend: str = Form("modal"),
    modal_app_name: str = Form("tooltucode-gpu-v1"),
    modal_tts_gpu: str = Form("L4"),
    tts_concurrency: int = Form(4),
    tts_parallel_backend: str = Form("process"),
    modal_xtts_dispatch: str = Form("spawn"),
    modal_xtts_artifact_volume: str = Form("tooltucode-xtts-artifacts"),
    modal_xtts_artifact_prefix: str = Form("xtts-jobs"),
    modal_xtts_download_workers: int = Form(8),
):
    job_dir = create_job_dir()
    job_id = os.path.basename(job_dir)
    init_job_status(job_dir, job_id)

    voice_ext = os.path.splitext(voice_sample.filename or "")[1] or ".mp3"
    voice_path = os.path.join(job_dir, f"input_voice{voice_ext}")
    render_config = {
        "xtts_segment_max_chars": xtts_segment_max_chars,
        "xtts_segment_min_chars": xtts_segment_min_chars,
        "gpu_backend": gpu_backend,
        "modal_app_name": modal_app_name,
        "modal_tts_gpu": modal_tts_gpu,
        "tts_concurrency": tts_concurrency,
        "tts_parallel_backend": tts_parallel_backend,
        "modal_xtts_dispatch": modal_xtts_dispatch,
        "modal_xtts_artifact_volume": modal_xtts_artifact_volume,
        "modal_xtts_artifact_prefix": modal_xtts_artifact_prefix,
        "modal_xtts_download_workers": modal_xtts_download_workers,
    }

    try:
        append_job_log(job_dir, "Saving uploaded voice sample.")
        save_upload_file(voice_sample, voice_path)
        set_overall_status(job_dir, "pending", "Files received. Preparing TTS pipeline.")
        app.state.executor.submit(run_tts_pipeline_job, job_dir, youtube_url, voice_path, render_config)
        return {
            "job_id": job_id,
            "status_url": f"/job/{job_id}",
            "logs_url": f"/job/{job_id}/logs",
        }
    except Exception as exc:
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


@app.get("/job/{job_id}")
def get_job_status(job_id: str):
    status = _load_existing_status(job_id)
    return status


@app.get("/job/{job_id}/logs")
def get_job_logs(job_id: str):
    job_dir = _existing_job_dir(job_id)
    log_path = job_log_path(job_dir)
    if not os.path.exists(log_path):
        raise HTTPException(status_code=404, detail="Job log not found.")
    return FileResponse(path=log_path, media_type="text/plain", filename="job.log")


@app.get("/job/{job_id}/result")
def get_job_result(job_id: str):
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
    job_dir = _existing_job_dir(job_id)
    try:
        update_transcript_data(job_dir, request.content)
        return {"success": True, "message": "Transcript updated."}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error updating transcript: {exc}") from exc


@app.post("/job/{job_id}/transcript/approve")
def approve_transcript_endpoint(job_id: str):
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
    job_dir = _existing_job_dir(job_id)
    try:
        update_script_data(job_dir, request.content)
        return {"success": True, "message": "Script updated."}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error updating script: {exc}") from exc


@app.post("/job/{job_id}/script/approve")
def approve_script_endpoint(job_id: str):
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
    job_dir = _existing_job_dir(job_id)
    audio_path = os.path.join(job_dir, "voice.wav")
    if not os.path.exists(audio_path):
        raise HTTPException(status_code=404, detail="Audio preview is not available.")
    return FileResponse(path=audio_path, media_type="audio/wav", filename="voice.wav")


@app.post("/job/{job_id}/audio/approve")
def approve_audio_endpoint(job_id: str):
    job_dir = _existing_job_dir(job_id)
    try:
        approve_voice(job_dir)
        return {"success": True, "message": "Audio preview approved."}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error approving audio preview: {exc}") from exc


def _existing_job_dir(job_id: str) -> str:
    job_dir = get_job_dir(job_id)
    if not os.path.exists(job_dir):
        raise HTTPException(status_code=404, detail="Job does not exist.")
    return job_dir


def _load_existing_status(job_id: str) -> dict:
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
