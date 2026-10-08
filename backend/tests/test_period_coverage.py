"""Coverage and ambiguous-duration parsing regressions."""

import unittest

from journey_agent.context import AppMatcher, Resolution
from journey_agent.planner import RuleParser
from journey_agent.thinking import requested_period_exceeds


class PeriodCoverageTests(unittest.TestCase):
    def test_explicit_period_outside_loaded_dates_is_detected(self):
        self.assertTrue(requested_period_exceeds(
            "from 5 September to 30 September", "2026-09-06", "2026-09-30"))
        self.assertFalse(requested_period_exceeds(
            "from 15 Sept to 30 Sept", "2026-09-06", "2026-09-30"))

    def test_spend_time_means_duration_not_money(self):
        parser = RuleParser(AppMatcher(["Amazon", "Meesho"]), data_year=2026)
        plan = parser.parse(
            "which user spend most time from 6 September to 30 September", Resolution())
        self.assertEqual(plan.intent, "aggregate")
        self.assertEqual(plan.metric, "total_duration")
        self.assertEqual(plan.group_by, "user")
        self.assertTrue(plan.singular)


if __name__ == "__main__":
    unittest.main()
