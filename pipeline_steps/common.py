import os
import re
import subprocess
import wave

from job_status import append_job_log


WORKER_DIR = os.path.dirname(os.path.dirname(__file__))


class ProcessingError(Exception):
    pass


DEFAULT_RENDER_CONFIG = {
    "xtts_segment_max_chars": 250,
    "xtts_segment_min_chars": 80,
    "use_inprocess_tts": True,
    "use_cached_xtts_conditioning": True,
    "chunking_strategy": "pysbd",
    "max_tts_chunk_chars": 250,
    "min_tts_chunk_chars": 150,
    "tts_concurrency": 4,
    "tts_parallel_backend": "process",
    "modal_xtts_dispatch": "spawn",
    "modal_xtts_artifact_volume": os.getenv("MODAL_XTTS_ARTIFACT_VOLUME", "tooltucode-xtts-artifacts"),
    "modal_xtts_artifact_prefix": os.getenv("MODAL_XTTS_ARTIFACT_PREFIX", "xtts-jobs"),
    "modal_xtts_download_workers": 8,
    "chunk_batch_size": 0,
    "gpu_backend": os.getenv("TTS_WORKER_GPU_BACKEND", "modal"),
    "modal_app_name": os.getenv("MODAL_APP_NAME", "tooltucode-gpu-v2"),
    "modal_step_3_app_name": None,
    "modal_tts_gpu": os.getenv("MODAL_TTS_GPU", "L4"),
}


def _normalize_modal_app_name(value: str | None, fallback: str) -> str:
    name = str(value or fallback or "").strip()
    return name or fallback


def resolve_modal_app_names(base_app_name: str | None = None) -> dict[str, str]:
    base_name = _normalize_modal_app_name(base_app_name, os.getenv("MODAL_APP_NAME", "tooltucode-gpu-v2"))
    return {
        "modal_app_name": base_name,
        "modal_step_3_app_name": f"{base_name}-step3",
    }


def normalize_render_config(render_config: dict | None = None) -> dict:
    config = DEFAULT_RENDER_CONFIG.copy()
    if render_config:
        config.update({key: value for key, value in render_config.items() if value is not None})

    try:
        config["xtts_segment_max_chars"] = max(120, min(400, int(config.get("xtts_segment_max_chars", 250))))
    except (TypeError, ValueError):
        config["xtts_segment_max_chars"] = DEFAULT_RENDER_CONFIG["xtts_segment_max_chars"]
    try:
        xtts_segment_min_chars = max(0, min(250, int(config.get("xtts_segment_min_chars", 80))))
    except (TypeError, ValueError):
        xtts_segment_min_chars = DEFAULT_RENDER_CONFIG["xtts_segment_min_chars"]
    config["xtts_segment_min_chars"] = min(xtts_segment_min_chars, config["xtts_segment_max_chars"])

    config["use_inprocess_tts"] = bool(config.get("use_inprocess_tts", True))
    config["use_cached_xtts_conditioning"] = bool(config.get("use_cached_xtts_conditioning", True))

    chunking_strategy = str(config.get("chunking_strategy", "pysbd")).lower().strip()
    config["chunking_strategy"] = chunking_strategy if chunking_strategy in {"regex", "pysbd"} else "pysbd"
    try:
        config["max_tts_chunk_chars"] = max(120, min(1200, int(config.get("max_tts_chunk_chars", 250))))
    except (TypeError, ValueError):
        config["max_tts_chunk_chars"] = DEFAULT_RENDER_CONFIG["max_tts_chunk_chars"]
    try:
        min_tts_chunk_chars = max(0, min(600, int(config.get("min_tts_chunk_chars", 150))))
    except (TypeError, ValueError):
        min_tts_chunk_chars = DEFAULT_RENDER_CONFIG["min_tts_chunk_chars"]
    config["min_tts_chunk_chars"] = min(min_tts_chunk_chars, config["max_tts_chunk_chars"])
    try:
        config["tts_concurrency"] = max(1, int(config.get("tts_concurrency", 1)))
    except (TypeError, ValueError):
        config["tts_concurrency"] = 1

    config["tts_parallel_backend"] = "process"
    modal_xtts_dispatch = str(config.get("modal_xtts_dispatch", "spawn")).lower().strip()
    if modal_xtts_dispatch == "segment":
        modal_xtts_dispatch = "spawn"
    config["modal_xtts_dispatch"] = modal_xtts_dispatch if modal_xtts_dispatch in {"spawn", "legacy"} else "spawn"
    config["modal_xtts_artifact_volume"] = str(
        config.get("modal_xtts_artifact_volume")
        or os.getenv("MODAL_XTTS_ARTIFACT_VOLUME", "tooltucode-xtts-artifacts")
    ).strip()
    modal_xtts_artifact_prefix = str(
        config.get("modal_xtts_artifact_prefix")
        or os.getenv("MODAL_XTTS_ARTIFACT_PREFIX", "xtts-jobs")
    ).strip().strip("/\\")
    config["modal_xtts_artifact_prefix"] = modal_xtts_artifact_prefix or "xtts-jobs"
    try:
        config["modal_xtts_download_workers"] = max(
            1,
            min(16, int(config.get("modal_xtts_download_workers", 8))),
        )
    except (TypeError, ValueError):
        config["modal_xtts_download_workers"] = DEFAULT_RENDER_CONFIG["modal_xtts_download_workers"]
    gpu_backend = str(config.get("gpu_backend", os.getenv("TTS_WORKER_GPU_BACKEND", "modal"))).lower().strip()
    config["gpu_backend"] = gpu_backend if gpu_backend in {"local", "modal"} else "modal"
    config.update(resolve_modal_app_names(config.get("modal_app_name")))
    config["modal_tts_gpu"] = str(config.get("modal_tts_gpu") or os.getenv("MODAL_TTS_GPU", "L4")).strip()
    return config


def _regex_sentences(text: str) -> list[str]:
    return [sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", text) if sentence.strip()]


def _pysbd_sentences(text: str) -> list[str]:
    try:
        import pysbd
    except Exception:
        return _regex_sentences(text)
    try:
        segmenter = pysbd.Segmenter(language="en", clean=False, char_span=False)
        sentences = [sentence.strip() for sentence in segmenter.segment(text) if sentence.strip()]
        return sentences or _regex_sentences(text)
    except Exception:
        return _regex_sentences(text)


def _split_long_text_at_boundaries(text: str, max_length: int) -> list[str]:
    remaining = text.strip()
    chunks = []
    soft_boundaries = ["; ", ": ", ", "]
    while len(remaining) > max_length:
        split_at = -1
        for boundary in soft_boundaries:
            candidate = remaining.rfind(boundary, 0, max_length + 1)
            if candidate > split_at:
                split_at = candidate + len(boundary) - 1
        if split_at < max_length // 2:
            split_at = remaining.rfind(" ", 0, max_length + 1)
        if split_at < max_length // 2:
            split_at = max_length
        chunk = remaining[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def split_script_for_xtts(
    script: str,
    max_chars: int = 250,
    min_chars: int = 80,
    strategy: str = "pysbd",
) -> list[str]:
    text = re.sub(r"\s+", " ", (script or "").strip())
    if not text:
        return []
    max_chars = max(1, min(400, int(max_chars or 250)))
    min_chars = max(0, min(int(min_chars or 0), max_chars))
    sentences = _pysbd_sentences(text) if strategy == "pysbd" else _regex_sentences(text)
    segments = []
    current = ""
    for sentence in sentences:
        if len(sentence) > max_chars:
            if current:
                segments.append(current.strip())
                current = ""
            segments.extend(_split_long_text_at_boundaries(sentence, max_chars))
            continue
        if not current:
            current = sentence
            continue
        candidate = f"{current} {sentence}".strip()
        if len(candidate) <= max_chars:
            current = candidate
        else:
            segments.append(current.strip())
            current = sentence
    if current:
        segments.append(current.strip())
    if min_chars <= 0 or len(segments) <= 1:
        return segments
    merged = []
    for segment in segments:
        if merged and len(merged[-1]) < min_chars and len(merged[-1]) + 1 + len(segment) <= max_chars:
            merged[-1] = f"{merged[-1]} {segment}".strip()
        else:
            merged.append(segment)
    return merged


def get_wav_duration_seconds(wav_path: str) -> float:
    with wave.open(wav_path, "rb") as wav_file:
        frames = wav_file.getnframes()
        rate = wav_file.getframerate()
        return frames / float(rate) if rate else 0.0


def concat_wav_files(wav_paths: list[str], output_path: str, job_dir: str) -> None:
    concat_list_path = os.path.join(os.path.dirname(output_path), "concat_list.txt")
    with open(concat_list_path, "w", encoding="utf-8") as file_obj:
        for wav_path in wav_paths:
            file_obj.write(f"file '{os.path.abspath(wav_path)}'\n")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path, "-c", "copy", output_path],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    append_job_log(job_dir, f"Audio concat completed: {output_path}")
