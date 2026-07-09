import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from job_status import append_job_log
from modal_costs import record_modal_call


def _load_modal():
    try:
        import modal
    except Exception as exc:
        raise RuntimeError(
            "Modal SDK is not available. Install dependencies with: pip install -r TTS_worker/requirements.txt"
        ) from exc
    return modal


def _modal_function(app_name: str, function_name: str):
    modal = _load_modal()
    try:
        return modal.Function.from_name(app_name, function_name)
    except Exception as exc:
        raise RuntimeError(
            f"Could not load Modal function '{function_name}' from app '{app_name}'."
        ) from exc


def _modal_cls(app_name: str, class_name: str):
    modal = _load_modal()
    try:
        return modal.Cls.from_name(app_name, class_name)
    except Exception as exc:
        raise RuntimeError(
            f"Could not load Modal class '{class_name}' from app '{app_name}'."
        ) from exc


def _with_gpu_option(function, gpu_name: str | None):
    gpu_name = str(gpu_name or "").strip()
    if not gpu_name:
        return function
    try:
        return function.with_options(gpu=gpu_name)
    except AttributeError:
        return function


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as file_obj:
        return file_obj.read()


def write_bytes(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as file_obj:
        file_obj.write(data)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@lru_cache(maxsize=None)
def _modal_volume(volume_name: str, version: int | None = None):
    modal = _load_modal()
    kwargs: dict[str, Any] = {"create_if_missing": True}
    if version is not None:
        kwargs["version"] = version
    return modal.Volume.from_name(volume_name, **kwargs)


def _download_xtts_artifact_bytes(volume_name: str, subpath: str) -> bytes:
    volume = _modal_volume(volume_name, version=2)
    return b"".join(volume.read_file(subpath))


def _modal_tts_dispatch_mode(render_config: dict[str, Any]) -> str:
    mode = str(render_config.get("modal_xtts_dispatch", "spawn")).lower().strip()
    if mode == "segment":
        mode = "spawn"
    return mode if mode in {"spawn", "legacy"} else "spawn"


def _sorted_modal_results(results) -> list[dict[str, Any]]:
    return sorted(list(results), key=lambda item: int(item["index"]))


def _hydrate_xtts_modal_results(
    raw_results: list[dict[str, Any]],
    payload_config: dict[str, Any],
    job_dir: str,
) -> list[dict[str, Any]]:
    artifact_volume = str(
        payload_config.get("modal_xtts_artifact_volume") or "tooltucode-xtts-artifacts"
    ).strip() or "tooltucode-xtts-artifacts"
    sorted_results = _sorted_modal_results(raw_results)
    max_workers = max(
        1,
        min(16, int(payload_config.get("modal_xtts_download_workers", 8) or 8), len(sorted_results) or 1),
    )

    def hydrate_one(metadata: dict[str, Any]) -> dict[str, Any]:
        volume_name = str(metadata.get("artifact_volume") or artifact_volume).strip() or artifact_volume
        volume_subpath = str(metadata["volume_subpath"])
        append_job_log(job_dir, f"[Modal] XTTS metadata received: segment={int(metadata['index']) + 1}.")
        metadata["voice_wav_bytes"] = _download_xtts_artifact_bytes(volume_name, volume_subpath)
        append_job_log(job_dir, f"[Modal] XTTS artifact downloaded: {volume_subpath}.")
        return metadata

    start = time.time()
    if max_workers <= 1:
        hydrated_results = [hydrate_one(metadata) for metadata in sorted_results]
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            hydrated_results = list(executor.map(hydrate_one, sorted_results))
    append_job_log(
        job_dir,
        f"[Modal] XTTS artifact hydration completed in {time.time() - start:.2f}s with workers={max_workers}.",
    )
    return _sorted_modal_results(hydrated_results)


def render_xtts_segments_modal(
    script_segments: list[str],
    voice_sample_path: str,
    render_config: dict[str, Any],
    job_dir: str,
) -> list[dict[str, Any]]:
    app_name = str(render_config.get("modal_step_3_app_name") or render_config.get("modal_app_name", "tooltucode-gpu-v1"))
    payload_config = dict(render_config)
    payload_config["modal_requested_gpu"] = render_config.get("modal_tts_gpu", "L4")
    payload_config["job_id"] = os.path.basename(os.path.abspath(job_dir))
    payload_config["modal_xtts_artifact_volume"] = (
        str(render_config.get("modal_xtts_artifact_volume") or "tooltucode-xtts-artifacts").strip()
        or "tooltucode-xtts-artifacts"
    )
    voice_sample_bytes = _read_bytes(voice_sample_path)

    if _modal_tts_dispatch_mode(payload_config) == "legacy":
        function = _modal_function(app_name, "render_xtts_segments")
        function = _with_gpu_option(function, payload_config["modal_requested_gpu"])
        append_job_log(job_dir, f"[Modal] Calling legacy render_xtts_segments on app={app_name}.")
        start = time.time()
        result = function.remote(script_segments, voice_sample_bytes, payload_config)
        append_job_log(job_dir, f"[Modal] legacy render_xtts_segments completed in {time.time() - start:.2f}s.")
        return _hydrate_xtts_modal_results(result, payload_config, job_dir)

    renderer_cls = _modal_cls(app_name, "XttsSegmentRenderer")
    renderer_cls = _with_gpu_option(renderer_cls, payload_config["modal_requested_gpu"])
    renderer = renderer_cls()
    append_job_log(
        job_dir,
        f"[Modal] Spawning {len(script_segments)} XTTS segment job(s) through XttsSegmentRenderer on app={app_name}, "
        f"gpu={payload_config['modal_requested_gpu']}.",
    )
    start = time.time()
    calls = []
    for segment_index, segment_text in enumerate(script_segments):
        started_at = _utc_now_iso()
        call = renderer.render.spawn(segment_index, segment_text, voice_sample_bytes, payload_config)
        calls.append((segment_index, call, started_at))
    metadata_results = []
    for segment_index, call, started_at in calls:
        metadata = call.get()
        metadata_results.append(metadata)
        ended_at = _utc_now_iso()
        record_modal_call(
            job_dir,
            {
                "job_id": payload_config["job_id"],
                "step_key": "step_3_coqui_tts",
                "operation": "xtts",
                "app_name": app_name,
                "function_name": "XttsSegmentRenderer.render",
                "gpu": payload_config["modal_requested_gpu"],
                "started_at": started_at,
                "ended_at": ended_at,
                "elapsed_seconds": (
                    datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
                    - datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                ).total_seconds(),
                "segment_index": segment_index,
            },
        )
        append_job_log(job_dir, f"[Modal] XTTS segment call completed: segment={segment_index + 1}.")
    append_job_log(job_dir, f"[Modal] XttsSegmentRenderer.render spawn/get completed in {time.time() - start:.2f}s.")
    return _hydrate_xtts_modal_results(metadata_results, payload_config, job_dir)
