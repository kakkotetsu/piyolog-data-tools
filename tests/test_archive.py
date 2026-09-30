"""Archive selection tests use generated temporary snapshots only."""

import json
from pathlib import Path
import tempfile
import unittest

from piyolog.archive import load_analysis_archive, load_latest_records
from sync_victorialogs import load_latest_records as sync_loader


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def write(self, name, generated, records, bounds=None):
        body = {"generated_at": generated, "records": records}
        if bounds is not None:
            body["range"] = {"from": bounds[0], "to": bounds[1]}
        (self.path / name).write_text(json.dumps(body), encoding="utf-8")

    def test_newest_uses_aware_time_and_keeps_sync_signature(self):
        old = {"event_id": "same", "datetime": "2026-09-23T10:00:00Z", "type": "Pee"}
        new = {**old, "datetime": "2026-09-24T10:00:00Z", "type": "Poop"}
        self.write("first.json", "2026-09-24T01:00:00+09:00", [old])
        self.write("second.json", "2026-09-23T17:00:00Z", [new])
        selected = load_latest_records(self.path)
        self.assertIs(sync_loader, load_latest_records)
        self.assertEqual(selected["same"], (new, "2026-09-23T17:00:00Z", "second.json"))

    def test_analysis_range_required_without_changing_sync(self):
        self.write("first.json", "2026-09-24T00:00:00Z", [{"event_id": "x"}])
        self.assertIn("x", sync_loader(self.path))
        with self.assertRaisesRegex(RuntimeError, "Missing range"):
            load_analysis_archive(self.path)

    def test_error_does_not_echo_private_content(self):
        self.write("private-name.json", "SECRET", [{"event_id": "x"}], ("bad", "bad"))
        with self.assertRaises(RuntimeError) as caught:
            load_analysis_archive(self.path)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertNotIn("private-name", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
