"""Self-contained regressions for failures observed against the live database.

These tests do not depend on the optional sample export under backend/data.
"""

import json
import re
from pathlib import Path
from unittest.mock import patch

import config
from analyst import metrics as metric_lib
from analyst.agent import ANSWER_REVIEW_SCHEMA, DEEP_PLAN_SCHEMA, AnalystAgent, comparison_facts
from analyst.catalog import _category_regime
from analyst.catalog import Catalog
from analyst.settings import Settings
from analyst.sources import Result


KNOWLEDGE = Path(__file__).resolve().parents[1] / "analyst" / "knowledge"


class StubSource:
    dialect = "postgres"
    kind = "stub"

    def query(self, sql, **_):
        if "PERCENTILE_CONT" in sql:
            return Result(["purchase_app", "purchases", "avg_hours", "median_hours"],
                          [["Amazon", 4, 2.5, 2.0], ["Meesho", 3, 3.2, 2.8], ["Flipkart", 2, 4.1, 3.9]])
        if "top_categories" in sql:
            days = ["2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-19", "2026-09-28", "2026-09-29"]
            rows = []
            for i, day in enumerate(days):
                new = day >= "2026-09-17"
                rows += [[day, "Other categories", 600, 60.0 if new else 20.0],
                         [day, "Softlines", 0 if new else 450, 0.0 if new else 45.0],
                         [day, "Uncategorised", 180, 18.0]]
                if new:
                    rows.append([day, "Women's Traditional Wear", 150 + i, 17.0 if day >= "2026-09-28" else 15.0])
            return Result(["day", "search_category", "journeys", "share_of_day_pct"], rows)
        if "checkout_confirmations" in sql:
            return Result(["app", "checkout_confirmations", "confirmation_share_pct", "sessions",
                           "sessions_with_confirmation_pct"], [["Amazon", 4, 57.1, 100, 4.0],
                                                                ["Meesho", 3, 42.9, 90, 3.3]])
        raise AssertionError(f"unexpected SQL: {sql}")


def make_agent() -> AnalystAgent:
    catalog = Catalog(KNOWLEDGE, "Amazon")
    catalog.apps = ["Amazon", "Meesho", "Flipkart"]
    catalog.date_range = ("2026-09-12", "2026-09-17")
    for table in catalog.tables.values():
        table.available = True
    for metric in catalog.metrics.values():
        metric.available = True
    return AnalystAgent(StubSource(), catalog, Settings(source="snapshot"))


def test_duration_question_uses_session_metric_without_model():
    agent = make_agent()
    route, _ = agent.router.route("which app name is top in session duration?")
    assert route.kind == "metric" and route.metric.name == "session_summary_by_app"


def openai_stub(plan: dict, verdict: str = "Not a spike: it is steady."):
    """OpenAI with a fixed Deep strategy and comparison verdict that approves every answer."""
    def complete(**kwargs):
        if kwargs.get("system", "").startswith("Answer a comparison question"):
            return verdict
        if kwargs.get("schema") is DEEP_PLAN_SCHEMA:
            return json.dumps({"reason": "one vetted metric", "interpretation": "as asked", **plan})
        if kwargs.get("schema") is ANSWER_REVIEW_SCHEMA:
            return json.dumps({"valid": True, "issues": []})
        raise AssertionError(f"unexpected model call: {kwargs.get('system', '')[:80]}")
    return patch.multiple("llm", complete=complete), patch.object(config, "LLM_PROVIDER", "openai")


def ask_deep(agent, question, plan, verdict="Not a spike: it is steady.", understood: dict | None = None):
    """Deep mode with OpenAI's reading of the question (``understood``) and a fixed strategy."""
    model, provider = openai_stub(plan, verdict)
    reading = {"rephrased_question": question, "intent": "analysis", **(understood or {})}
    with model, provider, patch("llm.call_tool", return_value=reading):
        return agent.ask(question, depth="deep")


def test_deep_comparison_highlights_apps_instead_of_filtering():
    # Observed live: OpenAI picks the right metric but puts every compared app in one filter value.
    agent = make_agent()
    result = ask_deep(agent, "What's our average time-to-purchase compared to Meesho and Flipkart?",
                      {"action": "metric", "metric": "time_to_purchase_by_app",
                       "params": {"app": "Amazon, Meesho, Flipkart", "window_hours": 72}},
                      verdict="Amazon is fastest in the 72-hour window: 2.5 average hours vs 3.2 for Meesho "
                              "and 4.1 for Flipkart.",
                      understood={"focus_app": "Amazon", "apps": ["Amazon", "Meesho", "Flipkart"]})
    assert result.route == "metric" and result.metric == "time_to_purchase_by_app" and result.verified
    assert result.depth == "deep" and "app" not in result.params
    assert "a.app_name =" not in result.sql
    assert result.answer.startswith("**Answer:** Amazon is fastest in the 72-hour window: 2.5 average hours")
    assert "- Amazon minus Meesho: -0.7 avg hours." in result.answer
    for line in ("Amazon: purchases 4, avg hours 2.5, median hours 2 - ranked 1 of 3 by purchases.",
                 "Meesho: purchases 3, avg hours 3.2, median hours 2.8 - ranked 2 of 3 by purchases.",
                 "Flipkart: purchases 2, avg hours 4.1, median hours 3.9 - ranked 3 of 3 by purchases."):
        assert line in result.answer


def test_deep_single_failed_part_explains_why():
    agent = make_agent()
    result = ask_deep(agent, "average time to purchase in a 36 hour window",
                      {"action": "metric", "metric": "time_to_purchase_by_app", "params": {"window_hours": 36}})
    assert result.route == "clarify" and not result.verified
    assert "window_hours must be 24, 48 or 72" in result.answer
    assert "could not answer any part" not in result.answer


def test_model_metric_params_split_comparisons():
    agent = make_agent()
    ttp = agent.cat.metrics["time_to_purchase_by_app"]
    params, focus, per_app = agent._model_metric_params(
        ttp, {"app": ["amazon", "Meesho"], "window_hours": 24, "bogus": 1}, "time to purchase on amazon and meesho")
    assert params == {"window_hours": 24} and focus == ["Amazon", "Meesho"] and per_app == []
    # One app and no comparison stays a filter.
    assert agent._model_metric_params(ttp, {"app": "Meesho"}, "How fast do Meesho shoppers buy?") == (
        {"app": "Meesho"}, [], [])
    # "we/our" in a list means the home app.
    _, focus, _ = agent._model_metric_params(ttp, {"app": "our app vs Flipkart"}, "how fast do we sell")
    assert focus == ["Amazon", "Flipkart"]
    # A single-app analysis cannot filter on several apps: it is run once per app.
    leak = agent.cat.metrics["leakage_destinations"]
    params, focus, per_app = agent._model_metric_params(leak, {"app": "Amazon and Flipkart"}, "where do they leak")
    assert "app" not in params and per_app == ["Amazon", "Flipkart"]


def test_structured_plan_drops_all_filter_and_resolves_home():
    agent = make_agent()
    _, route = agent._normalise_deep_part({
        "part": "time_to_purchase_by_app",
        "params": {"app": "HOME", "window_hours": 72, "category": "all"},
    })
    resolved = metric_lib.resolve_params(route.metric, route.params, agent.cat)
    sql = metric_lib.render(route.metric, resolved)
    assert resolved["app"] == "Amazon" and "category" not in resolved
    assert "search_category ILIKE" not in sql


def test_data_gap_uses_labelled_proxy_not_purchase_claim():
    agent = make_agent()
    agent.cat.metric_issues["purchase_share_by_app"] = "Purchase outcomes are not linked."
    result = ask_deep(agent, "What's Amazon's purchase share and who is ahead of us?",
                      {"action": "metric", "metric": "purchase_share_by_app", "params": {}})
    assert result.route == "proxy" and result.metric == "checkout_confirmation_share_by_app" and result.verified
    assert result.answer.startswith("Verified purchase share is unavailable")
    assert "Checkout-confirmation signal" in result.answer
    assert "not verified purchase share" in result.answer


def test_deep_spike_question_keeps_the_whole_period_and_answers_with_a_verdict():
    agent = make_agent()
    agent.cat.categories = ["Women's Traditional Wear", "Softlines"]
    agent.cat.category_comparable_from = "2026-09-17"
    agent.cat.date_range = ("2026-09-15", "2026-09-29")          # "this week" = 2026-09-28 to 2026-09-29
    # Observed live: OpenAI narrowed the trend to the named category and the last 8 days.
    result = ask_deep(agent, "Is Women's Traditional Wear always this dominant, or is this a spike this week?",
                      {"action": "metric", "metric": "search_category_trend",
                       "params": {"category": "Women's Traditional Wear", "date_from": "2026-09-24",
                                  "date_to": "2026-10-01"}},
                      verdict="Not a spike: Women's Traditional Wear averaged 17% this week vs 15% before.",
                      understood={"date_from": "2026-09-28", "date_to": "2026-09-29",
                                  "categories": [{"asked": "Women's Traditional Wear",
                                                  "matches": ["Women's Traditional Wear"]}]})
    assert "date_from" not in result.params and "date_to" not in result.params
    assert result.answer.startswith("**Answer:** Not a spike: Women's Traditional Wear averaged 17%")
    assert "Only days from 2026-09-17 are compared" in result.answer
    assert ("- Women's Traditional Wear: average share of day 17% in 2026-09-28 to 2026-09-29 (2 days) vs "
            "15% on the 3 earlier days (2026-09-17 to 2026-09-19).") in result.answer
    assert ("- Women's Traditional Wear had the highest share of day of any single search category on 5 of 5 days "
            "(2 of 2 in 2026-09-28 to 2026-09-29), not counting the Other categories and Uncategorised groups.") \
        in result.answer
    assert "Listing Women's Traditional Wear only; the table has all 4 search category values" in result.answer


def test_verdict_with_invented_number_is_dropped_but_facts_stay():
    agent = make_agent()
    agent.cat.categories = ["Women's Traditional Wear"]
    result = ask_deep(agent, "Is Women's Traditional Wear always this dominant, or is this a spike this week?",
                      {"action": "metric", "metric": "search_category_trend", "params": {}},
                      verdict="Yes, a spike: its share jumped 42% this week.")
    assert "**Answer:**" not in result.answer and "jumped 42%" not in result.answer
    assert "Comparison (computed from the rows):" in result.answer
    assert any("Comparison verdict rejected" in step["text"] for step in result.thinking)


def test_comparison_facts_by_app_rank_and_difference():
    table = {"columns": ["purchase_app", "purchases", "avg_hours"],
             "rows": [["Blinkit", 9, 11.4], ["Flipkart", 283, 5.2], ["Amazon", 82, 4.8], ["Meesho", 381, 3.6],
                      ["Unknown app", 3, 20.0]]}
    facts = comparison_facts(table, "our time to purchase vs meesho", ["Amazon", "Meesho"])
    assert facts == [
        "Amazon: avg hours 4.8; 2 of the other 3 purchase app values are higher and 1 lower "
        "(highest: Blinkit 11.4, lowest: Meesho 3.6).",
        "Meesho: avg hours 3.6; 3 of the other 3 purchase app values are higher and 0 lower "
        "(highest: Blinkit 11.4, lowest: Meesho 3.6).",
        "Amazon minus Meesho: 1.2 avg hours."]


def test_comparison_facts_over_time_without_groups_uses_last_week_as_recent():
    days = [f"2026-09-{d:02d}" for d in range(10, 24)]
    table = {"columns": ["day", "journeys", "conversion_pct"],
             "rows": [[d, 100, 2.0 if i < 7 else 4.0] for i, d in enumerate(days)]}
    facts = comparison_facts(table, "is conversion usually this high?", [], baseline=True)
    assert facts[0] == ("conversion: average conversion 4% in 2026-09-17 to 2026-09-23 (7 days) vs 2% on the "
                        "7 earlier days (2026-09-10 to 2026-09-16).")


def test_category_regime_detects_taxonomy_change_only_when_real():
    early = [(f"09-{d:02d}", 8) for d in range(6, 15)]
    late = [(f"09-{d:02d}", 190 + d % 5) for d in range(17, 31)]
    assert _category_regime(early + [("09-15", 33), ("09-16", 63)] + late) == ("09-17", 8, 192)
    assert _category_regime([(f"09-{d:02d}", 50 + d % 4) for d in range(1, 25)]) == (None, 0, 0)


def test_category_trend_folds_small_categories_and_keeps_the_named_one(tmp_path):
    from analyst.sources import SnapshotSource
    windows, wid = [], 0
    for day in ("2026-09-20", "2026-09-21"):
        for category, n in (("A", 9), ("B", 8), ("C", 7), ("D", 6), ("E", 5), ("F", 4), ("Rare", 1), (None, 3)):
            for _ in range(n):
                wid += 1
                windows.append({"id": wid, "window_hours": 24, "window_start": f"{day}T10:00:00+05:30",
                                "window_end": f"{day}T10:00:00+05:30", "search_category": category,
                                "is_converted": False, "journey_assoc_id": wid, "first_search_app_id": 1})
    (tmp_path / "journey_window.json").write_text(json.dumps(windows))
    (tmp_path / "app_master.json").write_text(json.dumps([{"id": 1, "app_name": "Amazon"}]))
    catalog = Catalog(KNOWLEDGE, "Amazon")
    source = SnapshotSource(tmp_path, catalog.blocked_tables)
    catalog.attach(source)
    metric = catalog.metrics["search_category_trend"]
    sql = metric_lib.render(metric, metric_lib.resolve_params(metric, {"category": "rare"}, catalog))
    from analyst import guard
    rows = source.query(guard.check(sql, catalog, dialect="duckdb").sql).rows
    day1 = {r[1]: (r[2], r[3]) for r in rows if str(r[0]) == "2026-09-20"}
    assert day1 == {"A": (9, 20.9), "B": (8, 18.6), "C": (7, 16.3), "D": (6, 14.0), "E": (5, 11.6),
                    "Other categories": (4, 9.3), "Uncategorised": (3, 7.0), "Rare": (1, 2.3)}


def test_no_metric_cross_joins_a_scalar_aggregate_cte():
    """A scalar-aggregate CTE must be read as a subquery, never CROSS JOINed.

    `session_master` metrics derive their columns from `split_part(session_id, ...)` behind a
    regex filter. PostgreSQL cannot estimate that selectivity, costs the CTE at ~32 rows instead
    of ~240,000, and therefore puts a CROSS JOINed aggregate on the inner side of a nested loop -
    re-aggregating the entire CTE once per row. "Total visits and visitors" ran for over 20
    minutes that way; as `(SELECT newest FROM latest)` the same query takes ~4 s, because an
    uncorrelated subquery is evaluated once as an InitPlan.
    """
    catalog = Catalog(KNOWLEDGE, "Amazon")
    offenders = []
    for metric in catalog.metrics.values():
        scalar_ctes = set(re.findall(
            r"(\w+)\s+AS\s*\(\s*SELECT\s+(?:MAX|MIN|COUNT|SUM|AVG)\s*\([^()]*\)\s+AS\s+\w+\s+FROM",
            metric.sql, re.I))
        for cte in scalar_ctes:
            if re.search(rf"\bCROSS\s+JOIN\s+{re.escape(cte)}\b", metric.sql, re.I):
                offenders.append(f"{metric.name} CROSS JOINs the scalar CTE '{cte}'")
    assert not offenders, "; ".join(offenders)


def test_session_metrics_do_not_join_back_onto_the_session_cte():
    """The same misestimate makes any join onto the per-session CTE a nested-loop trap.

    `active_users_by_app` used to join `p` (one row per session) to a per-user aggregate of `p`.
    It must aggregate to one row per app x user first and get the user's app count from a window
    function, which no row estimate can turn quadratic.
    """
    catalog = Catalog(KNOWLEDGE, "Amazon")
    for name in ("active_users_by_app", "visits_overview"):
        sql = catalog.metrics[name].sql
        assert "split_part(session_id" in sql.lower(), f"{name} no longer reads session_id"
        joins = re.findall(r"\b(?:INNER\s+|LEFT\s+|RIGHT\s+|CROSS\s+)?JOIN\s+(\w+)", sql, re.I)
        assert not {j.lower() for j in joins} & {"p", "s", "latest"}, \
            f"{name} joins back onto a per-session CTE: {joins}"


def _vetted_answer_for(question: str):
    """The metric that would answer `question`, or None when it becomes a custom query."""
    from analyst.agent import _grain_mismatch
    from analyst.router import Router
    catalog = Catalog(KNOWLEDGE, "Amazon")
    route, _ = Router(catalog).route(question)
    if route.kind != "metric" or _grain_mismatch(question, route.metric):
        return None
    return route.metric.name


def test_asking_for_the_users_themselves_never_answers_with_a_per_app_total():
    """A request for the users must not be answered by an analysis that cannot name one.

    "give me user list who visited maximum time in meesho" came back as "Active users by app" - a
    single row holding Meesho's user count - and the follow-ups "user id" and "give me list user top
    10" repeated that same row, each explaining it could not identify the users. Whatever answers
    these must return one row per user: `top_users_by_visits`, or a custom query.
    """
    from analyst.agent import _metric_groups_by
    catalog = Catalog(KNOWLEDGE, "Amazon")
    for question in ("give me user list who visisted maximum time in meesho",
                     "What are the user IDs of the users who visited Meesho the most?",
                     "Give me the top 10 users who visited Meesho the most",
                     "give me list user top 10",
                     "which users switched the most",
                     "user wise visits"):
        name = _vetted_answer_for(question)
        assert name is None or _metric_groups_by(catalog.metrics[name], r"\buser"), \
            f"{question!r} -> {name}, which does not return one row per user"


def test_ordinary_questions_still_reach_their_vetted_analysis():
    """The user-listing guard must not make the router shy about questions it answers well."""
    for question, expected in (
        ("how many users visited Amazon in the last 24 hours", "active_users_by_app"),
        ("active users by app today", "active_users_by_app"),
        ("which app had the most unique users yesterday", "active_users_by_app"),
        ("users who only use Flipkart", "active_users_by_app"),
        ("how many total visits", "visits_overview"),
        ("conversion by user attribute", "conversion_by_user_attribute"),
        ("users by age group", "users_by_attribute"),
        ("purchase share by app", "purchase_share_by_app"),
    ):
        assert _vetted_answer_for(question) == expected, question


def test_a_measure_named_after_users_is_not_a_row_per_user():
    """COUNT(DISTINCT user_key) AS users is one number about users, not a listing of them."""
    from analyst.agent import _metric_groups_by
    catalog = Catalog(KNOWLEDGE, "Amazon")
    assert not _metric_groups_by(catalog.metrics["active_users_by_app"], r"\buser")
    assert not _metric_groups_by(catalog.metrics["visits_overview"], r"\buser")


class _ReviewStubSource:
    """A source that records what it was asked to run and returns two rows."""

    dialect, kind = "postgres", "stub"

    def __init__(self):
        self.executed = []

    def query(self, sql, **_):
        self.executed.append(sql)
        return Result(["journey_id", "search_category"],
                      [["J054503", "Women's Traditional Wear"], ["J054559", "Women's Dresses"]])

    def introspect(self):
        return {"cross_app_journey": [("id", "integer"), ("first_search_time", "timestamptz")],
                "journey_window": [("journey_assoc_id", "integer"), ("search_category", "text"),
                                   ("window_hours", "integer")],
                "app_master": [("id", "integer"), ("app_name", "text")]}

    def row_counts(self, tables):
        return {t: 10 for t in tables}

    def close(self):
        pass


def _catalog_on(source):
    from analyst.catalog import Column
    catalog = Catalog(KNOWLEDGE, "Amazon")
    live = source.introspect()
    for name, doc in catalog.tables.items():
        doc.available = name in live
        if doc.available:
            doc.columns = {c: Column(c, t, "", "ok") for c, t in live[name]}
    for metric in catalog.metrics.values():
        metric.available = all(catalog.tables.get(t) and catalog.tables[t].available for t in metric.tables)
    return catalog


def test_a_query_the_reviewer_dislikes_still_returns_its_rows():
    """The reviewer is an opinion; the guard and the database are the hard constraints.

    Its objections used to be raised as GuardError, so a debatable one blocked as hard as a blocked
    column: "Add subcategory and master category to the table" spent 36 s and came back "I could not
    write a valid query for this". A query the guard approved and the database ran is now shown with
    the objection attached, so there is both an answer and a warning.
    """
    from analyst import agent as agent_mod
    from analyst.agent import AnalystAgent, _Run
    from analyst.router import Route
    from journey_agent.thinking import Budget

    source = _ReviewStubSource()
    agent = AnalystAgent(source, _catalog_on(source), Settings(source="snapshot"))
    sql = ("SELECT caj.id AS journey_id, jw.search_category AS search_category "
           "FROM cross_app_journey caj JOIN journey_window jw ON jw.journey_assoc_id = caj.id "
           "WHERE jw.window_hours = 24 LIMIT 100")
    agent._llm = lambda **_: json.dumps({"sql": sql, "explanation": "x"})
    agent._openai_review_sql = lambda *a, **k: ["The category_master join matches only top-level rows"]
    agent._narrate = lambda *a, **k: ("", True)

    run = _Run(Budget(60))
    token = agent_mod._RUN.set(run)
    try:
        answer = agent._free_sql("Add subcategory and master category to the table", [],
                                 ["cross_app_journey", "journey_window"],
                                 Route("sql", tables=["cross_app_journey", "journey_window"], confidence=1.0),
                                 [], strict_deep=True)
    finally:
        agent_mod._RUN.reset(token)

    assert answer.route == "sql", f"got {answer.route}: {answer.answer[:120]}"
    assert answer.table and answer.table["rows"], "the rows were thrown away"
    assert answer.answer.startswith("**Check before relying on this.**")
    assert any("category_master join" in s.text for s in run.trace), "the objection must be visible"
    assert "category_master" not in answer.answer, "the answer itself stays in business language (/ask)"
    assert answer.verified is False
    assert source.executed, "the query was never run"


def test_a_complete_table_is_never_reported_as_truncated():
    """The reviewer kept reading the answer's row limit as missing data.

    It rejected a 500-row result because the answer text listed 50 of them, which is the intended
    format - the complete table is rendered beside the answer. When the result is whole, that
    objection is false by construction and is dropped.
    """
    from analyst.agent import _COMPLETENESS_COMPLAINT
    for complaint in ("The answer lists only the first 50 of the 500 result rows. The full table is "
                      "not attached in the provided answer.",
                      "The result lists only the first 50 of 500 rows, so it is truncated"):
        assert _COMPLETENESS_COMPLAINT.search(complaint), complaint
    for genuine in ("The answer states Flipkart had 2191 users but the rows show 2181",
                    "The answer does not include the subcategory column that was requested",
                    "The query filters on the wrong app"):
        assert not _COMPLETENESS_COMPLAINT.search(genuine), genuine


def test_extending_a_result_needs_its_own_query():
    """A vetted analysis returns a fixed set of columns, so you cannot add one to it.

    "Add subcategory and master category to the table" kept the previous vetted analysis and
    answered "the results provide search categories and journey counts, but not subcategory or
    master category" - true, and useless. Such a request has to become a custom query that joins
    category_master.
    """
    for question in ("Add subcategory and master category to the table",
                     "add the master category column",
                     "also include the user id in the result",
                     "include master category along with the search category column"):
        assert _vetted_answer_for(question) is None, question


def test_the_extend_rule_leaves_ordinary_questions_alone():
    for question, expected in (("purchase share by app", "purchase_share_by_app"),
                               ("how many users visited Amazon in the last 24 hours", "active_users_by_app"),
                               ("which categories drive the most leakage", "leakage_by_category"),
                               ("search category trend", "search_category_trend"),
                               ("top 10 users by visits", "top_users_by_visits")):
        assert _vetted_answer_for(question) == expected, question


def test_the_search_category_taxonomy_join_is_documented():
    """The reviewer blocked a category query because nothing said which level search_category is.

    It is not one level. Measured on the live 24h windows: 75.1% of rows name a `sub_category` but
    24.9% name a `master_category` from the older broad taxonomy, so joining on `sub_category` alone
    silently drops a quarter of the rows - which is close to what the review objected to. The file
    has to carry both the level names and a join that covers all of them.
    """
    text = (KNOWLEDGE / "tables" / "category_master.md").read_text(encoding="utf-8")
    assert "master_category` is the TOP level" in text, "the level names must be spelled out"
    for level in ("cm.sub_category = jw.search_category",
                  "cm.category = jw.search_category",
                  "cm.master_category = jw.search_category"):
        assert level in text, f"the documented join must cover {level}"
    assert "LEFT JOIN" in text, "a quarter of rows would otherwise be dropped"


def test_deep_mode_obeys_the_same_shape_guard_as_the_router():
    """Deep mode checked only that OpenAI's metric existed, not that it fitted the question.

    So "and add user id", after a 223-row custom query about one app, came back as the whole
    "Top users by visits" table ranked across all apps - a different question. The router already
    refuses a metric that cannot produce the requested shape; Deep mode has to refuse it too.
    """
    from analyst.agent import _grain_mismatch
    catalog = Catalog(KNOWLEDGE, "Amazon")
    resolved = "Give me the user IDs and available user details of users who visited app 0005"
    assert _grain_mismatch(resolved, catalog.metrics["top_users_by_visits"], "and add user id"), \
        "the guard must reject this metric for an add-a-column follow-up"
    # ... and the Deep planner must consult the shape guard before using the metric OpenAI picks.
    source = (Path(__file__).resolve().parents[1] / "analyst" / "agent.py").read_text(encoding="utf-8")
    deep = source.split("def _deep(")[1].split("def _deep_plan(")[0]
    assert "self._wrong_shape(question, picked, original)" in deep, \
        "Deep mode must apply the shape guard to the metric OpenAI picks"


def test_top_users_metric_is_not_matched_by_a_bare_mention_of_user_id():
    """Its tags included "user id", so "and add user id" scored it as the whole question."""
    catalog = Catalog(KNOWLEDGE, "Amazon")
    tags = catalog.metrics["top_users_by_visits"].tags
    assert "user id" not in tags and "user ids" not in tags, tags
    assert "top users" in tags and "user list" in tags, "it must still answer its own questions"


def test_a_listing_of_entities_must_carry_its_identifier():
    """"give me users details" returned city/state/gender/device for 223 rows and no user id,
    so no row could be acted on. The query writer is told to put the identifier first."""
    from analyst.agent import SQL_SYSTEM
    assert "must identify the entity" in SQL_SYSTEM
    assert "user_master.id for a user" in SQL_SYSTEM


def test_a_forced_custom_query_builds_on_the_previous_one():
    """"add their city" must extend the previous answer, not re-derive it from scratch.

    The follow-up needs a shape no vetted analysis returns, so it becomes a custom query - and a
    custom query written from nothing invents its own period and counting rule. "add user id" came
    back with 250 visits where the previous answer said 494. The query behind the previous answer
    (a vetted metric's rendered SQL counts) is handed to the writer so the definitions carry over.
    """
    from analyst.agent import AnalystAgent
    from analyst.conversation import Memory
    from analyst.router import Route

    source = _ReviewStubSource()
    agent = AnalystAgent(source, _catalog_on(source), Settings(source="snapshot"))
    previous_sql = "SELECT u.id AS user_id, COUNT(*) AS visits FROM session_master GROUP BY u.id"
    memory = Memory([{ "question": "top 10 users who visited Meesho the most", "route": "metric",
                       "metric": "top_users_by_visits", "params": {"app": "Meesho"}, "answered": True,
                       "tables": ["session_master"], "sql": previous_sql, "columns": ["user_id", "visits"],
                       "rows": [["4783", 494]], "total_rows": 10, "entities": ["4783"]}])

    route = agent._continue_previous(Route("sql", tables=["session_master"], confidence=0.2),
                                     memory, "add their city and state to the table")
    assert route.kind == "sql"
    assert route.prior_sql == previous_sql, "the previous query must be handed to the writer"

    # A metric follow-up is still left to the router's own path, not forced into SQL.
    metric_route = Route("metric", agent.cat.metrics["active_users_by_app"], {}, ["session_master"], 1.0)
    assert agent._continue_previous(metric_route, memory, "what about Flipkart?").kind == "metric"
