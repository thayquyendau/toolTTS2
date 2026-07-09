import os

from job_status import append_job_log, set_render_config, update_audio_preview
from pipeline_steps.common import normalize_render_config
from pipeline_steps.step_1_transcript import step_1_get_transcript
from pipeline_steps.step_2_manual_script import wait_for_script_approval
from pipeline_steps.step_3_tts import step_3_coqui_tts


def run_tts_pipeline(
    youtube_url: str,
    voice_sample_path: str,
    job_dir: str,
    render_config: dict | None = None,
) -> str:
    config = normalize_render_config(render_config)
    set_render_config(job_dir, config)
    append_job_log(job_dir, "TTS pipeline started.")
    try:
        transcript_path = step_1_get_transcript(youtube_url, job_dir)
        script_path = wait_for_script_approval(transcript_path, job_dir)
        voice_path = step_3_coqui_tts(script_path, voice_sample_path, job_dir, config)
        job_id = os.path.basename(job_dir)
        update_audio_preview(
            job_dir,
            {
                "file": os.path.basename(voice_path),
                "url": f"/job/{job_id}/audio/file",
            },
        )
        append_job_log(job_dir, "TTS pipeline finished successfully.")
        return voice_path
    except Exception as exc:
        append_job_log(job_dir, f"TTS pipeline failed: {exc}")
        raise
