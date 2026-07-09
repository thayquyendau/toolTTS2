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

from pipeline_steps.common import resolve_modal_app_names


WORKER_DIR = Path(__file__).resolve().parent
DEPLOY_SOURCE = WORKER_DIR / "modal_apps" / "tooltucode_gpu.py"
DEPLOY_TARGETS = {
    "step_3": "tooltucode-gpu-v1-step3",
}
DEPLOY_TARGET_LABELS = {
    "step_3": "Deploy Step 3 only",
}
DEPLOY_TARGET_HELP = {
    "step_3": "Redeploy the XTTS app name for Step 3.",
}

DEPLOY_LOG_FILENAME = "deploy.log"
DEPLOY_STATE_FILENAME = "deploy.json"

_DEPLOYMENT_JOBS: dict[str, dict[str, Any]] = {}
_DEPLOYMENT_LOCK = threading.RLock()
_DEPLOYMENT_EXECUTOR = ThreadPoolExecutor(max_workers=1)


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


def _sanitize_base_app_name(base_app_name: str | None) -> str:
    value = str(base_app_name or os.getenv("MODAL_APP_NAME", "tooltucode-gpu-v1")).strip()
    return value or "tooltucode-gpu-v1"


def _deploy_root_dir() -> Path:
    deploy_dir = os.getenv("TOOLTUCODE_DEPLOY_DIR")
    if deploy_dir:
        return Path(deploy_dir)
    return WORKER_DIR / "deploy"


def _deploy_job_dir(job_id: str) -> Path:
    return _deploy_root_dir() / job_id


def _deploy_state_path(job_id: str) -> Path:
    return _deploy_job_dir(job_id) / DEPLOY_STATE_FILENAME


def _deploy_log_path(job_id: str) -> Path:
    return _deploy_job_dir(job_id) / DEPLOY_LOG_FILENAME


def _ensure_deploy_job_dir(job_id: str) -> Path:
    job_dir = _deploy_job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir


def _persist_job_state(job: dict[str, Any]) -> None:
    job_id = job["job_id"]
    _ensure_deploy_job_dir(job_id)
    state = {key: value for key, value in job.items() if key != "logs"}
    state_path = _deploy_state_path(job_id)
    tmp_path = state_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(state_path)


def _append_log(job: dict[str, Any], message: str) -> None:
    timestamp = _now_iso()
    line = f"[{timestamp}] {message}"
    job.setdefault("logs", []).append(line)
    job["updated_at"] = timestamp
    _ensure_deploy_job_dir(job["job_id"])
    with _deploy_log_path(job["job_id"]).open("a", encoding="utf-8") as file_obj:
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
    state_path = _deploy_state_path(job_id)
    if not state_path.exists():
        raise KeyError(job_id)
    job = json.loads(state_path.read_text(encoding="utf-8"))
    log_path = _deploy_log_path(job_id)
    job["logs"] = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
    return job


def _snapshot_job(job_id: str) -> dict[str, Any]:
    with _DEPLOYMENT_LOCK:
        job = _DEPLOYMENT_JOBS.get(job_id)
        if job is not None:
            return {key: (list(value) if key == "logs" else value) for key, value in job.items()}
    job = _load_persisted_job(job_id)
    return {key: (list(value) if key == "logs" else value) for key, value in job.items()}


def _extract_deploy_url(output: str) -> str | None:
    match = re.search(r"https://modal\.com/apps/[^\s]+", output)
    return match.group(0) if match else None


def _set_job_fields(job_id: str, **fields: Any) -> dict[str, Any]:
    with _DEPLOYMENT_LOCK:
        job = _DEPLOYMENT_JOBS[job_id]
        job.update(fields)
        job["updated_at"] = _now_iso()
        _persist_job_state(job)
        return job


def resolve_deploy_target(target: str, base_app_name: str | None = None) -> tuple[str, str]:
    del target
    base_name = _sanitize_base_app_name(base_app_name)
    derived_names = resolve_modal_app_names(base_name)
    return "step_3", derived_names["modal_step_3_app_name"]


def get_modal_deploy_targets() -> list[dict[str, str]]:
    return [
        {
            "value": "step_3",
            "label": DEPLOY_TARGET_LABELS["step_3"],
            "help": DEPLOY_TARGET_HELP["step_3"],
            "default_app_name": DEPLOY_TARGETS["step_3"],
        }
    ]


def get_modal_deploy_job(job_id: str) -> dict[str, Any]:
    return _snapshot_job(job_id)


def start_modal_deploy(target: str, base_app_name: str | None = None, strategy: str = "rolling") -> dict[str, Any]:
    normalized_target, deploy_name = resolve_deploy_target(target, base_app_name)
    job_id = f"deploy-{uuid.uuid4().hex[:10]}"
    job_dir = _ensure_deploy_job_dir(job_id)
    log_path = _deploy_log_path(job_id)
    log_path.touch(exist_ok=True)
    job = {
        "job_id": job_id,
        "job_dir": str(job_dir),
        "log_path": str(log_path),
        "target": normalized_target,
        "target_label": DEPLOY_TARGET_LABELS[normalized_target],
        "base_app_name": _sanitize_base_app_name(base_app_name),
        "app_name": deploy_name,
        "source": str(DEPLOY_SOURCE),
        "strategy": strategy if strategy in {"rolling", "recreate"} else "rolling",
        "status": "pending",
        "message": "Deployment queued.",
        "exit_code": None,
        "deployment_url": None,
        "error_detail": None,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "logs": [],
    }
    with _DEPLOYMENT_LOCK:
        _DEPLOYMENT_JOBS[job_id] = job
        _persist_job_state(job)
    _append_log(job, f"Queued Modal deploy for target={normalized_target}, app={deploy_name}.")
    _DEPLOYMENT_EXECUTOR.submit(_run_modal_deploy, job_id)
    return _snapshot_job(job_id)


def _run_modal_deploy(job_id: str) -> None:
    job: dict[str, Any] | None = None
    try:
        _set_job_fields(job_id, status="running", message="Deployment running.")
        with _DEPLOYMENT_LOCK:
            job = _DEPLOYMENT_JOBS[job_id]
            cmd = [
                sys.executable,
                "-X",
                "utf8",
                "-m",
                "modal",
                "deploy",
                str(DEPLOY_SOURCE),
                "--name",
                job["app_name"],
                "--strategy",
                job["strategy"],
                "--stream-logs",
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
        deployment_url = _extract_deploy_url(output_text)
        with _DEPLOYMENT_LOCK:
            job = _DEPLOYMENT_JOBS[job_id]
            job["exit_code"] = exit_code
            job["deployment_url"] = deployment_url or job.get("deployment_url")
            if exit_code == 0:
                job["status"] = "success"
                job["message"] = (
                    f"Deployment completed successfully: {deployment_url}"
                    if deployment_url
                    else "Deployment completed successfully."
                )
                job["error_detail"] = None
            else:
                job["status"] = "failed"
                job["message"] = "Modal deploy failed."
                job["error_detail"] = output_text[-4000:] or f"modal deploy exited with code {exit_code}"
            job["updated_at"] = _now_iso()
            _persist_job_state(job)
    except Exception as exc:
        if job is None:
            with _DEPLOYMENT_LOCK:
                job = _DEPLOYMENT_JOBS.get(job_id)
        if job is None:
            return
        with _DEPLOYMENT_LOCK:
            job["status"] = "failed"
            job["message"] = "Modal deploy crashed."
            job["error_detail"] = str(exc)
            job["updated_at"] = _now_iso()
            _persist_job_state(job)
        _append_log(job, f"Deployment error: {exc}")
