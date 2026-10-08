"""Deep-mode routing regressions for cohesive multi-metric questions."""

import unittest

from analyst.agent import _custom_sql_semantic_error, _extract_sql


class AnalystDeepRoutingTests(unittest.TestCase):
    def test_attribution_window_is_rejected_as_journey_duration(self):
        problem = _custom_sql_semantic_error(
            "Which users spent the most total time?",
            "SELECT user_assoc_id, SUM(window_hours) FROM journey_window GROUP BY user_assoc_id",
        )
        self.assertIn("must be calculated", problem)

    def test_duration_requires_first_and_last_search_times(self):
        sql = ("SELECT user_assoc_id, SUM(EXTRACT(EPOCH FROM "
               "(last_search_time - first_search_time)) / 60.0) FROM cross_app_journey "
               "GROUP BY user_assoc_id")
        self.assertIsNone(_custom_sql_semantic_error("Which users spent the most total time?", sql))

    def test_time_to_purchase_is_not_journey_duration(self):
        sql = "SELECT a.app_name, AVG(jw.hours_to_purchase) FROM journey_window jw JOIN app_master a ON a.id = 1"
        self.assertIsNone(_custom_sql_semantic_error(
            "What's our average time-to-purchase compared to Meesho and Flipkart?", sql))
        self.assertIsNone(_custom_sql_semantic_error("average time to purchase by app", sql))

    def test_sql_is_recovered_from_malformed_json_reply(self):
        raw = '{"sql": "SELECT \\"a\\"\\nFROM x", "explanation": unquoted}'
        self.assertEqual(_extract_sql(raw), 'SELECT "a"\nFROM x')


if __name__ == "__main__":
    unittest.main()
