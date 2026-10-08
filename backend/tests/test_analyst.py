"""Tests for the multi-table Data analyst.

Truth is computed from the JSON fixture in plain Python (never via SQL), metrics
are run on DuckDB AND a real PostgreSQL (when pgserver is installed), and the
guard is attacked with the queries a model might produce.
Run:  python -m pytest tests/test_analyst.py -q
"""

import json
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import llm  # noqa: E402
from analyst import guard, metrics as metric_lib  # noqa: E402
from analyst.agent import AnalystAgent  # noqa: E402
from analyst.catalog import Catalog  # noqa: E402
from analyst.settings import Settings  # noqa: E402
from analyst.sources import PostgresSource, SnapshotSource  # noqa: E402
from analyst_fixture import build  # noqa: E402

KNOWLEDGE = Path(__file__).resolve().parents[1] / "analyst" / "knowledge"


def offline(**_):
    raise llm.OllamaError("offline in tests")


@pytest.fixture(scope="session")
def snap(tmp_path_factory):
    return build(tmp_path_factory.mktemp("snap") / "public")


@pytest.fixture(scope="session")
def truth(snap):
    load = lambda n: json.loads((snap / f"{n}.json").read_text())  # noqa: E731
    return {n: load(n) for n in ("journey_window", "app_master", "cross_app_journey", "cross_app_journey_event",
                                 "user_master")}


@pytest.fixture(scope="session")
def duck(snap):
    cat = Catalog(KNOWLEDGE, "Amazon")
    src = SnapshotSource(snap, cat.blocked_tables)
    cat.attach(src)
    return src, cat


@pytest.fixture(scope="session")
def pg(snap, tmp_path_factory):
    pgserver = pytest.importorskip("pgserver")
    from pg_loader import load_folder
    server = pgserver.get_server(str(tmp_path_factory.mktemp("pg")), cleanup_mode="stop")
    cat = Catalog(KNOWLEDGE, "Amazon")
    load_folder(snap, server.get_uri(), cat.blocked_tables)
    import urllib.parse as up
    uri = up.urlparse(server.get_uri())
    host = dict(up.parse_qsl(uri.query)).get("host", "127.0.0.1")
    src = PostgresSource(Settings(source="postgres", db_host=host, db_port=uri.port or 5432,
                                  db_name=uri.path.lstrip("/") or "postgres", db_username=uri.username or "postgres",
                                  db_password="unused", ssh_host=""))
    cat.attach(src)
    yield src, cat
    src.close()


@pytest.fixture()
def agent(duck):
    src, cat = duck
    return AnalystAgent(src, cat, Settings(source="snapshot"))


def ask(agent, question, **kw):
    with patch("llm.complete", side_effect=offline) as mocked:
        result = agent.ask(question, **kw)
    return result, mocked


# --------------------------------------------------------------------------- knowledge
def test_knowledge_bundle_is_valid_okf():
    cat = Catalog(KNOWLEDGE, "Amazon")
    assert len(cat.tables) >= 15 and len(cat.metrics) >= 20
    for path in KNOWLEDGE.rglob("*.md"):
        if path.name == "index.md":
            continue
        text = path.read_text()
        assert text.startswith("---") and "\ntype:" in text.split("---")[1], path  # OKF: frontmatter with type
    assert {"analytics_token", "authtoken_token", "internal_user", "llm_api_key"} <= cat.blocked_tables


def test_secret_tables_never_loaded(duck):
    src, cat = duck
    assert "analytics_token" not in src.loaded and "internal_user" not in src.loaded
    assert cat.policy("user_master", "email") == "blocked"
    assert cat.policy("user_master", "date_of_birth") == "aggregate_only"


# --------------------------------------------------------------------------- guard
@pytest.mark.parametrize("sql, message", [
    ("DELETE FROM journey_window", "SELECT"),
    ("SELECT 1; DROP TABLE app_master", "exactly one"),
    ("SELECT * FROM app_master", "SELECT *"),
    ("SELECT a.* FROM app_master a", "SELECT *"),
    ("SELECT email FROM user_master", "personal or secret"),
    ("SELECT id FROM user_master WHERE mobile_number LIKE '9%'", "personal or secret"),
    ("SELECT date_of_birth FROM user_master", "personal data"),
    ("SELECT event_text FROM testing_accessibility_infos", "large raw data"),
    ("SELECT key FROM analytics_token", "security policy"),
    ("SELECT username FROM internal_user", "security policy"),
    ("SELECT usename FROM pg_catalog.pg_user", "not allowed"),
    ("SELECT table_name FROM information_schema.tables", "not allowed"),
    ("SELECT pg_read_file('/etc/passwd')", "not allowed"),
    ("SELECT id FROM secret_stuff", "Unknown table"),
    ("WITH u AS (SELECT email FROM user_master) SELECT email FROM u", "personal or secret"),
    ("SELECT id INTO copy_table FROM app_master", "not allowed"),
])
def test_guard_rejects(duck, sql, message):
    _, cat = duck
    with pytest.raises(guard.GuardError, match=message):
        guard.check(sql, cat)


def test_guard_allows_aggregates_and_caps_limit(duck):
    _, cat = duck
    ok = guard.check("SELECT city, COUNT(*) AS users, MIN(EXTRACT(YEAR FROM date_of_birth)) AS oldest_year "
                     "FROM user_master GROUP BY city", cat, max_rows=100)
    assert "LIMIT 101" in ok.sql and ok.tables == ["user_master"]
    capped = guard.check("SELECT id FROM app_master LIMIT 100000", cat, max_rows=50)
    assert "LIMIT 51" in capped.sql
    assert '"order"' not in guard.check("WITH x AS (SELECT id FROM app_master) SELECT id FROM x", cat).sql


# --------------------------------------------------------------------------- metrics
def _run_all(src, cat):
    out = {}
    for m in cat.metrics.values():
        given = {"app": "Meesho", "from_app": "Amazon", "to_app": "Meesho", "hours": 72}
        if m.param("dimension"):
            given["dimension"] = m.param("dimension").values[0]
        params = metric_lib.resolve_params(m, {k: v for k, v in given.items() if m.param(k)}, cat)
        checked = guard.check(metric_lib.render(m, params), cat, dialect=src.dialect)
        out[m.name] = src.query(checked.sql).rows
    return out


def test_every_metric_runs_on_duckdb(duck):
    results = _run_all(*duck)
    assert len(results) == len(duck[1].metrics)


def test_postgres_matches_duckdb(duck, pg):
    a, b = _run_all(*duck), _run_all(*pg)
    for name in a:
        assert json.dumps(a[name], default=str) == json.dumps(b[name], default=str), name


def test_purchase_share_matches_python(duck, truth):
    src, cat = duck
    apps = {a["id"]: a["app_name"] for a in truth["app_master"]}
    bought = Counter(apps.get(w["purchase_app_id"], "Unknown app") for w in truth["journey_window"]
                     if w["window_hours"] == 24 and w["is_converted"])
    total = sum(bought.values())
    m = cat.metrics["purchase_share_by_app"]
    rows = src.query(guard.check(metric_lib.render(m, metric_lib.resolve_params(m, {}, cat)), cat,
                                 dialect="duckdb").sql).dicts()
    for r in rows:
        assert r["purchases"] == bought[r["purchase_app"]]
        assert r["share_pct"] == round(100.0 * bought[r["purchase_app"]] / total, 1)


def test_conversion_by_first_app_matches_python(duck, truth):
    src, cat = duck
    when = lambda text: datetime.fromisoformat(text)  # noqa: E731
    horizon = max(when(w["window_start"]) for w in truth["journey_window"])       # finished = ended before newest data
    rows = [w for w in truth["journey_window"] if w["window_hours"] == 24 and when(w["window_end"]) <= horizon
            and w["first_search_app_id"] == 2]
    m = cat.metrics["conversion_by_first_app"]
    got = src.query(guard.check(metric_lib.render(m, metric_lib.resolve_params(m, {"app": "Amazon"}, cat)), cat,
                                dialect="duckdb").sql).dicts()[0]
    assert got["journeys"] == len(rows)
    assert got["converted"] == sum(w["is_converted"] for w in rows)
    assert got["leaked_pct"] == round(100.0 * sum(w["is_leaked"] for w in rows) / len(rows), 1)


def test_app_switch_paths_match_python(duck, truth):
    src, cat = duck
    apps = {a["id"]: a["app_name"] for a in truth["app_master"]}
    by_journey = defaultdict(list)
    for e in truth["cross_app_journey_event"]:
        by_journey[e["journey_assoc_id"]].append(e)
    switches = Counter()
    for events in by_journey.values():
        events.sort(key=lambda e: (e["step"], e["search_time"]))
        for prev, cur in zip(events, events[1:]):
            a, b = apps.get(prev["app_assoc_id"]), apps.get(cur["app_assoc_id"])
            if a and b and a != b:
                switches[(a, b)] += 1
    m = cat.metrics["app_switch_paths"]
    rows = src.query(guard.check(metric_lib.render(m, metric_lib.resolve_params(m, {"limit": 500}, cat)), cat,
                                 dialect="duckdb").sql).rows
    assert {(r[0], r[1]): r[2] for r in rows} == dict(switches)


def test_trend_share_is_computed_before_filtering(duck):
    src, cat = duck
    m = cat.metrics["purchase_share_trend"]
    rows = src.query(guard.check(metric_lib.render(m, metric_lib.resolve_params(m, {"app": "Meesho"}, cat)), cat,
                                 dialect="duckdb").sql).dicts()
    assert rows and any(r["share_of_day_pct"] < 100 for r in rows)


# --------------------------------------------------------------------------- router
@pytest.mark.parametrize("question, metric", [
    ("What's Amazon's overall purchase share right now, and who are the top two apps ahead of us?", "purchase_share_by_app"),
    ("Of all journeys that start on Amazon, what share end up buying on Amazon vs. somewhere else?", "conversion_by_first_app"),
    ("What's our average time-to-purchase compared to Meesho and Flipkart?", "time_to_purchase_by_app"),
    ("Has Meesho's 35% purchase share grown or shrunk over the past 4 weeks?", "purchase_share_trend"),
    ("Is Women's Traditional Wear always this dominant a search category, or is this a spike this week?", "search_category_trend"),
    ("Are we losing more or less search traffic to competitors this month compared to last month?", "competitor_switch_by_app"),
    ('Has the "1 app per journey" pattern always been this high, or is cross-app searching increasing over time?', "multi_app_trend"),
    ("When users search on Amazon and then leave, which specific categories drive most of that leakage?", "leakage_by_category"),
    ("For users who go Amazon → Meesho, what are they typically searching for right before they switch?", "queries_before_switch"),
    ("Why is our conversion-within-24h only 2.7% — is it concentrated in a few categories, or spread evenly?", "conversion_24h_by_category"),
    ("Is any smaller app (Zepto, Ajio, Shopsy) gaining share faster than the others recently?", "purchase_share_trend"),
    ("Which app do we lose to most often specifically in fashion/apparel categories, versus grocery/quick-commerce categories?", "lost_to_by_category_group"),
    ("Does our retention rate differ by city tier, device type, or time of day?", "conversion_by_user_attribute"),
    ('Are "repeat switchers" (users who bounce between 3+ apps) a different segment from single-app users?', "repeat_switchers"),
    ("average session time by app", "session_summary_by_app"),
    ("list all apps", "app_list"),
    ("How many users visited Amazon only in the last 24 hours?", "active_users_by_app"),
    ("which app had the most unique users yesterday", "active_users_by_app"),
])
def test_business_questions_route_to_the_right_analysis(agent, question, metric):
    route, _ = agent.router.route(question)
    assert route.metric is not None and route.metric.name == metric


def test_comparison_highlights_instead_of_filtering(agent):
    route, _ = agent.router.route("What's our average time-to-purchase compared to Meesho and Flipkart?")
    assert "app" not in route.params and route.focus == ["Amazon", "Meesho", "Flipkart"]


# --------------------------------------------------------------------------- agent (offline)
def test_purchase_share_answer_offline(agent, truth):
    result, mocked = ask(agent, "What's Amazon's overall purchase share right now, and who are the top two apps ahead of us?")
    mocked.assert_not_called()
    apps = {a["id"]: a["app_name"] for a in truth["app_master"]}
    bought = Counter(apps.get(w["purchase_app_id"]) for w in truth["journey_window"] if w["window_hours"] == 24 and w["is_converted"])
    share = round(100.0 * bought["Amazon"] / sum(bought.values()), 1)
    assert f"Amazon: purchases {bought['Amazon']}, share {share:g}%" in result.answer
    assert "Ahead of Amazon: Meesho" in result.answer
    assert result.state["metric"] == "purchase_share_by_app" and result.table["rows"]


def test_follow_up_uses_state(agent):
    first, _ = ask(agent, "conversion rate by app within 24h for Amazon")
    second, _ = ask(agent, "what about Flipkart?", state=first.state)
    assert second.metric == first.metric and second.params["app"] == "Flipkart"


def test_multi_dimension_question_goes_deep_and_notes_city_tier(agent):
    result, _ = ask(agent, "Does our retention rate differ by city tier, device type, or time of day?")
    assert result.depth == "deep" and len(result.parts) == 2
    assert {p["question"].split(" by ")[1].split(" (")[0] for p in result.parts} == {"device model", "hour of day"}
    assert "City tier is not recorded" in result.answer


def test_unknown_question_offline_is_honest(agent):
    result, _ = ask(agent, "how many products does Chefigo have")
    assert result.route == "help" and "language model" in result.answer


def test_period_questions_note_data_coverage(agent):
    result, _ = ask(agent, "Has Meesho's purchase share grown over the past 4 weeks?")
    assert "The database covers" in result.answer


# --------------------------------------------------------------------------- agent (model paths)
def test_model_sql_is_guarded_and_repaired(agent):
    replies = iter([
        json.dumps({"action": "sql", "tables": ["product_master"]}),
        json.dumps({"sql": "SELECT * FROM product_master WHERE brand = 'Chefigo'"}),         # rejected: *
        json.dumps({"sql": "SELECT email FROM user_master"}),                                # rejected: PII
        json.dumps({"sql": "SELECT brand, COUNT(*) AS products FROM product_master WHERE brand ILIKE 'chefigo' GROUP BY brand"}),
        "Chefigo has 999 products.",                                                         # invented number
    ])
    with patch("llm.complete", side_effect=lambda **_: next(replies)):
        result = agent.ask("how many products does Chefigo have", depth="fast")
    assert result.route == "sql" and "product_master" in result.tables
    rejected = [t for t in result.thinking if "rejected" in t["text"].lower()]
    assert len(rejected) >= 3                                   # *, PII, invented number
    assert "999" not in result.answer and "Chefigo" in result.answer


def _active_truth(truth_sessions, hours=None):
    """Users per app (and users of only that app) from raw session ids, in plain Python."""
    import datetime as dt
    rows = []
    for r in truth_sessions:
        parts = (r.get("session_id") or "").split("_")
        if len(parts) != 3 or r.get("insignificant_session") or not re.fullmatch(r"20\d{6}T\d{6}\d*", parts[2]):
            continue
        t = parts[2]
        started = dt.datetime(int(t[:4]), int(t[4:6]), int(t[6:8])) + dt.timedelta(
            hours=int(t[9:11]), minutes=int(t[11:13]), seconds=int(t[13:15]))
        rows.append((parts[0], parts[1], started))
    newest = max(s for _, _, s in rows)
    if hours:
        rows = [x for x in rows if x[2] > newest - dt.timedelta(hours=hours)]
    apps_of = {}
    for user, app, _ in rows:
        apps_of.setdefault(user, set()).add(app)
    out = {}
    for user, app, _ in rows:
        out.setdefault(app, set()).add(user)
    return {app: (len(users), sum(1 for u in users if len(apps_of[u]) == 1)) for app, users in out.items()}


def test_active_users_match_python(agent, snap):
    sessions = json.loads((snap / "session_master.json").read_text())
    for hours in (None, 24, 72):
        expected = _active_truth(sessions, hours)
        m = agent.cat.metrics["active_users_by_app"]
        params = metric_lib.resolve_params(m, {"hours": hours} if hours else {}, agent.cat)
        sql = guard.check(metric_lib.render(m, params), agent.cat, dialect=agent.source.dialect).sql
        got = {r[0]: (r[1], r[2]) for r in agent.source.query(sql).rows}
        assert got == expected, hours


def test_visited_only_last_24_hours_question(agent, snap):
    result, _ = ask(agent, "How many users visited Amazon only in the last 24 hours?")
    assert result.metric == "active_users_by_app" and result.params == {"app": "Amazon", "hours": 24}
    expected = _active_truth(json.loads((snap / "session_master.json").read_text()), 24).get("Amazon", (0, 0))
    assert f"users: {expected[0]}" in result.answer and f"users only this app: {expected[1]}" in result.answer
    assert "last 24 hours of data" in result.answer


def test_deep_rescues_parts_the_model_wrongly_calls_unanswerable(agent):
    def model(**kw):
        if "Split a business question" in kw.get("system", ""):
            return json.dumps({"parts": [], "cannot_answer": [
                {"part": "How many users visited Amazon only in the last 24 hours?", "missing": "user visits"}]})
        raise llm.OllamaError("not needed")
    with patch("llm.complete", side_effect=model):
        result = agent.ask("How many users visited Amazon only in the last 24 hours?", depth="deep")
    assert result.metric == "active_users_by_app" and "Cannot be answered" not in result.answer
    assert "Not answerable" not in result.answer and result.depth == "deep"


def test_deep_single_list_question_is_complete(agent):
    def model(**kw):
        if "Split a business question" in kw.get("system", ""):
            return json.dumps({"parts": ["list all apps"], "cannot_answer": []})
        raise llm.OllamaError("not needed")
    with patch("llm.complete", side_effect=model):
        result = agent.ask("list all apps", depth="deep")
    names = [row[1] for row in result.table["rows"]]
    assert len(names) > 15 or all(n in result.answer for n in names)
    assert all(str(n) in result.answer for n in names) and "the full table is attached" not in result.answer


def test_synthesis_sees_every_row(agent):
    seen = {}

    def model(**kw):
        system = kw.get("system", "")
        if "Split a business question" in system:
            return json.dumps({"parts": ["list all apps", "purchase share by app"], "cannot_answer": []})
        if "final answer" in system:
            seen["evidence"] = kw.get("user", "")
            return "Here are the apps."
        raise llm.OllamaError("not needed")
    with patch("llm.complete", side_effect=model):
        result = agent.ask("list all apps and their purchase share", depth="deep")
    last_app = result.parts[0]["table"]["rows"][-1][1]
    assert "Complete result" in seen["evidence"] and str(last_app) in seen["evidence"]


def test_deep_with_model_is_grounded(agent):
    def model(**kw):
        system = kw.get("system", "")
        if "Split a business question" in system:
            return json.dumps({"parts": ["purchase share by app", "conversion rate by app within 24h"],
                               "cannot_answer": [{"part": "share over 4 weeks", "missing": "only 6 days of data"}]})
        if "final answer" in system:
            return "Meesho leads purchases with 12345 orders."                          # invented -> rejected
        raise llm.OllamaError("not needed")
    with patch("llm.complete", side_effect=model):
        result = agent.ask("Purchase share and conversion by app, and the 4-week trend?", depth="deep")
    assert result.depth == "deep" and len(result.parts) == 2
    assert "12345" not in result.answer and "only 6 days of data" in result.answer


def test_time_limit_and_stop(agent):
    def hang(**kw):
        time.sleep(min(kw.get("timeout") or 30, 30))
        raise llm.OllamaError("timeout")
    started = time.monotonic()
    with patch("llm.complete", side_effect=hang):
        result = agent.ask("purchase share by app?\nconversion by app?", depth="deep", max_seconds=4)
    assert time.monotonic() - started < 5.5 and result.timed_out
    assert "Purchase share by app" in result.answer                                    # rules still answered
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    started = time.monotonic()
    with patch("llm.complete", side_effect=hang):
        agent.ask("purchase share by app?\nconversion by app?", depth="deep", cancel=stop)
    assert time.monotonic() - started < 2


# --------------------------------------------------------------------------- API
def test_api_catalog_chat_and_stream(snap, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("ANALYST_SOURCE", "snapshot")
    monkeypatch.setenv("ANALYST_SNAPSHOT_DIR", str(snap))
    from analyst import api
    api._state["agent"] = None
    client = TestClient(api.app)
    catalog = client.get("/api/catalog").json()
    assert "journey_window" in {t["name"] for t in catalog["tables"]} and catalog["home_app"] == "Amazon"
    assert "analytics_token" not in json.dumps(catalog)
    with patch("llm.complete", side_effect=offline):
        body = client.post("/api/chat", json={"message": "purchase share by app"}).json()
        assert body["metric"] == "purchase_share_by_app" and body["table"]["rows"]
        events = []
        with client.stream("POST", "/api/chat/stream", json={"message": "users by city", "depth": "fast"}) as s:
            for line in s.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:]))
    assert events[0]["type"] == "start" and events[-1]["type"] == "final"
    assert events[-1]["metric"] == "users_by_attribute"
