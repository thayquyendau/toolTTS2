import io
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any

import modal

from job_status import (
    LOG_FILENAME,
    STATUS_FILENAME,
    append_job_log,
    build_initial_job_status,
)


JOB_BACKEND_MODAL = "modal"
JOB_BACKEND_LOCAL = "local"
JOB_VOLUME_NAME = os.getenv("MODAL_TTS_JOB_VOLUME", "tooltucode-tts-jobs")
JOB_ROOT_PREFIX = os.getenv("MODAL_TTS_JOB_ROOT", "jobs").strip("/ ") or "jobs"

_job_volume = None


def job_backend() -> str:
    configured = str(os.getenv("TTS_JOB_BACKEND", "")).strip().lower()
    if configured in {JOB_BACKEND_MODAL, JOB_BACKEND_LOCAL}:
        return configured
    if str(os.getenv("VERCEL", "")).strip() == "1":
        return JOB_BACKEND_MODAL
    return JOB_BACKEND_LOCAL


def is_modal_job_backend() -> bool:
    return job_backend() == JOB_BACKEND_MODAL


def _volume():
    global _job_volume
    if _job_volume is None:
        _job_volume = modal.Volume.from_name(JOB_VOLUME_NAME, create_if_missing=True, version=2)
    return _job_volume


def create_modal_job_id() -> str:
    return f"video-{uuid.uuid4().hex[:10]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _job_prefix(job_id: str) -> str:
    return f"{JOB_ROOT_PREFIX}/{job_id}"


def job_remote_path(job_id: str, relative_path: str) -> str:
    return f"{_job_prefix(job_id)}/{relative_path.lstrip('/')}"


def _put_bytes(remote_path: str, data: bytes) -> None:
    with _volume().batch_upload() as batch:
        batch.put_file(io.BytesIO(data), remote_path)


def put_text(job_id: str, relative_path: str, text: str) -> None:
    _put_bytes(job_remote_path(job_id, relative_path), text.encode("utf-8"))


def put_json(job_id: str, relative_path: str, payload: Any) -> None:
    put_text(job_id, relative_path, json.dumps(payload, indent=2, ensure_ascii=False))


def upload_local_file(job_id: str, local_path: str, relative_path: str) -> None:
    with _volume().batch_upload() as batch:
        batch.put_file(local_path, job_remote_path(job_id, relative_path))


def read_bytes(job_id: str, relative_path: str) -> bytes:
    try:
        return b"".join(_volume().read_file(job_remote_path(job_id, relative_path)))
    except Exception as exc:
        raise FileNotFoundError(job_remote_path(job_id, relative_path)) from exc


def read_text(job_id: str, relative_path: str) -> str:
    return read_bytes(job_id, relative_path).decode("utf-8")


def read_json(job_id: str, relative_path: str) -> dict[str, Any]:
    return json.loads(read_text(job_id, relative_path))


def job_exists(job_id: str) -> bool:
    try:
        read_bytes(job_id, STATUS_FILENAME)
        return True
    except FileNotFoundError:
        return False


def load_status(job_id: str) -> dict[str, Any]:
    return read_json(job_id, STATUS_FILENAME)


def save_status(job_id: str, status_data: dict[str, Any]) -> None:
    put_json(job_id, STATUS_FILENAME, status_data)


def append_log(job_id: str, message: str) -> None:
    try:
        existing = read_text(job_id, LOG_FILENAME)
    except FileNotFoundError:
        existing = ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    local_dir = tempfile.mkdtemp(prefix="tooltucode_modal_log_")
    local_path = os.path.join(local_dir, LOG_FILENAME)
    try:
        with open(local_path, "w", encoding="utf-8") as file_obj:
            file_obj.write(existing)
        append_job_log(local_dir, message)
        upload_local_file(job_id, local_path, LOG_FILENAME)
    finally:
        try:
            os.remove(local_path)
            os.rmdir(local_dir)
        except Exception:
            pass


def init_modal_job(job_id: str, render_config: dict[str, Any] | None = None) -> dict[str, Any]:
    status_data = build_initial_job_status(job_id)
    if render_config is not None:
        status_data["render_config"] = render_config
    save_status(job_id, status_data)
    append_log(job_id, "Job initialized.")
    return status_data


def set_job_input(job_id: str, input_data: dict[str, Any]) -> dict[str, Any]:
    def mutate(status_data: dict[str, Any]) -> None:
        status_data["input"] = input_data

    status = update_status(job_id, mutate)
    append_log(job_id, "Job input registered.")
    return status


def update_status(job_id: str, mutator) -> dict[str, Any]:
    status_data = load_status(job_id)
    mutator(status_data)
    status_data["updated_at"] = _now_iso()
    save_status(job_id, status_data)
    return status_data


def update_text_field(job_id: str, field: str, value: str, approval_field: str | None = None) -> dict[str, Any]:
    def mutate(status_data: dict[str, Any]) -> None:
        status_data[field] = value
        if approval_field:
            status_data[approval_field] = False
        if field == "script_data":
            status_data["audio_preview"] = None
            status_data["voice_approved"] = False
            status_data["result_file"] = None
            status_data["step_3_spawn_status"] = "idle"
            status_data["step_3_modal_call_id"] = None
            status_data["step_3_spawned_at"] = None
            status_data["step_3_completed_at"] = None
    status = update_status(job_id, mutate)
    append_log(job_id, f"{field} updated via API.")
    return status


def approve_field(job_id: str, field: str, log_message: str) -> dict[str, Any]:
    def mutate(status_data: dict[str, Any]) -> None:
        status_data[field] = True
    status = update_status(job_id, mutate)
    append_log(job_id, log_message)
    return status


def spawn_modal_job_function(app_name: str, function_name: str, *args):
    fn = modal.Function.from_name(app_name, function_name)
    return fn.spawn(*args)


def prepare_step_1_spawn(job_id: str) -> dict[str, Any]:
    def mutate(status_data: dict[str, Any]) -> None:
        current = str(status_data.get("step_1_spawn_status") or "idle")
        if current in {"queued", "running", "completed"}:
            raise RuntimeError(f"step_1 already {current}")
        status_data["step_1_spawn_status"] = "queued"
        status_data["step_1_spawned_at"] = _now_iso()
        status_data["step_1_modal_call_id"] = None
        status_data["step_1_completed_at"] = None
        status_data["status"] = "queued"
        status_data["message"] = "Job queued for Modal processing."

    status = update_status(job_id, mutate)
    append_log(job_id, "Step 1 dispatch prepared.")
    return status


def record_step_1_call(job_id: str, call_id: str) -> dict[str, Any]:
    def mutate(status_data: dict[str, Any]) -> None:
        if status_data.get("step_1_spawn_status") not in {"completed", "failed"}:
            status_data["step_1_spawn_status"] = "running"
        status_data["step_1_modal_call_id"] = call_id

    status = update_status(job_id, mutate)
    append_log(job_id, f"Step 1 Modal call registered: {call_id}")
    return status


def fail_step_1_spawn(job_id: str, detail: str) -> dict[str, Any]:
    def mutate(status_data: dict[str, Any]) -> None:
        status_data["step_1_spawn_status"] = "failed"
        status_data["step_1_completed_at"] = _now_iso()
        status_data["status"] = "failed"
        status_data["message"] = "Could not prepare or dispatch the job to Modal."
        status_data["error_detail"] = detail

    status = update_status(job_id, mutate)
    append_log(job_id, f"Step 1 dispatch failed: {detail}")
    return status


def prepare_step_3_spawn(job_id: str) -> dict[str, Any]:
    def mutate(status_data: dict[str, Any]) -> None:
        current = str(status_data.get("step_3_spawn_status") or "idle")
        if current in {"queued", "running", "completed"}:
            raise RuntimeError(f"step_3 already {current}")
        status_data["step_3_spawn_status"] = "queued"
        status_data["step_3_spawned_at"] = _now_iso()
        status_data["step_3_modal_call_id"] = None
        status_data["step_3_completed_at"] = None
    status = update_status(job_id, mutate)
    append_log(job_id, "Step 3 spawn prepared.")
    return status


def record_step_3_call(job_id: str, call_id: str) -> dict[str, Any]:
    def mutate(status_data: dict[str, Any]) -> None:
        if status_data.get("step_3_spawn_status") not in {"completed", "failed"}:
            status_data["step_3_spawn_status"] = "running"
        status_data["step_3_modal_call_id"] = call_id
    status = update_status(job_id, mutate)
    append_log(job_id, f"Step 3 Modal call registered: {call_id}")
    return status


def fail_step_3_spawn(job_id: str, detail: str) -> dict[str, Any]:
    def mutate(status_data: dict[str, Any]) -> None:
        status_data["step_3_spawn_status"] = "failed"
        status_data["step_3_completed_at"] = _now_iso()
        status_data["error_detail"] = detail
    status = update_status(job_id, mutate)
    append_log(job_id, f"Step 3 spawn failed: {detail}")
    return status
