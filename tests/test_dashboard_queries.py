"""Dashboard checks; optional read-only integration tests against VictoriaLogs.

PIYOLOG_TEST_VICTORIALOGS_URL=http://127.0.0.1:9428 \
    .venv/bin/python -m unittest discover -s tests -v

Synthetic events exist only inside queries; nothing is inserted or deleted.
The integration server must contain at least one Piyolog log as a seed row.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import unittest
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


DASHBOARD = Path(__file__).resolve().parents[1] / "grafana/dashboards/piyolog-overview.json"
PANELS = {panel["id"]: panel for panel in json.loads(DASHBOARD.read_text())["panels"]}
START = "2026-09-20T00:00:00Z"
END = "2026-09-21T00:00:00Z"
URL = os.environ.get("PIYOLOG_TEST_VICTORIALOGS_URL")


def event(event_id, snapshot, kind="Solid", at="2026-09-20T10:00:00Z", **fields):
    return {
        "event_id": event_id,
        "snapshot_at": f"2026-09-{snapshot:02d}T12:00:00Z",
        "_time": at,
        "type": kind,
        "_msg": kind,
        **fields,
    }


FIXTURE = [
    event("meal", 20, memo="old"),
    event("meal", 21, memo="new"),
    event("cleared", 20, memo="removed"),
    event("cleared", 21),
    event("blank", 20),
    event("temperature", 20, "Temperature", value_value="38.5"),
    event("temperature", 21, "Temperature", value_value="36.5"),
    event("temperature-moved", 20, "Temperature", value_value="39.0"),
    event("temperature-moved", 21, "Temperature", at="2026-09-22T10:00:00Z", value_value="36.0"),
    event("changed-type", 20, "BreastFeeding"),
    event("changed-type", 21, "Pee"),
    event("feeding", 20, "BreastFeeding"),
    event("moved-out", 20),
    event("moved-out", 21, at="2026-09-22T10:00:00Z"),
    event("moved-in", 20, at="2026-09-22T11:00:00Z"),
    event("moved-in", 21, at="2026-09-20T09:00:00Z"),
    # Retrying an identical delivery must not add a second event.
    event("meal", 21, memo="new"),
]


def query_for(panel_id, synthetic=False):
    query = PANELS[panel_id]["targets"][0]["expr"]
    query = query.replace("${__from:date:iso}", START).replace("${__to:date:iso}", END)
    if synthetic:
        # Keep only the seed timestamp, then replace it with fixture fields.
        # unroll expands the JSON array without persisting any fixture data.
        fixture = json.dumps(FIXTURE, separators=(",", ":"))
        source = (
            'stream:piyolog | limit 1 | fields _time'
            f" | format '{fixture}' as fixture | unroll fixture"
            ' | unpack_json from fixture | delete fixture'
        )
        query = query.replace("stream:piyolog", source, 1)
    return query


class DashboardStructureTests(unittest.TestCase):
    def test_all_panels_select_latest_before_filtering(self):
        for panel in PANELS.values():
            with self.subTest(panel=panel["id"]):
                query = panel["targets"][0]["expr"]
                self.assertTrue(query.startswith("options (ignore_global_time_filter=true) stream:piyolog |"))
                latest = query.index("stats by (event_id) row_max(snapshot_at)")
                time_filter = query.index("filter _time:[${__from:date:iso}, ${__to:date:iso}]")
                self.assertLess(latest, time_filter)
                if "type:" in query:
                    self.assertLess(time_filter, query.index("type:"))

    def test_meal_table_keeps_time_and_memo(self):
        panel = PANELS[4]
        self.assertEqual(panel["targets"][0]["queryType"], "stats")
        transforms = {item["id"]: item["options"] for item in panel["transformations"]}
        self.assertEqual(transforms["organize"]["renameByName"], {"meal_time": "日時", "memo": "メモ"})
        self.assertEqual(transforms["filterFieldsByName"]["include"]["names"], ["meal_time", "memo"])
        self.assertIn("copy _time as meal_time", panel["targets"][0]["expr"])

    def test_temperature_uses_stats_without_automatic_time_partitioning(self):
        panel = PANELS[5]
        self.assertEqual(panel["targets"][0]["queryType"], "stats")
        transforms = {item["id"]: item["options"] for item in panel["transformations"]}
        self.assertEqual(transforms["filterFieldsByName"]["include"]["names"], ["temperature_time", "temperature_value"])
        self.assertEqual(transforms["convertFieldType"]["conversions"], [
            {"targetField": "temperature_time", "destinationType": "time"},
            {"targetField": "temperature_value", "destinationType": "number"},
        ])


@unittest.skipUnless(URL, "Set PIYOLOG_TEST_VICTORIALOGS_URL for read-only integration tests")
class DashboardIntegrationTests(unittest.TestCase):
    def query(self, query, endpoint="query", **params):
        body = urlencode({"query": query, "start": START, "end": END, **params}).encode()
        request = Request(f"{URL.rstrip('/')}/select/logsql/{endpoint}", data=body)
        # Integration tests intentionally use a direct connection, not .env secrets.
        with build_opener(ProxyHandler({})).open(request, timeout=30) as response:
            text = response.read().decode()
        if endpoint == "query":
            return [json.loads(line) for line in text.splitlines() if line]
        return json.loads(text)

    def test_counts_use_latest_type(self):
        for panel_id, expected in ((1, 7), (2, 1), (3, 1)):
            with self.subTest(panel=panel_id):
                rows = self.query(query_for(panel_id, synthetic=True))
                self.assertEqual(int(rows[0]["count(*)"]), expected)

    def test_meals_use_latest_memo_including_cleared_and_missing(self):
        rows = self.query(query_for(4, synthetic=True))
        self.assertEqual(len(rows), 4)
        by_id = {row["event_id"]: row for row in rows}
        self.assertEqual(by_id["meal"]["memo"], "new")
        for event_id in ("cleared", "blank", "moved-in"):
            self.assertEqual(by_id[event_id].get("memo", ""), "")
        self.assertTrue(by_id["moved-in"]["meal_time"].startswith("2026-09-20T09:00:00"))
        self.assertTrue(all(row["count(*)"] == "1" for row in rows))

    def test_temperature_discards_old_higher_value(self):
        rows = self.query(query_for(5, synthetic=True))
        self.assertEqual(len(rows), 1)
        self.assertEqual(float(rows[0]["temperature"]), 36.5)

    def test_latest_logs_keep_fields_and_sort_after_deduplication(self):
        rows = self.query(query_for(6, synthetic=True))
        self.assertEqual(len(rows), 7)
        self.assertEqual(len({row["event_id"] for row in rows}), 7)
        self.assertEqual([row["_time"] for row in rows], sorted((row["_time"] for row in rows), reverse=True))
        self.assertTrue(all(row["_msg"] and row["type"] for row in rows))
        self.assertNotIn("moved-out", {row["event_id"] for row in rows})

    def test_grafana_stats_endpoints(self):
        for panel_id in (1, 2, 3, 4, 5):
            with self.subTest(panel=panel_id):
                result = self.query(query_for(panel_id, synthetic=True), "stats_query", time=END)
                self.assertEqual(result["status"], "success")
                self.assertTrue(result["data"]["result"])
                if panel_id == 5:
                    series = result["data"]["result"]
                    self.assertEqual(len(series), 1)
                    self.assertEqual(float(series[0]["metric"]["temperature_value"]), 36.5)
                    self.assertTrue(series[0]["metric"]["temperature_time"].startswith("2026-09-20T10:00:00"))
                if panel_id == 4:
                    series = result["data"]["result"]
                    self.assertEqual(len(series), 4)
                    labels = {item["metric"]["event_id"]: item["metric"] for item in series}
                    self.assertEqual(labels["meal"]["memo"], "new")
                    self.assertEqual(labels["cleared"].get("memo", ""), "")

    def test_real_panels_match_latest_stored_events(self):
        def timestamp(value):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))

        raw = self.query("options (ignore_global_time_filter=true) stream:piyolog")
        self.assertTrue(raw, "Integration tests require at least one Piyolog log")
        latest = {}
        for row in raw:
            previous = latest.get(row["event_id"])
            if previous is None or row["snapshot_at"] > previous["snapshot_at"]:
                latest[row["event_id"]] = row
        selected = [row for row in latest.values() if timestamp(START) <= timestamp(row["_time"]) <= timestamp(END)]
        for panel_id, kind in ((1, None), (2, "BreastFeeding"), (3, "Pee")):
            expected = sum(kind is None or row["type"] == kind for row in selected)
            result = self.query(query_for(panel_id), "stats_query", time=END)
            values = result["data"]["result"]
            actual = float(values[0]["value"][1]) if values else 0
            self.assertEqual(actual, expected)

        expected_meals = {
            row["event_id"]: (timestamp(row["_time"]), row.get("memo", ""))
            for row in selected if row["type"] == "Solid"
        }
        meals = self.query(query_for(4), "stats_query", time=END)["data"]["result"]
        actual_meals = {
            row["metric"]["event_id"]: (timestamp(row["metric"]["meal_time"]), row["metric"].get("memo", ""))
            for row in meals
        }
        # Do not include personal notes in failure messages.
        self.assertTrue(actual_meals == expected_meals, "Meal timestamps or notes differ from latest events")
        self.assertEqual(len(meals), len(expected_meals))

        expected_temperatures = {}
        for row in selected:
            if row["type"] != "Temperature":
                continue
            bucket = timestamp(row["_time"]).astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
            value = float(row["value_value"])
            expected_temperatures[bucket] = max(expected_temperatures.get(bucket, value), value)
        temperatures = self.query(query_for(5), "stats_query", time=END)["data"]["result"]
        actual_temperatures = {
            timestamp(row["metric"]["temperature_time"]): float(row["metric"]["temperature_value"])
            for row in temperatures
        }
        self.assertTrue(actual_temperatures == expected_temperatures, "Temperature values differ from latest events")
        logs = self.query(query_for(6))
        self.assertTrue({row["event_id"] for row in logs} == {
            row["event_id"] for row in sorted(selected, key=lambda row: row["_time"], reverse=True)[:100]
        }, "Latest log IDs differ from expected events")


if __name__ == "__main__":
    unittest.main()
