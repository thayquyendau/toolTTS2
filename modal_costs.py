from datetime import datetime, timezone
from typing import Any

from job_status import load_job_status, save_job_status


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_modal_tracking_sections(status_data: dict[str, Any]) -> dict[str, Any]:
    modal_usage = status_data.setdefault(
        "modal_usage",
        {
            "calls": [],
            "summary": {
                "call_count": 0,
                "total_elapsed_seconds": 0.0,
                "steps": {},
            },
        },
    )
    modal_usage.setdefault("calls", [])
    modal_usage.setdefault("summary", {})
    modal_usage["summary"].setdefault("call_count", 0)
    modal_usage["summary"].setdefault("total_elapsed_seconds", 0.0)
    modal_usage["summary"].setdefault("steps", {})
    return status_data


def _rebuild_modal_usage_summary(calls: list[dict[str, Any]]) -> dict[str, Any]:
    steps: dict[str, dict[str, Any]] = {}
    total_elapsed_seconds = 0.0
    for call in calls:
        elapsed_seconds = float(call.get("elapsed_seconds") or 0.0)
        total_elapsed_seconds += elapsed_seconds
        step_key = str(call.get("step_key") or "unknown")
        step_summary = steps.setdefault(
            step_key,
            {
                "operation": call.get("operation"),
                "call_count": 0,
                "total_elapsed_seconds": 0.0,
                "gpus": [],
                "functions": [],
            },
        )
        step_summary["call_count"] += 1
        step_summary["total_elapsed_seconds"] = round(step_summary["total_elapsed_seconds"] + elapsed_seconds, 3)
        gpu = call.get("gpu")
        if gpu and gpu not in step_summary["gpus"]:
            step_summary["gpus"].append(gpu)
        function_name = call.get("function_name")
        if function_name and function_name not in step_summary["functions"]:
            step_summary["functions"].append(function_name)
    return {
        "call_count": len(calls),
        "total_elapsed_seconds": round(total_elapsed_seconds, 3),
        "steps": steps,
    }


def record_modal_call(job_dir: str, call_record: dict[str, Any]) -> dict[str, Any]:
    status_data = load_job_status(job_dir)
    if status_data is None:
        raise FileNotFoundError(f"Job status not found for modal call tracking: {job_dir}")
    ensure_modal_tracking_sections(status_data)
    calls = status_data["modal_usage"]["calls"]
    calls.append(call_record)
    status_data["modal_usage"]["summary"] = _rebuild_modal_usage_summary(calls)
    status_data["updated_at"] = _now_iso()
    save_job_status(job_dir, status_data)
    return call_record


def job_has_modal_usage(job_dir: str) -> bool:
    status_data = load_job_status(job_dir) or {}
    ensure_modal_tracking_sections(status_data)
    return bool(status_data["modal_usage"]["calls"])
