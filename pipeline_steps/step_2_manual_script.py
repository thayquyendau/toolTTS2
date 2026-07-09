import os

from job_status import (
    append_job_log,
    load_job_status,
    save_job_status,
    update_step_status,
    wait_for_approval,
)


def wait_for_script_approval(transcript_path: str, job_dir: str) -> str:
    with open(transcript_path, "r", encoding="utf-8") as file_obj:
        transcript_text = file_obj.read().strip()
    append_job_log(job_dir, f"Step 2 transcript chars: {len(transcript_text)}")

    status_data = load_job_status(job_dir)
    if status_data is None:
        raise FileNotFoundError(f"Status file not found: {job_dir}")
    if status_data.get("script_data") is None:
        status_data["script_data"] = ""
        status_data["script_approved"] = False
        save_job_status(job_dir, status_data)

    manual_status = status_data
    if not manual_status.get("script_approved"):
        manual_status = wait_for_approval(
            job_dir=job_dir,
            approval_key="script_approved",
            step_key="step_2_manual_script",
            message="Waiting for user approval: manual script before TTS.",
        )

    script = (manual_status.get("script_data") or "").strip()
    if not script:
        message = "Manual script is empty. Paste script content before approving."
        update_step_status(job_dir, "step_2_manual_script", "failure", message, detail=message)
        raise RuntimeError(message)

    out_path = os.path.join(job_dir, "script.txt")
    with open(out_path, "w", encoding="utf-8") as file_obj:
        file_obj.write(script)
    append_job_log(job_dir, f"Step 2 manual script chars: {len(script)}")
    update_step_status(job_dir, "step_2_manual_script", "success", "Manual script accepted.")
    return out_path
