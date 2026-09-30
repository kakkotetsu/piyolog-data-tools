"""Advisor workflow checks using synthetic temporary files and mocked LLM calls."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import analyze_piyolog as cli


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 9, 30, 12, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


class AdvisorCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "archive"
        self.data.mkdir()
        self.output = self.root / "reports"
        self.config = self.root / "advisor.toml"
        self.env = self.root / ".env"
        self.env.write_text(
            "ADVISOR_LLM_PROVIDER=responses\nADVISOR_LLM_MODEL=synthetic-model\n"
            "ADVISOR_LLM_BASE_URL=https://example.invalid/v1\nADVISOR_LLM_API_KEY=synthetic-secret\n"
        )
        self.snapshot = self.data / "snapshot.json"
        self.snapshot.write_text(json.dumps({
            "schema_version": 1,
            "generated_at": "2026-09-12T01:00:00Z",
            "range": {"from": "2026-09-09T15:00:00Z", "to": "2026-09-11T15:00:00Z"},
            "records": [
                {"event_id": "private-id", "datetime": "2026-09-10T01:00:00Z", "type": "Solid", "memo": "synthetic-memo", "child_name": "synthetic-name"},
                {"event_id": "s", "datetime": "2026-09-09T15:00:00Z", "type": "Sleep"},
                {"event_id": "w", "datetime": "2026-09-10T00:00:00Z", "type": "WakeUp"},
                {"event_id": "s2", "datetime": "2026-09-10T15:00:00Z", "type": "Sleep"},
                {"event_id": "w2", "datetime": "2026-09-11T00:00:00Z", "type": "WakeUp"},
                {"event_id": "s3", "datetime": "2026-09-11T15:00:00Z", "type": "Sleep"},
            ],
        }))
        self.before = self.snapshot.read_bytes()
        self.write_config()

    def write_config(self, *, allow=False, details=False, memos=False):
        self.config.write_text(
            '[child]\nbirth_date = "2026-01-01"\n'
            '[analysis]\ndays = 2\ntimezone = "Asia/Tokyo"\nfocus = ["sleep"]\n'
            '[privacy]\n'
            f'allow_external_llm = {str(allow).lower()}\n'
            f'send_event_details = {str(details).lower()}\n'
            f'send_memos = {str(memos).lower()}\n'
        )

    def run_cli(self, *extra, sender=None):
        args = ["--config", str(self.config), "--env-file", str(self.env),
                "--data-dir", str(self.data), "--output-dir", str(self.output),
                "--end-date", "2026-09-12", *extra]
        out = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch.object(cli, "datetime", FixedDateTime), \
                patch.object(cli, "generate_advice", side_effect=sender) as generate, \
                redirect_stdout(out), redirect_stderr(out):
            status = cli.main(args)
        self.assertEqual(self.snapshot.read_bytes(), self.before)
        return status, out.getvalue(), generate

    def test_dry_run_works_without_credentials_and_excludes_private_fields(self):
        self.env.unlink()
        status, output, generate = self.run_cli("--dry-run")
        self.assertEqual(status, 0, output)
        generate.assert_not_called()
        run = next(self.output.iterdir())
        body = json.loads((run / "request.json").read_text())
        payload = json.loads(body["input"])
        self.assertFalse(body["store"])
        self.assertEqual(payload["profile"]["age_months"], 8)
        self.assertNotIn("events", payload["analysis"])
        self.assertNotIn("memos", payload["analysis"])
        text = (run / "request.json").read_text()
        for secret in ("2026-01-01", "private-id", "synthetic-memo", "synthetic-name", "snapshot.json"):
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, output)
        self.assertIn("AIは実行していません", (run / "report.md").read_text())
        self.assertEqual(run.stat().st_mode & 0o777, 0o700)
        for file in run.iterdir():
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_external_permission_is_required_before_network(self):
        status, output, generate = self.run_cli()
        self.assertEqual(status, 1)
        generate.assert_not_called()
        self.assertIn("allow_external_llm", output)
        self.assertFalse(self.output.exists())

    def test_success_saves_exact_sent_body_and_computed_report(self):
        self.write_config(allow=True)
        status, output, generate = self.run_cli(sender=lambda *a, **kw: "## 試せる工夫\n合成の提案です。")
        self.assertEqual(status, 0, output)
        generate.assert_called_once()
        run = next(self.output.iterdir())
        self.assertEqual(json.loads((run / "request.json").read_text()), generate.call_args.args[0])
        self.assertIn("合成の提案です", (run / "report.md").read_text())
        for path in run.iterdir():
            self.assertNotIn("synthetic-secret", path.read_text())
        self.assertNotIn("synthetic-secret", output)

    def test_failed_request_does_not_leave_completed_report(self):
        self.write_config(allow=True)
        status, output, generate = self.run_cli(sender=ValueError("LLM request failed"))
        self.assertEqual(status, 1)
        generate.assert_called_once()
        self.assertFalse(list(self.output.glob("*/report.md")))
        self.assertEqual(len(list(self.output.glob("*/request.json"))), 1)

    def test_optional_details_and_memos_have_independent_switches(self):
        for details, memos in ((True, False), (False, True), (True, True)):
            with self.subTest(details=details, memos=memos):
                self.write_config(details=details, memos=memos)
                status, output, generate = self.run_cli("--dry-run")
                self.assertEqual(status, 0, output)
                generate.assert_not_called()
                path = max(self.output.glob("*/request.json"), key=lambda p: p.stat().st_mtime_ns)
                analysis = json.loads(json.loads(path.read_text())["input"])["analysis"]
                self.assertEqual("events" in analysis, details)
                self.assertEqual("memos" in analysis, memos)
                self.assertEqual("synthetic-memo" in path.read_text(), memos)
                self.assertNotIn("private-id", path.read_text())

    def test_repeated_runs_keep_previous_reports(self):
        for _ in range(2):
            status, output, _ = self.run_cli("--dry-run")
            self.assertEqual(status, 0, output)
        self.assertEqual(len(list(self.output.glob("*/report.md"))), 2)

    def test_archive_output_and_symlink_alias_are_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.data, target_is_directory=True)
        for path in (self.data, self.data / "reports", alias / "reports"):
            with self.subTest(path=path):
                status, output, generate = self.run_cli("--dry-run", "--output-dir", str(path))
                self.assertEqual(status, 1)
                generate.assert_not_called()
                self.assertEqual(list(self.data.iterdir()), [self.snapshot])

    def test_future_end_date_and_invalid_days_fail_without_network(self):
        for extra in (("--end-date", "2026-10-01"), ("--days", "0"), ("--days", "91")):
            with self.subTest(extra=extra):
                status, output, generate = self.run_cli("--dry-run", *extra)
                self.assertEqual(status, 1)
                generate.assert_not_called()

    def test_missing_credentials_fail_before_creating_report(self):
        self.write_config(allow=True)
        self.env.unlink()
        status, output, generate = self.run_cli()
        self.assertEqual(status, 1)
        generate.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_disk_error_never_publishes_partial_report(self):
        path = self.root / "report.md"
        with patch.object(cli.os, "fsync", side_effect=OSError("synthetic disk full")):
            with self.assertRaises(OSError):
                cli.private_write(path, "partially written output")
        self.assertFalse(path.exists())
        self.assertEqual(list(self.root.glob(".report.md-*")), [])

    def test_publish_never_replaces_existing_report(self):
        path = self.root / "report.md"
        path.write_text("original")
        with self.assertRaises(FileExistsError):
            cli.private_write(path, "replacement")
        self.assertEqual(path.read_text(), "original")
        self.assertEqual(list(self.root.glob(".report.md-*")), [])


if __name__ == "__main__":
    unittest.main()
