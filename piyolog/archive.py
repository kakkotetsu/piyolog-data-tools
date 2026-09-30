"""Read archived feed snapshots without modifying the originals."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def parse_timestamp(value: Any) -> datetime:
    """Parse an aware ISO 8601 timestamp, without reflecting input in errors."""
    if not isinstance(value, str):
        raise RuntimeError("Invalid timestamp in Piyolog snapshot.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RuntimeError("Invalid timestamp in Piyolog snapshot.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("Timestamp in Piyolog snapshot must have a timezone.")
    return parsed.astimezone(timezone.utc)


def iter_snapshots(data_dir: Path) -> Iterator[tuple[dict[str, Any], str]]:
    """Yield validated top-level snapshots and their private basenames."""
    for json_file in sorted(data_dir.glob("*.json")):
        try:
            snapshot = json.loads(json_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Cannot read a Piyolog feed snapshot.") from exc
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("generated_at"), str) or not isinstance(snapshot.get("records"), list):
            raise RuntimeError("Invalid Piyolog feed snapshot.")
        for record in snapshot["records"]:
            if not isinstance(record, dict) or not isinstance(record.get("event_id"), str):
                raise RuntimeError("Invalid event in Piyolog snapshot.")
        yield snapshot, json_file.name


def load_latest_records(data_dir: Path) -> dict[str, tuple[dict[str, Any], str, str]]:
    """Select the newest version of each event, including changed dates/types."""
    latest: dict[str, tuple[dict[str, Any], str, str]] = {}
    latest_keys: dict[str, tuple[datetime, str]] = {}
    for snapshot, basename in iter_snapshots(data_dir):
        generated_at = snapshot["generated_at"]
        key = (parse_timestamp(generated_at), basename)
        for record in snapshot["records"]:
            event_id = record["event_id"]
            if event_id not in latest_keys or key >= latest_keys[event_id]:
                latest_keys[event_id] = key
                latest[event_id] = (record, generated_at, basename)
    return latest


def load_analysis_archive(data_dir: Path) -> tuple[dict[str, tuple[dict[str, Any], str, str]], list[tuple[datetime, datetime]], datetime | None]:
    """Load latest events and validated coverage ranges for analysis."""
    latest: dict[str, tuple[dict[str, Any], str, str]] = {}
    latest_keys: dict[str, tuple[datetime, str]] = {}
    ranges: list[tuple[datetime, datetime]] = []
    latest_snapshot_at: datetime | None = None
    for snapshot, basename in iter_snapshots(data_dir):
        generated = parse_timestamp(snapshot["generated_at"])
        bounds = snapshot.get("range")
        if not isinstance(bounds, dict):
            raise RuntimeError("Missing range in Piyolog snapshot.")
        start = parse_timestamp(bounds.get("from"))
        end = parse_timestamp(bounds.get("to"))
        if end <= start:
            raise RuntimeError("Invalid range in Piyolog snapshot.")
        ranges.append((start, min(end, generated)))
        latest_snapshot_at = max(generated, latest_snapshot_at) if latest_snapshot_at else generated
        key = (generated, basename)
        for record in snapshot["records"]:
            event_id = record["event_id"]
            if event_id not in latest_keys or key >= latest_keys[event_id]:
                latest_keys[event_id] = key
                latest[event_id] = (record, snapshot["generated_at"], basename)
    return latest, ranges, latest_snapshot_at
