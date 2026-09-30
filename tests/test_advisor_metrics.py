"""Analysis tests use only synthetic events and temporary archive files."""

import json
from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
import unittest

from piyolog.advisor import build_analysis


NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def write(self, name, start, end, records, generated=None):
        body = {"generated_at": generated or end, "range": {"from": start, "to": end}, "records": records}
        (self.path / name).write_text(json.dumps(body), encoding="utf-8")

    def event(self, identifier, at, kind="Pee", **fields):
        return {"event_id": identifier, "datetime": at, "type": kind, **fields}

    def analyze(self, days=2, end=date(2026, 9, 30), **options):
        return build_analysis(self.path, days=days, end_date=end, timezone_name="UTC", now=NOW, **options)

    def test_corrections_duplicates_and_half_denominators(self):
        self.write("first.json", "2026-09-26T00:00:00Z", "2026-09-28T00:00:00Z", [
            self.event("changed", "2026-09-26T10:00:00Z", "Pee"),
            self.event("duplicate", "2026-09-27T10:00:00Z", "Pee"),
        ])
        self.write("second.json", "2026-09-28T00:00:00Z", "2026-09-30T00:00:00Z", [
            self.event("changed", "2026-09-29T10:00:00Z", "Poop"),
            self.event("duplicate", "2026-09-27T10:00:00Z", "Pee"),
            self.event("milk", "2026-09-29T11:00:00Z", "Milk"),
            self.event("food", "2026-09-28T11:00:00Z", "Solid"),
        ])
        result = self.analyze(days=4)
        self.assertEqual([row["counts"]["Pee"] for row in result["daily"]], [0, 1, 0, 0])
        self.assertEqual(result["daily"][-1]["counts"]["Poop"], 1)
        self.assertEqual(result["summary"]["counts_days"], 4)
        self.assertEqual(result["summary"]["halves"]["earlier"]["counts_days"], 2)
        self.assertEqual(result["summary"]["halves"]["later"]["counts_days"], 2)
        self.assertEqual(result["summary"]["halves"]["later_minus_earlier"]["Pee"], -0.5)

    def test_coverage_gap_partial_days_and_future_cap(self):
        self.write("a.json", "2026-09-28T06:00:00Z", "2026-09-29T06:00:00Z", [self.event("a", "2026-09-28T08:00:00Z")])
        self.write("b.json", "2026-09-29T12:00:00Z", "2026-10-01T00:00:00Z", [self.event("b", "2026-09-29T13:00:00Z")], generated="2026-10-01T00:00:00Z")
        result = self.analyze()
        self.assertEqual([row["coverage_hours"] for row in result["daily"]], [18, 18])
        self.assertEqual(result["coverage"]["complete_days"], 0)
        self.assertIsNone(result["summary"]["counts_average"]["Pee"])

    def test_sleep_complete_day_from_boundary_transitions(self):
        self.write("a.json", "2026-09-27T00:00:00Z", "2026-09-30T00:00:00Z", [
            self.event("s1", "2026-09-27T23:00:00Z", "Sleep"),
            self.event("w1", "2026-09-28T08:00:00Z", "WakeUp"),
            self.event("s2", "2026-09-28T20:00:00Z", "Sleep"),
            self.event("w2", "2026-09-29T07:00:00Z", "WakeUp"),
            self.event("s3", "2026-09-29T23:00:00Z", "Sleep"),
            self.event("w3", "2026-09-30T00:00:00Z", "WakeUp"),
        ])
        result = self.analyze()
        self.assertEqual([row["sleep_hours"] for row in result["daily"]], [12, 8])
        self.assertEqual(result["summary"]["sleep_days"], 2)
        self.assertEqual(result["summary"]["sleep_average_hours"], 10)
        self.assertEqual(result["summary"]["sleep_min_hours"], 8)
        self.assertEqual(result["summary"]["sleep_max_hours"], 12)

    def test_complete_day_food_feeding_and_weight_summaries(self):
        self.write("partial.json", "2026-09-27T12:00:00Z", "2026-09-28T00:00:00Z", [
            self.event("partial-food", "2026-09-27T13:00:00Z", "Solid"),
            self.event("partial-milk", "2026-09-27T14:00:00Z", "Milk", value={"value": 999, "unit": "ml"}),
        ])
        self.write("complete.json", "2026-09-28T00:00:00Z", "2026-09-30T00:00:00Z", [
            self.event("breakfast", "2026-09-28T10:00:00Z", "Solid"),
            self.event("dinner", "2026-09-29T23:00:00Z", "Solid"),
            self.event("breast-1", "2026-09-28T11:00:00Z", "BreastFeeding", leftTime=120, rightTime=180),
            self.event("breast-2", "2026-09-29T11:00:00Z", "BreastFeeding", leftTime=60),
            self.event("breast-invalid", "2026-09-29T12:00:00Z", "BreastFeeding", leftTime="private"),
            self.event("formula", "2026-09-28T13:00:00Z", "Formula", value={"value": 120, "unit": "ml"}),
            self.event("milk", "2026-09-29T13:00:00Z", "Milk", value={"value": 80, "unit": "ml"}),
            self.event("weight", "2026-09-29T14:00:00Z", "Weight"),
        ])
        summary = self.analyze(days=3)["summary"]
        self.assertEqual(sum(summary["solid_hour_counts"]), 2)
        self.assertEqual(summary["solid_hour_counts"][10], 1)
        self.assertEqual(summary["solid_hour_counts"][23], 1)
        self.assertEqual(summary["breastfeeding_minutes"], {"records": 2, "average_minutes": 3})
        self.assertEqual(summary["formula_milk_volume_by_unit"]["ml"], {"records": 2, "total": 200, "average": 100})
        self.assertEqual(summary["weight_record_count"], 1)

    def test_night_summary_requires_continuous_known_state(self):
        self.write("full.json", "2026-09-27T00:00:00Z", "2026-09-30T00:00:00Z", [
            self.event("s0", "2026-09-27T23:00:00Z", "Sleep"),
            self.event("w0", "2026-09-28T07:00:00Z", "WakeUp"),
            self.event("s1", "2026-09-28T19:00:00Z", "Sleep"),
            self.event("w1", "2026-09-29T00:00:00Z", "WakeUp"),
            self.event("s2", "2026-09-29T01:00:00Z", "Sleep"),
            self.event("w2", "2026-09-29T09:00:00Z", "WakeUp"),
            self.event("s3", "2026-09-29T19:00:00Z", "Sleep"),
        ])
        nights = self.analyze()["summary"]["nights"]
        self.assertEqual(nights, {"nights": 1, "wakings_average": 1, "longest_sleep_average_hours": 7})
        self.write("full.json", "2026-09-27T00:00:00Z", "2026-09-29T00:00:00Z", [
            self.event("s", "2026-09-28T19:00:00Z", "Sleep"),
        ])
        self.write("gap.json", "2026-09-29T01:00:00Z", "2026-09-30T00:00:00Z", [
            self.event("w", "2026-09-29T09:00:00Z", "WakeUp"),
        ])
        self.assertEqual(self.analyze()["summary"]["nights"]["nights"], 0)

    def test_sleep_conflicts_repeats_open_and_gap_are_unknown(self):
        cases = [
            [self.event("s1", "2026-09-28T00:00:00Z", "Sleep"), self.event("s2", "2026-09-28T02:00:00Z", "Sleep"), self.event("w", "2026-09-28T08:00:00Z", "WakeUp")],
            [self.event("s", "2026-09-28T00:00:00Z", "Sleep"), self.event("w", "2026-09-28T00:00:00Z", "WakeUp")],
            [self.event("s", "2026-09-28T00:00:00Z", "Sleep")],
        ]
        for records in cases:
            with self.subTest(records=len(records)):
                self.write("a.json", "2026-09-28T00:00:00Z", "2026-09-29T00:00:00Z", records)
                row = self.analyze(days=1, end=date(2026, 9, 29))["daily"][0]
                self.assertIsNone(row["sleep_hours"])
                self.assertGreater(row["unknown_sleep_hours"], 0)
        self.write("a.json", "2026-09-28T00:00:00Z", "2026-09-28T10:00:00Z", [self.event("s", "2026-09-28T01:00:00Z", "Sleep")])
        self.write("b.json", "2026-09-28T12:00:00Z", "2026-09-29T00:00:00Z", [self.event("w", "2026-09-28T13:00:00Z", "WakeUp")])
        row = self.analyze(days=1, end=date(2026, 9, 29))["daily"][0]
        self.assertEqual(row["known_sleep_hours"], 0)

    def test_details_allowlist_and_memos_opt_in(self):
        self.write("a.json", "2026-09-29T00:00:00Z", "2026-09-30T00:00:00Z", [
            self.event("secret-id", "2026-09-29T10:00:00Z", "BreastFeeding", memo="private memo", child_name="private child", value={"value": float("nan"), "unit": "SECRET"}, leftTime=120, arbitrary="SECRET"),
            self.event("unknown", "2026-09-29T11:00:00Z", "PRIVATE_TYPE", memo="private type memo"),
            self.event("memo", "2026-09-29T12:00:00Z", "Memo", memo="specific note"),
        ])
        default = self.analyze(days=1)
        self.assertNotIn("events", default)
        self.assertNotIn("memos", default)
        detailed = self.analyze(days=1, include_event_details=True)
        self.assertEqual(detailed["events"], [
            {"datetime": "2026-09-29T10:00:00+00:00", "type": "BreastFeeding", "leftTime": 120},
            {"datetime": "2026-09-29T12:00:00+00:00", "type": "Memo"},
        ])
        self.assertNotIn("private", json.dumps(detailed).lower())
        memo = self.analyze(days=1, include_memos=True)
        self.assertEqual(memo["memos"][0]["memo"], "private memo")
        self.assertEqual([item["memo"] for item in memo["memos"]], ["private memo", "specific note"])

    def test_no_coverage_or_no_events_raises(self):
        self.write("a.json", "2026-09-20T00:00:00Z", "2026-09-21T00:00:00Z", [self.event("a", "2026-09-20T12:00:00Z")])
        with self.assertRaises(RuntimeError):
            self.analyze(days=1)
        self.write("a.json", "2026-09-29T00:00:00Z", "2026-09-30T00:00:00Z", [])
        with self.assertRaises(RuntimeError):
            self.analyze(days=1)

    def test_zero_events_in_period_with_archive_events_elsewhere_is_valid(self):
        self.write("a.json", "2026-09-29T00:00:00Z", "2026-09-30T00:00:00Z", [self.event("old", "2026-09-20T12:00:00Z")])
        result = self.analyze(days=1)
        self.assertEqual(result["daily"][0]["counts"]["Pee"], 0)
        self.assertEqual(result["summary"]["counts_days"], 1)

    def test_dst_day_uses_actual_utc_duration(self):
        self.write("a.json", "2026-11-01T03:00:00Z", "2026-11-02T06:00:00Z", [
            self.event("s", "2026-11-01T04:00:00Z", "Sleep"),
            self.event("w", "2026-11-02T05:00:00Z", "WakeUp"),
        ])
        result = build_analysis(self.path, days=1, end_date=date(2026, 11, 2), timezone_name="America/New_York", now=datetime(2026, 11, 3, tzinfo=timezone.utc))
        self.assertEqual(result["daily"][0]["coverage_hours"], 25)
        self.assertEqual(result["daily"][0]["sleep_hours"], 25)

    def test_dst_night_uses_actual_utc_duration(self):
        self.write("a.json", "2026-10-31T04:00:00Z", "2026-11-02T05:00:00Z", [
            self.event("s", "2026-10-31T23:00:00Z", "Sleep"),
            self.event("w", "2026-11-01T13:00:00Z", "WakeUp"),
            self.event("s2", "2026-11-01T14:00:00Z", "Sleep"),
        ])
        result = build_analysis(self.path, days=2, end_date=date(2026, 11, 2), timezone_name="America/New_York", now=datetime(2026, 11, 3, tzinfo=timezone.utc))
        self.assertEqual(result["summary"]["nights"], {"nights": 1, "wakings_average": 0, "longest_sleep_average_hours": 13})

    def test_bad_timestamp_exception_has_no_input_text(self):
        secret = "secret timestamp payload"
        self.write("a.json", "2026-09-29T00:00:00Z", "2026-09-30T00:00:00Z", [self.event("x", secret)])
        with self.assertRaises(RuntimeError) as caught:
            self.analyze(days=1)
        self.assertNotIn(secret, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
