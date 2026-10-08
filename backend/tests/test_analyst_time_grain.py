"""Day-wise / week-wise questions must get the requested period grain and a complete answer."""

import datetime
import unittest
from types import SimpleNamespace

from analyst.agent import _grain_mismatch, render_result, requested_time_grain
from analyst.sources import Result


def metric(sql, focus=None):
    return SimpleNamespace(sql=sql, focus_parameter=focus)


DAILY = metric("SELECT CAST(j.first_search_time AS date) AS day, COUNT(*) AS journeys FROM cross_app_journey j")
DAILY_BY_APP = metric("SELECT CAST(t AS date) AS day, a.app_name AS app, COUNT(*) FROM x", focus="app")
WHOLE_PERIOD = metric("SELECT a.app_name AS first_search_app, COUNT(*) AS journeys FROM journey_window jw",
                      focus="app")


class TimeGrainTests(unittest.TestCase):
    def test_detects_spellings(self):
        for q in ("day wise journeys", "daywise journeys", "daywiese data", "day-wise count", "daily searches",
                  "journeys per day", "date wise purchases"):
            self.assertEqual(requested_time_grain(q), "day", q)
        for q in ("week wise journeys", "WEEK WIESE data", "weekly purchase rate", "week over week searches"):
            self.assertEqual(requested_time_grain(q), "week", q)
        self.assertIsNone(requested_time_grain("purchase rate by app"))
        self.assertIsNone(requested_time_grain("journeys in the last 7 days"))

    def test_metric_must_match_grain_and_app_split(self):
        self.assertTrue(_grain_mismatch("week wise purchase rate by app", WHOLE_PERIOD))
        self.assertTrue(_grain_mismatch("week wise journeys", DAILY))
        self.assertTrue(_grain_mismatch("day wise journey count for each app", DAILY))
        self.assertFalse(_grain_mismatch("day wise journeys", DAILY))
        self.assertFalse(_grain_mismatch("day wise journeys for each app", DAILY_BY_APP))
        self.assertFalse(_grain_mismatch("purchase rate by app", WHOLE_PERIOD))

    def test_period_by_app_answer_lists_every_row(self):
        apps = ["Amazon", "Flipkart", "Meesho"]
        rows = [[datetime.date(2026, 9, 6) + datetime.timedelta(days=d), app, d * 10 + i, 1.5]
                for d in range(25) for i, app in enumerate(apps)]
        text = render_result(Result(["day", "app", "journeys", "purchase_pct"], rows),
                             title="Result", detail="", focus=[], max_lines=10)
        self.assertIn("2026-09-06:", text)
        self.assertIn("2026-09-30:", text)
        self.assertEqual(text.count("  - Meesho:"), 25)
        self.assertIn("25 periods x 3 app values, all rows shown.", text)
        self.assertNotIn("Showing", text)

    def test_week_answer_shows_partial_week_span(self):
        rows = [["2026-08-31", "2026-09-06", "2026-09-06", 34], ["2026-09-28", "2026-09-28", "2026-09-30", 5116]]
        text = render_result(Result(["week_start", "first_day", "last_day", "journeys"], rows),
                             title="Result", detail="", focus=[])
        self.assertIn("Week of 2026-08-31 (2026-09-06 to 2026-09-06): journeys 34", text)
        self.assertIn("Week of 2026-09-28 (2026-09-28 to 2026-09-30): journeys 5,116", text)


if __name__ == "__main__":
    unittest.main()
