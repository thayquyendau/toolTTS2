import os
import shutil
import subprocess
import sys
import tempfile
import wave
import hashlib
import json
from datetime import datetime
from pathlib import Path
from pathlib import PurePosixPath

import modal
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from job_status import load_job_status, save_job_status, set_overall_status, update_audio_preview, update_step_status


APP_NAME = os.getenv("MODAL_APP_NAME", "tooltucode-gpu-v2")
MODEL_CACHE_VOLUME = "tooltucode-model-cache"
XTTS_ARTIFACT_VOLUME = os.getenv("MODAL_XTTS_ARTIFACT_VOLUME", "tooltucode-xtts-artifacts")
JOB_VOLUME_NAME = os.getenv("MODAL_TTS_JOB_VOLUME", "tooltucode-tts-jobs")
REMOTE_ROOT = PurePosixPath("/root")
REMOTE_WORKER = REMOTE_ROOT / "worker"
REMOTE_LIVEPORTRAIT = REMOTE_WORKER / "LivePortrait"
REMOTE_XTTS_ARTIFACTS = PurePosixPath("/xtts-artifacts")
REMOTE_JOB_ROOT = PurePosixPath("/jobs")

LOCAL_WORKER = Path(__file__).resolve().parents[1]
LOCAL_PIPELINE_STEPS = LOCAL_WORKER / "pipeline_steps"
LOCAL_LIVEPORTRAIT = LOCAL_WORKER / "LivePortrait"

volume = modal.Volume.from_name(MODEL_CACHE_VOLUME, create_if_missing=True)
xtts_artifact_volume = modal.Volume.from_name(XTTS_ARTIFACT_VOLUME, create_if_missing=True, version=2)
job_volume = modal.Volume.from_name(JOB_VOLUME_NAME, create_if_missing=True, version=2)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .env({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
    .apt_install("ffmpeg", "git", "libgl1", "libglib2.0-0", "libsndfile1")
    .pip_install(
        "TTS==0.22.0",
        "transformers==4.33.2",
        "torch==2.4.1",
        "torchaudio==2.4.1",
        "torchvision==0.19.1",
        "faster-whisper",
        "numpy==1.26.4",
        "opencv-python-headless==4.10.0.84",
        "scikit-image==0.22.0",
        "imageio==2.34.2",
        "imageio-ffmpeg==0.4.9",
        "pydub==0.25.1",
        "pysbd==0.3.4",
        "yacs==0.1.8",
        "kornia==0.6.8",
        "resampy==0.4.3",
        "face-alignment==1.3.5",
        "librosa==0.10.2.post1",
        "pyyaml",
        "tqdm",
        "safetensors",
        "av",
        "facexlib==0.3.0",
        "requests==2.32.3",
    )
    .add_local_dir(str(LOCAL_PIPELINE_STEPS), remote_path=str(REMOTE_WORKER / "pipeline_steps"))
    .add_local_file(str(LOCAL_WORKER / "job_status.py"), remote_path=str(REMOTE_WORKER / "job_status.py"))
    .add_local_file(str(LOCAL_WORKER / "modal_costs.py"), remote_path=str(REMOTE_WORKER / "modal_costs.py"))
    .add_local_file(str(LOCAL_WORKER / "modal_gpu_client.py"), remote_path=str(REMOTE_WORKER / "modal_gpu_client.py"))
)

if LOCAL_LIVEPORTRAIT.exists():
    image = image.add_local_dir(str(LOCAL_LIVEPORTRAIT), remote_path=str(REMOTE_LIVEPORTRAIT))

app = modal.App(APP_NAME, image=image)


def _configure_runtime() -> None:
    sys.path.insert(0, str(REMOTE_WORKER))
    os.environ.setdefault("COQUI_TOS_AGREED", "1")
    os.environ.setdefault("TTS_HOME", "/cache/coqui")
    os.environ.setdefault("HF_HOME", "/cache/huggingface")
    os.environ.setdefault("XDG_CACHE_HOME", "/cache")
    os.environ.setdefault("TTS_WORKER_OUTPUT_DIR", str(REMOTE_JOB_ROOT))


def _log_xtts_dependency_report() -> None:
    import torch
    import transformers

    try:
        import TTS as tts_package
        tts_version = getattr(tts_package, "__version__", "unknown")
    except Exception as exc:
        tts_version = f"unavailable: {exc}"

    has_beam_search_scorer = hasattr(transformers, "BeamSearchScorer")
    print(
        "[Modal] XTTS dependency report: "
        f"TTS={tts_version}, "
        f"torch={torch.__version__}, "
        f"transformers={transformers.__version__}, "
        f"BeamSearchScorer={has_beam_search_scorer}",
        flush=True,
    )
    if not has_beam_search_scorer:
        raise RuntimeError(
            "Modal XTTS dependency mismatch: transformers does not expose BeamSearchScorer. "
            "Use TTS==0.22.0 with transformers==4.33.2."
        )


def _write_bytes(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as file_obj:
        file_obj.write(data)


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as file_obj:
        return file_obj.read()


def _wav_duration_seconds(path: str) -> float:
    with wave.open(path, "rb") as wav_file:
        return wav_file.getnframes() / float(wav_file.getframerate())


def _probe_media_duration_seconds(path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def _format_timestamp(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millisecs = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millisecs:03d}"


def _concat_wavs(wav_paths: list[str], output_path: str) -> None:
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


def _xtts_model(use_cuda: bool = True):
    _configure_runtime()
    _log_xtts_dependency_report()
    from TTS.api import TTS

    return TTS("tts_models/multilingual/multi-dataset/xtts_v2", gpu=use_cuda)


def _voice_cache_key(voice_sample_bytes: bytes) -> str:
    return hashlib.sha256(voice_sample_bytes).hexdigest()


def _xtts_artifact_prefix(render_config: dict) -> str:
    prefix = str(render_config.get("modal_xtts_artifact_prefix", "xtts-jobs")).strip("/ ")
    return prefix or "xtts-jobs"


def _xtts_artifact_subpath(render_config: dict, job_id: str, segment_index: int) -> str:
    return f"{_xtts_artifact_prefix(render_config)}/{job_id}/segment_{segment_index:03d}/voice.wav"


def _xtts_metadata_subpath(render_config: dict, job_id: str, segment_index: int) -> str:
    return f"{_xtts_artifact_prefix(render_config)}/{job_id}/segment_{segment_index:03d}/metadata.json"


def _xtts_artifact_mount_path(subpath: str) -> str:
    relative_path = PurePosixPath(subpath)
    return str(REMOTE_XTTS_ARTIFACTS.joinpath(*relative_path.parts))


def _render_xtts_segment_payload(
    segment_index: int,
    segment_text: str,
    voice_sample_bytes: bytes,
    render_config: dict,
    tts,
    conditioning_cache: dict[str, tuple],
) -> dict:
    _configure_runtime()
    from pipeline_steps.step_3_tts import clean_and_chunk_text, _save_xtts_wav

    work_dir = tempfile.mkdtemp(prefix=f"tooltucode_xtts_segment_{segment_index:03d}_")
    try:
        voice_sample_path = os.path.join(work_dir, "input_voice.wav")
        prepared_sample_path = os.path.join(work_dir, "sample_prepared.wav")
        _write_bytes(voice_sample_path, voice_sample_bytes)
        subprocess.run(
            ["ffmpeg", "-y", "-i", voice_sample_path, "-ar", "22050", "-ac", "1", prepared_sample_path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        cache_key = _voice_cache_key(voice_sample_bytes)
        cached_conditioning = conditioning_cache.get(cache_key)
        if cached_conditioning is None:
            xtts_model = getattr(getattr(tts, "synthesizer", None), "tts_model", None)
            config = getattr(xtts_model, "config", None)
            if xtts_model is not None and config is not None:
                gpt_cond_latent, speaker_embedding = xtts_model.get_conditioning_latents(
                    audio_path=prepared_sample_path,
                    gpt_cond_len=getattr(config, "gpt_cond_len", 6),
                    max_ref_length=getattr(config, "max_ref_len", 30),
                    sound_norm_refs=getattr(config, "sound_norm_refs", False),
                )
                cached_conditioning = (xtts_model, config, gpt_cond_latent, speaker_embedding)
                conditioning_cache[cache_key] = cached_conditioning

        segment_dir = os.path.join(work_dir, f"segment_{segment_index:03d}")
        os.makedirs(segment_dir, exist_ok=True)
        max_chars = int(render_config.get("max_tts_chunk_chars", 250))
        strategy = str(render_config.get("chunking_strategy", "pysbd"))
        chunks = clean_and_chunk_text(segment_text, max_length=max_chars, strategy=strategy)
        if not chunks:
            raise RuntimeError(f"Segment {segment_index + 1} is empty after text cleanup.")

        chunk_paths = []
        for chunk_index, chunk in enumerate(chunks):
            chunk_path = os.path.join(segment_dir, f"chunk_{chunk_index}.wav")
            if cached_conditioning:
                xtts_model, config, gpt_cond_latent, speaker_embedding = cached_conditioning
                out = xtts_model.inference(
                    text=chunk,
                    language="en",
                    gpt_cond_latent=gpt_cond_latent,
                    speaker_embedding=speaker_embedding,
                    temperature=getattr(config, "temperature", 0.75),
                    length_penalty=getattr(config, "length_penalty", 1.0),
                    repetition_penalty=getattr(config, "repetition_penalty", 10.0),
                    top_k=getattr(config, "top_k", 50),
                    top_p=getattr(config, "top_p", 0.85),
                )
                _save_xtts_wav(out["wav"], chunk_path, sample_rate=24000)
            else:
                tts.tts_to_file(text=chunk, speaker_wav=prepared_sample_path, language="en", file_path=chunk_path)
            chunk_paths.append(chunk_path)

        segment_voice_path = os.path.join(segment_dir, "voice.wav")
        _concat_wavs(chunk_paths, segment_voice_path)
        job_id = str(render_config.get("job_id") or "modal-job")
        artifact_subpath = _xtts_artifact_subpath(render_config, job_id, segment_index)
        metadata_subpath = _xtts_metadata_subpath(render_config, job_id, segment_index)
        artifact_path = _xtts_artifact_mount_path(artifact_subpath)
        metadata_path = _xtts_artifact_mount_path(metadata_subpath)
        os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
        os.makedirs(os.path.dirname(metadata_path), exist_ok=True)
        shutil.copy2(segment_voice_path, artifact_path)
        metadata = {
            "index": segment_index,
            "artifact_volume": str(render_config.get("modal_xtts_artifact_volume") or XTTS_ARTIFACT_VOLUME),
            "volume_subpath": artifact_subpath,
            "duration_seconds": _wav_duration_seconds(segment_voice_path),
            "chunk_count": len(chunks),
        }
        with open(metadata_path, "w", encoding="utf-8") as file_obj:
            json.dump(metadata, file_obj)
        xtts_artifact_volume.commit()
        return metadata
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _job_dir(job_id: str) -> str:
    return str(REMOTE_JOB_ROOT / str(job_id))


def _prime_script_waiting_state(job_dir: str) -> None:
    status_data = load_job_status(job_dir)
    if status_data is None:
        raise FileNotFoundError(f"Status file not found: {job_dir}")
    if status_data.get("script_data") is None:
        status_data["script_data"] = ""
        status_data["script_approved"] = False
        save_job_status(job_dir, status_data)
    update_step_status(
        job_dir,
        "step_2_manual_script",
        "waiting_approval",
        "Waiting for user approval: manual script before TTS.",
    )


def _voice_file_extension(filename: str) -> str:
    suffix = Path(str(filename or "")).suffix.lower()
    return suffix if suffix and len(suffix) <= 12 else ".mp3"


def _allowed_voice_sample_hosts() -> tuple[str, ...]:
    configured = os.getenv("MODAL_VOICE_SAMPLE_ALLOWED_HOSTS", ".public.blob.vercel-storage.com")
    return tuple(host.strip().lower() for host in configured.split(",") if host.strip())


def _validate_voice_sample_url(voice_sample_url: str) -> None:
    parsed = requests.utils.urlparse(voice_sample_url)
    hostname = str(parsed.hostname or "").lower()
    if parsed.scheme != "https" or not hostname:
        raise ValueError("voice_sample_url must be an absolute HTTPS URL.")
    allowed_hosts = _allowed_voice_sample_hosts()
    if not allowed_hosts:
        raise ValueError("MODAL_VOICE_SAMPLE_ALLOWED_HOSTS must contain at least one host.")
    if not any(
        hostname == allowed.lstrip(".") or (allowed.startswith(".") and hostname.endswith(allowed))
        for allowed in allowed_hosts
    ):
        raise ValueError("voice_sample_url host is not allowed.")


def _voice_download_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _download_voice_sample_to_job(voice_sample_url: str, voice_sample_filename: str, job_dir: str) -> str:
    _validate_voice_sample_url(voice_sample_url)
    max_bytes = int(os.getenv("MODAL_VOICE_SAMPLE_MAX_BYTES", str(200 * 1024 * 1024)))
    output_path = os.path.join(job_dir, f"input_voice{_voice_file_extension(voice_sample_filename)}")
    session = _voice_download_session()
    try:
        with session.get(voice_sample_url, stream=True, timeout=(10, 120)) as response:
            response.raise_for_status()
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type and not (content_type.startswith("audio/") or content_type == "application/octet-stream"):
                raise ValueError(f"Voice sample returned unsupported content type: {content_type}")
            expected_length = int(response.headers.get("Content-Length") or 0)
            if expected_length and expected_length > max_bytes:
                raise ValueError("Voice sample exceeds the configured maximum size.")

            total_bytes = 0
            with open(output_path, "wb") as file_obj:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total_bytes += len(chunk)
                    if total_bytes > max_bytes:
                        raise ValueError("Voice sample exceeds the configured maximum size.")
                    file_obj.write(chunk)
            if total_bytes == 0:
                raise ValueError("Voice sample download returned an empty file.")
            return output_path
    except Exception:
        if os.path.exists(output_path):
            os.remove(output_path)
        raise
    finally:
        session.close()


@app.cls(
    gpu=os.getenv("MODAL_TTS_GPU", "L4"),
    volumes={
        "/cache": volume,
        str(REMOTE_XTTS_ARTIFACTS): xtts_artifact_volume,
    },
    timeout=60 * 60,
    scaledown_window=10 * 60,
)
class XttsSegmentRenderer:
    @modal.enter()
    def setup(self):
        _configure_runtime()
        self.tts = _xtts_model(use_cuda=True)
        self.conditioning_cache = {}

    @modal.method()
    def render(self, segment_index: int, segment_text: str, voice_sample_bytes: bytes, render_config: dict) -> dict:
        return _render_xtts_segment_payload(
            segment_index=segment_index,
            segment_text=segment_text,
            voice_sample_bytes=voice_sample_bytes,
            render_config=render_config,
            tts=self.tts,
            conditioning_cache=self.conditioning_cache,
        )


@app.function(
    gpu=os.getenv("MODAL_TTS_GPU", "L4"),
    volumes={
        "/cache": volume,
        str(REMOTE_XTTS_ARTIFACTS): xtts_artifact_volume,
    },
    timeout=60 * 60,
    scaledown_window=10 * 60,
)
def render_xtts_segments(script_segments: list[str], voice_sample_bytes: bytes, render_config: dict) -> list[dict]:
    _configure_runtime()
    try:
        tts = _xtts_model(use_cuda=True)
        conditioning_cache = {}
        results = []
        for segment_index, segment_text in enumerate(script_segments):
            results.append(
                _render_xtts_segment_payload(
                    segment_index=segment_index,
                    segment_text=segment_text,
                    voice_sample_bytes=voice_sample_bytes,
                    render_config=render_config,
                    tts=tts,
                    conditioning_cache=conditioning_cache,
                )
            )
        return results
    finally:
        pass


@app.function(
    gpu=os.getenv("MODAL_LIVEPORTRAIT_GPU", "A10"),
    volumes={"/cache": volume},
    timeout=60 * 60,
    scaledown_window=10 * 60,
)
def render_liveportrait_segment(image_bytes: bytes, voice_wav_bytes: bytes, render_config: dict) -> dict:
    _configure_runtime()
    work_dir = tempfile.mkdtemp(prefix="tooltucode_lp_")
    try:
        image_path = os.path.join(work_dir, "source.jpg")
        voice_path = os.path.join(work_dir, "voice.wav")
        output_path = os.path.join(work_dir, "video_raw.mp4")
        _write_bytes(image_path, image_bytes)
        _write_bytes(voice_path, voice_wav_bytes)
        cmd = [
            sys.executable,
            str(REMOTE_LIVEPORTRAIT / "inference.py"),
            "--source_image",
            image_path,
            "--driving_audio",
            voice_path,
            "--output",
            output_path,
            "--device",
            "cuda",
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError("LivePortrait did not produce a non-empty video.")
        volume.commit()
        return {
            "video_mp4_bytes": _read_bytes(output_path),
            "duration_seconds": _probe_media_duration_seconds(output_path),
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@app.function(
    gpu=os.getenv("MODAL_WHISPER_GPU", "L4"),
    volumes={"/cache": volume},
    timeout=30 * 60,
    scaledown_window=10 * 60,
)
def transcribe_whisper_srt(voice_wav_bytes: bytes, render_config: dict) -> dict:
    _configure_runtime()
    from faster_whisper import WhisperModel

    work_dir = tempfile.mkdtemp(prefix="tooltucode_whisper_")
    try:
        voice_path = os.path.join(work_dir, "voice.wav")
        _write_bytes(voice_path, voice_wav_bytes)
        model = WhisperModel("base", device="cuda", compute_type="float16", download_root="/cache/whisper")
        segments, _info = model.transcribe(
            voice_path,
            beam_size=5,
            vad_filter=True,
            word_timestamps=True,
        )
        segments = list(segments)
        srt_lines = []
        word_timings = []
        for index, segment in enumerate(segments, start=1):
            srt_lines.append(str(index))
            srt_lines.append(f"{_format_timestamp(segment.start)} --> {_format_timestamp(segment.end)}")
            srt_lines.append(segment.text.strip())
            srt_lines.append("")
            for word in getattr(segment, "words", []) or []:
                word_text = str(getattr(word, "word", "") or "").strip()
                if not word_text:
                    continue
                word_timings.append(
                    {
                        "text": word_text,
                        "start_seconds": float(word.start),
                        "end_seconds": float(word.end),
                    }
                )
        volume.commit()
        return {
            "subtitle_srt_text": "\n".join(srt_lines),
            "segment_count": len(segments),
            "word_timings": word_timings,
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@app.function(
    volumes={
        "/cache": volume,
        str(REMOTE_JOB_ROOT): job_volume,
    },
    timeout=20 * 60,
    scaledown_window=10 * 60,
)
def run_pipeline_step_1(
    job_id: str,
    youtube_url: str,
    voice_sample_url: str | None = None,
    voice_sample_filename: str = "",
    render_config: dict | None = None,
) -> dict:
    _configure_runtime()
    from pipeline_steps.common import normalize_render_config
    from pipeline_steps.step_1_transcript import step_1_get_transcript

    job_dir = _job_dir(job_id)
    os.makedirs(job_dir, exist_ok=True)
    try:
        status_data = load_job_status(job_dir)
        if status_data is not None:
            status_data["step_1_spawn_status"] = "running"
            save_job_status(job_dir, status_data)
        if voice_sample_url:
            set_overall_status(job_dir, "running", "Downloading voice sample from Blob.")
            _download_voice_sample_to_job(voice_sample_url, voice_sample_filename, job_dir)
        elif not any(name.startswith("input_voice.") for name in os.listdir(job_dir)):
            raise FileNotFoundError("Voice sample file not found for job.")
        set_overall_status(job_dir, "running", "Fetching transcript from YouTube.")
        transcript_path = step_1_get_transcript(youtube_url, job_dir)
        _prime_script_waiting_state(job_dir)
        status_data = load_job_status(job_dir)
        if status_data is not None:
            status_data["step_1_spawn_status"] = "completed"
            status_data["step_1_completed_at"] = datetime.now().isoformat() + "Z"
            save_job_status(job_dir, status_data)
        job_volume.commit()
        return {
            "job_id": job_id,
            "transcript_path": transcript_path,
            "render_config": normalize_render_config(render_config),
        }
    except Exception as exc:
        set_overall_status(job_dir, "failed", str(exc), error_detail=str(exc))
        status_data = load_job_status(job_dir)
        if status_data is not None:
            status_data["step_1_spawn_status"] = "failed"
            status_data["step_1_completed_at"] = datetime.now().isoformat() + "Z"
            save_job_status(job_dir, status_data)
        job_volume.commit()
        raise


@app.function(
    gpu=os.getenv("MODAL_TTS_GPU", "L4"),
    volumes={
        "/cache": volume,
        str(REMOTE_XTTS_ARTIFACTS): xtts_artifact_volume,
        str(REMOTE_JOB_ROOT): job_volume,
    },
    timeout=60 * 60,
    scaledown_window=10 * 60,
)
def run_pipeline_step_3(job_id: str, render_config: dict | None = None) -> dict:
    _configure_runtime()
    from pipeline_steps.common import normalize_render_config
    from pipeline_steps.step_3_tts import step_3_coqui_tts

    job_dir = _job_dir(job_id)
    config = normalize_render_config(render_config)
    script_path = os.path.join(job_dir, "script.txt")
    voice_sample_path = None
    for candidate_name in os.listdir(job_dir):
        if candidate_name.startswith("input_voice."):
            voice_sample_path = os.path.join(job_dir, candidate_name)
            break
    if voice_sample_path is None:
        raise FileNotFoundError("Voice sample file not found for job.")
    if not os.path.exists(script_path):
        raise FileNotFoundError("script.txt not found for job.")
    try:
        status_data = load_job_status(job_dir)
        if status_data is not None:
            status_data["step_3_spawn_status"] = "running"
            save_job_status(job_dir, status_data)
        set_overall_status(job_dir, "running", "Generating audio on Modal.")
        voice_output_path = step_3_coqui_tts(script_path, voice_sample_path, job_dir, config)
        update_audio_preview(
            job_dir,
            {
                "file": os.path.basename(voice_output_path),
                "url": f"/job/{job_id}/audio/file",
            },
        )
        set_overall_status(
            job_dir,
            "completed",
            "TTS pipeline completed.",
            result_file=os.path.basename(voice_output_path),
        )
        status_data = load_job_status(job_dir)
        if status_data is not None:
            status_data["step_3_spawn_status"] = "completed"
            status_data["step_3_completed_at"] = datetime.now().isoformat() + "Z"
            save_job_status(job_dir, status_data)
        job_volume.commit()
        return {
            "job_id": job_id,
            "result_file": os.path.basename(voice_output_path),
        }
    except Exception as exc:
        set_overall_status(job_dir, "failed", str(exc), error_detail=str(exc))
        status_data = load_job_status(job_dir)
        if status_data is not None:
            status_data["step_3_spawn_status"] = "failed"
            status_data["step_3_completed_at"] = datetime.now().isoformat() + "Z"
            save_job_status(job_dir, status_data)
        job_volume.commit()
        raise
