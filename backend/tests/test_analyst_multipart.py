"""One message, many small questions: every question is answered, or the answer says why not."""

import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import config  # noqa: E402
import llm  # noqa: E402
from analyst import parts as parts_lib  # noqa: E402
from analyst import understanding as understanding_lib  # noqa: E402
from test_analyst_report import thin_agent  # noqa: E402
from test_analyst_visits import make_agent  # noqa: E402

TOTAL = "How many visits are there in total across all apps and all loaded data?"
PER_DAY = "How many visits were there per day across all loaded data?"
DEPENDENT = "How many visits did the app with the most visits in question 1 get?"
RESOLVED = "How many visits are there in total across all apps for the whole period?"
DAY_SQL = "SELECT CAST(created_at AS date) AS day, COUNT(*) AS visits FROM session_master GROUP BY 1 ORDER BY 1"

LONG_MESSAGE_READING = {
    "rephrased_question": "Seven questions about visits, a campaign, city tiers and a poem",
    "intent": "multi_part", "goal": "Understand visits and the campaign",
    "parts": [
        {"question": TOTAL, "status": "answer", "metric": "visits_overview"},
        {"question": PER_DAY, "status": "answer", "tables": ["session_master"], "time_grain": "day"},
        {"question": "How did the festive campaign perform?", "status": "clarify"},
        {"question": "What is the conversion by city tier?", "status": "unsupported",
         "reason": "City tier is not recorded; user_master.tier is a loyalty tier"},
        {"question": "Write me a poem about our sales", "status": "out_of_scope"},
        {"question": DEPENDENT, "status": "answer", "depends_on": [1]},
        {"question": "Split question 4's result by gender", "status": "answer", "depends_on": [4]},
    ],
    "more_questions": 2,
    "clarifications": [{"part": 3, "question": "Which campaign do you mean?", "why": "There is no campaign field.",
                        "options": ["Big Billion Days", "Diwali week"]}],
}


def openai_after_reading(calls: list):
    def complete(system="", user="", **_):
        calls.append(system[:30])
        if system.startswith("You route a business question"):
            return json.dumps({"action": "metric", "metric": "visits_overview"})
        if system.startswith("You write ONE PostgreSQL"):
            return json.dumps({"sql": DAY_SQL})
        if system.startswith("Review a proposed"):
            return json.dumps({"blocking_issues": [], "minor_issues": []})
        if system.startswith("Verify an analytics answer"):
            return json.dumps({"valid": True, "issues": []})
        if system.startswith("Rewrite the user's new message"):
            assert "1,200" in user or "1200" in user, "the earlier question's result is given to the rewrite"
            return json.dumps({"question": RESOLVED, "used_context": True})
        if system.startswith("Answer the business question"):
            return "Visits are counted from app sessions."
        raise llm.LLMError("not needed in this test")
    return complete


def test_a_long_message_with_many_small_questions_answers_or_explains_every_one():
    calls: list = []
    with patch.object(config, "LLM_PROVIDER", "openai"), patch("llm.call_tool", return_value=LONG_MESSAGE_READING), \
            patch("llm.complete", side_effect=openai_after_reading(calls)):
        result = make_agent().ask("…a long message with nine questions…", depth="auto", max_seconds=120)
    answer = result.answer
    assert answer.startswith("**Your message has 9 questions:** 3 answered, 1 needs your input, 1 not in the "
                             "data, 1 outside data analysis, 1 waiting on another question, 2 not read."), answer
    for n, part in enumerate(LONG_MESSAGE_READING["parts"], 1):
        assert f"**{n}. {part['question']}**" in answer, n
    assert answer.index("**1.") < answer.index("**2.") < answer.index("**7."), "questions keep their order"
    assert "Visits are counted from app sessions." in answer, "each answered question opens with a direct answer"
    assert "2026-09-07: visits 2,826" in answer                                    # question 2, custom query
    assert f"_Read as: {RESOLVED}_" in answer                                      # question 6 used question 1
    assert "Not in the data: City tier is not recorded" in answer
    assert "Outside data analysis" in answer
    assert "Not answered: it needs the result of question 4" in answer
    assert "**2 more questions in your message were not read**" in answer
    assert [c["part"] for c in result.clarifications] == [3]
    assert [p["status"] for p in result.parts] == ["answered", "answered", "clarify", "unsupported",
                                                   "out_of_scope", "answered", "blocked"]
    # Question 1's analysis came with the reading, question 2 named its tables: only the rewritten question 6
    # needed a planning call.
    assert sum(c.startswith("You route a business question") for c in calls) == 1
    assert not result.verified and not result.follow_ups


def test_parts_run_in_waves_and_only_earlier_parts_can_be_needed():
    reading = understanding_lib.parse(
        {"rephrased_question": "q", "intent": "multi_part", "parts": [
            {"question": "a", "status": "answer", "depends_on": [2, 1]},          # a later part / itself: ignored
            {"question": "b", "status": "answer", "depends_on": [1]},
            {"question": "c", "status": "answer", "depends_on": [0, 5, 2]},
            {"question": "d", "status": "answer"},
            {"question": "e", "status": "unsupported", "reason": "no field"}]},
        apps=[], metrics={}, tables=set(), date_range=(None, None), fallback_question="q")
    assert [p.depends_on for p in reading.parts] == [[], [1], [2], [], []]
    assert [[p.number for p in wave] for wave in parts_lib.run_order(reading.parts)] == [[1, 4], [2], [3]]


def test_a_message_beyond_the_limit_counts_the_rest():
    many = [{"question": f"Question {i}?", "status": "answer"} for i in range(1, 16)]
    reading = understanding_lib.parse({"rephrased_question": "q", "intent": "multi_part", "parts": many},
                                      apps=[], metrics={}, tables=set(), date_range=(None, None),
                                      fallback_question="q")
    assert len(reading.parts) == understanding_lib.MAX_PARTS and reading.more_questions == 3


def test_one_request_runs_at_most_the_part_limit_at_once():
    agent, lock, active, peak = make_agent(), threading.Lock(), [0], [0]

    def job(i):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return i

    results = agent._parallel([lambda i=i: job(i) for i in range(9)], limit=3)
    assert results == list(range(9)) and peak[0] == 3


def test_a_brief_among_other_questions_runs_with_them():
    reading = {"rephrased_question": "A brief and a poem", "intent": "multi_part", "focus_app": "Flipkart",
               "date_from": "2026-10-08", "date_to": "2026-10-08", "breakdowns": ["gender"], "min_users": 30,
               "parts": [{"question": "Executive summary of Flipkart on 2026-10-08 by gender", "status": "answer",
                          "kind": "brief"},
                         {"question": "Write a poem about it", "status": "out_of_scope"}]}
    with patch.object(config, "LLM_PROVIDER", "openai"), patch("llm.call_tool", return_value=reading), \
            patch("llm.complete", side_effect=llm.LLMError("not needed")):
        result = thin_agent().ask("brief and a poem", depth="auto", max_seconds=60)
    assert "**1. Executive summary of Flipkart on 2026-10-08 by gender**\n**Flipkart: executive summary" \
        in result.answer, result.answer
    assert "**2. Write a poem about it**\nOutside data analysis" in result.answer
    assert "1. Brief · Overall" in [p["question"] for p in result.parts]


def test_a_too_long_message_gets_a_sentence_not_a_schema_error():
    with patch("analyst.settings_store.load_overrides", return_value={}):
        from analyst import api
    from fastapi.testclient import TestClient
    client = TestClient(api.app)
    r = client.post("/api/chat", json={"message": "x" * (api.MAX_MESSAGE_CHARS + 1)})
    assert r.status_code == 422
    assert r.json()["detail"].startswith("Your message is too long (12,000 characters at most).")
    assert api.ChatRequest(message="x" * api.MAX_MESSAGE_CHARS).message
