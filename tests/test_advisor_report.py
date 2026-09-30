import unittest
from datetime import datetime, timezone

from piyolog.advisor.report import render_report


class AdvisorReportTests(unittest.TestCase):
    def test_computed_and_ai_sections(self):
        analysis = {
            "period": {"start": "2026-09-01", "end_exclusive": "2026-09-03", "timezone": "Asia/Tokyo", "requested_days": 2},
            "coverage": {"complete_days": 1, "latest_snapshot_at": "2026-09-02T12:00:00+09:00"},
            "summary": {
                "counts_average": {"Pee": 5}, "counts_days": 1,
                "sleep_average_hours": 12, "sleep_days": 1, "sleep_min_hours": 11, "sleep_max_hours": 13,
                "solid_hour_counts": [0] * 12 + [2] + [0] * 11,
                "breastfeeding_minutes": {"records": 3, "average_minutes": 14},
                "formula_milk_volume_by_unit": {"ml": {"records": 2, "total": 150, "average": 75}},
                "weight_record_count": 1,
                "nights": {"nights": 1, "wakings_average": 2, "longest_sleep_average_hours": 5},
                "halves": {
                    "earlier": {"counts_average": {"Pee": 4}, "counts_days": 1},
                    "later": {"counts_average": {"Pee": 6}, "counts_days": 1},
                    "later_minus_earlier": {"Pee": 2},
                },
            },
            "daily": [{"date": "2026-09-01", "coverage_hours": 24, "complete": True, "known_sleep_hours": 12, "unknown_sleep_hours": 0}],
            "limitations": ["2日目は部分的"],
            "events": [{"memo": "private-event-text"}],
            "memos": [{"memo": "private-memo-text"}],
        }
        report = render_report(analysis, "観察です。", model="gpt-6-astra", generated_at=datetime(2026, 9, 3, tzinfo=timezone.utc), profile={"age_months": 3, "age_as_of": "2026-09-02", "corrected_age_months": 2})
        self.assertIn("| おしっこ | 5回/日 | 1日 |", report)
        self.assertIn("判明している睡眠", report)
        self.assertIn("修正月齢: 2か月", report)
        self.assertLess(report.index("## 集計"), report.index("## AIによる文章"))
        self.assertIn("医療者が検証した医療指示ではありません", report)
        self.assertIn("2日目は部分的", report)
        self.assertIn("睡眠・覚醒が不明な時間", report)
        self.assertIn("前半の集計対象: 1日、後半の集計対象: 1日", report)
        self.assertIn("| おしっこ | 4回/日 | 6回/日 | 2回/日 |", report)
        self.assertIn("| 12:00〜12:59 | 2件 |", report)
        self.assertIn("| ml | 2件 | 150ml | 75ml |", report)
        self.assertIn("| 夜間の起床回数の平均 | 2回/夜 |", report)
        self.assertIn("栄養量の充足", report)
        self.assertNotIn("```json", report)
        self.assertNotIn("private-event-text", report)
        self.assertNotIn("private-memo-text", report)
        dry = render_report(analysis, None, model="", generated_at=datetime.now(timezone.utc), profile={"age_months": 3}, dry_run=True)
        self.assertIn("AIは実行していません", dry)


if __name__ == "__main__":
    unittest.main()
