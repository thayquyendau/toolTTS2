import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional


STEP_CONFIG: List[Dict[str, str]] = [
    {"key": "step_1_get_transcript", "name": "Step 1 - Transcript"},
    {"key": "step_2_manual_script", "name": "Step 2 - Manual Script"},
    {"key": "step_3_coqui_tts", "name": "Step 3 - Coqui TTS"},
]

STATUS_FILENAME = "status.json"
LOG_FILENAME = "job.log"


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def job_status_path(job_dir: str) -> str:
    return os.path.join(job_dir, STATUS_FILENAME)


def job_log_path(job_dir: str) -> str:
    return os.path.join(job_dir, LOG_FILENAME)


def _create_step_entries() -> List[Dict[str, Any]]:
    return [
        {
            "key": step["key"],
            "name": step["name"],
            "status": "pending",
            "message": "Waiting to start.",
            "started_at": None,
            "ended_at": None,
            "detail": None,
        }
        for step in STEP_CONFIG
    ]


def save_job_status(job_dir: str, status_data: Dict[str, Any]) -> None:
    with open(job_status_path(job_dir), "w", encoding="utf-8") as file_obj:
        json.dump(status_data, file_obj, indent=2, ensure_ascii=False)


def load_job_status(job_dir: str) -> Optional[Dict[str, Any]]:
    path = job_status_path(job_dir)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as file_obj:
        return json.load(file_obj)


def append_job_log(job_dir: str, message: str) -> None:
    os.makedirs(job_dir, exist_ok=True)
    with open(job_log_path(job_dir), "a", encoding="utf-8") as file_obj:
        file_obj.write(f"[{_now_iso()}] {message}\n")


def _base_modal_usage() -> dict[str, Any]:
    return {
        "calls": [],
        "summary": {
            "call_count": 0,
            "total_elapsed_seconds": 0.0,
            "steps": {},
        },
    }


def init_job_status(job_dir: str, job_id: str) -> Dict[str, Any]:
    status_data = {
        "job_id": job_id,
        "status": "pending",
        "message": "Job created and waiting for execution.",
        "current_step": None,
        "result_file": None,
        "error_detail": None,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "steps": _create_step_entries(),
        "transcript_data": None,
        "transcript_approved": False,
        "script_data": None,
        "script_approved": False,
        "audio_preview": None,
        "voice_approved": False,
        "render_config": None,
        "segments": [],
        "modal_usage": _base_modal_usage(),
    }
    save_job_status(job_dir, status_data)
    append_job_log(job_dir, "Job initialized.")
    return status_data


def _find_step(status_data: Dict[str, Any], step_key: str) -> Dict[str, Any]:
    for step in status_data.get("steps", []):
        if step.get("key") == step_key:
            return step
    raise KeyError(f"Step key '{step_key}' does not exist in status.json")


def set_render_config(job_dir: str, render_config: Dict[str, Any]) -> None:
    status_data = load_job_status(job_dir)
    if status_data is None:
        raise FileNotFoundError(f"Status file not found: {job_dir}")
    status_data["render_config"] = render_config
    status_data["updated_at"] = _now_iso()
    save_job_status(job_dir, status_data)
    append_job_log(job_dir, f"Render config: {json.dumps(render_config, ensure_ascii=False)}")


def init_segments(job_dir: str, segment_count: int) -> None:
    status_data = load_job_status(job_dir)
    if status_data is None:
        raise FileNotFoundError(f"Status file not found: {job_dir}")
    status_data["segments"] = [
        {
            "index": index,
            "name": f"segment_{index:03d}",
            "status": "pending",
            "phase": "Waiting",
            "started_at": None,
            "ended_at": None,
            "duration_seconds": None,
            "files": {},
            "message": "Waiting to start.",
        }
        for index in range(segment_count)
    ]
    status_data["updated_at"] = _now_iso()
    save_job_status(job_dir, status_data)
    append_job_log(job_dir, f"Initialized {segment_count} TTS segment(s).")


def update_segment_status(
    job_dir: str,
    segment_index: int,
    status: Optional[str] = None,
    phase: Optional[str] = None,
    message: Optional[str] = None,
    duration_seconds: Optional[float] = None,
    files: Optional[Dict[str, str]] = None,
) -> None:
    status_data = load_job_status(job_dir)
    if status_data is None:
        raise FileNotFoundError(f"Status file not found: {job_dir}")
    segments = status_data.setdefault("segments", [])
    if segment_index < 0 or segment_index >= len(segments):
        raise IndexError(f"Segment index out of range: {segment_index}")

    segment = segments[segment_index]
    if status is not None:
        segment["status"] = status
        if status == "running" and not segment.get("started_at"):
            segment["started_at"] = _now_iso()
        if status in {"success", "failure"}:
            segment["ended_at"] = _now_iso()
    if phase is not None:
        segment["phase"] = phase
    if message is not None:
        segment["message"] = message
    if duration_seconds is not None:
        segment["duration_seconds"] = duration_seconds
    if files:
        segment.setdefault("files", {}).update(files)

    status_data["updated_at"] = _now_iso()
    save_job_status(job_dir, status_data)
    append_job_log(
        job_dir,
        f"Segment {segment_index + 1}/{len(segments)} -> "
        f"{segment.get('status', '').upper()}: {segment.get('phase', '')}"
        f"{' - ' + message if message else ''}",
    )


def update_step_status(
    job_dir: str,
    step_key: str,
    status: str,
    message: Optional[str] = None,
    detail: Optional[str] = None,
) -> None:
    status_data = load_job_status(job_dir)
    if status_data is None:
        raise FileNotFoundError(f"Status file not found: {job_dir}")

    step = _find_step(status_data, step_key)
    step["status"] = status
    if message is not None:
        step["message"] = message
    if detail is not None:
        step["detail"] = detail
    if status == "running":
        if not step.get("started_at"):
            step["started_at"] = _now_iso()
        status_data["current_step"] = step_key
        status_data["status"] = "running"
        status_data["message"] = message or f"Running {step['name']}."
    elif status == "waiting_approval":
        if not step.get("started_at"):
            step["started_at"] = _now_iso()
        status_data["current_step"] = step_key
        status_data["status"] = "waiting_approval"
        status_data["message"] = message or f"Waiting for user approval: {step['name']}."
    elif status in {"success", "failure"}:
        step["ended_at"] = _now_iso()
        status_data["message"] = message or step["message"]
        if status == "failure":
            status_data["status"] = "failed"
            status_data["error_detail"] = detail or message

    status_data["updated_at"] = _now_iso()
    save_job_status(job_dir, status_data)
    append_job_log(job_dir, f"{step['name']} -> {status.upper()}: {message or ''}".strip())


def set_overall_status(
    job_dir: str,
    status: str,
    message: Optional[str] = None,
    result_file: Optional[str] = None,
    error_detail: Optional[str] = None,
) -> None:
    status_data = load_job_status(job_dir)
    if status_data is None:
        raise FileNotFoundError(f"Status file not found: {job_dir}")

    status_data["status"] = status
    if message is not None:
        status_data["message"] = message
    if result_file is not None:
        status_data["result_file"] = result_file
    if error_detail is not None:
        status_data["error_detail"] = error_detail
    status_data["updated_at"] = _now_iso()
    save_job_status(job_dir, status_data)
    append_job_log(job_dir, f"JOB -> {status.upper()}: {message or ''}".strip())


def update_transcript_data(job_dir: str, transcript_text: str) -> None:
    status_data = load_job_status(job_dir)
    if status_data is None:
        raise FileNotFoundError(f"Status file not found: {job_dir}")
    status_data["transcript_data"] = transcript_text
    status_data["transcript_approved"] = False
    status_data["updated_at"] = _now_iso()
    save_job_status(job_dir, status_data)
    append_job_log(job_dir, "Transcript data updated.")


def approve_transcript(job_dir: str) -> None:
    status_data = load_job_status(job_dir)
    if status_data is None:
        raise FileNotFoundError(f"Status file not found: {job_dir}")
    status_data["transcript_approved"] = True
    status_data["updated_at"] = _now_iso()
    save_job_status(job_dir, status_data)
    append_job_log(job_dir, "Transcript approved by user.")


def update_script_data(job_dir: str, script_text: str) -> None:
    status_data = load_job_status(job_dir)
    if status_data is None:
        raise FileNotFoundError(f"Status file not found: {job_dir}")
    status_data["script_data"] = script_text
    status_data["script_approved"] = False
    status_data["updated_at"] = _now_iso()
    save_job_status(job_dir, status_data)
    append_job_log(job_dir, "Script data updated.")


def approve_script(job_dir: str) -> None:
    status_data = load_job_status(job_dir)
    if status_data is None:
        raise FileNotFoundError(f"Status file not found: {job_dir}")
    status_data["script_approved"] = True
    status_data["updated_at"] = _now_iso()
    save_job_status(job_dir, status_data)
    append_job_log(job_dir, "Script approved by user.")


def update_audio_preview(job_dir: str, audio_preview: Dict[str, Any]) -> None:
    status_data = load_job_status(job_dir)
    if status_data is None:
        raise FileNotFoundError(f"Status file not found: {job_dir}")
    status_data["audio_preview"] = audio_preview
    status_data["voice_approved"] = False
    status_data["updated_at"] = _now_iso()
    save_job_status(job_dir, status_data)
    append_job_log(job_dir, "Audio preview updated.")


def approve_voice(job_dir: str) -> None:
    status_data = load_job_status(job_dir)
    if status_data is None:
        raise FileNotFoundError(f"Status file not found: {job_dir}")
    status_data["voice_approved"] = True
    status_data["updated_at"] = _now_iso()
    save_job_status(job_dir, status_data)
    append_job_log(job_dir, "Voice preview approved by user.")


def wait_for_approval(
    job_dir: str,
    approval_key: str,
    step_key: str,
    message: str,
    poll_seconds: float = 1.0,
) -> Dict[str, Any]:
    update_step_status(job_dir, step_key, "waiting_approval", message)
    append_job_log(job_dir, f"Waiting for approval: {approval_key}")
    while True:
        status_data = load_job_status(job_dir)
        if status_data is None:
            raise FileNotFoundError(f"Status file not found: {job_dir}")
        if bool(status_data.get(approval_key, False)):
            append_job_log(job_dir, f"Approval received: {approval_key}")
            return status_data
        time.sleep(poll_seconds)
