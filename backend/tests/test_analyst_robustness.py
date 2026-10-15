"""Long Deep questions must not end with nothing: cut-off replies, unreadable reviews and the time limit."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import config  # noqa: E402
import llm  # noqa: E402
from journey_agent.thinking import ThinkingTimeout  # noqa: E402
from test_analyst_visits import make_agent  # noqa: E402

SQL = "SELECT CAST(created_at AS date) AS day, COUNT(*) AS visits FROM session_master GROUP BY 1 ORDER BY 1"
PLAN = {"action": "custom_sql", "tables": ["session_master"], "reason": "day grain",
        "interpretation": "Visits are sessions per day."}


# --------------------------------------------------------------------------- OpenAI replies cut off

class FakeResponses:
    def __init__(self, replies):
        self.replies, self.limits = list(replies), []

    def create(self, **request):
        self.limits.append(request["max_output_tokens"])
        status, text = self.replies.pop(0)
        return SimpleNamespace(status=status, output_text=text,
                               incomplete_details=SimpleNamespace(reason="max_output_tokens") if status == "incomplete"
                               else None)


def _client(responses):
    return patch("llm._openai_client", return_value=SimpleNamespace(responses=responses))


def test_a_reply_cut_off_at_the_limit_is_asked_again_with_room():
    responses = FakeResponses([("incomplete", '{"sql": "SELECT a, b FROM'), ("completed", '{"sql": "SELECT 1"}')])
    with _client(responses):
        assert llm._complete_openai("s", "u", max_tokens=700, schema=None, timeout=30) == '{"sql": "SELECT 1"}'
    assert responses.limits == [700, 1400]


def test_a_reply_cut_off_twice_is_an_error_not_broken_json():
    responses = FakeResponses([("incomplete", '{"pla'), ("incomplete", '{"plan": "')])
    with _client(responses), pytest.raises(llm.LLMError, match="cut off"):
        llm._complete_openai("s", "u", max_tokens=700, schema=None, timeout=30)


# --------------------------------------------------------------------------- Deep mode keeps what it has

def _deep(review, verify=None, sql_calls=None):
    def complete(system="", user="", **_):
        if system.startswith("You are the lead analyst"):
            return json.dumps(PLAN)
        if system.startswith("You write ONE PostgreSQL"):
            if sql_calls is not None:
                sql_calls.append(1)
                if len(sql_calls) > 1:
                    raise ThinkingTimeout("the 1 min thinking limit was reached")
            return json.dumps({"sql": SQL})
        if system.startswith("Review a proposed"):
            return review
        if system.startswith("Verify an analytics answer"):
            return verify or json.dumps({"valid": True, "issues": []})
        return "Visits were counted from sessions."
    return complete


def test_running_out_of_time_shows_the_guard_approved_query_flagged():
    """Screenshot: SQL rejected by the reviewer, then the 60 s limit - and the user got nothing at all."""
    blocking = json.dumps({"blocking_issues": ["category mapping relies on partial text matches"], "minor_issues": []})
    with patch.object(config, "LLM_PROVIDER", "openai"), \
            patch("llm.complete", side_effect=_deep(blocking, sql_calls=[])):
        result = make_agent().ask("day wise visits", depth="deep", max_seconds=60)
    assert result.table and result.table["rows"], result.answer
    assert result.answer.startswith("**Check before relying on this.** An automatic check raised a doubt")
    assert any("category mapping relies on partial text matches" in s["text"] for s in result.thinking)
    assert not result.verified
    assert any("guard-approved" in s["text"] for s in result.thinking)


def test_an_unreadable_sql_review_does_not_end_the_analysis():
    with patch.object(config, "LLM_PROVIDER", "openai"), patch("llm.complete", side_effect=_deep("not json {")):
        result = make_agent().ask("day wise visits", depth="deep", max_seconds=60)
    assert result.route == "sql" and result.verified and result.table["rows"], result.answer
    assert any("review could not be read" in s["text"] for s in result.thinking)


def test_an_unreadable_verification_marks_the_answer_unchecked():
    ok = json.dumps({"blocking_issues": [], "minor_issues": []})
    with patch.object(config, "LLM_PROVIDER", "openai"), \
            patch("llm.complete", side_effect=_deep(ok, verify="{oops")):
        result = make_agent().ask("day wise visits", depth="deep", max_seconds=60)
    assert result.table["rows"] and not result.verified
    assert result.answer.startswith("**Check before relying on this.**")
    assert any("unchecked" in s["text"] for s in result.thinking)


def test_an_unusable_plan_says_why():
    def complete(system="", **_):
        return json.dumps({"action": "custom_sql", "tables": ["no_such_table"], "reason": "r", "interpretation": "i"})
    with patch.object(config, "LLM_PROVIDER", "openai"), patch("llm.complete", side_effect=complete):
        result = make_agent().ask("day wise visits", depth="deep", max_seconds=60)
    assert result.route == "error" and "could not work out how to answer" in result.answer
    assert any("did not select tables" in s["text"] for s in result.thinking)
