import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from piyolog.advisor.config import load_config, load_llm_settings


class AdvisorConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "advisor.toml"

    def test_defaults_and_dates(self):
        self.path.write_text("[child]\nbirth_date = 2026-01-01\ncorrected_birth_date = 2026-02-01\n")
        config = load_config(self.path)
        self.assertEqual(config.child.birth_date, date(2026, 1, 1))
        self.assertEqual(config.child.corrected_birth_date, date(2026, 2, 1))
        self.assertEqual(config.analysis.days, 14)
        self.assertFalse(config.privacy.allow_external_llm)

    def test_quoted_iso_date(self):
        self.path.write_text("[child]\nbirth_date = '2026-01-01'\n")
        self.assertEqual(load_config(self.path).child.birth_date, date(2026, 1, 1))

    def test_strict_types_and_unknown_keys(self):
        invalid = (
            "[child]\nbirth_date = '2026-02-30'\n",
            "[child]\nbirth_date = 2026-01-01\n[analysis]\ndays = true\n",
            "[child]\nbirth_date = 2026-01-01\n[privacy]\nallow_external_llm = 1\n",
            "[child]\nbirth_date = 2026-01-01\n[analysis]\ndyas = 3\n",
            "[child]\nbirth_date = 2026-01-01\n[analysis]\ntimezone = 'No/Such_Zone'\n",
            "[child]\nbirth_date = 2026-01-01\n[llm]\nmax_output_tokens = 15\n",
        )
        for source in invalid:
            with self.subTest(source=source):
                self.path.write_text(source)
                with self.assertRaises(ValueError):
                    load_config(self.path)

    def test_env_indirection_and_safe_url(self):
        env_file = Path(self.temp.name) / "settings.env"
        env_file.write_text(
            "ADVISOR_LLM_BASE_URL=https://example.test/v1\n"
            "ADVISOR_LLM_MODEL=gpt-6-astra\n"
            "ADVISOR_LLM_API_KEY_ENV=MY_LLM_API_KEY\n"
            "MY_LLM_API_KEY=file-key\n"
        )
        with patch.dict(os.environ, {"MY_LLM_API_KEY": "os-key"}, clear=True):
            settings = load_llm_settings(env_file)
        self.assertEqual(settings.api_key, "os-key")
        self.assertEqual(settings.model, "gpt-6-astra")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_llm_settings(env_file).api_key, "file-key")

    def test_default_and_responses_provider_are_equivalent(self):
        env_file = Path(self.temp.name) / "settings.env"
        common = (
            "ADVISOR_LLM_BASE_URL=https://example.test/v1\n"
            "ADVISOR_LLM_MODEL=model\n"
            "ADVISOR_LLM_API_KEY=key\n"
        )
        with patch.dict(os.environ, {}, clear=True):
            env_file.write_text(common)
            default_settings = load_llm_settings(env_file)
            env_file.write_text(common + "ADVISOR_LLM_PROVIDER=responses\n")
            explicit_settings = load_llm_settings(env_file)
        self.assertEqual(default_settings.provider, "responses")
        self.assertEqual(default_settings, explicit_settings)

    def test_responses_provider_os_environment_takes_precedence(self):
        env_file = Path(self.temp.name) / "settings.env"
        env_file.write_text(
            "ADVISOR_LLM_PROVIDER=unsupported\n"
            "ADVISOR_LLM_BASE_URL=https://example.test/v1\n"
            "ADVISOR_LLM_MODEL=model\n"
            "ADVISOR_LLM_API_KEY=key\n"
        )
        with patch.dict(os.environ, {"ADVISOR_LLM_PROVIDER": "responses"}, clear=True):
            settings = load_llm_settings(env_file)
        self.assertEqual(settings.provider, "responses")

    def test_unsupported_provider_error_does_not_echo_value(self):
        env_file = Path(self.temp.name) / "settings.env"
        env_file.write_text("ADVISOR_LLM_PROVIDER=private-provider-name\n")
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError) as caught:
            load_llm_settings(env_file, require_credentials=False)
        self.assertEqual(str(caught.exception), "ADVISOR_LLM_PROVIDER must be responses")
        self.assertNotIn("private-provider-name", str(caught.exception))

    def test_dry_run_settings_and_url_rejection(self):
        env_file = Path(self.temp.name) / "settings.env"
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_llm_settings(env_file, require_credentials=False).model, "")
            with self.assertRaises(ValueError):
                load_llm_settings(env_file)
        for url in ("http://example.test/v1", "https://user:pw@example.test/v1", "https://example.test/v1?key=x", "https://example.test/a b"):
            env_file.write_text(f"ADVISOR_LLM_BASE_URL={url}\n")
            with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
                load_llm_settings(env_file, require_credentials=False)

    def test_api_key_control_character_never_appears_in_error(self):
        env_file = Path(self.temp.name) / "settings.env"
        env_file.write_text("ADVISOR_LLM_BASE_URL=https://example.test/v1\nADVISOR_LLM_MODEL=model\n")
        secret = "private-token\r\nX-Injected: yes"
        with patch.dict(os.environ, {"ADVISOR_LLM_API_KEY": secret}, clear=True):
            with self.assertRaises(ValueError) as caught:
                load_llm_settings(env_file)
        self.assertNotIn("private-token", str(caught.exception))
        self.assertNotIn("X-Injected", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
