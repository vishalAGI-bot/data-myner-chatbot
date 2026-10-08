"""A chat is one session: a question may lean on the turns before it.

Before this, the model saw earlier turns as 300 characters of rendered prose - a heading and about
two table rows - so "which of those is growing?" referred to data it had never been shown, and Deep
mode dropped the previous answer's parameters altogether. These tests pin the pieces that fixed it.
"""

from dataclasses import dataclass, field

from analyst.conversation import MEMORY_TURNS, Memory, needs_context


@dataclass
class FakeAnswer:
    """The fields Memory.remember() reads off a real AnalystAnswer."""

    route: str = "metric"
    metric: str | None = "active_users_by_app"
    resolved: str = "Active users by app"
    params: dict = field(default_factory=lambda: {"date_from": "2026-09-24"})
    tables: list = field(default_factory=lambda: ["session_master"])
    sql: str | None = None
    table: dict | None = field(default_factory=lambda: {
        "columns": ["app", "users", "pct_of_active_users"],
        "rows": [["Flipkart", 2191, 82.4], ["Meesho", 1654, 62.2], ["Amazon", 1596, 60.0]],
        "total_rows": 14,
    })


def remembered(question: str = "which apps has most users visited", answer=None) -> Memory:
    memory = Memory()
    memory.remember(question, answer or FakeAnswer())
    return memory


# --------------------------------------------------------------- detection

def test_references_and_continuations_are_follow_ups():
    for question in ("which of those is growing?", "what about Flipkart?", "why is it so high?",
                     "and the second one", "same for last week", "by city?", "compare them"):
        assert needs_context(question, True), question


def test_a_setting_change_is_a_follow_up():
    """The reported failure: "what is the result with 24 hr window?" after a purchase query.

    It carries no pronoun and is 8 words long, so the first version of this gate let it through as a
    fresh question; the keyword router then matched it to "Active users by app" and answered with
    session counts instead of re-running the purchase query at 24 hours.
    """
    for question in ("what is the result with 24 hr window?",
                     "with a 24 hour window instead",
                     "the answer for 48h?",
                     "show that total by city"):
        assert needs_context(question, True), question


def test_self_contained_questions_are_left_alone():
    """A period is not a reference: "this week" must not cost a resolution call."""
    for question in ("which apps has most users visited",
                     "how many total visits this week",
                     "day wise purchase share by app for the last 14 days"):
        assert not needs_context(question, True), question


def test_nothing_is_a_follow_up_without_memory():
    assert not needs_context("what about Flipkart?", False)


# ------------------------------------------------------------------ memory

def test_memory_survives_the_state_round_trip():
    """The client echoes `state` back, so the memory has to rebuild from it exactly."""
    restored = Memory.from_state(remembered().to_state())
    assert restored.last["metric"] == "active_users_by_app"
    assert restored.last["params"] == {"date_from": "2026-09-24"}


def test_prompt_carries_the_query_that_produced_the_answer():
    """A follow-up that changes one setting has to adapt the previous query, so it must see it."""
    answer = FakeAnswer()
    answer.sql = "SELECT COUNT(DISTINCT u.id) FROM journey_window jw WHERE jw.window_hours = 72"
    prompt = remembered(answer=answer).prompt()
    assert "Query used:" in prompt
    assert "window_hours = 72" in prompt


def test_prompt_carries_the_result_itself_not_prose():
    prompt = remembered().prompt()
    assert "app, users, pct_of_active_users" in prompt      # the columns
    assert "Flipkart | 2191 | 82.4" in prompt               # real values, not a clipped sentence
    assert "14 rows in total" in prompt                     # that more rows exist
    assert "active_users_by_app" in prompt                  # which analysis produced it


def test_ordinals_resolve_from_the_stored_rows_without_a_model():
    memory = remembered()
    assert memory.ordinal_entity("the second one") == "Meesho"
    assert memory.ordinal_entity("tell me about the first app") == "Flipkart"
    assert memory.ordinal_entity("the last one") == "Amazon"
    assert memory.ordinal_entity("what about Flipkart?") is None


def test_memory_keeps_only_the_recent_turns():
    memory = Memory()
    for i in range(MEMORY_TURNS + 3):
        memory.remember(f"question {i}", FakeAnswer())
    assert len(memory.turns) == MEMORY_TURNS
    assert memory.turns[-1]["question"] == f"question {MEMORY_TURNS + 2}"


def test_an_unanswered_turn_is_recorded_as_unanswered():
    """Otherwise the next question is resolved against a gap the model cannot see."""
    memory = remembered("conversion by city tier",
                        FakeAnswer(route="data_gap", metric=None, resolved="", table=None))
    assert "was not answered" in memory.prompt()


def test_remembering_an_empty_state_does_not_crash():
    assert not Memory.from_state(None)
    assert not Memory.from_state({})
    assert not Memory.from_state({"recent": "not a list"})
    assert not Memory.from_state({"recent": [None, 3]})
