import os
import re
import subprocess
import sys
import threading
import wave
from concurrent.futures import ProcessPoolExecutor, as_completed

from job_status import (
    append_job_log,
    init_segments,
    update_segment_status,
    update_step_status,
    wait_for_approval,
)
from modal_gpu_client import render_xtts_segments_modal, write_bytes
from pipeline_steps.common import (
    WORKER_DIR,
    concat_wav_files,
    get_wav_duration_seconds,
    normalize_render_config,
    split_script_for_xtts,
)


_PROCESS_TTS_CACHED_CONDITIONING = None


def _numpy_module():
    try:
        import numpy as np
    except Exception as exc:
        raise RuntimeError("numpy is required for local XTTS synthesis.") from exc
    return np


def _torch_module():
    try:
        import torch
    except Exception as exc:
        raise RuntimeError("torch is required for local XTTS synthesis.") from exc
    return torch


def _torch_cuda_available() -> bool:
    return _torch_module().cuda.is_available()


def _torch_device_name() -> str:
    return _torch_module().cuda.get_device_name(0)


def _clean_tts_text(text: str) -> str:
    text = re.sub(r"\*\*", "", text)
    text = re.sub(r"\*", "", text)
    text = re.sub(r"[\"'\(\)]", "", text)
    text = re.sub(r"\n+", ". ", text)
    return re.sub(r"\s+", " ", text).strip()


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


def clean_and_chunk_text(text: str, max_length: int = 350, strategy: str = "pysbd") -> list[str]:
    text = _clean_tts_text(text)
    sentences = _pysbd_sentences(text) if strategy == "pysbd" else _regex_sentences(text)
    chunks = []
    current_chunk = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > max_length:
            if current_chunk:
                chunks.append(current_chunk.strip())
                current_chunk = ""
            chunks.extend(_split_long_text_at_boundaries(sentence, max_length))
            continue
        if not current_chunk:
            current_chunk = sentence
        elif len(current_chunk) + 1 + len(sentence) <= max_length:
            current_chunk += " " + sentence
        else:
            chunks.append(current_chunk.strip())
            current_chunk = sentence
    if current_chunk:
        chunks.append(current_chunk.strip())
    return chunks


def _save_xtts_wav(wav, wav_path: str, sample_rate: int = 24000) -> None:
    np = _numpy_module()
    wav_array = np.asarray(wav, dtype=np.float32).squeeze()
    if wav_array.size == 0:
        raise RuntimeError("XTTS returned empty audio.")
    scale = 32767 / max(0.01, float(np.max(np.abs(wav_array))))
    wav_int16 = (wav_array * scale).astype(np.int16)
    with wave.open(wav_path, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(wav_int16.tobytes())


def _remove_espeak_from_path(env: dict) -> tuple[dict, list[str]]:
    filtered_parts = []
    removed_parts = []
    for path_part in env.get("PATH", "").split(os.pathsep):
        if not path_part:
            continue
        espeak_ng_path = os.path.join(path_part, "espeak-ng.exe")
        espeak_path = os.path.join(path_part, "espeak.exe")
        if os.path.exists(espeak_ng_path) or os.path.exists(espeak_path):
            removed_parts.append(path_part)
            continue
        filtered_parts.append(path_part)
    updated_env = env.copy()
    updated_env["PATH"] = os.pathsep.join(filtered_parts)
    return updated_env, removed_parts


def _load_tts_api(job_dir: str):
    original_path = os.environ.get("PATH", "")
    sanitized_env, removed_espeak_paths = _remove_espeak_from_path(os.environ)
    if removed_espeak_paths:
        append_job_log(
            job_dir,
            f"Temporarily removed {len(removed_espeak_paths)} espeak PATH entrie(s) before importing TTS.api.",
        )
    try:
        os.environ["PATH"] = sanitized_env.get("PATH", "")
        from TTS.api import TTS
        return TTS
    finally:
        os.environ["PATH"] = original_path


def _render_tts_chunk(
    chunk_index: int,
    chunk: str,
    sample_wav: str,
    job_dir: str,
    output_dir: str,
    total_chunks: int,
    env: dict,
    use_cuda: bool = False,
) -> tuple[int, str]:
    wav_path = os.path.join(output_dir, f"chunk_{chunk_index}.wav")
    worker_venv_python = os.path.join(WORKER_DIR, "venv", "Scripts", "python.exe")
    worker_venv_scripts = os.path.join(WORKER_DIR, "venv", "Scripts")
    tts_python = worker_venv_python if os.path.exists(worker_venv_python) else sys.executable
    tts_env = env.copy()
    if os.path.isdir(worker_venv_scripts):
        tts_env["PATH"] = worker_venv_scripts + os.pathsep + tts_env.get("PATH", "")
    tts_env, removed_espeak_paths = _remove_espeak_from_path(tts_env)
    tts_cmd = [
        tts_python,
        "-m",
        "TTS.bin.synthesize",
        "--model_name",
        "tts_models/multilingual/multi-dataset/xtts_v2",
        "--text",
        chunk,
        "--speaker_wav",
        sample_wav,
        "--language_idx",
        "en",
        "--out_path",
        wav_path,
    ]
    if use_cuda:
        tts_cmd.extend(["--use_cuda", "true"])
    append_job_log(job_dir, f"Rendering chunk {chunk_index + 1}/{total_chunks}")
    append_job_log(job_dir, f"Using Coqui TTS Python: {tts_python}")
    if removed_espeak_paths:
        append_job_log(job_dir, f"Removed {len(removed_espeak_paths)} espeak PATH entrie(s) for XTTS compatibility.")
    result = subprocess.run(tts_cmd, env=tts_env, capture_output=True, text=True)
    if result.returncode != 0:
        stderr_tail = (result.stderr or "").strip()
        stdout_tail = (result.stdout or "").strip()
        detail = stderr_tail or stdout_tail or "Unknown TTS failure."
        raise RuntimeError(f"TTS Chunk {chunk_index + 1} failed: {detail}")
    return chunk_index, wav_path


def _render_tts_chunk_inprocess(
    chunk_index: int,
    chunk: str,
    sample_wav: str,
    job_dir: str,
    output_dir: str,
    total_chunks: int,
    use_cuda: bool = False,
    cached_conditioning: dict | None = None,
) -> tuple[int, str]:
    wav_path = os.path.join(output_dir, f"chunk_{chunk_index}.wav")
    tts_instance = _get_tts_instance(job_dir, use_cuda)
    if cached_conditioning:
        try:
            _render_tts_chunk_with_cached_conditioning(
                tts_instance=tts_instance,
                cached_conditioning=cached_conditioning,
                chunk=chunk,
                wav_path=wav_path,
            )
            return chunk_index, wav_path
        except Exception as exc:
            append_job_log(
                job_dir,
                f"Cached XTTS inference failed for chunk {chunk_index + 1}; falling back to speaker_wav: {exc}",
            )
    with _tts_instance_lock():
        tts_instance.tts_to_file(
            text=chunk,
            speaker_wav=sample_wav,
            language="en",
            file_path=wav_path,
        )
    return chunk_index, wav_path


def _tts_instance_lock():
    global _TTS_INSTANCE, _TTS_INSTANCE_LOCK
    try:
        _TTS_INSTANCE
    except NameError:
        _TTS_INSTANCE = None
    try:
        _TTS_INSTANCE_LOCK
    except NameError:
        _TTS_INSTANCE_LOCK = threading.Lock()
    return _TTS_INSTANCE_LOCK


def _get_tts_instance(job_dir: str, use_cuda: bool = False):
    global _TTS_INSTANCE
    _tts_instance_lock()
    model_name = "tts_models/multilingual/multi-dataset/xtts_v2"
    if _TTS_INSTANCE is None:
        with _tts_instance_lock():
            if _TTS_INSTANCE is None:
                TTS = _load_tts_api(job_dir)
                _TTS_INSTANCE = TTS(model_name, gpu=use_cuda)
                append_job_log(job_dir, f"Loaded in-process TTS model: {model_name}")
    return _TTS_INSTANCE


def _prepare_cached_xtts_conditioning(job_dir: str, sample_wav: str, use_cuda: bool) -> dict | None:
    tts_instance = _get_tts_instance(job_dir, use_cuda)
    xtts_model = getattr(getattr(tts_instance, "synthesizer", None), "tts_model", None)
    if xtts_model is None or not hasattr(xtts_model, "get_conditioning_latents") or not hasattr(xtts_model, "inference"):
        append_job_log(job_dir, "Cached XTTS conditioning unavailable: loaded model does not expose XTTS internals.")
        return None
    config = getattr(xtts_model, "config", None)
    if config is None:
        append_job_log(job_dir, "Cached XTTS conditioning unavailable: XTTS config is missing.")
        return None
    with _tts_instance_lock():
        gpt_cond_latent, speaker_embedding = xtts_model.get_conditioning_latents(
            audio_path=sample_wav,
            gpt_cond_len=getattr(config, "gpt_cond_len", 6),
            max_ref_length=getattr(config, "max_ref_len", 30),
            sound_norm_refs=getattr(config, "sound_norm_refs", False),
        )
    append_job_log(job_dir, "Cached XTTS speaker conditioning prepared.")
    return {
        "xtts_model": xtts_model,
        "gpt_cond_latent": gpt_cond_latent,
        "speaker_embedding": speaker_embedding,
        "config": config,
    }


def _render_tts_chunk_with_cached_conditioning(
    tts_instance,
    cached_conditioning: dict,
    chunk: str,
    wav_path: str,
) -> None:
    del tts_instance
    xtts_model = cached_conditioning["xtts_model"]
    config = cached_conditioning["config"]
    with _tts_instance_lock():
        out = xtts_model.inference(
            text=chunk,
            language="en",
            gpt_cond_latent=cached_conditioning["gpt_cond_latent"],
            speaker_embedding=cached_conditioning["speaker_embedding"],
            temperature=getattr(config, "temperature", 0.75),
            length_penalty=getattr(config, "length_penalty", 1.0),
            repetition_penalty=getattr(config, "repetition_penalty", 10.0),
            top_k=getattr(config, "top_k", 50),
            top_p=getattr(config, "top_p", 0.85),
        )
    _save_xtts_wav(out["wav"], wav_path, sample_rate=24000)


def _init_tts_process_worker(
    job_dir: str,
    sample_wav: str,
    use_cuda: bool,
    use_inprocess_tts: bool,
    use_cached_conditioning: bool,
) -> None:
    global _PROCESS_TTS_CACHED_CONDITIONING
    os.environ["COQUI_TOS_AGREED"] = "1"
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")
    if not use_inprocess_tts:
        return
    _get_tts_instance(job_dir, use_cuda)
    _PROCESS_TTS_CACHED_CONDITIONING = None
    if use_cached_conditioning:
        try:
            _PROCESS_TTS_CACHED_CONDITIONING = _prepare_cached_xtts_conditioning(job_dir, sample_wav, use_cuda)
            append_job_log(job_dir, "Process TTS worker warmed with cached XTTS conditioning.")
        except Exception as exc:
            append_job_log(job_dir, f"Process TTS worker cached conditioning unavailable; using fallback: {exc}")


def _render_tts_segment_process_task(args: tuple) -> tuple[int, str]:
    (
        segment_index,
        segment_text,
        sample_wav,
        segment_dir,
        job_dir,
        env,
        use_cuda,
        render_config,
    ) = args
    cached_conditioning = _PROCESS_TTS_CACHED_CONDITIONING if render_config.get("use_inprocess_tts", True) else None
    segment_voice_path = _render_tts_segment(
        segment_index,
        segment_text,
        sample_wav,
        segment_dir,
        job_dir,
        env,
        use_cuda,
        render_config=render_config,
        cached_conditioning=cached_conditioning,
        update_status=False,
    )
    return segment_index, segment_voice_path


def synthesize_chunk(
    chunk_index: int,
    chunk: str,
    sample_wav: str,
    job_dir: str,
    output_dir: str,
    total_chunks: int,
    env: dict,
    use_cuda: bool,
    use_inprocess: bool,
    cached_conditioning: dict | None = None,
) -> tuple[int, str]:
    if use_inprocess:
        return _render_tts_chunk_inprocess(
            chunk_index,
            chunk,
            sample_wav,
            job_dir,
            output_dir,
            total_chunks,
            use_cuda,
            cached_conditioning=cached_conditioning,
        )
    return _render_tts_chunk(chunk_index, chunk, sample_wav, job_dir, output_dir, total_chunks, env, use_cuda)


def _concat_wav_files(wav_paths: list[str], output_path: str, job_dir: str) -> None:
    concat_wav_files(wav_paths, output_path, job_dir)


def _render_tts_segment(
    segment_index: int,
    segment_text: str,
    sample_wav: str,
    segment_dir: str,
    job_dir: str,
    env: dict,
    use_cuda: bool,
    render_config: dict | None = None,
    cached_conditioning: dict | None = None,
    update_status: bool = True,
) -> str:
    os.makedirs(segment_dir, exist_ok=True)
    segment_script_path = os.path.join(segment_dir, "script.txt")
    with open(segment_script_path, "w", encoding="utf-8") as file_obj:
        file_obj.write(segment_text)
    max_chunk_chars = 250 if render_config is None else int(render_config.get("max_tts_chunk_chars", 250))
    chunking_strategy = "pysbd" if render_config is None else str(render_config.get("chunking_strategy", "pysbd"))
    chunks = clean_and_chunk_text(segment_text, max_length=max_chunk_chars, strategy=chunking_strategy)
    if not chunks:
        raise RuntimeError(f"Segment {segment_index + 1} is empty after text cleanup.")
    if update_status:
        update_segment_status(
            job_dir,
            segment_index,
            status="running",
            phase="Generating TTS",
            message=f"Rendering {len(chunks)} TTS chunk(s).",
            files={"script": os.path.relpath(segment_script_path, job_dir)},
        )
    append_job_log(job_dir, f"Segment {segment_index + 1}: prepared {len(chunks)} TTS chunk(s)")
    chunk_wavs = []
    use_inprocess = True if render_config is None else bool(render_config.get("use_inprocess_tts", True))
    for chunk_index, chunk in enumerate(chunks):
        index, wav_path = synthesize_chunk(
            chunk_index,
            chunk,
            sample_wav,
            job_dir,
            segment_dir,
            len(chunks),
            env,
            use_cuda,
            use_inprocess=use_inprocess,
            cached_conditioning=cached_conditioning if use_inprocess else None,
        )
        chunk_wavs.append((index, wav_path))
    chunk_wavs.sort(key=lambda item: item[0])
    segment_voice_path = os.path.join(segment_dir, "voice.wav")
    _concat_wav_files([wav_path for _, wav_path in chunk_wavs], segment_voice_path, job_dir)
    duration_seconds = get_wav_duration_seconds(segment_voice_path)
    append_job_log(job_dir, f"Segment {segment_index + 1} audio duration: {duration_seconds:.2f}s")
    if update_status:
        update_segment_status(
            job_dir,
            segment_index,
            status="success",
            phase="TTS ready",
            message=f"Audio duration {duration_seconds:.2f}s.",
            duration_seconds=duration_seconds,
            files={"voice": os.path.relpath(segment_voice_path, job_dir)},
        )
    return segment_voice_path


def step_3_coqui_tts(script_path: str, voice_sample_path: str, job_dir: str, render_config: dict | None = None) -> str:
    config = normalize_render_config(render_config)
    approved_status = wait_for_approval(
        job_dir=job_dir,
        approval_key="script_approved",
        step_key="step_3_coqui_tts",
        message="Waiting for user approval: script before generating TTS.",
    )
    approved_script = approved_status.get("script_data") or ""
    if approved_script.strip():
        with open(script_path, "w", encoding="utf-8") as file_obj:
            file_obj.write(approved_script)

    update_step_status(job_dir, "step_3_coqui_tts", "running", "Generating TTS from approved script.")
    append_job_log(job_dir, "Step 3 start: generating TTS segments")

    update_step_status(job_dir, "step_3_coqui_tts", "running", "Loading model: preparing Coqui TTS.")
    append_job_log(job_dir, "Step 3 phase: Loading model")
    sample_wav = os.path.join(job_dir, "sample_prepared.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-i", voice_sample_path, "-ar", "22050", "-ac", "1", sample_wav],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    with open(script_path, "r", encoding="utf-8") as file_obj:
        original_script = file_obj.read()

    script_segments = split_script_for_xtts(
        original_script,
        max_chars=config["xtts_segment_max_chars"],
        min_chars=config["xtts_segment_min_chars"],
        strategy=config["chunking_strategy"],
    )
    if not script_segments:
        message = "Script is empty after segment split."
        update_step_status(job_dir, "step_3_coqui_tts", "failure", message)
        raise RuntimeError(message)

    init_segments(job_dir, len(script_segments))
    append_job_log(
        job_dir,
        f"Prepared {len(script_segments)} XTTS text segment(s): "
        f"max_chars={config['xtts_segment_max_chars']}, min_chars={config['xtts_segment_min_chars']}, "
        f"strategy={config['chunking_strategy']}.",
    )

    env = os.environ.copy()
    env["COQUI_TOS_AGREED"] = "1"
    segments_root = os.path.join(job_dir, "segments")
    os.makedirs(segments_root, exist_ok=True)

    if config.get("gpu_backend") == "modal":
        update_step_status(job_dir, "step_3_coqui_tts", "running", "Running Modal XTTS: generating TTS segments.")
        append_job_log(job_dir, "[Modal] Step 3 backend enabled.")
        append_job_log(
            job_dir,
            f"[Modal] Step 3 dispatch mode={config.get('modal_xtts_dispatch', 'spawn')}, "
            f"segments={len(script_segments)}, tts_concurrency={config.get('tts_concurrency')}.",
        )
        try:
            modal_results = render_xtts_segments_modal(script_segments, sample_wav, config, job_dir)
            segment_voice_paths = []
            for result in sorted(modal_results, key=lambda item: int(item["index"])):
                segment_index = int(result["index"])
                segment_dir = os.path.join(segments_root, f"segment_{segment_index:03d}")
                os.makedirs(segment_dir, exist_ok=True)
                segment_script_path = os.path.join(segment_dir, "script.txt")
                with open(segment_script_path, "w", encoding="utf-8") as file_obj:
                    file_obj.write(script_segments[segment_index])
                segment_voice_path = os.path.join(segment_dir, "voice.wav")
                write_bytes(segment_voice_path, result["voice_wav_bytes"])
                duration_seconds = get_wav_duration_seconds(segment_voice_path)
                update_segment_status(
                    job_dir,
                    segment_index,
                    status="success",
                    phase="TTS ready",
                    message=f"Modal audio duration {duration_seconds:.2f}s, chunks={result.get('chunk_count', '?')}.",
                    duration_seconds=duration_seconds,
                    files={
                        "script": os.path.relpath(segment_script_path, job_dir),
                        "voice": os.path.relpath(segment_voice_path, job_dir),
                    },
                )
                segment_voice_paths.append(segment_voice_path)
            update_step_status(job_dir, "step_3_coqui_tts", "running", "Merging output: combining Modal TTS segments.")
            out_voice = os.path.join(job_dir, "voice.wav")
            _concat_wav_files(segment_voice_paths, out_voice, job_dir)
            total_duration = get_wav_duration_seconds(out_voice)
            append_job_log(job_dir, f"Step 3 total audio duration: {total_duration:.2f}s")
            update_step_status(job_dir, "step_3_coqui_tts", "success", "TTS generated on Modal and merged successfully.")
            return out_voice
        except Exception as exc:
            message = f"Modal XTTS artifact retrieval failed: {exc}"
            update_step_status(job_dir, "step_3_coqui_tts", "failure", message, detail=str(exc))
            append_job_log(job_dir, message)
            raise RuntimeError(message) from exc

    if not _torch_cuda_available():
        message = "CUDA (GPU) is not available. Local XTTS requires an NVIDIA GPU."
        update_step_status(job_dir, "step_3_coqui_tts", "failure", message)
        raise RuntimeError(message)

    env["CUDA_VISIBLE_DEVICES"] = "0"
    append_job_log(job_dir, f"[GPU] CUDA available: {_torch_device_name()}")

    segment_voice_paths = []
    use_cuda = _torch_cuda_available()
    cached_conditioning = None
    concurrency = int(config.get("tts_concurrency", 1))
    use_process_pool = concurrency > 1
    if concurrency <= 1:
        append_job_log(job_dir, "[GPU] Step 3 uses sequential TTS segments.")
    else:
        append_job_log(job_dir, f"[GPU] Step 3 uses process TTS workers: concurrency={concurrency}.")

    if (
        config.get("use_inprocess_tts", True)
        and config.get("use_cached_xtts_conditioning", True)
        and not use_process_pool
    ):
        try:
            cached_conditioning = _prepare_cached_xtts_conditioning(job_dir, sample_wav, use_cuda)
        except Exception as exc:
            append_job_log(job_dir, f"Cached XTTS conditioning failed; using speaker_wav fallback: {exc}")

    update_step_status(job_dir, "step_3_coqui_tts", "running", "Running inference: generating TTS chunks.")
    append_job_log(job_dir, "Step 3 phase: Running inference")

    if concurrency <= 1:
        for segment_index, segment_text in enumerate(script_segments):
            segment_dir = os.path.join(segments_root, f"segment_{segment_index:03d}")
            try:
                segment_voice_paths.append(
                    _render_tts_segment(
                        segment_index,
                        segment_text,
                        sample_wav,
                        segment_dir,
                        job_dir,
                        env,
                        use_cuda,
                        render_config=config,
                        cached_conditioning=cached_conditioning,
                    )
                )
            except Exception as exc:
                message = str(exc)
                update_segment_status(job_dir, segment_index, status="failure", phase="Generating TTS", message=message)
                update_step_status(job_dir, "step_3_coqui_tts", "failure", message, detail=message)
                append_job_log(job_dir, f"TTS segment failed: {message}")
                raise
    elif use_process_pool:
        with ProcessPoolExecutor(
            max_workers=concurrency,
            initializer=_init_tts_process_worker,
            initargs=(
                job_dir,
                sample_wav,
                use_cuda,
                bool(config.get("use_inprocess_tts", True)),
                bool(config.get("use_cached_xtts_conditioning", True)),
            ),
        ) as executor:
            futures = {}
            for segment_index, segment_text in enumerate(script_segments):
                segment_dir = os.path.join(segments_root, f"segment_{segment_index:03d}")
                os.makedirs(segment_dir, exist_ok=True)
                segment_script_path = os.path.join(segment_dir, "script.txt")
                with open(segment_script_path, "w", encoding="utf-8") as file_obj:
                    file_obj.write(segment_text)
                update_segment_status(
                    job_dir,
                    segment_index,
                    status="running",
                    phase="Queued for process TTS",
                    message="Queued for warm XTTS process worker.",
                    files={"script": os.path.relpath(segment_script_path, job_dir)},
                )
                task_args = (
                    segment_index,
                    segment_text,
                    sample_wav,
                    segment_dir,
                    job_dir,
                    env,
                    use_cuda,
                    config,
                )
                futures[executor.submit(_render_tts_segment_process_task, task_args)] = segment_index
            for future in as_completed(futures):
                segment_index = futures[future]
                try:
                    result_index, path = future.result()
                    duration_seconds = get_wav_duration_seconds(path)
                    update_segment_status(
                        job_dir,
                        result_index,
                        status="success",
                        phase="TTS ready",
                        message=f"Audio duration {duration_seconds:.2f}s.",
                        duration_seconds=duration_seconds,
                        files={"voice": os.path.relpath(path, job_dir)},
                    )
                    segment_voice_paths.append((result_index, path))
                except Exception as exc:
                    message = str(exc)
                    update_segment_status(job_dir, segment_index, status="failure", phase="Generating TTS", message=message)
                    update_step_status(job_dir, "step_3_coqui_tts", "failure", message, detail=message)
                    append_job_log(job_dir, f"TTS segment failed: {message}")
                    raise

    if segment_voice_paths and isinstance(segment_voice_paths[0], tuple):
        segment_voice_paths = [path for _, path in sorted(segment_voice_paths, key=lambda item: item[0])]

    update_step_status(job_dir, "step_3_coqui_tts", "running", "Merging output: combining TTS chunks into voice.wav.")
    append_job_log(job_dir, "Step 3 phase: Merging output")
    out_voice = os.path.join(job_dir, "voice.wav")
    try:
        _concat_wav_files(segment_voice_paths, out_voice, job_dir)
        total_duration = get_wav_duration_seconds(out_voice)
        append_job_log(job_dir, f"Step 3 total audio duration: {total_duration:.2f}s")
        append_job_log(job_dir, "TTS merge completed successfully.")
    except subprocess.CalledProcessError as exc:
        message = f"TTS merge failed: {exc.stderr}"
        append_job_log(job_dir, message)
        update_step_status(job_dir, "step_3_coqui_tts", "failure", message, detail=str(exc))
        raise RuntimeError(message) from exc

    update_step_status(job_dir, "step_3_coqui_tts", "success", "TTS generated and merged successfully.")
    return out_voice
