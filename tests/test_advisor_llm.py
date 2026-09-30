import io
import json
import unittest
from datetime import date
from http.client import IncompleteRead
from urllib.error import HTTPError
from unittest.mock import patch

from piyolog.advisor.config import AdvisorConfig, ChildConfig, LLMSettings
from piyolog.advisor.llm import MAX_RESPONSE_BYTES, _NoRedirect, build_request_body, generate_advice


class _Response:
    status = 200

    def __init__(self, data):
        self.stream = io.BytesIO(data)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        return self.stream.read(limit)


class AdvisorLLMTests(unittest.TestCase):
    def setUp(self):
        self.settings = LLMSettings("responses", "https://example.test/v1", "gpt-6-astra", "secret")

    def test_request_body(self):
        body = build_request_body({"value": "日本語"}, self.settings, AdvisorConfig(ChildConfig(date(2026, 1, 1))))
        self.assertEqual(body["model"], "gpt-6-astra")
        self.assertFalse(body["store"])
        self.assertEqual(json.loads(body["input"]), {"value": "日本語"})
        self.assertIn("医療指示", body["instructions"])

    def test_dry_run_body_without_model_cannot_send(self):
        blank = LLMSettings("responses", "", "", "")
        body = build_request_body({}, blank, AdvisorConfig(ChildConfig(date(2026, 1, 1))))
        self.assertIn("ドライラン", body["model"])
        with self.assertRaises(ValueError):
            generate_advice(body, blank, timeout_seconds=10)

    def test_response_and_request_headers(self):
        result = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "助言"}]}]}
        class Opener:
            def open(self, request, *, timeout):
                self.request = request
                self.timeout = timeout
                return _Response(json.dumps(result).encode())
        opener = Opener()
        with patch("piyolog.advisor.llm.build_opener", return_value=opener):
            self.assertEqual(generate_advice({"model": "gpt-6-astra"}, self.settings, timeout_seconds=10), "助言")
        self.assertEqual(opener.request.full_url, "https://example.test/v1/responses")
        self.assertEqual(opener.request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(opener.timeout, 10)

    def test_incomplete_refusal_and_empty_fail(self):
        bad = (
            {"status": "incomplete", "output": []},
            {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]},
            {"status": "completed", "output": []},
        )
        for result in bad:
            class Opener:
                def open(self, *args, **kwargs):
                    return _Response(json.dumps(result).encode())
            with self.subTest(result=result), patch("piyolog.advisor.llm.build_opener", return_value=Opener()):
                with self.assertRaises(ValueError):
                    generate_advice({}, self.settings, timeout_seconds=10)

    def test_redirect_and_http_error_are_sanitized(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test"))
        class Opener:
            def open(self, *args, **kwargs):
                raise HTTPError("https://secret.test", 302, "secret-token", {}, None)
        with patch("piyolog.advisor.llm.build_opener", return_value=Opener()):
            with self.assertRaises(ValueError) as caught:
                generate_advice({}, self.settings, timeout_seconds=10)
        self.assertEqual(str(caught.exception), "LLM request failed")

    def test_response_size_limit(self):
        class Opener:
            def open(self, *args, **kwargs):
                return _Response(b" " * (MAX_RESPONSE_BYTES + 1))
        with patch("piyolog.advisor.llm.build_opener", return_value=Opener()):
            with self.assertRaisesRegex(ValueError, "too large"):
                generate_advice({}, self.settings, timeout_seconds=10)

    def test_request_and_response_transport_errors_are_sanitized(self):
        settings = LLMSettings("responses", "https://example.test/v1", "model", "private-token\r\nX-Injected: yes")
        with self.assertRaises(ValueError) as caught:
            generate_advice({}, settings, timeout_seconds=10)
        self.assertEqual(str(caught.exception), "LLM request failed")

        class Opener:
            def open(self, *args, **kwargs):
                class Broken(_Response):
                    def read(self, limit):
                        raise IncompleteRead(b"private-token", limit)
                return Broken(b"")
        with patch("piyolog.advisor.llm.build_opener", return_value=Opener()):
            with self.assertRaises(ValueError) as caught:
                generate_advice({}, self.settings, timeout_seconds=10)
        self.assertEqual(str(caught.exception), "LLM request failed")


if __name__ == "__main__":
    unittest.main()
