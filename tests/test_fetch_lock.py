"""Exercise fetch locking without accessing the real feed or archive."""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
FAKE_CURL = '''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
if os.environ.get("FETCH_TEST_READY"):
    Path(os.environ["FETCH_TEST_READY"]).touch()
    time.sleep(30)
body = {"schema_version": 1, "generated_at": "2026-09-25T00:00:00Z",
        "range": {"from": "2026-09-24T00:00:00Z", "to": "2026-09-25T00:00:00Z"},
        "records": []}
Path(sys.argv[sys.argv.index("--output") + 1]).write_text(json.dumps(body))
'''


class FetchLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="piyolog-lock-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copy2(ROOT / "fetch-piyolog.sh", self.root / "fetch-piyolog.sh")
        (self.root / ".env").write_text("PIYOLOG_FEED_URL=https://example.invalid/feed\n")
        self.archive = self.root / "data" / "piyolog"
        self.archive.mkdir(parents=True)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        fake_curl = bin_dir / "curl"
        fake_curl.write_text(FAKE_CURL)
        fake_curl.chmod(0o700)
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
        self.env.pop("FETCH_TEST_READY", None)
        self.command = ["bash", str(self.root / "fetch-piyolog.sh")]

    def fetch(self):
        return subprocess.run(self.command, env=self.env, capture_output=True, text=True, timeout=5)

    def start_blocked_fetch(self):
        ready = self.root / "ready"
        proc = subprocess.Popen(
            self.command, env=dict(self.env, FETCH_TEST_READY=str(ready)),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        def stop_test_processes():
            # This is the isolated process group created by this test only.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)

        self.addCleanup(stop_test_processes)
        deadline = time.monotonic() + 5
        while not ready.exists():
            if proc.poll() is not None or time.monotonic() > deadline:
                self.fail("Mock curl did not start")
            time.sleep(0.02)
        return proc

    def test_concurrent_fetch_is_rejected(self):
        self.start_blocked_fetch()
        result = self.fetch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("another fetch is already running", result.stderr)
        self.assertEqual(list(self.archive.glob("*.json")), [])

    def test_killed_parent_releases_lock_even_while_curl_is_running(self):
        proc = self.start_blocked_fetch()
        proc.kill()
        proc.wait(timeout=5)
        # The old mock curl still runs. It must not inherit the lock descriptor.
        result = self.fetch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(list(self.archive.glob("*.json")))

    def test_old_lock_directory_and_persistent_lock_file_do_not_block(self):
        (self.archive / ".fetch.lock").mkdir()
        for _ in range(2):
            result = self.fetch()
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.archive / ".fetch.flock").is_file())
        self.assertTrue((self.archive / ".fetch.lock").is_dir())


if __name__ == "__main__":
    unittest.main()
