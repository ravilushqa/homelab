import unittest
from pathlib import Path

import yaml


class PaperlessGPTComposeTests(unittest.TestCase):
    def test_retry_guards_and_ocr_model_bounds(self):
        compose = yaml.safe_load((Path(__file__).resolve().parents[1] / "compose.yaml").read_text())
        service = compose["services"]["paperless-gpt"]
        self.assertEqual(service["image"], "icereed/paperless-gpt:v0.31.0")
        env = service["environment"]
        expected = {
            "OCR_MAX_RETRIES": "1",
            "AUTO_TAG_MAX_RETRIES": "1",
            "VISION_LLM_MAX_RETRIES": "0",
            "LLM_MAX_RETRIES": "1",
            "OCR_PROVIDER": "llm",
            "LLM_PROVIDER": "openai",
            "LLM_MODEL": "openai/gpt-6-luna",
            "VISION_LLM_PROVIDER": "openai",
            "VISION_LLM_MODEL": "openai/gpt-6.1-terra",
            "OCR_LIMIT_PAGES": "5",
            "IMAGE_MAX_TOTAL_PIXELS": "10000000",
            "VISION_LLM_MAX_TOKENS": "16384",
        }
        self.assertNotIn("LLM_MAX_TOKENS", env)
        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertEqual(env.get(key), value)


if __name__ == "__main__":
    unittest.main()
