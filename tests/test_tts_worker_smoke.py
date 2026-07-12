import io
import os
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from unittest.mock import patch


WORKER_DIR = Path(__file__).resolve().parents[1]
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))


def _write_wav(path: str, duration_seconds: float = 0.1, sample_rate: int = 22050) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frame_count = max(1, int(duration_seconds * sample_rate))
    silence_frame = struct.pack("<h", 0)
    with wave.open(path, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(silence_frame * frame_count)


def test_run_tts_pipeline_returns_voice_wav_after_manual_script_gate():
    from job_status import init_job_status, load_job_status
    from pipeline_steps import orchestrator
    from pipeline_steps import step_1_transcript as transcript_step
    from pipeline_steps import step_3_tts as tts_step

    with tempfile.TemporaryDirectory() as temp_dir:
        job_dir = os.path.join(temp_dir, "job")
        os.makedirs(job_dir, exist_ok=True)
        init_job_status(job_dir, "tts-job")

        voice_sample_path = os.path.join(job_dir, "input_voice.wav")
        _write_wav(voice_sample_path)

        script_text = "A short approved script for XTTS."

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffmpeg" and "sample_prepared.wav" in cmd[-1]:
                _write_wav(cmd[-1])
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if "TTS.bin.synthesize" in cmd:
                out_path = cmd[cmd.index("--out_path") + 1]
                _write_wav(out_path)
                return subprocess.CompletedProcess(cmd, 0, "tts ok", "")
            if cmd[0] == "ffmpeg" and "concat" in cmd:
                _write_wav(cmd[-1])
                return subprocess.CompletedProcess(cmd, 0, "", "")
            raise AssertionError(f"Unexpected subprocess command: {cmd}")

        with patch.object(
            transcript_step,
            "_download_transcript_with_innertube",
            return_value="Transcript text for manual script mode.",
        ), patch.object(
            orchestrator,
            "wait_for_script_approval",
            return_value=os.path.join(job_dir, "script.txt"),
        ), patch.object(
            tts_step,
            "wait_for_approval",
            return_value={"script_data": script_text},
        ), patch.object(
            tts_step.torch.cuda,
            "is_available",
            return_value=True,
        ), patch.object(
            tts_step.torch.cuda,
            "get_device_name",
            return_value="Smoke GPU",
        ), patch.object(
            tts_step.subprocess,
            "run",
            side_effect=fake_run,
        ):
            script_path = os.path.join(job_dir, "script.txt")
            Path(script_path).write_text(script_text, encoding="utf-8")
            voice_path = orchestrator.run_tts_pipeline(
                youtube_url="https://www.youtube.com/watch?v=abc123xyz00",
                voice_sample_path=voice_sample_path,
                job_dir=job_dir,
                render_config={"gpu_backend": "local", "tts_concurrency": 1, "use_inprocess_tts": False},
            )

        assert os.path.exists(voice_path)
        status = load_job_status(job_dir)
        assert status["status"] in {"pending", "running"}
        assert next(step for step in status["steps"] if step["key"] == "step_3_coqui_tts")["status"] == "success"
