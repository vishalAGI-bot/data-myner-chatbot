"""Visit questions: routing, ambiguity handling in Deep mode, and database-error handling."""

import json
from pathlib import Path
from unittest.mock import patch

from analyst.agent import AnalystAgent, _grain_mismatch, _metric_by_app
from analyst.catalog import Catalog
from analyst.settings import Settings
from analyst.sources import Result, SourceError

KNOWLEDGE = Path(__file__).resolve().parents[1] / "analyst" / "knowledge"


class VisitSource:
    dialect = "postgres"
    kind = "stub"

    def __init__(self, fail: bool = False):
        self.fail = fail

    def query(self, sql, **_):
        if self.fail:
            raise SourceError("canceling statement due to statement timeout")
        if "AS day" in sql:
            return Result(["day", "visits"], [["2026-09-06", 279], ["2026-09-07", 2826]])
        if "visits_per_user" in sql:
            return Result(["visits", "users", "visits_per_user", "apps", "avg_visit_minutes", "period_start",
                           "period_end"], [[1200, 300, 4.0, 9, 3.5, "2026-09-06 00:00:00", "2026-09-30 10:00:00"]])
        raise AssertionError(f"unexpected SQL: {sql}")


def make_agent(source=None) -> AnalystAgent:
    catalog = Catalog(KNOWLEDGE, "Amazon")
    catalog.apps = ["Amazon", "Meesho", "Flipkart"]
    catalog.date_range = ("2026-09-06", "2026-09-30")
    for table in catalog.tables.values():
        table.available = True
    for metric in catalog.metrics.values():
        metric.available = True
    return AnalystAgent(source or VisitSource(), catalog, Settings(source="snapshot"))


def test_visit_questions_route_to_totals_without_breaking_per_app_routes():
    agent = make_agent()
    expected = {
        "how many total user visit?": "visits_overview",
        "total visits": "visits_overview",
        "how many sessions per app": "session_summary_by_app",
        "how many users visited Amazon in the last 24 hours": "active_users_by_app",
        "which app had the most unique users yesterday": "active_users_by_app",
    }
    for question, metric in expected.items():
        route, _ = agent.router.route(question)
        assert route.metric is not None and route.metric.name == metric, question


def test_per_app_request_needs_a_per_app_metric():
    cat = make_agent().cat
    assert not _metric_by_app(cat.metrics["visits_overview"])
    assert _metric_by_app(cat.metrics["leakage_destinations"])        # positional GROUP BY 1 on an app column
    assert _metric_by_app(cat.metrics["app_list"])
    assert _grain_mismatch("total visits by app", cat.metrics["visits_overview"])
    assert not _grain_mismatch("total visits across all apps", cat.metrics["visits_overview"])


def test_total_visits_answer_offline():
    result = make_agent().ask("how many total user visit?", depth="fast")
    assert result.metric == "visits_overview" and result.verified
    assert "visits: 1,200" in result.answer and "users: 300" in result.answer


def test_database_error_in_vetted_metric_is_an_answer_not_a_crash():
    result = make_agent(VisitSource(fail=True)).ask("total visits", depth="fast")
    assert result.route == "error" and not result.verified
    assert "failed on the database" in result.answer


def _deep_model(plan: dict):
    def complete(system="", user="", **_):
        if system.startswith("You are the lead analyst"):
            return json.dumps(plan)
        if system.startswith("You write ONE PostgreSQL"):
            return json.dumps({"sql": "SELECT COUNT(*) AS journeys FROM cross_app_journey"})
        if system.startswith("Review a proposed"):
            return json.dumps({"blocking_issues": ["counts journeys, not visits"], "minor_issues": []})
        if system.startswith("Verify an analytics answer"):
            return json.dumps({"valid": True, "issues": []})
        return "Visits were counted from sessions."
    return complete


def test_deep_falls_back_to_vetted_metric_when_no_custom_query_is_approved():
    agent = make_agent()
    plan = {"action": "custom_sql", "tables": ["session_master"], "reason": "custom count",
            "interpretation": "Visits are sessions in session_master across all apps and all data."}
    reading = {"rephrased_question": "How many visits are there in total across all apps and all data?",
               "intent": "analysis", "metric": "visits_overview"}
    with patch("config.LLM_PROVIDER", "openai"), patch("llm.complete", side_effect=_deep_model(plan)), \
            patch("llm.call_tool", return_value=reading):
        result = agent.ask("how many total user visit?", depth="deep", max_seconds=120)
    assert result.metric == "visits_overview" and result.verified, result.answer
    assert result.resolved == reading["rephrased_question"], "'Understood as' shows OpenAI's rephrasing"
    assert "Interpreted as: Visits are sessions" in result.answer
    assert any("closest vetted analysis" in step["text"] for step in result.thinking)


def test_deep_unsupported_answer_states_the_reason():
    agent = make_agent()
    plan = {"action": "unsupported", "reason": "no field", "message": "City tier is not recorded",
            "interpretation": "Visits by city tier."}
    with patch("config.LLM_PROVIDER", "openai"), patch("llm.complete", side_effect=_deep_model(plan)):
        result = agent.ask("visits by city tier", depth="deep")
    assert result.route == "data_gap" and "City tier is not recorded." in result.answer


def test_metric_grain_reads_output_columns_only():
    from analyst.agent import _metric_grain
    cat = make_agent().cat
    assert _metric_grain(cat.metrics["visits_overview"]) is None       # CAST(... AS date) inside a CTE
    assert _metric_grain(cat.metrics["active_users_by_app"]) is None
    assert _metric_grain(cat.metrics["purchase_share_trend"]) == "day"  # bare "day" output column
    assert _grain_mismatch("day wise visits", cat.metrics["visits_overview"])


def test_plan_without_action_is_normalised():
    from analyst.agent import _normalise_plan
    cat = make_agent().cat
    assert _normalise_plan({"metric": "visits_overview", "reason": "r"}, cat)["action"] == "metric"
    assert _normalise_plan({"strategy": {"action": "sql", "tables": ["session_master"]}}, cat)["action"] == "custom_sql"
    assert _normalise_plan({"reason": "nothing"}, cat)["action"] is None


def test_rejected_summary_is_dropped_and_table_reverified():
    agent = make_agent()
    verdicts = iter([{"valid": False, "issues": ["the summary names the wrong peak day"]},
                     {"valid": True, "issues": []}])

    def complete(system="", user="", **_):
        if system.startswith("You are the lead analyst"):
            return json.dumps({"action": "custom_sql", "tables": ["session_master"], "reason": "day grain",
                               "interpretation": "Visits are sessions per day."})
        if system.startswith("You write ONE PostgreSQL"):
            return json.dumps({"sql": "SELECT CAST(created_at AS date) AS day, COUNT(*) AS visits "
                                      "FROM session_master GROUP BY 1 ORDER BY 1"})
        if system.startswith("Review a proposed"):
            return json.dumps({"blocking_issues": [], "minor_issues": []})
        if system.startswith("Verify an analytics answer"):
            return json.dumps(next(verdicts))
        return "Visits peaked on 2026-09-06."

    with patch("config.LLM_PROVIDER", "openai"), patch("llm.complete", side_effect=complete):
        result = agent.ask("day wise visits", depth="deep", max_seconds=120)
    assert result.verified and result.answer.startswith("Result:"), result.answer
    assert "peaked" not in result.answer and "2026-09-07: visits 2,826" in result.answer


DAY_SQL = "SELECT CAST(created_at AS date) AS day, COUNT(*) AS visits FROM session_master GROUP BY 1 ORDER BY 1"
TWO_ASKS = "how many total user visit? how many visits per day?"


def test_auto_mode_without_openai_answers_each_part():
    """Auto picked Deep for a two-part question, and without OpenAI that used to end in
    "Deep analysis requires the OpenAI provider" for a user who never asked for Deep."""
    def local_model(system="", user="", **_):
        if system.startswith("You route a business question"):
            return json.dumps({"action": "sql", "tables": ["session_master"]})
        if system.startswith("You write ONE PostgreSQL"):
            return json.dumps({"sql": DAY_SQL})
        return "Visits are counted from sessions."

    with patch("config.LLM_PROVIDER", "ollama"), patch("llm.complete", side_effect=local_model):
        result = make_agent().ask(TWO_ASKS, depth="auto")
    assert "requires the OpenAI provider" not in result.answer
    assert result.route == "deep" and result.verified, result.answer
    assert [p["question"] for p in result.parts] == ["how many total user visit?", "how many visits per day?"]
    # The per-day part went through the Fast path's shape check: the whole-period metric cannot
    # answer it, so it got its own day-wise query instead of a single total.
    assert "visits: 1,200" in result.parts[0]["answer"]
    assert "2,826" in result.parts[1]["answer"] and "AS day" in result.parts[1]["sql"]


def test_explicit_deep_without_openai_says_what_it_needs():
    with patch("config.LLM_PROVIDER", "ollama"):
        result = make_agent().ask(TWO_ASKS, depth="deep")
    assert result.route == "error" and "requires the OpenAI provider" in result.answer


def test_decomposed_deep_answer_shows_the_interpretation_once():
    plan = {"action": "decompose", "parts": TWO_ASKS.replace("? ", "?|").split("|"), "reason": "two asks",
            "interpretation": "Visits are sessions in session_master, in total and per visit day."}

    def complete(system="", user="", **_):
        if system.startswith("You are the lead analyst"):
            return json.dumps(plan)
        if system.startswith("You route a business question"):
            per_day = "per day" in user
            return json.dumps({"action": "sql", "tables": ["session_master"]} if per_day
                              else {"action": "metric", "metric": "visits_overview"})
        if system.startswith("You write ONE PostgreSQL"):
            return json.dumps({"sql": DAY_SQL})
        if system.startswith("Verify an analytics answer"):
            return json.dumps({"valid": True, "issues": []})
        raise AssertionError(f"unexpected model call: {system[:60]}")

    with patch("config.LLM_PROVIDER", "openai"), patch("llm.complete", side_effect=complete):
        result = make_agent().ask(TWO_ASKS, depth="deep", max_seconds=120)
    assert result.route == "deep" and result.verified and len(result.parts) == 2, result.answer
    assert result.answer.count("Interpreted as: Visits are sessions") == 1, result.answer
