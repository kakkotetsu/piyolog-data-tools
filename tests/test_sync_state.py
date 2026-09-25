"""Destination state and process-lock tests using temporary data and mock HTTP."""

from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import sync_victorialogs as sync


URL_A = "http://localhost:9428"
URL_B = "http://localhost:19428"
RECORD = {"event_id": "test-event", "datetime": "2026-09-25T01:00:00Z", "type": "Solid", "memo": "test"}
SNAPSHOT_AT = "2026-09-25T02:00:00Z"


def run_main(project, args, sender):
    output = io.StringIO()
    with (
        patch.object(sync, "PROJECT_DIR", project),
        patch("sys.argv", ["sync_victorialogs.py", *args]),
        patch.object(sync, "post_json_lines", side_effect=sender) as post,
        redirect_stdout(output), redirect_stderr(output),
    ):
        status = sync.main()
    return status, output.getvalue(), post


def blocking_sync(project, args, started, release):
    def sender(*_):
        started.set()
        if not release.wait(15):
            raise RuntimeError("Test timed out waiting to release mock delivery")

    status, _, _ = run_main(project, args, sender)
    raise SystemExit(status)


class SyncStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.data = self.project / "data"
        self.data.mkdir()
        self.state = self.project / "state.sqlite3"
        self.snapshot = self.data / "snapshot.json"
        self.write_snapshot()
        self.args = ["--data-dir", str(self.data), "--state-file", str(self.state)]

    def write_snapshot(self, record=None):
        self.snapshot.write_text(sync.canonical_json({
            "generated_at": SNAPSHOT_AT, "records": [record or RECORD],
        }), encoding="utf-8")

    def run_sync(self, url=URL_A, extra=(), sender=lambda *_: None):
        return run_main(self.project, [*self.args, "--url", url, *extra], sender)

    def rows(self):
        with closing(sqlite3.connect(self.state)) as connection:
            return connection.execute("SELECT destination_url, event_id, payload_hash FROM delivered_event ORDER BY destination_url").fetchall()

    def create_legacy(self):
        with closing(sqlite3.connect(self.state)) as connection, connection:
            connection.execute("""
                CREATE TABLE delivered_event (
                    event_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
                    snapshot_at TEXT NOT NULL, source_file TEXT NOT NULL,
                    delivered_at TEXT NOT NULL
                )
            """)
            connection.execute("INSERT INTO delivered_event VALUES (?, ?, ?, ?, ?)", (
                RECORD["event_id"], sync.payload_hash(RECORD), SNAPSHOT_AT, self.snapshot.name, SNAPSHOT_AT,
            ))

    def test_destination_normalization(self):
        for raw, expected in (
            ("HTTP://LOCALHOST:80/", "http://localhost"),
            ("https://EXAMPLE.test:443/prefix///", "https://example.test/prefix"),
            ("http://[::1]:9428/", "http://[::1]:9428"),
            ("http://localhost:9428/", URL_A),
        ):
            with self.subTest(url=raw):
                self.assertEqual(sync.normalize_destination(raw), expected)
        for raw in ("file:///tmp/logs", "http://", "http://example:bad", "http://user:secret@example", "http://example?q=1", "http://example#part", " http://example"):
            with self.subTest(url=raw), self.assertRaises(RuntimeError):
                sync.normalize_destination(raw)

    def test_destination_switch_and_switch_back(self):
        for url, calls in ((URL_A, 1), (URL_A + "/", 0), (URL_B, 1), (URL_A, 0), (URL_A + "/prefix", 1)):
            with self.subTest(url=url):
                status, output, post = self.run_sync(url)
                self.assertEqual(status, 0, output)
                self.assertEqual(post.call_count, calls)
        self.assertEqual(len(self.rows()), 3)

    def test_edits_are_pending_independently_per_destination(self):
        self.run_sync(URL_A)
        self.run_sync(URL_B)
        self.write_snapshot({**RECORD, "memo": "updated"})
        status, output, post = self.run_sync(URL_A)
        self.assertEqual(status, 0, output)
        post.assert_called_once()
        digests = {url: digest for url, _, digest in self.rows()}
        self.assertNotEqual(digests[URL_A], digests[URL_B])
        status, output, post = self.run_sync(URL_B)
        self.assertEqual(status, 0, output)
        post.assert_called_once()
        self.assertEqual(len({digest for _, _, digest in self.rows()}), 1)

    def test_failure_can_retry_and_dry_run_debug_do_not_mark_delivered(self):
        for extra, calls in ((("--dry-run",), 0), (("--debug",), 1)):
            status, output, post = self.run_sync(extra=extra)
            self.assertEqual(status, 0, output)
            self.assertEqual(post.call_count, calls)
            self.assertEqual(self.rows(), [])
        status, _, post = self.run_sync(sender=RuntimeError("mock HTTP failure"))
        self.assertEqual(status, 1)
        post.assert_called_once()
        self.assertEqual(self.rows(), [])
        status, output, post = self.run_sync()
        self.assertEqual(status, 0, output)
        post.assert_called_once()
        self.assertEqual(len(self.rows()), 1)

    def test_debug_still_sends_after_all_events_are_delivered(self):
        self.run_sync()
        before = self.state.read_bytes()
        status, output, post = self.run_sync(extra=("--debug",))
        self.assertEqual(status, 0, output)
        post.assert_called_once_with(URL_A, [sync.to_log_record(RECORD, SNAPSHOT_AT, self.snapshot.name)], None, True)
        self.assertIn("Debug sample: 1 of 1", output)
        self.assertIn("HTTP success", output)
        self.assertIn("server logs", output)
        self.assertEqual(self.state.read_bytes(), before)
        status, output, post = self.run_sync()
        self.assertEqual(status, 0, output)
        post.assert_not_called()

    def test_debug_selects_five_newest_events_with_latest_payloads(self):
        records = [
            {**RECORD, "event_id": f"event-{hour}", "datetime": f"2026-09-25T0{hour}:00:00Z"}
            for hour in range(7)
        ]
        records.append({**RECORD, "event_id": "offset-event", "datetime": "2026-09-25T10:30:00+09:00"})
        self.snapshot.write_text(sync.canonical_json({"generated_at": SNAPSHOT_AT, "records": records}))
        (self.data / "newer.json").write_text(sync.canonical_json({
            "generated_at": "2026-09-26T02:00:00Z",
            "records": [{**records[6], "memo": "edited"}, {**records[0], "memo": "also edited"}],
        }))
        status, output, post = self.run_sync(extra=("--debug",))
        self.assertEqual(status, 0, output)
        post.assert_called_once()
        logs = post.call_args.args[1]
        self.assertEqual([row["event_id"] for row in logs], [f"event-{hour}" for hour in (6, 5, 4, 3, 2)])
        self.assertEqual(logs[0]["memo"], "edited")
        self.assertEqual(logs[0]["source_file"], "newer.json")
        self.assertIn("Debug sample: 5 of 8", output)
        self.assertFalse(self.state.exists())

    def test_debug_does_not_open_state_or_create_state_directory(self):
        state = self.project / "unused" / "state.sqlite3"
        with patch.object(sync, "open_state", side_effect=AssertionError("Debug must not open state")):
            status, output, post = self.run_sync(extra=("--debug", "--state-file", str(state)))
        self.assertEqual(status, 0, output)
        post.assert_called_once()
        self.assertFalse(state.parent.exists())

    def test_debug_works_without_migrating_legacy_state(self):
        self.create_legacy()
        before = self.state.read_bytes()
        status, output, post = self.run_sync(extra=("--debug",))
        self.assertEqual(status, 0, output)
        post.assert_called_once()
        self.assertEqual(self.state.read_bytes(), before)

    def test_debug_no_events_is_an_error_without_request(self):
        self.snapshot.write_text(sync.canonical_json({"generated_at": SNAPSHOT_AT, "records": []}))
        status, output, post = self.run_sync(extra=("--debug",))
        self.assertEqual(status, 1)
        post.assert_not_called()
        self.assertIn("No archived events", output)
        self.assertIn("no request was sent", output)
        self.assertFalse(self.state.exists())

    def test_debug_failed_request_is_an_error_without_state_change(self):
        self.run_sync()
        before = self.state.read_bytes()
        status, output, post = self.run_sync(extra=("--debug",), sender=RuntimeError("mock HTTP failure"))
        self.assertEqual(status, 1)
        post.assert_called_once()
        self.assertNotIn("HTTP success", output)
        self.assertEqual(self.state.read_bytes(), before)

    def test_debug_invalid_event_datetime_does_not_send(self):
        for timestamp in ("invalid", "2026-09-25T10:00:00", None):
            with self.subTest(timestamp=timestamp):
                self.write_snapshot({**RECORD, "datetime": timestamp})
                status, output, post = self.run_sync(extra=("--debug",))
                self.assertEqual(status, 1)
                self.assertIn("Invalid event datetime", output)
                post.assert_not_called()

    def test_debug_http_parameter_and_payload(self):
        logs = [sync.to_log_record(RECORD, SNAPSHOT_AT, self.snapshot.name)]
        for debug in (True, False):
            with self.subTest(debug=debug), patch.object(sync, "urlopen") as urlopen:
                urlopen.return_value.__enter__.return_value.status = 204
                sync.post_json_lines(URL_A, logs, None, debug)
                request = urlopen.call_args.args[0]
                query = parse_qs(urlsplit(request.full_url).query)
                self.assertEqual(query.get("debug"), ["1"] if debug else None)
                self.assertEqual(query["_time_field"], ["datetime"])
                self.assertEqual(query["_msg_field"], ["message"])
                self.assertEqual(urlsplit(request.full_url).path, "/insert/jsonline")
                self.assertEqual([json.loads(line) for line in request.data.decode().splitlines()], logs)

    def test_legacy_state_requires_explicit_destination(self):
        self.create_legacy()
        status, output, post = self.run_sync(URL_B)
        self.assertEqual(status, 1)
        self.assertIn("--migrate-state-url", output)
        post.assert_not_called()
        with closing(sqlite3.connect(self.state)) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(delivered_event)")}
            self.assertNotIn("destination_url", columns)
            self.assertEqual(connection.execute("SELECT count(*) FROM delivered_event").fetchone()[0], 1)

    def test_migration_preserves_original_and_never_sends(self):
        self.create_legacy()
        status, output, post = run_main(self.project, [*self.args, "--migrate-state-url", URL_A + "/"], None)
        self.assertEqual(status, 0, output)
        post.assert_not_called()
        self.assertEqual(self.rows(), [(URL_A, RECORD["event_id"], sync.payload_hash(RECORD))])
        with closing(sqlite3.connect(self.state)) as connection:
            old = connection.execute("SELECT * FROM delivered_event_legacy").fetchall()
            new = connection.execute("SELECT event_id, payload_hash, snapshot_at, source_file, delivered_at FROM delivered_event").fetchall()
            self.assertEqual(old, new)
        status, output, post = self.run_sync(URL_A)
        self.assertEqual(status, 0, output)
        post.assert_not_called()
        status, output, post = self.run_sync(URL_B)
        self.assertEqual(status, 0, output)
        post.assert_called_once()
        status, _, post = run_main(self.project, [*self.args, "--migrate-state-url", URL_B], None)
        self.assertEqual(status, 1)
        post.assert_not_called()
        self.assertEqual(len(self.rows()), 2)

    def test_failed_migration_rolls_back_schema_changes(self):
        self.create_legacy()
        with closing(sqlite3.connect(self.state)) as connection, connection:
            connection.execute("UPDATE delivered_event SET event_id = NULL")
        with self.assertRaises(sqlite3.IntegrityError):
            sync.open_state(self.state, URL_A)
        with closing(sqlite3.connect(self.state)) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(delivered_event)")}
            self.assertNotIn("destination_url", columns)
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE name='delivered_event_legacy'").fetchall(), [])

    def start_blocking_sync(self):
        context = multiprocessing.get_context("fork")
        started, release = context.Event(), context.Event()
        process = context.Process(target=blocking_sync, args=(self.project, [*self.args, "--url", URL_A], started, release))
        process.start()

        def cleanup():
            release.set()
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            process.close()

        self.addCleanup(cleanup)
        self.assertTrue(started.wait(5), "Mock delivery never started")
        return process, release

    def test_overlapping_run_is_rejected_until_delivery_and_commit_complete(self):
        process, release = self.start_blocking_sync()
        status, output, post = self.run_sync()
        self.assertEqual(status, 1)
        self.assertIn("already running", output)
        post.assert_not_called()
        self.assertEqual(self.rows(), [])
        release.set()
        process.join(timeout=5)
        self.assertEqual(process.exitcode, 0)
        status, output, post = self.run_sync()
        self.assertEqual(status, 0, output)
        post.assert_not_called()
        self.assertEqual(len(self.rows()), 1)

    def test_symlink_to_state_uses_same_lock(self):
        self.start_blocking_sync()
        alias = self.project / "alias.sqlite3"
        alias.symlink_to(self.state)
        status, output, post = run_main(self.project, [*self.args, "--state-file", str(alias), "--url", URL_A], None)
        self.assertEqual(status, 1)
        self.assertIn("already running", output)
        post.assert_not_called()

    def test_killed_process_releases_lock_and_allows_retry(self):
        process, _ = self.start_blocking_sync()
        process.kill()
        process.join(timeout=5)
        self.assertNotEqual(process.exitcode, 0)
        self.assertTrue(Path(str(self.state) + ".lock").exists())
        status, output, post = self.run_sync()
        self.assertEqual(status, 0, output)
        post.assert_called_once()
        self.assertEqual(len(self.rows()), 1)


if __name__ == "__main__":
    unittest.main()
