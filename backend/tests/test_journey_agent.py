"""Tests for the isolated journey agent.

Ground truth is computed straight from the raw JSON with plain Python, never
through JourneyStore, so these tests catch wrong SQL - not just crashes.
Run:  python -m pytest tests/test_journey_agent.py -q
"""

import json
import re
import tempfile
import unittest
from collections import Counter, defaultdict
from pathlib import Path
from unittest.mock import patch

import llm
from journey_agent.agent import JourneyAgent
from journey_agent.context import AppMatcher, extract_user_ids, resolve_references
from journey_agent.store import JourneyDataError, JourneyStore

DATA = Path(__file__).resolve().parents[1] / "data" / "cross-app-journey"
JSON_FILE = DATA / "data-1790154732687.json"
CSV_FILE = DATA / "data-1790154732687.csv"


def _offline(**_):
    raise llm.OllamaError("offline in tests")


class Truth:
    """Independent ground truth from the raw file."""

    def __init__(self, path: Path):
        self.records = json.loads(path.read_text(encoding="utf-8"))
        self.app_searches = Counter()
        self.app_journeys = Counter()
        self.app_purchased = Counter()
        self.user_journeys = Counter()
        self.user_apps = defaultdict(Counter)
        self.user_app_journeys = defaultdict(Counter)
        for r in self.records:
            self.user_journeys[r["user_assoc_id"]] += 1
            for name, count in re.findall(r"([^,(]+?)\s*\((\d+)\)", r["apps_summary"] or ""):
                name = name.strip()
                self.app_searches[name] += int(count)
                self.app_journeys[name] += 1
                self.app_purchased[name] += r["close_reason"] == "purchased"
                self.user_apps[r["user_assoc_id"]][name] += int(count)
                self.user_app_journeys[r["user_assoc_id"]][name] += 1

    def count(self, **where) -> int:
        return sum(all(r.get(k) == v for k, v in where.items()) for r in self.records)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = JourneyStore(JSON_FILE)
        cls.truth = Truth(JSON_FILE)
        cls.agent = JourneyAgent(cls.store)

    def ask(self, question, history=None):
        with patch("llm.complete", side_effect=_offline) as mocked:
            result = self.agent.ask(question, history or [])
        return result, mocked


# --------------------------------------------------------------------------- #
class StoreTests(Base):
    def test_totals_match_raw_file(self):
        s = self.store.summary()
        t = self.truth
        self.assertEqual(s["journeys"], len(t.records))
        self.assertEqual(s["users"], len(t.user_journeys))
        self.assertEqual(s["events"], sum(r["total_events"] for r in t.records))
        self.assertEqual(s["purchased"], t.count(close_reason="purchased"))
        self.assertEqual(s["timed_out"], t.count(close_reason="timeout"))
        self.assertEqual(s["open_journeys"], t.count(status="open"))
        self.assertEqual(s["data_quality"]["records_skipped"], 0)

    def test_csv_and_json_load_identically(self):
        csv_store = JourneyStore(CSV_FILE)
        try:
            a, b = self.store.summary(), csv_store.summary()
            for key in ("journeys", "users", "events", "cross_app", "purchased", "apps"):
                self.assertEqual(a[key], b[key], key)
        finally:
            csv_store.close()

    def test_chat_suggestions_are_generated_from_loaded_data(self):
        summary = self.store.summary()
        groups = {group["key"]: group for group in summary["suggestion_groups"]}
        self.assertIn("search_interests", groups)
        self.assertIn("app_usage", groups)
        self.assertIn("record_details", groups)
        self.assertIn("data_quality", groups)

        questions = "\n".join(q for group in groups.values() for q in group["questions"])
        self.assertIn(summary["last_date"], questions)
        self.assertTrue(any(app in questions for app in summary["apps"]))
        suggested_id = re.search(r"Show journey (J\d+)", questions).group(1)
        self.assertTrue(self.store.journey_exists(suggested_id))

    def test_sandbox_blocks_other_tables_even_via_comma_join(self):
        for sql in ("SELECT name FROM journey, sqlite_master",
                    "SELECT COUNT(*) FROM journey; DROP TABLE journey",
                    "DELETE FROM journey",
                    "SELECT load_extension('x')"):
            with self.assertRaises(JourneyDataError, msg=sql):
                self.store.run_untrusted_sql(sql)

    def test_sandbox_allows_cte_and_hides_hash(self):
        r = self.store.run_untrusted_sql(
            "WITH u AS (SELECT user_assoc_id FROM journey) SELECT COUNT(DISTINCT user_assoc_id) FROM u")
        self.assertEqual(r.scalar(), len(self.truth.user_journeys))
        r = self.store.run_untrusted_sql("SELECT journey_hash FROM journey LIMIT 1")
        self.assertIsNone(r.scalar())

    def test_runaway_query_is_stopped(self):
        with self.assertRaises(JourneyDataError):
            self.store.run_untrusted_sql("SELECT COUNT(*) FROM journey a, journey b, journey c")

    def test_partial_results_are_labelled(self):
        text = self.store.run_untrusted_sql("SELECT journey_id FROM journey", max_rows=5).to_text()
        self.assertIn("PARTIAL", text)
        self.assertIn(str(len(self.truth.records)), text)

    def test_bad_records_are_quarantined_not_fatal(self):
        good = dict(self.truth.records[0])
        bad_rows = [good, {"no": "id"}, dict(good), "not an object",
                    {**good, "journey_id": "J999999", "apps_summary": "Meesho (5)", "total_events": 2}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps(bad_rows))
            store = JourneyStore(path, strict=False)
            q = store.summary()["data_quality"]
            self.assertEqual(store.record_count, 2)
            self.assertEqual(q["records_skipped"], 3)
            self.assertGreaterEqual(q["warnings"], 1)
            store.close()
            with self.assertRaises(JourneyDataError):
                JourneyStore(path, strict=True)


# --------------------------------------------------------------------------- #
class ContextTests(unittest.TestCase):
    apps = AppMatcher(["Ajio", "Amazon", "Amazon Now", "Flipkart", "Flipkart Minutes",
                       "Meesho", "Swiggy", "Swiggy Instamart"])

    def test_user_ids_are_not_confused_by_list_numbers(self):
        text = "Journeys by user:\n1. User 5330: 36 journeys\n2. User 4598: 31 journeys"
        self.assertEqual(extract_user_ids(text), [5330, 4598])

    def test_longest_app_name_wins_and_typos_match(self):
        self.assertEqual(self.apps.names("amazon now vs amazon"), ["Amazon Now", "Amazon"])
        self.assertEqual(self.apps.names("instamart"), ["Swiggy Instamart"])
        self.assertEqual(self.apps.names("mesho"), ["Meesho"])
        self.assertEqual(self.apps.names("average minutes now"), [])

    def test_this_user_uses_most_recent_mention(self):
        history = [{"role": "user", "content": "show user 63"},
                   {"role": "assistant", "content": "User 63 made 4 journeys."},
                   {"role": "user", "content": "which user has most journeys"},
                   {"role": "assistant", "content": "User 5330 has the most journeys."}]
        res = resolve_references("apps of this user", history, self.apps)
        self.assertEqual(res.user_ids, [5330])

    def test_list_reference_needs_ordinal_or_asks(self):
        history = [{"role": "assistant", "content": "1. User 5330: 36\n2. User 4598: 31"}]
        self.assertIsNotNone(resolve_references("details of this user", history, self.apps).ambiguous)
        self.assertEqual(resolve_references("details of the second user", history,
                                            self.apps).user_ids, [4598])
        self.assertEqual(resolve_references("apps of the top user", history,
                                            self.apps).user_ids, [5330])


# --------------------------------------------------------------------------- #
class ScreenshotRegressionTests(Base):
    """The exact conversation that produced wrong answers before the rewrite."""

    def test_conversation(self):
        t = self.truth
        history = []

        def turn(question):
            result, mocked = self.ask(question, history)
            mocked.assert_not_called()          # answered without the language model
            history.extend([{"role": "user", "content": question},
                            {"role": "assistant", "content": result.answer}])
            return result.answer

        top = turn("Which users have the most journeys?")
        leader, leader_count = t.user_journeys.most_common(1)[0]
        self.assertIn(f"User {leader}: {leader_count} journeys", top)
        self.assertEqual(len(re.findall(r"^\d+\. User", top, re.M)), 10)   # top 10 only
        self.assertIn(f"Showing 10 of {len(t.user_journeys)}", top)

        apps = t.user_apps[leader]
        top_app, top_searches = apps.most_common(1)[0]
        answer = turn(f"ok User {leader} , app name")
        # Before: "Meesho ... with 35 search events" (35 was the journey count).
        self.assertIn(f"{top_app}: {top_searches} searches in "
                      f"{t.user_app_journeys[leader][top_app]} journeys", answer)

        answer = turn("all app name of this user")
        # Before: all 12 apps in the file.
        self.assertIn(f"User {leader} searched on {len(apps)} apps", answer)
        for app in t.app_searches:
            (self.assertIn if app in apps else self.assertNotIn)(f"{app}:", answer)

        answer = turn("of particular user")
        # Before: "Insufficient evidence to answer the question."
        self.assertIn(f"User {leader} searched on {len(apps)} apps", answer)

    def test_follow_up_switches_user_and_adds_filter(self):
        t = self.truth
        history = [{"role": "user", "content": "all apps of user 5330"},
                   {"role": "assistant", "content": "User 5330 searched on 3 apps"}]
        answer, _ = self.ask("what about user 4598?", history)
        self.assertIn(f"User 4598 searched on {len(t.user_apps[4598])} apps", answer.answer)
        history += [{"role": "user", "content": "what about user 4598?"},
                    {"role": "assistant", "content": answer.answer}]
        answer, _ = self.ask("and purchased only", history)
        expected = sum(1 for r in t.records
                       if r["user_assoc_id"] == 4598 and r["close_reason"] == "purchased")
        self.assertEqual(answer.plan["outcome"], "purchased")
        self.assertIn(f"{expected} journey", answer.answer)


# --------------------------------------------------------------------------- #
class GoldenQuestionTests(Base):
    """Common questions answered deterministically, checked against raw data."""

    def check(self, question, *expected, route="rules"):
        result, mocked = self.ask(question)
        mocked.assert_not_called()
        self.assertEqual(result.route, route, question)
        for text in expected:
            self.assertIn(text, result.answer, question)
        return result

    def test_purchase_count(self):
        n = self.truth.count(close_reason="purchased")
        self.check("How many journeys ended in purchase?", f"{n} journeys")

    def test_app_with_most_searches(self):
        app, n = self.truth.app_searches.most_common(1)[0]
        self.check("Which app has the most search events?", f"{app} has the most searches: {n:,} searches")

    def test_every_app_listed(self):
        r = self.check("Give me all app list", f"The file has {len(self.truth.app_searches)} apps")
        for app, n in self.truth.app_searches.items():
            self.assertIn(f"{app}: {n:,} searches", r.answer)

    def test_app_filtered_counts(self):
        n = self.truth.app_journeys["Meesho"]
        self.check("how many journeys on meesho", f"{n:,} journeys on Meesho")
        # Flipkart vs Flipkart Minutes must be separate apps.
        exact = sum(1 for r in self.truth.records if r["close_reason"] == "timeout" and
                    any(n.strip() == "Flipkart" for n, _ in
                        re.findall(r"([^,(]+?)\s*\((\d+)\)", r["apps_summary"])))
        self.check("how many timed out journeys on flipkart", f"{exact} journeys on Flipkart")

    def test_purchase_rate_per_app(self):
        t = self.truth
        r = self.check("purchase rate per app")
        for app in t.app_journeys:
            rate = round(100.0 * t.app_purchased[app] / t.app_journeys[app], 1)
            rate_text = f"{rate:g}" if rate != int(rate) else f"{int(rate)}"
            self.assertIn(f"{app}: {rate_text}% purchased ({t.app_purchased[app]} of "
                          f"{t.app_journeys[app]:,} journeys)", r.answer)

    def test_journey_detail(self):
        rec = next(r for r in self.truth.records if r["journey_id"] == "J000002")
        self.check("Show me journey J000002", "Journey J000002",
                   f"user {rec['user_assoc_id']}", f"{rec['total_events']} searches")

    def test_unknown_ids_are_reported(self):
        self.check("user 999999", "does not appear", route="not_found")
        self.check("show journey J999999", "no journey J999999", route="not_found")

    def test_unsupported_fields_are_refused_honestly(self):
        self.check("what price did user 5330 pay", "Not available from the current data",
                   route="data_gap")

    def test_code_and_random_content_requests_are_refused_without_calling_model(self):
        self.check("write Python code for factorial", "I can’t generate general content",
                   route="out_of_scope")
        self.check("tell me a joke", "only able to answer questions about the available data",
                   route="out_of_scope")

    def test_user_profile(self):
        uid = 5330
        n = self.truth.user_journeys[uid]
        self.check(f"complete details of user {uid}", f"User {uid} made {n} journeys")

    def test_data_quality_report_is_built_in(self):
        self.check("Show the journey data-quality report.",
                   "Data-quality report:", "records were loaded", "were skipped", route="quality")


# --------------------------------------------------------------------------- #
class TieAndSimpleModeTests(Base):
    """Second screenshot: 'which user have min journey?' with Simple meaning on."""

    def test_min_journey_tie_reports_true_size_and_ids(self):
        t = self.truth
        low = min(t.user_journeys.values())
        tied = sorted(u for u, n in t.user_journeys.items() if n == low)
        history = []
        for question in ("which user have min journey?", "ok give user id"):
            result, mocked = self.ask(question, history)
            mocked.assert_not_called()
            self.assertIn(f"{len(tied)} users are tied for the fewest journeys: {low} journey each",
                          result.answer)
            self.assertNotIn("10 searches", result.answer)       # no leaked per-user figure
            ids_in_answer = {int(x) for x in re.findall(r"\d+", result.answer.split("):", 1)[1])}
            self.assertTrue(ids_in_answer <= set(tied))
            self.assertEqual(len(result.table["rows"]), len(tied))
            history += [{"role": "user", "content": question},
                        {"role": "assistant", "content": result.answer}]

    def test_least_with_top_n_sorts_ascending(self):
        result, _ = self.ask("top 5 users with least journeys")
        self.assertEqual(result.plan["order"], "asc")

    def test_simple_mode_keeps_facts_and_rejects_invented_numbers(self):
        question = "Explain as a real-life story. Original question: which user have min journey?"
        with patch("llm.complete", return_value="Wow, 10 users each had 1 journey and 10 searches!"):
            bad = self.agent.ask(question)
        self.assertNotIn("Wow", bad.answer)
        self.assertIn("users are tied", bad.answer)
        good_text = "Many shoppers came only once and did not come back for another product hunt."
        with patch("llm.complete", return_value=good_text):
            good = self.agent.ask(question)
        self.assertTrue(good.answer.startswith("In simple words: " + good_text))
        self.assertIn("users are tied", good.answer)               # facts still underneath

    def test_journey_duration_is_human_readable(self):
        result, _ = self.ask("Show me journey J000002")
        self.assertIn("over 50 seconds", result.answer)


# --------------------------------------------------------------------------- #
class PurchaseShareTests(Base):
    """Business question: 'Amazon purchase share, top 2 apps ahead of us'."""

    def test_share_of_all_purchases_and_apps_ahead(self):
        t = self.truth
        total = t.count(close_reason="purchased")
        ranked = t.app_purchased.most_common()
        amazon = t.app_purchased["Amazon"]
        pos = [a for a, _ in ranked].index("Amazon")
        for question in ("What's Amazon's overall purchase share right now, and who are the "
                         "top two apps ahead of us?", "what is our purchase share"):
            result, mocked = self.ask(question)
            mocked.assert_not_called()
            self.assertIn(f"Amazon has {100 * amazon / total:.1f}% of all purchases "
                          f"({amazon} of {total} journeys)", result.answer, question)
            for app, n in ranked[:pos]:
                self.assertIn(f"{app} {100 * n / total:.1f}% ({n})", result.answer)

    def test_purchase_share_is_not_conversion_rate(self):
        result, _ = self.ask("purchase share by app")
        self.assertIn("Share of all purchases", result.answer)
        self.assertNotIn("% purchased (", result.answer)


# --------------------------------------------------------------------------- #
class StartAppTests(Base):
    """'Of all journeys that start on Amazon, what share end up buying on Amazon vs. elsewhere?'"""

    def test_start_funnel_matches_raw_data(self):
        votes = defaultdict(Counter)
        for r in self.truth.records:
            if r["total_apps"] == 1:
                votes[r["first_search_app_id"]][r["apps_summary"].split(" (")[0]] += 1
        amazon_id = next(i for i, c in votes.items() if c.most_common(1)[0][0] == "Amazon")
        start = [r for r in self.truth.records if r["first_search_app_id"] == amazon_id]
        single_bought = sum(r["close_reason"] == "purchased" and r["total_apps"] == 1 for r in start)
        multi_bought = sum(r["close_reason"] == "purchased" and r["total_apps"] > 1 for r in start)
        moved = sum(r["total_apps"] > 1 for r in start)
        result, mocked = self.ask("Of all journeys that start on Amazon, what share end up "
                                  "buying on Amazon vs. somewhere else?")
        mocked.assert_not_called()
        self.assertEqual(result.plan["intent"], "start_funnel")
        self.assertIn(f"{len(start)} journeys started on Amazon.", result.answer)
        self.assertIn(f"Bought, using only Amazon: {single_bought} ", result.answer)
        self.assertIn(f"Bought after also using another app: {multi_bought} ", result.answer)
        self.assertIn(f"Moved on to at least one other app: {moved} ", result.answer)


def _fake_model(bad_first_draft=False, include_gap=True):
    state = {"drafts": 0}

    def complete(**kw):
        system = kw.get("system", "")
        if "break a business question" in system:
            return json.dumps({"parts": ["Amazon purchase share", "journeys that start on Amazon"],
                               "cannot_answer": ([{"part": "Meesho share over 4 weeks",
                                                    "missing": "history beyond 6 days"}]
                                                 if include_gap else [])})
        if "check whether data answers" in system:
            return json.dumps({"complete": True})
        if "Write the final answer" in system:
            state["drafts"] += 1
            if bad_first_draft and state["drafts"] == 1:
                return "Amazon holds 19.9% of purchases."
            return "Amazon has 12.5% of all purchases; 406 journeys started on Amazon."
        raise llm.OllamaError("unexpected call")
    return complete


class DeepThinkingTests(Base):
    QUESTION = ("What's Amazon's purchase share? Also, of journeys that start on Amazon, how many "
                "buy elsewhere? Has Meesho's share grown over the past 4 weeks?")

    def test_auto_keeps_simple_questions_fast(self):
        result, mocked = self.ask("purchase share by app")
        mocked.assert_not_called()
        self.assertEqual(result.depth, "fast")

    def test_deep_mode_keeps_one_query_questions_concise(self):
        with patch("llm.complete", side_effect=_offline) as mocked:
            result = self.agent.ask("Show journey counts by day.", depth="deep")
        mocked.assert_not_called()
        self.assertEqual(result.depth, "deep")
        self.assertNotIn("Could not answer reliably", result.answer)
        self.assertNotIn("Checked:", result.answer)

    def test_data_gap_is_short_and_suggests_available_questions(self):
        with patch("llm.complete", side_effect=_fake_model()) as mocked:
            result = self.agent.ask(self.QUESTION)
        self.assertEqual(result.depth, "deep")
        self.assertEqual(result.route, "data_gap")
        self.assertFalse(result.verified)
        self.assertTrue(result.answer.startswith("Not available from the current data."))
        self.assertNotIn("Checked:", result.answer)
        self.assertGreaterEqual(len(result.suggested_questions), 2)
        stages = [t["stage"] for t in result.thinking]
        self.assertIn("plan", stages)
        self.assertNotIn("query", stages)
        mocked.assert_not_called()

    def test_invented_number_in_final_draft_is_rejected(self):
        available_question = ("What's Amazon's purchase share? Also, of journeys that start on Amazon, "
                              "how many buy elsewhere?")
        with patch("llm.complete", side_effect=_fake_model(bad_first_draft=True, include_gap=False)):
            result = self.agent.ask(available_question, depth="deep")
        self.assertNotIn("19.9", result.answer)
        self.assertTrue(any("Draft rejected" in t["text"] for t in result.thinking))

    def test_hard_time_limit_and_stop_button(self):
        import threading
        import time

        def hang(**kw):
            time.sleep(min(kw.get("timeout") or 30, 30))
            raise llm.OllamaError("timed out")

        question = "Amazon purchase share?\nJourneys that start on Amazon?"
        started = time.monotonic()
        with patch("llm.complete", side_effect=hang):
            result = self.agent.ask(question, depth="deep", max_seconds=4)
        self.assertLess(time.monotonic() - started, 5.5)
        self.assertTrue(result.timed_out)
        self.assertIn("Amazon has 12.5% of all purchases", result.answer)   # rules still answer

        stop = threading.Event()
        threading.Timer(0.5, stop.set).start()
        started = time.monotonic()
        with patch("llm.complete", side_effect=hang):
            result = self.agent.ask(question, depth="deep", max_seconds=60, cancel=stop)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertIn("stopped by the user", result.answer)

    def test_budget_is_capped_at_sixty_seconds(self):
        from journey_agent.thinking import Budget
        self.assertEqual(Budget(600).seconds, 60)


# --------------------------------------------------------------------------- #
class LLMPathTests(Base):
    def test_unclear_question_uses_llm_plan_and_validates_it(self):
        plan = json.dumps({"intent": "aggregate", "metric": "searches", "group_by": "user",
                           "apps": ["meesho"], "order": "desc", "limit": 3})
        with patch("llm.complete", return_value=plan) as mocked:
            result = self.agent.ask("meesho power shoppers leaderboard")
        mocked.assert_called_once()
        self.assertEqual(result.route, "llm_plan")
        self.assertEqual(result.plan["apps"], ["Meesho"])          # normalised name
        best_user = max(self.truth.user_apps, key=lambda u: self.truth.user_apps[u]["Meesho"])
        self.assertIn(f"User {best_user}", result.answer)

    def test_agent_answer_with_invented_numbers_is_rejected(self):
        replies = iter([
            json.dumps({"intent": "agent"}),
            json.dumps({"thought": "count", "tool": "run_sql",
                        "sql": "SELECT COUNT(*) AS n FROM journey WHERE total_apps = 3"}),
            json.dumps({"thought": "done", "tool": "final_answer", "answer": "There are 987654 such journeys."}),
            json.dumps({"thought": "fix", "tool": "final_answer", "answer": "__REAL__"}),
        ])
        real = sum(1 for r in self.truth.records if r["total_apps"] == 3)

        def fake(**_):
            value = next(replies)
            return value.replace("__REAL__", f"There are {real} such journeys.")

        with patch("llm.complete", side_effect=fake):
            result = self.agent.ask("journeys whose app count equals exactly three, hmm")
        self.assertEqual(result.route, "agent")
        self.assertEqual(result.answer, f"There are {real} such journeys.")
        rejected = [s for s in result.steps if s.error and "not in the tool results" in s.observation]
        self.assertEqual(len(rejected), 1)

    def test_simple_style_rewrite_cannot_change_numbers(self):
        with patch("llm.complete", return_value="Shoppers bought things 99999 times!"):
            result = self.agent.ask("Explain simply. Original question: How many journeys ended in purchase?")
        self.assertIn(str(self.truth.count(close_reason="purchased")), result.answer)
        self.assertNotIn("99999", result.answer)


# --------------------------------------------------------------------------- #
class ApiTests(unittest.TestCase):
    def test_upload_chat_and_long_history(self):
        from fastapi.testclient import TestClient
        from journey_agent import api

        client = TestClient(api.app)
        with JSON_FILE.open("rb") as handle:
            r = client.post("/api/dataset", files={"file": (JSON_FILE.name, handle, "application/json")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["journeys"], len(json.loads(JSON_FILE.read_text())))

        long_answer = "x" * 9000              # used to trigger a 422 on the next request
        history = [{"role": "user", "content": "complete details of user 5330"},
                   {"role": "assistant", "content": long_answer}] * 20
        with patch("llm.complete", side_effect=_offline):
            r = client.post("/api/chat", json={"message": "all apps of this user", "history": history})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("User 5330 searched on", body["answer"])
        self.assertEqual(body["route"], "rules")
        self.assertIn("journey_app", body["sql"])

        events = []
        with patch("llm.complete", side_effect=_offline):
            with client.stream("POST", "/api/chat/stream",
                               json={"message": "Amazon purchase share?\nJourneys that start on Amazon?",
                                     "depth": "deep", "max_seconds": 10}) as stream:
                for line in stream.iter_lines():
                    if line.startswith("data:"):
                        events.append(json.loads(line[5:]))
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds[0], "start")
        self.assertIn("thinking", kinds)
        self.assertEqual(kinds[-1], "final")
        self.assertEqual(events[-1]["depth"], "deep")
        r = client.post("/api/chat", json={"message": "hi", "max_seconds": 120})
        self.assertEqual(r.status_code, 422)            # more than 60 s is refused


if __name__ == "__main__":
    unittest.main()
