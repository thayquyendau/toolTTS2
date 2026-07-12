import io
import os
import sys
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


WORKER_DIR = Path(__file__).resolve().parents[1]
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))


def test_generate_accepts_youtube_url_and_voice_sample_without_image():
    from main import app

    client = TestClient(app)

    with patch("main.create_job_dir", return_value=os.path.join(str(WORKER_DIR), "output", "video-999")), \
         patch("main.init_job_status"), \
         patch("main.save_upload_file"), \
         patch("main.set_overall_status"), \
         patch.object(type(app.state.executor), "submit", return_value=None, create=True):
        response = client.post(
            "/generate",
            data={
                "youtube_url": "https://www.youtube.com/watch?v=abc123xyz00",
                "gpu_backend": "modal",
            },
            files={
                "voice_sample": ("voice.wav", io.BytesIO(b"fake wav bytes"), "audio/wav"),
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["job_id"] == "video-999"
    assert payload["status_url"] == "/job/video-999"


def test_app_config_disables_blob_upload_without_token_on_vercel():
    from main import app

    client = TestClient(app)

    with patch.dict(os.environ, {"VERCEL": "1"}, clear=False):
        with patch("main.list_modal_profiles", return_value={"profiles": [], "default_profile": None}):
            response = client.get("/app-config")

    assert response.status_code == 200
    payload = response.json()
    assert payload["use_blob_upload"] is False
    assert payload["blob_upload_url"] is None
    assert payload["modal_token_linking_enabled"] is False


def test_app_config_enables_blob_upload_with_token_on_vercel():
    from main import app

    client = TestClient(app)

    with patch.dict(os.environ, {"VERCEL": "1", "BLOB_READ_WRITE_TOKEN": "token"}, clear=False):
        with patch("main.list_modal_profiles", return_value={"profiles": [], "default_profile": None}):
            response = client.get("/app-config")

    assert response.status_code == 200
    payload = response.json()
    assert payload["use_blob_upload"] is True
    assert payload["blob_upload_url"] == "/api/blob/upload"


def test_generate_with_blob_dispatches_modal_work_without_downloading_on_vercel():
    from main import app

    client = TestClient(app)
    fake_call = type("Call", (), {"object_id": "fc-step-1"})()

    with patch.dict(os.environ, {"VERCEL": "1"}, clear=False), \
         patch("main.init_modal_job"), \
         patch("main.modal_set_job_input"), \
         patch("main.prepare_step_1_spawn"), \
         patch("main.spawn_modal_job_function", return_value=fake_call) as spawn, \
         patch("main.record_step_1_call"), \
         patch("main.modal_append_log"), \
         patch("main.create_modal_job_id", return_value="video-modal-1"), \
         patch("main._upload_voice_sample_to_modal") as upload:
        response = client.post(
            "/generate",
            json={
                "youtube_url": "https://www.youtube.com/watch?v=abc123xyz00",
                "voice_sample_url": "https://store.public.blob.vercel-storage.com/voice.wav",
                "voice_sample_filename": "voice.wav",
            },
        )

    assert response.status_code == 200
    assert response.json()["job_id"] == "video-modal-1"
    assert spawn.call_args.args[1] == "run_pipeline_step_1"
    upload.assert_not_called()


def test_generate_with_multipart_uploads_voice_then_uses_same_modal_dispatch():
    from main import app

    client = TestClient(app)
    fake_call = type("Call", (), {"object_id": "fc-step-1-multipart"})()

    with patch.dict(os.environ, {"TTS_JOB_BACKEND": "modal", "VERCEL": "0"}, clear=False), \
         patch("main.init_modal_job"), \
         patch("main.modal_set_job_input"), \
         patch("main.prepare_step_1_spawn"), \
         patch("main.spawn_modal_job_function", return_value=fake_call) as spawn, \
         patch("main.record_step_1_call"), \
         patch("main.modal_append_log"), \
         patch("main.create_modal_job_id", return_value="video-modal-2"), \
         patch("main._upload_voice_sample_to_modal", return_value="input_voice.wav") as upload:
        response = client.post(
            "/generate",
            data={"youtube_url": "https://www.youtube.com/watch?v=abc123xyz00"},
            files={"voice_sample": ("voice.wav", io.BytesIO(b"fake wav bytes"), "audio/wav")},
        )

    assert response.status_code == 200
    assert response.json()["job_id"] == "video-modal-2"
    upload.assert_called_once()
    assert spawn.call_args.args[1] == "run_pipeline_step_1"


def test_modal_token_linking_is_disabled_on_vercel():
    from main import app

    client = TestClient(app)
    with patch.dict(os.environ, {"VERCEL": "1"}, clear=False):
        response = client.post("/modal/token/new", json={})

    assert response.status_code == 410
