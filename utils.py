import os
import re
import shutil
from pathlib import Path

from fastapi import UploadFile


WORKER_DIR = Path(__file__).resolve().parent
BASE_OUTPUT_DIR = os.getenv("TTS_WORKER_OUTPUT_DIR", str(WORKER_DIR / "output"))
_VIDEO_JOB_DIR_RE = re.compile(r"^video-(\d+)$")


def create_job_dir() -> str:
    os.makedirs(BASE_OUTPUT_DIR, exist_ok=True)
    existing_indices: list[int] = []
    for entry in os.scandir(BASE_OUTPUT_DIR):
        if not entry.is_dir():
            continue
        match = _VIDEO_JOB_DIR_RE.match(entry.name)
        if match:
            existing_indices.append(int(match.group(1)))

    next_index = (max(existing_indices) if existing_indices else 0) + 1
    while True:
        job_id = f"video-{next_index}"
        job_dir = os.path.join(BASE_OUTPUT_DIR, job_id)
        try:
            os.makedirs(job_dir, exist_ok=False)
            open(os.path.join(job_dir, "job.log"), "a", encoding="utf-8").close()
            return job_dir
        except FileExistsError:
            next_index += 1


def get_job_dir(job_id: str) -> str:
    return os.path.join(BASE_OUTPUT_DIR, job_id)


def save_upload_file(upload_file: UploadFile, output_path: str) -> str:
    with open(output_path, "wb") as file_obj:
        shutil.copyfileobj(upload_file.file, file_obj)
    return output_path
