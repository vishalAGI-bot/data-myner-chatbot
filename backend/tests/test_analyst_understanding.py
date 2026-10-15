"""With OpenAI selected, OpenAI's understand_question tool reads every question; no rules interpret it."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import config  # noqa: E402
import llm  # noqa: E402
from analyst import report as report_lib  # noqa: E402
from analyst import understanding as understanding_lib  # noqa: E402
from test_analyst_report import thin_agent  # noqa: E402
from test_analyst_visits import make_agent  # noqa: E402

REAL_CALL_TOOL = llm.call_tool          # captured before the shared stand-in (conftest.py) replaces it


def ask(agent, message, reading, depth="auto", complete=None, state=None):
    def no_completion(**kwargs):
        # Only optional wording may be asked for (an answer's summary sentence, a brief's summary); it is
        # skipped when the model says no.
        assert kwargs.get("system", "").startswith(("Answer the business question", "You write the executive")), \
            kwargs.get("system", "")[:60]
        raise llm.LLMError("not needed in this test")
    with patch.object(config, "LLM_PROVIDER", "openai"), patch("llm.call_tool", return_value=reading) as tool, \
            patch("llm.complete", side_effect=complete or no_completion):
        result = agent.ask(message, depth=depth, max_seconds=60, state=state)
    return result, tool


# --------------------------------------------------------------------------- the tool call itself

def test_the_understanding_is_a_forced_openai_tool_call():
    seen = {}

    def create(**request):
        seen.update(request)
        return SimpleNamespace(status="completed", incomplete_details=None, output=[SimpleNamespace(
            type="function_call", name="understand_question",
            arguments=json.dumps({"rephrased_question": "Q", "intent": "analysis"}))])

    with patch.object(config, "LLM_PROVIDER", "openai"), \
            patch("llm._openai_client", return_value=SimpleNamespace(responses=SimpleNamespace(create=create))):
        args = REAL_CALL_TOOL("sys", "user", understanding_lib.TOOL_NAME, understanding_lib.TOOL_DESCRIPTION,
                             understanding_lib.PARAMETERS)
    assert args == {"rephrased_question": "Q", "intent": "analysis"}
    assert seen["tool_choice"] == {"type": "function", "name": "understand_question"}
    assert seen["tools"][0]["parameters"] is understanding_lib.PARAMETERS and seen["store"] is False


def test_a_reply_without_the_tool_call_is_an_error():
    create = lambda **_: SimpleNamespace(status="completed", incomplete_details=None,  # noqa: E731
                                         output=[SimpleNamespace(type="message")])
    with patch.object(config, "LLM_PROVIDER", "openai"), \
            patch("llm._openai_client", return_value=SimpleNamespace(responses=SimpleNamespace(create=create))), \
            pytest.raises(llm.LLMError, match="did not call"):
        REAL_CALL_TOOL("s", "u", "understand_question", "d", {"type": "object"})


# --------------------------------------------------------------------------- no rules interpret the question

def test_openai_decides_who_we_are_and_what_is_asked_not_the_keyword_rules():
    message = "am flipkart's insights lead, how many total user visit?"
    reading = {"rephrased_question": "How many visits did Meesho users make across all loaded data?",
               "intent": "analysis", "focus_app": "Meesho", "metric": "visits_overview"}
    result, tool = ask(make_agent(), message, reading)
    assert result.metric == "visits_overview" and result.verified, result.answer
    assert result.resolved == reading["rephrased_question"]
    steps = [s["text"] for s in result.thinking]
    assert "OpenAI rephrased the question: How many visits did Meesho users make across all loaded data?" in steps
    assert not any("Flipkart" in s for s in steps), "the persona regex would have said Flipkart"
    assert tool.call_args.kwargs["name"] == "understand_question"
    assert "Latest message: am flipkart's insights lead" in tool.call_args.kwargs["user"]
    live = tool.call_args.kwargs["system"]
    assert "the data covers 2026-09-06 to 2026-09-30" in live and "Meesho" in live, "live data facts in the prompt"


def test_a_brief_is_whatever_openai_reads_as_one_even_without_report_words():
    message = "give the meesho teams the lowdown on yesterday, cut every way you can"
    assert not report_lib.looks_like_report(message), "no keyword rule would have recognised this"
    reading = {"rephrased_question": "Executive summary of Meesho's performance on 2026-10-08 vs competitors by "
                                     "gender and age group", "intent": "brief", "focus_app": "Meesho",
               "date_from": "2026-10-08", "date_to": "2026-10-08", "breakdowns": ["gender", "age_group"],
               "min_users": 30}
    result, _ = ask(thin_agent(), message, reading)
    assert result.answer.startswith("**Meesho: executive summary, 2026-10-08**"), result.answer
    assert [p["question"] for p in result.parts] == ["Overall", "Gender", "Age group"]


def test_when_openai_cannot_read_the_question_there_is_no_rule_fallback():
    with patch.object(config, "LLM_PROVIDER", "openai"), \
            patch("llm.call_tool", side_effect=llm.LLMError("OpenAI request failed: APIConnectionError")):
        result = make_agent().ask("how many total user visit?", depth="auto", max_seconds=60)
    assert result.route == "error" and result.metric is None
    assert "could not read this question just now" in result.answer and "OpenAI" not in result.answer
    assert any("APIConnectionError" in s["text"] for s in result.thinking)


def test_openai_intents_clarify_and_out_of_coverage():
    result, _ = ask(make_agent(), "visits?", {"rephrased_question": "visits?", "intent": "clarify",
                                               "message": "Total visits, or visits per app?"})
    assert result.route == "clarify" and result.clarifications[0]["question"] == "Total visits, or visits per app?"
    result, _ = ask(make_agent(), "visits last year", {"rephrased_question": "Visits in 2025", "intent": "analysis",
                                                       "date_from": "2025-01-01", "date_to": "2025-12-31"})
    assert result.route == "data_gap" and "2026-09-06 to 2026-09-30" in result.answer


def test_a_day_wise_request_never_gets_a_whole_period_analysis():
    """The shape asked for comes from OpenAI's reading (time_grain), not from matching 'daywise' in the text."""
    def write_sql(system="", **_):
        if system.startswith("Answer the business question"):
            raise llm.LLMError("not needed in this test")
        assert system.startswith("You write ONE PostgreSQL"), system[:60]
        return json.dumps({"sql": "SELECT CAST(created_at AS date) AS day, COUNT(*) AS visits FROM session_master "
                                  "GROUP BY 1 ORDER BY 1"})

    reading = {"rephrased_question": "Number of visits per day across all loaded data", "intent": "analysis",
               "metric": "visits_overview", "time_grain": "day", "tables": ["session_master"]}
    result, _ = ask(make_agent(), "visits each day", reading, complete=write_sql)
    assert result.route == "sql" and "AS day" in result.sql and result.table["rows"], result.answer
    assert any("does not return the shape asked for" in s["text"] for s in result.thinking)


# --------------------------------------------------------------------------- long requests: clarify, answer, follow up

VISITS = "How many visits are there in total across all apps and all loaded data?"
UNCLEAR = {"question": "How did the festive campaign perform?", "clear": False}
ASK_BACK = {"part": 2, "question": "Which campaign do you mean?", "why": "The data has no campaign field, so it "
            "decides which period and apps to compare.", "options": ["Big Billion Days", "Diwali week", "Other"]}


def openai(plan=None, follow_ups=None, calls=None):
    """OpenAI completions after the understanding: per-part plans, answer checks and follow-up questions."""
    def complete(system="", user="", schema=None, **_):
        if calls is not None:
            calls.append(system[:40])
        if system.startswith("You route a business question"):
            return json.dumps((plan or {}).get(user.split("Question: ")[-1].split("\n")[0], {
                "action": "metric", "metric": "visits_overview"}))
        if system.startswith("You write ONE PostgreSQL"):
            return json.dumps({"sql": "SELECT CAST(created_at AS date) AS day, COUNT(*) AS visits "
                                      "FROM session_master GROUP BY 1 ORDER BY 1"})
        if system.startswith("Verify an analytics answer"):
            return json.dumps({"valid": True, "issues": []})
        if system.startswith("Suggest 2 or 3 follow-up questions"):
            return json.dumps({"questions": follow_ups or []})
        if system.startswith("You are the lead analyst"):
            raise AssertionError("the parts OpenAI found must be answered as they are, without a second plan")
        raise llm.LLMError("not needed in this test")
    return complete


def test_a_blocking_ambiguity_is_asked_first_with_options_and_nothing_runs():
    reading = {"rephrased_question": "How did the festive campaign perform?", "intent": "clarify",
               "goal": "Judge the festive campaign", "parts": [{**UNCLEAR}],
               "clarifications": [{**ASK_BACK, "part": 1}]}
    result, _ = ask(make_agent(), "how did the festive campaign do?", reading, complete=openai())
    assert result.route == "clarify" and result.metric is None and result.table is None
    assert result.clarifications == [{"part": 1, "question": "Which campaign do you mean?", "why": ASK_BACK["why"],
                                      "options": ["Big Billion Days", "Diwali week"]}], "the UI adds 'Other'"
    assert result.request["goal"] == "Judge the festive campaign" and not result.follow_ups


def test_the_clear_parts_are_answered_and_only_the_unclear_one_is_asked():
    reading = {"rephrased_question": f"{VISITS} And how did the festive campaign perform?", "intent": "multi_part",
               "parts": [{"question": VISITS, "clear": True}, UNCLEAR], "clarifications": [ASK_BACK],
               "constraints": ["all apps"], "expected_output": "a number and an assessment"}
    result, _ = ask(make_agent(), "total visits? and how did the festive campaign do?", reading,
                    complete=openai(follow_ups=["Visits by app?"]))
    assert result.metric == "visits_overview" and result.table["rows"], result.answer
    assert [c["question"] for c in result.clarifications] == ["Which campaign do you mean?"]
    assert result.answer.startswith("**Your message has 2 questions:** 1 answered, 1 needs your input.")
    assert "**2. How did the festive campaign perform?**\nNeeds your input" in result.answer
    assert result.follow_ups == [], "no optional suggestions while a needed answer is outstanding"
    assert result.request["parts"] == [VISITS, UNCLEAR["question"]] and result.request["constraints"] == ["all apps"]


def test_every_part_found_in_a_long_request_is_answered():
    per_day = "How many visits were there per day across all loaded data?"
    reading = {"rephrased_question": f"{VISITS} {per_day}", "intent": "multi_part",
               "parts": [{"question": VISITS, "clear": True}, {"question": per_day, "clear": True}]}
    calls: list = []
    result, _ = ask(make_agent(), "total visits and visits per day", reading,
                    complete=openai(plan={per_day: {"action": "sql", "tables": ["session_master"]}}, calls=calls,
                                    follow_ups=["Which app had the most visits?", "Why did visits fall 37%?"]))
    assert [p["question"] for p in result.parts] == [f"1. {VISITS}", f"2. {per_day}"], result.answer
    assert all(p["verified"] for p in result.parts)
    assert result.follow_ups == ["Which app had the most visits?"], "a suggestion implying a figure is dropped"
    assert not any(c.startswith("You are the lead analyst") for c in calls)


def test_follow_up_questions_follow_a_complete_answer():
    reading = {"rephrased_question": VISITS, "intent": "analysis", "metric": "visits_overview",
               "parts": [{"question": VISITS, "clear": True}]}
    result, _ = ask(make_agent(), "how many total user visit?", reading, complete=openai(follow_ups=[
        "What share of the 1,200 visits came from Meesho?", "How did visits change over the last 30 days",
        "Which day had 9,999 visits?"]))
    assert result.follow_ups == ["What share of the 1,200 visits came from Meesho?",
                                 "How did visits change over the last 30 days?"]
    assert not result.clarifications


def test_the_reply_to_a_clarification_is_read_with_what_was_asked():
    reading = {"rephrased_question": "How did the festive campaign perform?", "intent": "clarify",
               "parts": [{**UNCLEAR}], "clarifications": [{**ASK_BACK, "part": 1}]}
    agent = make_agent()
    first, _ = ask(agent, "how did the festive campaign do?", reading, complete=openai())
    second, tool = ask(agent, 'For "Which campaign do you mean?": Big Billion Days',
                       {"rephrased_question": VISITS, "intent": "analysis", "metric": "visits_overview"},
                       complete=openai(), state=first.state)
    context = tool.call_args.kwargs["user"]
    assert "We asked the user: Which campaign do you mean? (options: Big Billion Days | Diwali week)" in context
    assert second.metric == "visits_overview"
