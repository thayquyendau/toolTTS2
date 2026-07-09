import json
import os
import re
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WORKER_DIR = Path(__file__).resolve().parent
TOKEN_LOG_FILENAME = "token.log"
TOKEN_STATE_FILENAME = "token.json"

_TOKEN_JOBS: dict[str, dict[str, Any]] = {}
_TOKEN_LOCK = threading.RLock()
_TOKEN_EXECUTOR = ThreadPoolExecutor(max_workers=1)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONLEGACYWINDOWSSTDIO", "0")
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("LC_ALL", "C.UTF-8")
    return env


def _token_root_dir() -> Path:
    token_dir = os.getenv("TOOLTUCODE_TOKEN_DIR")
    if token_dir:
        return Path(token_dir)
    return WORKER_DIR / "modal_tokens"


def _token_job_dir(job_id: str) -> Path:
    return _token_root_dir() / job_id


def _token_state_path(job_id: str) -> Path:
    return _token_job_dir(job_id) / TOKEN_STATE_FILENAME


def _token_log_path(job_id: str) -> Path:
    return _token_job_dir(job_id) / TOKEN_LOG_FILENAME


def _ensure_token_job_dir(job_id: str) -> Path:
    job_dir = _token_job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir


def _persist_job_state(job: dict[str, Any]) -> None:
    job_id = job["job_id"]
    _ensure_token_job_dir(job_id)
    state = {key: value for key, value in job.items() if key != "logs"}
    state_path = _token_state_path(job_id)
    tmp_path = state_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(state_path)


def _append_log(job: dict[str, Any], message: str) -> None:
    timestamp = _now_iso()
    line = f"[{timestamp}] {message}"
    job.setdefault("logs", []).append(line)
    job["updated_at"] = timestamp
    _ensure_token_job_dir(job["job_id"])
    with _token_log_path(job["job_id"]).open("a", encoding="utf-8") as file_obj:
        file_obj.write(f"{line}\n")
    _persist_job_state(job)


def _iter_process_output(stream: Any, chunk_size: int = 256):
    buffer = ""
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        buffer += chunk
        parts = re.split(r"[\r\n]+", buffer)
        buffer = parts.pop() if parts else ""
        for part in parts:
            line = part.strip()
            if line:
                yield line
    tail = buffer.strip()
    if tail:
        yield tail


def _load_persisted_job(job_id: str) -> dict[str, Any]:
    state_path = _token_state_path(job_id)
    if not state_path.exists():
        raise KeyError(job_id)
    job = json.loads(state_path.read_text(encoding="utf-8"))
    log_path = _token_log_path(job_id)
    job["logs"] = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
    return job


def _snapshot_job(job_id: str) -> dict[str, Any]:
    with _TOKEN_LOCK:
        job = _TOKEN_JOBS.get(job_id)
        if job is not None:
            return {key: (list(value) if key == "logs" else value) for key, value in job.items()}
    job = _load_persisted_job(job_id)
    return {key: (list(value) if key == "logs" else value) for key, value in job.items()}


def _set_job_fields(job_id: str, **fields: Any) -> dict[str, Any]:
    with _TOKEN_LOCK:
        job = _TOKEN_JOBS[job_id]
        job.update(fields)
        job["updated_at"] = _now_iso()
        _persist_job_state(job)
        return job


def _sanitize_profile_name(profile: str | None) -> str:
    value = str(profile or os.getenv("MODAL_PROFILE", "default")).strip()
    return value or "default"


def get_modal_token_job(job_id: str) -> dict[str, Any]:
    return _snapshot_job(job_id)


def start_modal_token_new(profile: str | None, activate: bool = True, verify: bool = True) -> dict[str, Any]:
    normalized_profile = _sanitize_profile_name(profile)
    job_id = f"token-{uuid.uuid4().hex[:10]}"
    job_dir = _ensure_token_job_dir(job_id)
    log_path = _token_log_path(job_id)
    log_path.touch(exist_ok=True)
    job = {
        "job_id": job_id,
        "job_dir": str(job_dir),
        "log_path": str(log_path),
        "profile": normalized_profile,
        "activate": bool(activate),
        "verify": bool(verify),
        "status": "pending",
        "message": "Token flow queued.",
        "exit_code": None,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "logs": [],
    }
    with _TOKEN_LOCK:
        _TOKEN_JOBS[job_id] = job
        _persist_job_state(job)
    _append_log(job, f"Queued Modal token flow for profile={normalized_profile}.")
    _TOKEN_EXECUTOR.submit(_run_modal_token_new, job_id)
    return _snapshot_job(job_id)


def _run_modal_token_new(job_id: str) -> None:
    job: dict[str, Any] | None = None
    try:
        _set_job_fields(job_id, status="running", message="Token flow running.")
        with _TOKEN_LOCK:
            job = _TOKEN_JOBS[job_id]
            cmd = [
                sys.executable,
                "-X",
                "utf8",
                "-m",
                "modal",
                "token",
                "new",
                "--profile",
                job["profile"],
                "--activate" if job["activate"] else "--no-activate",
                "--verify" if job["verify"] else "--no-verify",
            ]
        _append_log(job, f"Running command: {' '.join(cmd)}")
        process = subprocess.Popen(
            cmd,
            cwd=str(WORKER_DIR),
            env=_subprocess_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        output_chunks: list[str] = []
        assert process.stdout is not None
        for line in _iter_process_output(process.stdout):
            output_chunks.append(line)
            _append_log(job, line)
        exit_code = process.wait()
        output_text = "\n".join(output_chunks)
        with _TOKEN_LOCK:
            job = _TOKEN_JOBS[job_id]
            job["exit_code"] = exit_code
            if exit_code == 0:
                job["status"] = "success"
                job["message"] = "Token flow completed successfully."
                job["error_detail"] = None
            else:
                job["status"] = "failed"
                job["message"] = "Modal token flow failed."
                job["error_detail"] = output_text[-4000:] or f"modal token new exited with code {exit_code}"
            job["updated_at"] = _now_iso()
            _persist_job_state(job)
    except Exception as exc:
        if job is None:
            with _TOKEN_LOCK:
                job = _TOKEN_JOBS.get(job_id)
        if job is None:
            return
        with _TOKEN_LOCK:
            job["status"] = "failed"
            job["message"] = "Modal token flow crashed."
            job["error_detail"] = str(exc)
            job["updated_at"] = _now_iso()
            _persist_job_state(job)
        _append_log(job, f"Token flow error: {exc}")
