"""Executive-summary briefs: scope, the vetted scorecard (DuckDB and PostgreSQL), findings and checked summaries.

The data is synthetic and built here, so these tests need no export. Expected figures are computed from the
generated rows in plain Python, never through SQL. PostgreSQL runs when pgserver is installed or
ANALYST_TEST_PG_DSN points at a scratch database (it is overwritten).
"""

import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import config  # noqa: E402
import llm  # noqa: E402
from analyst import report as report_lib  # noqa: E402
from analyst.agent import AnalystAgent, _sentence  # noqa: E402
from analyst.catalog import Catalog  # noqa: E402
from analyst.settings import Settings  # noqa: E402
from analyst.sources import PostgresSource, SnapshotSource  # noqa: E402

KNOWLEDGE = Path(__file__).resolve().parents[1] / "analyst" / "knowledge"
DAY = date(2026, 10, 8)                 # "yesterday": the newest data day is DAY + 1
APPS = {1: "Amazon", 2: "Flipkart", 3: "Meesho", 4: "Flipkart Minutes"}
CATEGORIES = [  # master, category, sub_category
    ("Beauty & Personal Care", "Skin Care", "Face Wash"), ("Beauty & Personal Care", "Hair Care", "Shampoo"),
    ("Mobiles & Tablets", "Smartphones", "Android Phones"), ("Mobiles & Tablets", "Smartphones", "iPhones"),
    ("Home & Kitchen", "Cookware", "Pans"),
    # "Others" is a category under two masters; "Accessories" a sub-category with two parents.
    ("Beauty & Personal Care", "Others", "Beauty Others"), ("Home & Kitchen", "Others", "Kitchen Others"),
    ("Mobiles & Tablets", "Smartphones", "Accessories"), ("Home & Kitchen", "Cookware", "Accessories")]
PATHS = [(m, c, s) for m, c, s in CATEGORIES]
EXTRA = {"Beauty Others": 5, "Kitchen Others": 5, "Accessories": 5}
# Out of 10, how many journeys convert, per first-search app and category: Flipkart leads in beauty, trails in phones.
CONVERT = {"Flipkart": {"Face Wash": 7, "Shampoo": 6, "Android Phones": 3, "iPhones": 2, "Pans": 5},
           "Amazon": {"Face Wash": 4, "Shampoo": 4, "Android Phones": 6, "iPhones": 6, "Pans": 5},
           "Meesho": {"Face Wash": 5, "Shampoo": 5, "Android Phones": 2, "iPhones": 2, "Pans": 5},
           "Flipkart Minutes": {"Face Wash": 9, "Shampoo": 9, "Android Phones": 9, "iPhones": 9, "Pans": 9}}
for _rates in CONVERT.values():
    _rates.update(EXTRA)
BRIEF = ("am flipkart's insights lead and need to give my category teams of beauty and personal care, small "
         "electronics and smartphones an executive summary of how did we perform yesterday. i need what we did well, "
         "where we stumbled, where competition did better than us. go as micro as possible: categories, city tiers, "
         "age groups, genders, the more interlocking the better. each analysis base should be minimum 30 people")


def _ts(day: date, hour: int) -> str:
    return datetime(day.year, day.month, day.day, hour).isoformat() + "+05:30"


def build_snapshot(folder: Path) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    users, journeys, windows = [], [], []
    for u in range(1, 721):
        users.append({"id": u, "gender": "Female" if u % 2 else "Male", "city": ["Mumbai", "Delhi", "Pune"][u % 3],
                      "tier": ["Gold", "Silver", "Bronze"][u % 3], "state": "MH", "zone": "West",
                      "device_model": "Pixel", "date_of_birth": f"{1975 + u % 30}-06-15"})

    def add(user: int, app_id: int, sub: str, day: date, hour: int, k: int):
        jid = len(journeys) + 1
        app = APPS[app_id]
        converted = (user * 7 + k) % 10 < CONVERT[app][sub]
        retained = converted and user % 4 != 0
        purchase = (app_id if retained else (app_id % 3) + 1) if converted else None
        journeys.append({"id": jid, "user_assoc_id": user, "first_search_app_id": app_id,
                         "first_search_time": _ts(day, hour), "last_search_time": _ts(day, hour + 1),
                         "status": "closed", "close_reason": "purchased" if converted else "timeout"})
        for hours in (24, 48):
            start = datetime.fromisoformat(_ts(day, hour))
            end = start + timedelta(hours=hours)
            windows.append({"id": len(windows) + 1, "journey_assoc_id": jid, "window_hours": hours,
                            "window_start": start.isoformat(), "window_end": end.isoformat(),
                            "first_search_app_id": app_id, "purchase_app_id": purchase, "search_category": sub,
                            "is_converted": converted, "is_retained": retained,
                            "is_leaked": converted and not retained, "is_final": False,
                            "hours_to_purchase": 2.5 if converted else None,
                            "order_amount": 999.0 if converted else None})

    for u in range(1, 721):
        app_id = 4 if u > 680 else (u % 3) + 1                 # 40 users only ever use Flipkart Minutes
        for k, sub in enumerate(("Face Wash", "Android Phones", "Pans")):
            add(u, app_id, sub, DAY, 10, k)
        if u % 5 == 0:
            add(u, app_id, "Shampoo", DAY - timedelta(days=1), 10, 3)       # the day before: outside the period
        for k, (every, sub) in enumerate(((7, "Kitchen Others"), (11, "Accessories"), (13, "Beauty Others")), 4):
            if u % every == 0:
                add(u, app_id, sub, DAY, 10, k)
    add(1, 1, "Face Wash", DAY + timedelta(days=1), 22, 0)                # newest data: the latest window start
    # One app session per user on DAY (and some the day before). created_at is the load time, three days later:
    # a visit is dated by the start encoded in session_id, never by created_at.
    sessions = []
    for u in range(1, 721):
        app = APPS[4 if u > 680 else (u % 3) + 1]
        for day in (DAY,) + ((DAY - timedelta(days=1),) if u % 5 == 0 else ()):
            sessions.append({"id": len(sessions) + 1, "session_id": f"m{u}_{app}_{day:%Y%m%d}T100000",
                             "insignificant_session": False, "competition_switch_bfr_5min": u % 4 == 0,
                             "created_at": _ts(DAY + timedelta(days=3), 9), "total_session_time_min": 5})
    tables = {"user_master": users, "cross_app_journey": journeys, "journey_window": windows,
              "session_master": sessions,
              "app_master": [{"id": i, "app_name": n, "parent_id": 2 if i == 4 else None} for i, n in APPS.items()],
              "category_master": [{"id": i + 1, "master_category": m, "category": c, "sub_category": s}
                                  for i, (m, c, s) in enumerate(CATEGORIES)]}
    for name, rows in tables.items():
        (folder / f"{name}.json").write_text(json.dumps(rows), encoding="utf-8")
    return tables


def truth(tables: dict, app: str, subs: set[str]) -> dict:
    """Overall figures for ``app`` on DAY in ``subs``, 24-hour windows, computed without SQL."""
    app_ids = {i for i, n in APPS.items() if n == app}
    users = {j["id"]: j["user_assoc_id"] for j in tables["cross_app_journey"]}
    rows = [w for w in tables["journey_window"] if w["window_hours"] == 24 and w["search_category"] in subs
            and w["window_start"][:10] == DAY.isoformat()]
    mine = [w for w in rows if w["first_search_app_id"] in app_ids]
    purchases = [w for w in rows if w["is_converted"]]
    won = [w for w in purchases if w["purchase_app_id"] in app_ids]
    return {"users": len({users[w["journey_assoc_id"]] for w in mine}), "journeys": len(mine),
            "conversion_pct": round(100.0 * sum(w["is_converted"] for w in mine) / len(mine), 1),
            "purchase_share_pct": round(100.0 * len(won) / len(purchases), 1)}


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    folder = tmp_path_factory.mktemp("report") / "public"
    return folder, build_snapshot(folder)


@pytest.fixture(scope="module")
def duck(data):
    cat = Catalog(KNOWLEDGE, "Amazon")
    src = SnapshotSource(data[0], cat.blocked_tables)
    cat.attach(src)
    return src, cat


@pytest.fixture(scope="module")
def pg(data, tmp_path_factory):
    from pg_loader import load_folder
    import urllib.parse as up
    dsn, server = os.getenv("ANALYST_TEST_PG_DSN"), None
    if not dsn:
        pgserver = pytest.importorskip("pgserver")
        server = pgserver.get_server(str(tmp_path_factory.mktemp("pg")), cleanup_mode="stop")
        dsn = server.get_uri()
    cat = Catalog(KNOWLEDGE, "Amazon")
    load_folder(data[0], dsn, cat.blocked_tables)
    uri = up.urlparse(dsn)
    host = dict(up.parse_qsl(uri.query)).get("host") or uri.hostname or "127.0.0.1"
    src = PostgresSource(Settings(source="postgres", db_host=host, db_port=uri.port or 5432,
                                  db_name=uri.path.lstrip("/") or "postgres", db_username=uri.username or "postgres",
                                  db_password=uri.password or "unused", ssh_host=""))
    cat.attach(src)
    yield src, cat
    src.close()


def scorecard_rows(src, cat, dimension: str, category_map: dict | None = None) -> list[list]:
    agent = AnalystAgent(src, cat, Settings(source="snapshot"), use_llm=False)
    metric = cat.metrics[report_lib.SCORECARD]
    if category_map:
        from dataclasses import replace
        metric = replace(metric, dimensions={**metric.dimensions,
                                             "requested_category": report_lib.requested_category_sql(category_map)},
                         params=[replace(p, values=[*p.values, "requested_category"]) if p.name == "dimension"
                                 else p for p in metric.params])
    scope_params = {"window_hours": 24, "min_users": 30, "date_from": DAY.isoformat(), "date_to": DAY.isoformat()}
    if category_map:
        scope_params["categories"] = [v for vs in category_map.values() for v in vs]
    result = agent._run_scorecard(metric, {**scope_params, "dimension": dimension}, dimension, keep_back=0)
    assert result.route == "metric", result.answer
    return result.table["rows"]


ASKED = {"beauty and personal care": ["Beauty & Personal Care"], "smartphones": ["Mobiles & Tablets > Smartphones"]}
# What those paths cover: never Kitchen Others (another master's "Others"), never Accessories (two parents).
ASKED_SUBS = {"Face Wash", "Shampoo", "Beauty Others", "Android Phones", "iPhones"}


# --------------------------------------------------------------------------- reading the brief

def test_brief_is_recognised_and_ordinary_questions_are_not():
    assert report_lib.looks_like_report(BRIEF)
    for question in ("purchase share by app", "conversion by gender", "users by age group and city",
                     "how many users visited Amazon in the last 24 hours", "top 10 users who visited Meesho"):
        assert not report_lib.looks_like_report(question), question


def test_who_we_are_comes_from_the_asker():
    apps = ["Amazon", "Amazon Now", "Flipkart", "Meesho"]
    assert report_lib.stated_home_app(BRIEF, apps) == "Flipkart"
    assert report_lib.stated_home_app("I'm at Meesho, how did we do?", apps) == "Meesho"
    assert report_lib.stated_home_app("we at Amazon Now want a brief", apps) == "Amazon Now"
    assert report_lib.stated_home_app("compare Flipkart and Meesho conversion", apps) is None
    assert report_lib.stated_home_app("is Amazon's share higher than Flipkart's insights?", apps) is None


def test_min_users_and_breakdowns_are_read_without_a_model():
    assert report_lib.min_users_from(BRIEF) == 30
    assert report_lib.min_users_from("only groups with at least 50 users") == 50
    assert report_lib.min_users_from("sample size must be minimum 100") == 100
    wanted = report_lib.breakdowns_from(BRIEF)
    assert {"requested_category", "gender", "age_group"} <= set(wanted)
    assert "city" not in wanted and "tier" not in wanted, "city tiers is neither a city nor a loyalty-tier split"
    assert "requested_category+gender" in wanted, "'interlocking' asks for crossed breakdowns"


def test_category_names_resolve_to_exact_values_only():
    levels = {"Beauty & Personal Care": {"master_category"}, "Smartphones": {"category"},
              "Small Home Appliances": {"category"}, "Face Wash": {"sub_category"}}
    assert report_lib.resolve_category("beauty and personal care", levels) == (["Beauty & Personal Care"], [])
    assert report_lib.resolve_category("smartphone", levels) == (["Smartphones"], [])
    values, close = report_lib.resolve_category("small electronics", levels)
    assert values == [] and "Small Home Appliances" in close, "a near name is suggested, never silently used"
    assert report_lib.categories_in_text(BRIEF, levels, []) == ["Beauty & Personal Care", "Smartphones"]


def test_a_category_name_shared_by_several_masters_is_tied_to_the_right_one():
    paths, labels, left_out = report_lib.to_paths(["Skin Care", "Others"], PATHS)
    assert paths == ["Beauty & Personal Care > Skin Care", "Beauty & Personal Care > Others"] and not left_out
    assert labels == ["Skin Care", "Others (in Beauty & Personal Care)"]
    assert report_lib.to_paths(["Others"], PATHS) == ([], [], ["Others"]), "alone it could be any master's Others"
    assert report_lib.to_paths(["Beauty & Personal Care", "Smartphones"], PATHS)[0] == [
        "Beauty & Personal Care", "Mobiles & Tablets > Smartphones"]


# --------------------------------------------------------------------------- the scorecard SQL

def test_scorecard_matches_figures_computed_without_sql(duck, data):
    src, cat = duck
    rows = scorecard_rows(src, cat, "overall", ASKED)
    cols = ["segment", "app", "users", "journeys", "converted", "conversion_pct", "retained_pct", "leaked_pct",
            "purchases_won", "purchase_share_pct"]
    by_app = {r[1]: dict(zip(cols, r)) for r in rows}
    for app in ("Flipkart", "Amazon", "Meesho", "Flipkart Minutes"):
        expected = truth(data[1], app, ASKED_SUBS)
        assert {k: by_app[app][k] for k in expected} == expected, app
    # Pans, Kitchen Others, Accessories (two parents) and the day before are outside the brief.
    assert all(r[0] == "All journeys" for r in rows)


def test_scorecard_keeps_only_groups_with_enough_users(duck):
    src, cat = duck
    for dimension in ("requested_category+gender", "city", "age_group"):
        rows = scorecard_rows(src, cat, dimension, ASKED)
        assert rows and all(r[2] >= 30 for r in rows), dimension
    segments = {r[0] for r in scorecard_rows(src, cat, "requested_category+gender", ASKED)}
    assert segments <= {"Beauty & Personal Care · Female", "Beauty & Personal Care · Male",
                        "Smartphones · Female", "Smartphones · Male"}, segments


def test_scorecard_is_identical_on_postgresql(duck, pg):
    for dimension in ("overall", "requested_category", "requested_category+gender", "age_group", "gender"):
        assert scorecard_rows(*pg, dimension, ASKED) == scorecard_rows(*duck, dimension, ASKED), dimension


def session_rows(src, cat, name: str) -> list[list]:
    agent = AnalystAgent(src, cat, Settings(source="snapshot"), use_llm=False)
    day = {"date_from": DAY.isoformat(), "date_to": DAY.isoformat()}
    result = agent._run_scorecard(cat.metrics[name], day, name, keep_back=0)
    assert result.route == "metric", result.answer
    return result.table["rows"]


def test_session_measures_are_dated_by_the_visit_not_the_load_time(duck, pg):
    switching = {r[0]: r for r in session_rows(*duck, "competitor_switch_by_app")}
    # 227 Flipkart sessions on DAY (none of the day before); created_at, three days later, is ignored.
    assert switching["Flipkart"][1:] == [227, 57, 25.1]
    for name in ("competitor_switch_by_app", "active_users_by_app"):
        assert session_rows(*pg, name) == session_rows(*duck, name), name


# --------------------------------------------------------------------------- findings and the written summary

COLS = ["segment", "app", "users", "journeys", "converted", "conversion_pct", "leaked_pct", "purchase_share_pct"]


def test_fisher_exact_test_matches_textbook_values():
    assert round(report_lib.fisher_p(3, 4, 1, 4), 4) == 0.4857         # Fisher's tea-tasting table
    assert round(report_lib.fisher_p(1, 10, 11, 14), 6) == 0.002759
    assert report_lib.fisher_p(0, 50, 0, 60) == 1.0
    # Live screenshot: Flipkart 3 of 80 vs Amazon 3 of 47 was reported as "where we stumbled". It is chance.
    assert report_lib.fisher_p(3, 80, 3, 47) > 0.5


def test_findings_are_like_for_like_and_skip_catch_all_groups():
    rows = [["Female", "Flipkart", 40, 400, 40, 10.0, 5.0, 30.0],
            ["Female", "Flipkart Minutes", 35, 300, 150, 50.0, 0, 20.0],      # our own sub-app: never a rival
            ["Female", "Amazon", 45, 500, 150, 30.0, 10.0, 40.0],
            ["Male", "Flipkart", 40, 400, 160, 40.0, 5.0, 45.0], ["Male", "Meesho", 31, 300, 60, 20.0, 2.0, 25.0],
            ["Male", "Zepto", 33, 50, 25, 50.0, 1.0, 5.0],                   # a grocery app: higher rate, other mix
            ["Unknown", "Flipkart", 30, 60, 0, 0.0, 0, 10.0], ["Unknown", "Amazon", 35, 100, 30, 30.0, 1.0, 50.0],
            ["Kids", "Amazon", 33, 40, 8, 20.0, 1.0, 50.0]]
    overall = {"converted": 200, "journeys": 860}
    found, chance = report_lib.keep_significant(report_lib.findings(
        "gender", COLS, rows, "Flipkart", {"Flipkart Minutes": "Flipkart"}, 30, overall=overall))
    kinds = {(f.kind, f.segment) for f in found if f.kind != "coverage"}
    assert kinds == {("behind", "Female"), ("weak", "Female"), ("lead", "Male")}, kinds
    behind = next(f for f in found if f.kind == "behind")
    assert ("vs 30% (150 of 500) for Amazon, the biggest competitor here (-20.0 pts, p < 0.001)" in behind.text)
    weak = next(f for f in found if f.kind == "weak")
    assert "vs 34.8% in the rest of its journeys (-24.8 pts" in weak.text
    texts = " ".join(f.text for f in found)
    assert "Zepto" not in texts and "Flipkart Minutes" not in texts and "Unknown" not in texts
    assert any(f.kind == "coverage" and "1 of 4 groups" in f.text for f in found)


def test_within_a_category_every_competitor_counts_and_chance_gaps_are_dropped():
    rows = [["Shoes", "Myntra", 60, 200, 1, 0.5, 0, 2.0], ["Shoes", "Flipkart", 90, 200, 30, 15.0, 2.0, 60.0],
            ["Shoes", "Meesho", 70, 200, 10, 5.0, 1.0, 20.0],
            ["Bags", "Myntra", 40, 80, 3, 3.8, 0, 30.0], ["Bags", "Amazon", 34, 47, 3, 6.4, 4.3, 14.3]]
    found, chance = report_lib.keep_significant(report_lib.findings("category", COLS, rows, "Myntra", {}, 30))
    gaps = [f for f in found if f.kind == "behind"]
    assert len(gaps) == 1 and gaps[0].segment == "Shoes", "one finding per group: the largest significant gap"
    assert "for Flipkart, the best-converting competitor here" in gaps[0].text
    assert chance == 1, "Bags: 3 of 80 vs 3 of 47 could be chance"


def test_false_discovery_rate_is_controlled_across_the_brief():
    found = [report_lib.Finding("", "behind", "city", c, c, ["A"], -5.0, p)
             for c, p in (("a", 0.001), ("b", 0.02), ("c", 0.03), ("d", 0.2))]
    kept, chance = report_lib.keep_significant(found)
    assert [f.segment for f in kept] == ["a", "b", "c"] and chance == 1


def test_summary_sentences_sit_under_the_heading_of_their_findings():
    behind = report_lib.Finding("F1", "behind", "gender", "Female", "Gender Female: Flipkart converted 30% vs 50% "
                                "for Amazon (-20.0 pts, p < 0.001).", ["Flipkart", "Amazon"], -20.0, 0.0001)
    lead = report_lib.Finding("F2", "lead", "gender", "Male", "Gender Male: Flipkart converted 60% vs 40% for "
                              "Meesho (+20.0 pts, p < 0.001).", ["Flipkart", "Meesho"], 20.0, 0.0001)
    reply = {"went_well": [{"text": "Men converted 60% with us vs 40% at Meesho.", "findings": ["F2"]}],
             "stumbled": [{"text": "Women converted 30% vs 50% at Amazon.", "findings": ["F1"]}],
             "competitors_better": [{"text": "Amazon converted 50% of women vs our 30%.", "findings": ["F1"]},
                                    {"text": "Amazon is ahead with women.", "findings": ["F1"]}],
             "actions": [{"text": "Test a women's offer.", "findings": ["F1"]}]}
    kept, dropped = report_lib.validated_summary(reply, {"F1": behind, "F2": lead}, ["Amazon", "Flipkart", "Meesho"])
    assert list(kept) == ["went_well", "competitors_better", "actions"]
    assert len(kept["competitors_better"]) == 1, "the same finding is not repeated"
    assert any("cites behind findings under 'Where we stumbled'" in d for d in dropped)


def test_summary_sentences_must_repeat_their_findings():
    a = report_lib.Finding("F1", "behind", "gender", "Female", "Gender Female: Flipkart converted 30% vs 50% "
                           "for Amazon, the best competitor (-20.0 pts).", ["Flipkart", "Amazon"])
    b = report_lib.Finding("F2", "lead", "gender", "Male", "Gender Male: Flipkart converted 60% vs 40% for "
                           "Meesho, the best competitor (+20.0 pts).", ["Flipkart", "Meesho"])
    by_id, apps = {"F1": a, "F2": b}, ["Amazon", "Flipkart", "Meesho"]
    assert report_lib.check_bullet("Amazon beat us with women: 50% vs our 30%.", ["F1"], by_id, apps) == []
    assert report_lib.check_bullet("Amazon beat us with women by 25 points.", ["F1"], by_id, apps)    # new number
    assert report_lib.check_bullet("Meesho beat us with women: 50% vs 30%.", ["F1"], by_id, apps)     # wrong app
    assert report_lib.check_bullet("Male shoppers: 50% at Amazon vs our 30%.", ["F1"], by_id, apps)   # wrong group
    assert report_lib.check_bullet("We did fine.", [], by_id, apps) == ["it cites no finding"]


# --------------------------------------------------------------------------- the whole brief

# What OpenAI's understand_question tool returns for BRIEF (the scope of the brief comes from here).
BRIEF_READING = {
    "rephrased_question": f"Executive summary of Flipkart's performance on {DAY.isoformat()} in beauty and "
                          "personal care, small electronics and smartphones vs competitors, by category, age "
                          "group and gender, groups of at least 30 users",
    "intent": "brief", "focus_app": "Flipkart", "date_from": DAY.isoformat(), "date_to": DAY.isoformat(),
    "categories": [{"asked": "beauty and personal care", "matches": ["Skin Care", "Hair Care", "Others"]},
                   {"asked": "small electronics", "matches": ["Gadgets"]},
                   {"asked": "smartphones", "matches": ["Smartphones"]}],
    "breakdowns": ["requested_category", "gender", "age_group", "requested_category+gender"],
    "min_users": 30, "measures": ["visitors", "conversion", "cross_app_switching"],
    "unavailable": [{"asked": "city tiers", "reason": "City tier is not recorded; tier is a loyalty tier"},
                    {"asked": "graphs", "reason": "No graph/chart output is provided by the vetted analyses."}]}


def _openai_brief(found_ids: list):
    """OpenAI writing the summary: one faithful sentence, one invented, one under the wrong heading."""
    def complete(system="", user="", schema=None, **_):
        if schema is report_lib.SUMMARY_SCHEMA:
            rows = re.findall(r"^(F\d+) \[(\w+)\] (.*)$", user, re.M)
            found_ids.extend(i for i, _, _ in rows)
            findings = {i: t for i, _, t in rows}
            lead = next(i for i, k, t in rows if k == "lead" and not t.startswith("Overall"))
            behind = next(i for i, k, _ in rows if k == "behind")
            return json.dumps({
                "went_well": [{"text": findings[lead].split(" (")[0] + ".", "findings": [lead]}],
                "stumbled": [{"text": "Smartphones were our weak spot.", "findings": [behind]}],
                "competitors_better": [{"text": "Amazon converted 99.9% of phone journeys.", "findings": [behind]}],
                "actions": [{"text": "Move smartphone ad spend to the segments where Amazon leads.",
                             "findings": [behind]}]})
        raise AssertionError(f"unexpected model call: {system[:70]}")
    return complete


def test_the_brief_end_to_end(duck, data):
    src, cat = duck
    agent = AnalystAgent(src, cat, Settings(source="snapshot"))
    found_ids: list = []
    with patch.object(config, "LLM_PROVIDER", "openai"), patch("llm.complete", side_effect=_openai_brief(found_ids)), \
            patch("llm.call_tool", return_value=BRIEF_READING):
        result = agent.ask(BRIEF, depth="auto", max_seconds=120)
    assert result.route == "deep" and result.verified, result.answer
    assert result.answer.startswith(f"**Flipkart: executive summary, {DAY.isoformat()}**")
    assert result.resolved == BRIEF_READING["rephrased_question"]
    assert cat.home_app == "Amazon", "'we' = Flipkart for this question only, never for other requests"
    assert "home_app" not in result.state, "OpenAI reads 'we' from the conversation; nothing is kept aside"
    assert [c["question"] for c in result.clarifications] == ["Which category did you mean by 'small electronics'?"]
    assert "graphs" not in result.answer.lower(), "every table has a chart: never claim graphs are missing"
    # Visitors and switching come from sessions dated by session_id: 227 Flipkart users on DAY, one session each,
    # 57 of them (every 4th user) opened a competitor app within 5 minutes.
    assert "**Visitors and app switching**" in result.answer
    assert "Flipkart had 227 visitors in 227 sessions" in result.answer
    assert "in 25.1% of Flipkart's 227 sessions the user opened a competitor app within 5 minutes" in result.answer
    assert {"Visitors (app sessions)", "Switching to a competitor app (sessions)"} <= {p["question"] for p in result.parts}
    assert "**Still open:** 1 part of your request needs a detail from you" in result.answer
    assert "'small electronics'" in result.answer and "City tier is not recorded" in result.answer
    assert "**What went well**" in result.answer and "99.9" not in result.answer
    assert "**Suggested actions for 2026-10-09**" in result.answer
    assert "summary sentence was checked against the findings it cites" in result.answer
    assert any("Summary sentence dropped" in s["text"] for s in result.thinking)
    labels = [p["question"] for p in result.parts]
    assert labels[:4] == ["Overall", "Asked category", "Gender", "Age group"]
    assert "Asked category × Gender" in labels
    assert "beauty and personal care = Skin Care, Hair Care, Others (in Beauty & Personal Care)" in result.answer
    assert "statistically significant (Fisher's exact test" in result.answer
    assert all(p["verified"] and p["table"]["rows"] for p in result.parts)
    overall = {r[1]: r for r in result.parts[0]["table"]["rows"]}
    assert overall["Flipkart"][5] == truth(data[1], "Flipkart", ASKED_SUBS)["conversion_pct"]
    assert found_ids and all(f"{i}:" in "\n".join(p["answer"] for p in result.parts) for i in found_ids)
    assert not any("for Flipkart Minutes" in p["answer"] for p in result.parts)
    assert any("cites behind findings under 'Where we stumbled'" in s["text"] for s in result.thinking)


def test_the_brief_without_openai_uses_rules_and_computed_findings(duck):
    src, cat = duck
    agent = AnalystAgent(src, cat, Settings(source="snapshot"))

    def offline(**_):
        raise llm.OllamaError("offline in tests")

    with patch.object(config, "LLM_PROVIDER", "ollama"), patch("llm.complete", side_effect=offline):
        result = agent.ask(BRIEF, depth="auto", max_seconds=60)
    assert result.route == "deep" and result.verified, result.answer
    assert "Categories: beauty and personal care" not in result.answer          # exact names, found in the text
    assert "Beauty & Personal Care" in result.answer and "Smartphones" in result.answer
    assert "**Where competitors did better**" in result.answer
    assert "summary sentence was checked" not in result.answer


def test_notes_read_as_sentences():
    assert _sentence("analysis timed out before completion") == "Analysis timed out before completion."
    assert _sentence("City tier is not recorded.") == "City tier is not recorded."


# --------------------------------------------------------------------------- a thin base (live screenshot)

SCORECARD_COLUMNS = ["segment", "app", "users", "journeys", "converted", "conversion_pct", "retained_pct",
                     "leaked_pct", "purchases_won", "purchase_share_pct"]
PARTLY_LINKED = ("13,469 journeys ended in a purchase, but only 1,974 of those purchases are linked to their app, "
                 "time and value. Conversion, leakage, purchase-share and time-to-purchase figures describe linked "
                 "purchases only and may understate purchasing.")


class ThinSource:
    """Yesterday in four categories: only Flipkart reached 30 users, and no smaller group did."""
    dialect, kind = "postgres", "stub"

    def query(self, sql, **_):
        from analyst.sources import Result
        assert "segment_purchases" in sql, sql
        rows = [["All journeys", "Flipkart", 36, 61, 2, 3.3, 3.3, 0, 5, 10.2]] if "'All journeys'" in sql else []
        return Result(SCORECARD_COLUMNS, rows)


def thin_agent() -> AnalystAgent:
    cat = Catalog(KNOWLEDGE, "Amazon")
    cat.apps = ["Amazon", "Flipkart", "Flipkart Minutes", "Meesho"]
    cat.app_parents = {"Flipkart Minutes": "Flipkart"}
    cat.date_range = ("2026-09-06", "2026-10-09")
    for doc in list(cat.tables.values()) + list(cat.metrics.values()):
        doc.available = True
    cat.quality_issues = [
        {"code": "partly_linked_purchase_outcomes", "detail": PARTLY_LINKED,
         "tables": ["cross_app_journey", "journey_window"]},
        {"code": "stale_is_final", "detail": "Only 28,688 of 173,562 finished windows have is_final = true.",
         "tables": ["journey_window"]}]
    return AnalystAgent(ThinSource(), cat, Settings(source="snapshot"))


def test_a_thin_base_is_not_reported_as_no_difference():
    """Live: only Flipkart reached the base, and the brief said 'No difference ... between Flipkart and its
    competitors' - which the simple-meaning intro turned into 'no clear difference'. Nothing was compared."""
    def offline(**_):
        raise llm.OllamaError("offline in tests")

    with patch.object(config, "LLM_PROVIDER", "ollama"), patch("llm.complete", side_effect=offline):
        result = thin_agent().ask(BRIEF, depth="auto", max_seconds=60)
    assert "No difference" not in result.answer and "No gap" not in "".join(p["answer"] for p in result.parts)
    assert "**No competitor reached 30 users in any group where Flipkart did**" in result.answer
    assert ("Overall: no competitor reached 30 users, so Flipkart (3.3% of 61 journeys converted, 36 users) "
            "is not compared.") in result.parts[0]["answer"]
    assert "Too few people to compare (under 30 per group) by: Category, Gender, Age group" in result.answer
    assert re.search(r"\*\*Data caveat \(F\d+\):\*\* " + re.escape(PARTLY_LINKED), result.answer)
    assert "is_final" not in result.answer
    assert result.verified and all(p["verified"] for p in result.parts)
    suggestions = result.follow_ups
    assert any("in the last 7 days" in q for q in suggestions) and any("minimum 10 users" in q for q in suggestions)


def test_wider_questions_are_read_back_as_briefs_with_the_new_scope():
    scope = report_lib.Scope("Flipkart", date_from="2026-10-08", date_to="2026-10-08",
                             category_map={"beauty and personal care": ["Beauty & Personal Care"],
                                           "smartphones": ["Mobiles & Tablets > Smartphones"]},
                             unresolved={"small electronics": []},
                             breakdowns=["overall", "requested_category", "category", "gender", "age_group"])
    coverage = ("2026-09-06", "2026-10-09")
    week, every, smaller = report_lib.wider_questions(scope, coverage)
    assert report_lib.period_words(scope, "2026-10-12") == "on 2026-10-08", "never 'from X to X'"
    assert week == ("Executive summary of how Flipkart performed in the last 7 days in beauty and personal care, "
                    "smartphones and small electronics vs competitors, by category, gender and age group, "
                    "minimum 30 users per group"), "every asked category is kept and no split is repeated"
    assert "yesterday across all categories" in every and "yesterday in beauty" in smaller
    for question in (week, every, smaller):
        assert report_lib.looks_like_report(question), question
        assert {"gender", "age_group"} <= set(report_lib.breakdowns_from(question)), question
    assert report_lib.min_users_from(smaller) == 10 and report_lib.min_users_from(week) == 30
    # A week-long brief widens to a month, never to "the last 7 days" again.
    scope.date_from = "2026-10-03"
    assert "in the last 30 days" in report_lib.wider_questions(scope, coverage)[0]


def test_a_brief_about_another_app_in_the_same_chat_is_about_that_app():
    """'We' = Flipkart carries over in the chat, but 'how did Amazon perform' is a brief about Amazon."""
    def offline(**_):
        raise llm.OllamaError("offline in tests")

    question = "Executive summary of how Amazon performed yesterday vs competitors, by category, gender and age group"
    assert report_lib.subject_app(question, ["Amazon", "Amazon Now", "Flipkart"]) == "Amazon"
    assert report_lib.subject_app("how did we perform yesterday by city and gender", ["Amazon", "Flipkart"]) is None
    with patch.object(config, "LLM_PROVIDER", "ollama"), patch("llm.complete", side_effect=offline):
        result = thin_agent().ask(question, state={"home_app": "Flipkart"}, depth="auto", max_seconds=60)
    assert result.answer.startswith("**Amazon: executive summary, 2026-10-08**"), result.answer
    assert "**Amazon has fewer than 30 users** (period: 2026-10-08)" in result.answer
    assert result.state["home_app"] == "Flipkart", "the chat's 'we' is unchanged"


class CloseCallSource(ThinSource):
    """Flipkart 3 of 80 vs Amazon 3 of 47: a gap that may well be chance (the live Amazon brief)."""

    def query(self, sql, **_):
        from analyst.sources import Result
        rows = ([["All journeys", "Flipkart", 44, 80, 3, 3.8, 3.8, 0, 6, 85.7],
                 ["All journeys", "Amazon", 34, 47, 3, 6.4, 2.1, 4.3, 1, 14.3]] if "'All journeys'" in sql else [])
        return Result(SCORECARD_COLUMNS, rows)


def test_without_a_reliable_gap_the_requested_actions_are_tests_on_labelled_facts():
    """The user asked for five improvements; nothing was significant. The actions still come, built on the
    possible-chance gap and the data caveat, and headed as tests - never as proven findings."""
    agent = thin_agent()
    agent.source = CloseCallSource()
    reading = {"rephrased_question": "Executive summary of Flipkart on 2026-10-08 vs competitors by gender, with "
                                     "five actionable improvements", "intent": "brief", "focus_app": "Flipkart",
               "date_from": "2026-10-08", "date_to": "2026-10-08", "breakdowns": ["gender"], "min_users": 30}

    def complete(system="", user="", schema=None, **_):
        if schema is report_lib.SUMMARY_SCHEMA:
            ids = dict(re.findall(r"^(F\d+) \[(\w+)\]", user, re.M))
            chance = next(i for i, k in ids.items() if k == "directional")
            caveat = next(i for i, k in ids.items() if k == "context")
            return json.dumps({"went_well": [], "stumbled": [],
                               "competitors_better": [{"text": "Amazon beat us.", "findings": [chance]}],
                               "actions": [{"text": "A/B test checkout reminders against Amazon.", "findings": [chance]},
                                           {"text": "Fix purchase linking before the next read.", "findings": [caveat]}]})
        raise llm.LLMError("not needed")

    with patch.object(config, "LLM_PROVIDER", "openai"), patch("llm.call_tool", return_value=reading), \
            patch("llm.complete", side_effect=complete):
        result = agent.ask("brief", depth="auto", max_seconds=60)
    assert "**Suggested actions for 2026-10-09 (tests to run: no gap here is statistically reliable yet)**" \
        in result.answer, result.answer
    assert "Fix purchase linking before the next read." in result.answer
    assert "Amazon beat us" not in result.answer, "a possible-chance gap is never shown as a competitor win"
    assert "not statistically reliable at this sample size" in result.parts[0]["answer"]
    assert "**Data caveat (F" in result.answer


# --------------------------------------------------------------------------- a category asked by name, at any level

def metric_rows(src, cat, name: str, params: dict) -> list[list]:
    agent = AnalystAgent(src, cat, Settings(source="snapshot"), use_llm=False)
    result = agent._run_scorecard(cat.metrics[name], params, name, keep_back=0)
    assert result.route == "metric", result.answer
    return result.table["rows"]


def finished_journeys(tables: dict, subs: set[str]) -> int:
    newest = max(w["window_start"] for w in tables["journey_window"])
    return sum(1 for w in tables["journey_window"] if w["window_hours"] == 24 and w["search_category"] in subs
               and w["window_end"] <= newest)


def test_a_category_or_master_category_name_finds_its_sub_categories(duck, data):
    """Live: "conversion in Mobiles" found no rows - journeys store the sub-category ("Smartphones"), and the
    filter looked for the word "Mobiles" inside it."""
    src, cat = duck
    phones = finished_journeys(data[1], {"Android Phones", "iPhones"})      # never "Accessories": two parents
    for asked, expected in (("Smartphones", phones), ("mobiles & tablets", phones),
                            ("Android", finished_journeys(data[1], {"Android Phones"}))):
        rows = metric_rows(src, cat, "conversion_by_first_app", {"window_hours": 24, "category": asked})
        assert sum(r[1] for r in rows) == expected, asked


def test_category_filters_are_identical_on_postgresql(duck, pg):
    for name in ("conversion_by_first_app", "purchase_share_by_app", "leakage_destinations", "order_value_by_app",
                 "time_to_purchase_by_app", "search_category_trend"):
        params = {"window_hours": 24, "category": "Mobiles & Tablets"}
        assert metric_rows(*pg, name, params) == metric_rows(*duck, name, params), name
