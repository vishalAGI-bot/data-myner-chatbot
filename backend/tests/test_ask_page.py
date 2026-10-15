"""The /ask page promise: what a user reads never names the model, the provider, tables, columns or SQL.

The working page (/) and /ask get the same answers; technical detail lives in the thinking steps, the SQL and
the table list, which only the working page shows. These scenarios cover every kind of answer and failure.
"""

import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import config  # noqa: E402
import llm  # noqa: E402
from analyst.agent import AnalystAgent  # noqa: E402
from analyst.catalog import Catalog  # noqa: E402
from analyst.settings import Settings  # noqa: E402
from analyst.sources import SnapshotSource  # noqa: E402
from test_analyst_multipart import LONG_MESSAGE_READING, openai_after_reading  # noqa: E402
from test_analyst_report import BRIEF, BRIEF_READING, KNOWLEDGE, _openai_brief, build_snapshot, thin_agent  # noqa
from test_analyst_robustness import _deep  # noqa: E402
from test_analyst_visits import VisitSource, make_agent  # noqa: E402

TECHNICAL = re.compile(
    r"\bOpenAI\b|language model|\bLLM\b|\bGPT\b|\bSQL\b|journey_window|session_master|user_master|app_master|"
    r"category_master|cross_app_journey|\bis_final\b|\bwindow_end\b|\b[a-z]+_[a-z_]+\b", re.I)


def visible(result) -> list[str]:
    """Everything the /ask page shows: answer, "Understood as", each question's card, clarifications,
    follow-up and suggested questions."""
    texts = [result.answer, result.resolved] + list(result.follow_ups) + list(result.suggested_questions)
    for part in result.parts:
        texts += [str(part.get("question") or ""), str(part.get("answer") or ""), str(part.get("label") or "")]
    for c in result.clarifications:
        texts += [c.get("question", ""), c.get("why", "")] + list(c.get("options") or [])
    return texts


def assert_business_language(result):
    leaks = sorted({m.group(0) for text in visible(result) for m in TECHNICAL.finditer(text or "")})
    assert not leaks, f"technical words a user would read: {leaks}\n\n{result.answer}"


@pytest.fixture(scope="module")
def snapshot_agent(tmp_path_factory):
    folder = tmp_path_factory.mktemp("ask") / "public"
    build_snapshot(folder)
    cat = Catalog(KNOWLEDGE, "Amazon")
    src = SnapshotSource(folder, cat.blocked_tables)
    cat.attach(src)
    return AnalystAgent(src, cat, Settings(source="snapshot"))


def ask(agent, message, reading=None, complete=None, provider="openai", depth="auto", tool_error=None):
    tool = {"side_effect": tool_error} if tool_error else {"return_value": reading}
    with patch.object(config, "LLM_PROVIDER", provider), patch("llm.call_tool", **tool), \
            patch("llm.complete", side_effect=complete or llm.LLMError("not needed")):
        return agent.ask(message, depth=depth, max_seconds=120)


def test_a_brief(snapshot_agent):
    assert_business_language(ask(snapshot_agent, BRIEF, BRIEF_READING, _openai_brief([])))


def test_a_message_with_many_questions():
    assert_business_language(ask(make_agent(), "nine questions", LONG_MESSAGE_READING,
                                 openai_after_reading([])))


def test_a_thin_brief_with_a_data_caveat():
    reading = {**BRIEF_READING, "categories": [], "measures": []}
    assert_business_language(ask(thin_agent(), BRIEF, reading))


def test_failures_explain_themselves_in_plain_words():
    assert_business_language(ask(make_agent(), "visits?", tool_error=llm.LLMError("OpenAI request failed")))
    plan = lambda **_: json.dumps({"action": "custom_sql", "tables": ["no_such_table"], "reason": "r",  # noqa
                                   "interpretation": "i"})
    assert_business_language(ask(make_agent(), "day wise visits", {"rephrased_question": "Visits per day",
                                                                  "intent": "analysis"}, plan, depth="deep"))
    broken = make_agent(VisitSource(fail=True))
    assert_business_language(ask(broken, "visits", {"rephrased_question": "Total visits", "intent": "analysis",
                                                    "metric": "visits_overview"}))
    assert_business_language(ask(make_agent(), "visits", provider="ollama", depth="deep"))


def test_a_result_shown_with_a_caution():
    blocking = json.dumps({"blocking_issues": ["joins journey_window on search_category"], "minor_issues": []})
    result = ask(make_agent(), "day wise visits", {"rephrased_question": "Visits per day", "intent": "analysis"},
                 _deep(blocking, sql_calls=[]), depth="deep")
    assert result.answer.startswith("**Check before relying on this.**")
    assert_business_language(result)
