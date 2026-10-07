"""Tests for Gemini-specific application configuration."""

import os
import unittest
from unittest.mock import patch

from postradar.config import Settings


class SettingsTests(unittest.TestCase):
    def test_gemini_environment_values_are_loaded(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": "test-key",
                "GEMINI_PRIMARY_MODEL": "gemini-test-primary",
                "GEMINI_FALLBACK_MODEL": "gemini-test-fallback",
                "AI_EDIT_ENABLED": "false",
            },
        ):
            settings = Settings(_env_file=None)

        self.assertEqual(settings.gemini_api_key, "test-key")
        self.assertEqual(settings.gemini_primary_model, "gemini-test-primary")
        self.assertEqual(settings.gemini_fallback_model, "gemini-test-fallback")
        self.assertFalse(settings.ai_edit_enabled)
        self.assertFalse(hasattr(settings, "openai_api_key"))
        self.assertFalse(hasattr(settings, "openai_model"))

    def test_gemini_defaults(self) -> None:
        settings = Settings(_env_file=None)
        self.assertEqual(settings.gemini_api_key, "")
        self.assertEqual(settings.gemini_primary_model, "gemini-3.5-flash-lite")
        self.assertEqual(settings.gemini_fallback_model, "gemini-3.8-flash")
        self.assertTrue(settings.ai_edit_enabled)


if __name__ == "__main__":
    unittest.main()
