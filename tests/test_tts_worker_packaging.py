import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class TtsWorkerPackagingTests(unittest.TestCase):
    def test_local_requirements_and_readme_guidance_exist(self):
        requirements_local = ROOT / "requirements-local.txt"
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertTrue(requirements_local.exists())
        self.assertIn("python main.py", readme)
        self.assertIn("requirements-local.txt", readme)

    def test_vercel_requirements_stay_runtime_focused(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

        self.assertIn("fastapi", requirements)
        self.assertIn("modal", requirements)
        self.assertNotIn("uvicorn", requirements)
        self.assertNotIn("torch", requirements)
        self.assertNotIn("TTS==", requirements)


if __name__ == "__main__":
    unittest.main()
