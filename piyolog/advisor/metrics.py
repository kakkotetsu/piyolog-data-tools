"""Conservative, privacy-aware daily metrics from Piyolog archives."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from piyolog.archive import load_analysis_archive, parse_timestamp


COUNT_TYPES = ("Pee", "Poop", "BreastFeeding", "Formula", "Milk", "Solid")
KNOWN_TYPES = frozenset((*COUNT_TYPES, "Sleep", "WakeUp", "Bath", "Temperature", "Height", "Weight", "Hospital", "Walking", "Vaccine", "Milestone", "Memo"))
KNOWN_UNITS = frozenset(("ml", "mL", "g", "kg", "cm", "celsius", "fahrenheit"))
FEED_UNITS = frozenset(("ml", "mL"))
SECONDS_PER_HOUR = 3600


def _merge_ranges(ranges: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[list[datetime]] = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _overlap_seconds(start: datetime, end: datetime, ranges: list[tuple[datetime, datetime]]) -> float:
    return sum(max(0.0, (min(end, right) - max(start, left)).total_seconds()) for left, right in ranges)


def _local_midnight(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, zone).astimezone(timezone.utc)


def _safe_event(record: dict[str, Any], at: datetime, zone: ZoneInfo) -> dict[str, Any]:
    result: dict[str, Any] = {"datetime": at.astimezone(zone).isoformat(), "type": record["type"]}
    value = record.get("value")
    if isinstance(value, dict):
        amount = value.get("value")
        unit = value.get("unit")
        if _finite_number(amount) and isinstance(unit, str) and unit in KNOWN_UNITS:
            result["value"] = {"value": amount, "unit": unit}
    if record["type"] == "BreastFeeding":
        for field in ("leftTime", "rightTime"):
            amount = record.get(field)
            if _finite_number(amount) and amount >= 0:
                result[field] = amount
    return result


def _finite_number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _average_counts(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    complete = [row for row in rows if row["complete"]]
    return {kind: sum(row["counts"][kind] for row in complete) / len(complete) if complete else None for kind in COUNT_TYPES}


def _average(values: list[float | int]) -> float | None:
    return sum(values) / len(values) if values else None


def build_analysis(
    data_dir: Path,
    *,
    days: int = 14,
    end_date: date | None = None,
    timezone_name: str = "Asia/Tokyo",
    now: datetime | None = None,
    include_event_details: bool = False,
    include_memos: bool = False,
) -> dict[str, Any]:
    """Return JSON-serializable metrics for days before an exclusive local date."""
    if type(days) is not int or days <= 0:
        raise ValueError("days must be a positive integer.")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("Invalid timezone.") from exc
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("now must have a timezone.")
    clock = clock.astimezone(timezone.utc)
    if end_date is None:
        end_date = clock.astimezone(zone).date()
    if not isinstance(end_date, date) or isinstance(end_date, datetime):
        raise ValueError("end_date must be a date.")
    start_date = end_date - timedelta(days=days)
    boundaries = [_local_midnight(start_date + timedelta(days=index), zone) for index in range(days + 1)]
    period_start, period_end = boundaries[0], boundaries[-1]

    latest, raw_ranges, latest_generated = load_analysis_archive(data_dir)
    coverage = _merge_ranges([(start, min(end, clock)) for start, end in raw_ranges])
    if not latest or not any(start < period_end and end > period_start for start, end in coverage):
        raise RuntimeError("No archived events with coverage in the requested period.")

    daily: list[dict[str, Any]] = []
    for index in range(days):
        start, end = boundaries[index : index + 2]
        covered = _overlap_seconds(start, end, coverage)
        total = (end - start).total_seconds()
        daily.append({
            "date": (start_date + timedelta(days=index)).isoformat(),
            "coverage_hours": covered / SECONDS_PER_HOUR,
            "complete": abs(covered - total) < 0.001,
            "counts": {kind: 0 for kind in COUNT_TYPES},
            "sleep_hours": None,
            "known_sleep_hours": 0.0,
            "unknown_sleep_hours": total / SECONDS_PER_HOUR,
        })

    transitions: dict[datetime, list[str]] = defaultdict(list)
    details: list[tuple[datetime, dict[str, Any]]] = []
    memos: list[tuple[datetime, dict[str, Any]]] = []
    solid_hour_counts = [0] * 24
    breastfeeding_minutes: list[float] = []
    breastfeeding_total_minutes = 0.0
    feed_amounts: dict[str, list[float]] = {unit: [] for unit in FEED_UNITS}
    feed_totals: dict[str, float] = {unit: 0.0 for unit in FEED_UNITS}
    weight_record_count = 0
    for record, _, _ in latest.values():
        at = parse_timestamp(record.get("datetime"))
        kind = record.get("type")
        if not isinstance(kind, str):
            raise RuntimeError("Invalid event type in Piyolog snapshot.")
        if kind in ("Sleep", "WakeUp"):
            transitions[at].append(kind)
        if not period_start <= at < period_end or kind not in KNOWN_TYPES:
            continue
        index = (at.astimezone(zone).date() - start_date).days
        if kind in COUNT_TYPES:
            daily[index]["counts"][kind] += 1
        if daily[index]["complete"]:
            if kind == "Solid":
                solid_hour_counts[at.astimezone(zone).hour] += 1
            elif kind == "BreastFeeding":
                durations = [record.get(side) for side in ("leftTime", "rightTime")]
                valid_durations = [value for value in durations if _finite_number(value) and value >= 0]
                if valid_durations and _finite_number(sum(valid_durations)):
                    minutes = sum(valid_durations) / 60
                    if _finite_number(breastfeeding_total_minutes + minutes):
                        breastfeeding_minutes.append(minutes)
                        breastfeeding_total_minutes += minutes
            elif kind in ("Formula", "Milk"):
                value = record.get("value")
                if isinstance(value, dict):
                    unit, amount = value.get("unit"), value.get("value")
                    if isinstance(unit, str) and unit in FEED_UNITS and _finite_number(amount) and amount >= 0 and _finite_number(feed_totals[unit] + amount):
                        feed_amounts[unit].append(amount)
                        feed_totals[unit] += amount
            elif kind == "Weight":
                weight_record_count += 1
        if include_event_details:
            details.append((at, _safe_event(record, at, zone)))
        if include_memos:
            memo = record.get("memo")
            if isinstance(memo, str):
                memos.append((at, {"datetime": at.astimezone(zone).isoformat(), "type": kind, "memo": memo}))

    known_sleep = [0.0] * days
    known_awake = [0.0] * days
    known_intervals: list[tuple[datetime, datetime, str]] = []
    transition_items = sorted(transitions.items())
    for (left_at, left_kinds), (right_at, right_kinds) in zip(transition_items, transition_items[1:]):
        if len(left_kinds) != 1 or len(right_kinds) != 1 or left_kinds[0] == right_kinds[0]:
            continue
        if not any(start <= left_at and right_at <= end for start, end in coverage):
            continue
        known_intervals.append((left_at, right_at, left_kinds[0]))
        target = known_sleep if left_kinds[0] == "Sleep" else known_awake
        for index in range(days):
            target[index] += max(0.0, (min(right_at, boundaries[index + 1]) - max(left_at, boundaries[index])).total_seconds())

    for index, row in enumerate(daily):
        total = (boundaries[index + 1] - boundaries[index]).total_seconds()
        unknown = max(0.0, total - known_sleep[index] - known_awake[index])
        row["known_sleep_hours"] = known_sleep[index] / SECONDS_PER_HOUR
        row["unknown_sleep_hours"] = unknown / SECONDS_PER_HOUR
        if row["complete"] and unknown < 0.001:
            row["sleep_hours"] = row["known_sleep_hours"]

    complete = [row for row in daily if row["complete"]]
    sleep_complete = [row for row in daily if row["sleep_hours"] is not None]
    night_wakings: list[int] = []
    night_longest_sleep: list[float] = []
    for index in range(days - 1):
        night_date = start_date + timedelta(days=index)
        night_start = datetime.combine(night_date, time(20), zone).astimezone(timezone.utc)
        night_end = datetime.combine(night_date + timedelta(days=1), time(8), zone).astimezone(timezone.utc)
        night_seconds = (night_end - night_start).total_seconds()
        if night_start < period_start or night_end > period_end:
            continue
        if abs(_overlap_seconds(night_start, night_end, coverage) - night_seconds) >= 0.001:
            continue
        known_seconds = _overlap_seconds(night_start, night_end, [(start, end) for start, end, _ in known_intervals])
        if abs(known_seconds - night_seconds) >= 0.001:
            continue
        night_wakings.append(sum(night_start <= at < night_end and kinds == ["WakeUp"] for at, kinds in transition_items))
        night_longest_sleep.append(max((max(0.0, (min(end, night_end) - max(start, night_start)).total_seconds()) for start, end, kind in known_intervals if kind == "Sleep"), default=0.0) / SECONDS_PER_HOUR)
    midpoint = days // 2
    first, second = daily[:midpoint], daily[midpoint:]
    first_avg, second_avg = _average_counts(first), _average_counts(second)
    result: dict[str, Any] = {
        "period": {"start": start_date.isoformat(), "end_exclusive": end_date.isoformat(), "timezone": timezone_name, "requested_days": days},
        "coverage": {"latest_snapshot_at": latest_generated.isoformat() if latest_generated else None, "complete_days": len(complete)},
        "daily": daily,
        "summary": {
            "counts_average": _average_counts(daily), "counts_days": len(complete),
            "sleep_average_hours": sum(row["sleep_hours"] for row in sleep_complete) / len(sleep_complete) if sleep_complete else None,
            "sleep_min_hours": min((row["sleep_hours"] for row in sleep_complete), default=None),
            "sleep_max_hours": max((row["sleep_hours"] for row in sleep_complete), default=None),
            "sleep_days": len(sleep_complete),
            "solid_hour_counts": solid_hour_counts,
            "breastfeeding_minutes": {"records": len(breastfeeding_minutes), "average_minutes": _average(breastfeeding_minutes)},
            "formula_milk_volume_by_unit": {unit: {"records": len(values), "total": feed_totals[unit], "average": _average(values)} for unit, values in sorted(feed_amounts.items()) if values},
            "weight_record_count": weight_record_count,
            "nights": {"nights": len(night_wakings), "wakings_average": _average(night_wakings), "longest_sleep_average_hours": _average(night_longest_sleep)},
            "halves": {
                "earlier": {"counts_average": first_avg, "counts_days": sum(row["complete"] for row in first)},
                "later": {"counts_average": second_avg, "counts_days": sum(row["complete"] for row in second)},
                "later_minus_earlier": {kind: second_avg[kind] - first_avg[kind] if first_avg[kind] is not None and second_avg[kind] is not None else None for kind in COUNT_TYPES},
            },
        },
        "limitations": [
            "取得範囲が完全でも、入力記録の完全性は保証できません。",
            "原本から削除されたイベントは削除として反映できません。",
            "睡眠は連続した取得範囲内で隣接する就寝・起床記録の間だけ推定しています。",
        ],
    }
    if include_event_details:
        result["events"] = [item for _, item in sorted(details, key=lambda pair: pair[0])]
    if include_memos:
        result["memos"] = [item for _, item in sorted(memos, key=lambda pair: pair[0])]
    return result
