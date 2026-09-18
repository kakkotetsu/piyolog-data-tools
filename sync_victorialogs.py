#!/usr/bin/env python3
"""Synchronize archived Piyolog feed records to VictoriaLogs.

The archive JSON files remain the source of truth.  This script chooses the
newest snapshot for every event_id and stores the last delivered payload hash
in a local SQLite database, so unchanged events aren't sent repeatedly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = PROJECT_DIR / "data" / "piyolog"
DEFAULT_STATE_FILE = DEFAULT_DATA_DIR / "victorialogs-state.sqlite3"
DEFAULT_VICTORIALOGS_URL = "http://127.0.0.1:9428"

TYPE_LABELS = {
    "Pee": "おしっこ",
    "Poop": "うんち",
    "Solid": "離乳食",
    "Bath": "入浴",
    "Sleep": "就寝",
    "WakeUp": "起床",
    "BreastFeeding": "授乳",
    "Milk": "ミルク",
    "Temperature": "体温",
    "Height": "身長",
    "Weight": "体重",
    "Hospital": "病院",
    "Walking": "さんぽ",
    "Vaccine": "予防接種",
    "Milestone": "できた",
}


def read_env_value(name: str, env_file: Path) -> str | None:
    """Read one unquoted KEY=value entry without executing .env."""
    if not env_file.is_file():
        return None
    prefix = f"{name}="
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    return None


def apply_proxy_settings(env_file: Path) -> None:
    """Apply optional proxy settings from .env to urllib for this process."""
    https_proxy = read_env_value("HTTPS_PROXY", env_file)
    no_proxy = read_env_value("NO_PROXY", env_file)
    if https_proxy:
        # urllib gives lower-case variables precedence and uses this for HTTPS URLs.
        os.environ["https_proxy"] = https_proxy
    if no_proxy:
        os.environ["no_proxy"] = no_proxy


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(record: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(record).encode("utf-8")).hexdigest()


def format_seconds(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return ""
    seconds = round(value)
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}分{seconds:02d}秒" if seconds else f"{minutes}分"


def make_message(record: dict[str, Any]) -> str:
    record_type = str(record.get("type", "Unknown"))
    label = TYPE_LABELS.get(record_type, record_type)
    if record_type == "BreastFeeding":
        sides = []
        for side, key in (("左", "leftTime"), ("右", "rightTime")):
            duration = format_seconds(record.get(key))
            if duration:
                sides.append(f"{side} {duration}")
        return f"{label}（{'、'.join(sides)}）" if sides else label
    if record_type == "Temperature":
        value = record.get("value")
        if isinstance(value, dict) and "value" in value:
            unit = "°C" if value.get("unit") == "celsius" else str(value.get("unit", ""))
            return f"{label} {value['value']}{unit}"
    return label


def flatten(value: Any, prefix: str, output: dict[str, Any]) -> None:
    """Flatten nested Piyolog fields for easier LogSQL filtering."""
    if isinstance(value, dict):
        for key, child in value.items():
            flatten(child, f"{prefix}_{key}", output)
    elif isinstance(value, list):
        output[prefix] = canonical_json(value)
    elif value is not None:
        output[prefix] = value


def to_log_record(record: dict[str, Any], snapshot_at: str, source_file: str) -> dict[str, Any]:
    log_record: dict[str, Any] = {
        "stream": "piyolog",
        "datetime": record["datetime"],
        "message": make_message(record),
        "event_id": record["event_id"],
        "type": record["type"],
        "snapshot_at": snapshot_at,
        "source_file": source_file,
    }
    for key, value in record.items():
        if key not in {"event_id", "datetime", "type"}:
            flatten(value, key, log_record)
    return log_record


def load_latest_records(data_dir: Path) -> dict[str, tuple[dict[str, Any], str, str]]:
    latest: dict[str, tuple[dict[str, Any], str, str]] = {}
    for json_file in sorted(data_dir.glob("*.json")):
        try:
            snapshot = json.loads(json_file.read_text(encoding="utf-8"))
            generated_at = snapshot["generated_at"]
            records = snapshot["records"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RuntimeError(f"Cannot read a Piyolog feed snapshot: {json_file.name}") from exc
        if not isinstance(generated_at, str) or not isinstance(records, list):
            raise RuntimeError(f"Invalid Piyolog feed snapshot: {json_file.name}")
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("event_id"), str):
                raise RuntimeError(f"Invalid event in snapshot: {json_file.name}")
            candidate = (record, generated_at, json_file.name)
            previous = latest.get(record["event_id"])
            # generated_at is RFC3339 UTC, so lexical ordering is chronological.
            if previous is None or (generated_at, json_file.name) >= (previous[1], previous[2]):
                latest[record["event_id"]] = candidate
    return latest


def open_state(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS delivered_event (
            event_id TEXT PRIMARY KEY,
            payload_hash TEXT NOT NULL,
            snapshot_at TEXT NOT NULL,
            source_file TEXT NOT NULL,
            delivered_at TEXT NOT NULL
        )
        """
    )
    return connection


def pending_records(
    connection: sqlite3.Connection,
    latest: dict[str, tuple[dict[str, Any], str, str]],
) -> list[tuple[str, str, str, str, dict[str, Any]]]:
    pending = []
    for event_id, (record, snapshot_at, source_file) in latest.items():
        digest = payload_hash(record)
        row = connection.execute(
            "SELECT payload_hash FROM delivered_event WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None or row[0] != digest:
            pending.append((event_id, digest, snapshot_at, source_file, record))
    return sorted(pending, key=lambda item: (item[4]["datetime"], item[0]))


def post_json_lines(url: str, logs: list[dict[str, Any]], bearer_token: str | None, debug: bool) -> None:
    base_url = url.rstrip("/") + "/insert/jsonline"
    params = {
        "_stream_fields": "stream",
        "_time_field": "datetime",
        "_msg_field": "message",
    }
    if debug:
        params["debug"] = "1"
    body = ("\n".join(canonical_json(log) for log in logs) + "\n").encode("utf-8")
    headers = {
        "Content-Type": "application/stream+json",
        "User-Agent": "piyolog-data-tools/1",
    }
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    request = Request(f"{base_url}?{urlencode(params)}", data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=30) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f"VictoriaLogs returned HTTP {response.status}")
    except HTTPError as exc:
        raise RuntimeError(f"VictoriaLogs returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError("Could not connect to VictoriaLogs") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync archived Piyolog records to VictoriaLogs.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be sent without sending it.")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Ask VictoriaLogs to validate input without storing it; does not update sync state.",
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--url", help="VictoriaLogs base URL; overrides VICTORIALOGS_URL.")
    args = parser.parse_args()
    if args.dry_run and args.debug:
        parser.error("--dry-run and --debug cannot be used together")

    env_file = PROJECT_DIR / ".env"
    apply_proxy_settings(env_file)
    url = args.url or read_env_value("VICTORIALOGS_URL", env_file) or DEFAULT_VICTORIALOGS_URL
    bearer_token = read_env_value("VICTORIALOGS_BEARER_TOKEN", env_file)
    try:
        latest = load_latest_records(args.data_dir)
        connection = open_state(args.state_file)
        with connection:
            pending = pending_records(connection, latest)
        type_counts = Counter(record[4].get("type", "Unknown") for record in pending)
        print(f"Latest events: {len(latest)}; pending delivery: {len(pending)}")
        if type_counts:
            print("Pending by type: " + ", ".join(f"{name}={count}" for name, count in sorted(type_counts.items())))
        if not pending or args.dry_run:
            return 0

        logs = [to_log_record(record, snapshot_at, source_file) for _, _, snapshot_at, source_file, record in pending]
        post_json_lines(url, logs, bearer_token, args.debug)
        if args.debug:
            print("VictoriaLogs accepted the debug request; no data or sync state was stored.")
            return 0

        delivered_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with connection:
            connection.executemany(
                """
                INSERT INTO delivered_event (event_id, payload_hash, snapshot_at, source_file, delivered_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    payload_hash = excluded.payload_hash,
                    snapshot_at = excluded.snapshot_at,
                    source_file = excluded.source_file,
                    delivered_at = excluded.delivered_at
                """,
                [(event_id, digest, snapshot_at, source_file, delivered_at) for event_id, digest, snapshot_at, source_file, _ in pending],
            )
        print(f"Delivered {len(pending)} event(s) to VictoriaLogs.")
        return 0
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if "connection" in locals():
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
