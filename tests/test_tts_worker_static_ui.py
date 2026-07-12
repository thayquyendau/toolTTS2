import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "static"


class TtsWorkerStaticUiTests(unittest.TestCase):
    def test_index_exposes_tts_worker_surfaces(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="job-form"', html)
        self.assertIn('id="youtube_url"', html)
        self.assertIn('id="voice_sample"', html)
        self.assertIn('id="settings-open"', html)
        self.assertIn('id="btn-modal-deploy"', html)
        self.assertIn('id="btn-modal-token-link"', html)
        self.assertIn('id="transcript-text"', html)
        self.assertIn('id="script-text"', html)
        self.assertIn('id="audio-player"', html)
        self.assertIn('id="log-output"', html)

        self.assertNotIn('id="image"', html)
        self.assertNotIn('id="job_mode"', html)
        self.assertNotIn('id="prompt-text"', html)

    def test_frontend_modules_target_tts_only_flow(self):
        dom_js = (STATIC_DIR / "js" / "dom.js").read_text(encoding="utf-8")
        job_form_js = (STATIC_DIR / "js" / "job-form.js").read_text(encoding="utf-8")
        polling_js = (STATIC_DIR / "js" / "polling.js").read_text(encoding="utf-8")
        deploy_js = (STATIC_DIR / "js" / "deploy.js").read_text(encoding="utf-8")

        self.assertIn("transcriptText", dom_js)
        self.assertIn("deployModalAppNameInput", dom_js)
        self.assertNotIn("imageInput", dom_js)
        self.assertNotIn("jobModeSelect", dom_js)

        self.assertIn('apiFetch("/generate"', job_form_js)
        self.assertNotIn('formData.set("video_style"', job_form_js)
        self.assertNotIn('formData.delete("youtube_url")', job_form_js)

        self.assertIn("loadTranscript", polling_js)
        self.assertNotIn("loadPrompt", polling_js)

        self.assertIn('"step_3"', deploy_js)
        self.assertNotIn('"step_4"', deploy_js)
        self.assertNotIn('"step_5"', deploy_js)

    def test_backend_mounts_static_control_room_and_modal_support(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")

        self.assertIn('app.mount("/static"', main_text)
        self.assertIn('RedirectResponse(url="/static/index.html")', main_text)
        self.assertIn('@app.post("/modal/deploy")', main_text)
        self.assertIn('@app.post("/modal/token/new")', main_text)
        self.assertNotIn("image: UploadFile", main_text)


if __name__ == "__main__":
    unittest.main()
