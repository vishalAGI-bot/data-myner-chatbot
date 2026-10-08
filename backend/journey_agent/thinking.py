"""Deep thinking: decompose -> answer parts from data -> verify -> grounded synthesis.

Everything runs inside a hard time budget (60 s by default, up to 20 min on request).
When it runs out the agent stops and returns the best *verified* answer it has,
saying which parts were not finished. The model plans and writes; the numbers
always come from the deterministic query layer.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date

# A question may ask for up to 20 minutes of thinking. Long analytical queries on the
# live database (visits per app over weeks) legitimately run for minutes, and the old
# 60 s ceiling stopped them before any answer existed.
MAX_THINK_SECONDS = 1200
# What a request gets when it does not ask for a budget. Most questions finish in
# seconds, so the default stays short and the user raises it deliberately.
DEFAULT_THINK_SECONDS = 60
DEPTHS = ("auto", "fast", "deep")


def format_seconds(seconds: float) -> str:
    """'45s' / '2 min' / '20 min' - for limits shown to a person."""
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    return f"{minutes:.0f} min" if abs(minutes - round(minutes)) < 0.05 else f"{minutes:.1f} min"


class ThinkingTimeout(RuntimeError):
    """The thinking budget ran out (or the user pressed stop)."""


@dataclass
class Budget:
    seconds: float = DEFAULT_THINK_SECONDS
    cancel: threading.Event | None = None
    started: float = field(default_factory=time.monotonic)
    no_llm: bool = False          # set after the deadline: only instant, rule-based answers

    def __post_init__(self):
        self.seconds = max(1.0, min(float(self.seconds or DEFAULT_THINK_SECONDS), MAX_THINK_SECONDS))
        self.deadline = self.started + self.seconds

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    @property
    def cancelled(self) -> bool:
        return bool(self.cancel and self.cancel.is_set())

    def scaled(self, reserve: float) -> float:
        """A safety margin never bigger than 15% of the whole budget."""
        return min(reserve, self.seconds * 0.15)

    def check(self, reserve: float = 0.0) -> None:
        """Raise if stopped or if less than ``reserve`` seconds are left."""
        reserve = self.scaled(reserve)
        if self.cancelled:
            raise ThinkingTimeout("stopped by the user")
        if self.remaining() <= reserve:
            raise ThinkingTimeout(f"the {format_seconds(self.seconds)} thinking limit was reached")


@dataclass
class ThinkingStep:
    stage: str            # understand | plan | query | verify | write | stop
    text: str
    elapsed_ms: int
    ok: bool = True

    def as_dict(self) -> dict:
        return {"stage": self.stage, "text": self.text, "elapsed_ms": self.elapsed_ms, "ok": self.ok}


# --------------------------------------------------------------------------- #
# When is a question "complex"?
# --------------------------------------------------------------------------- #

_MULTI_ASK = re.compile(
    r"\?.+\?"                                              # two question marks
    r"|\b(and|also|plus)\s+(who|what|which|how|where|why|when|is|are|does|do)\b"
    r"|\b(compared? (to|with)|versus|vs\.?|relative to)\b"
    r"|\b(why|explain why|what drives|root cause)\b"
    r"|\b(trend|over time|grown|shrunk|increasing|decreasing|spike|week over week|"
    r"month over month|changed)\b"
    r"|\b(segment|cohort|distribution of .+ by)\b",
    re.I)


def looks_complex(question: str, rules_confident: bool) -> bool:
    """Deep thinking only when one direct query will not do.

    A question the keyword rules understand completely is answered by one exact
    query - thinking longer adds latency, not accuracy. Otherwise go deep when it
    has several asks, comparisons, trends or 'why', or is long.
    """
    several = (question.count("?") >= 2 or question.strip().count("\n") >= 1
               or len(re.findall(r"^\s*[*\-•]", question, re.M)) >= 2)
    if several:
        return True
    if rules_confident:
        return False
    return bool(_MULTI_ASK.search(question)) or len(question.split()) >= 10


def needs_decomposition(question: str) -> bool:
    """Whether a question contains multiple asks or reasoning that needs separate parts.

    Unlike :func:`looks_complex`, length alone is not a reason to decompose a question
    that the deterministic planner already understands as one exact query.
    """
    several = (question.count("?") >= 2 or question.strip().count("\n") >= 1
               or len(re.findall(r"^\s*[*\-•]", question, re.M)) >= 2)
    return several or bool(_MULTI_ASK.search(question))


def split_parts_without_llm(question: str) -> list[str]:
    """Fallback decomposition: split bullet lists, lines and '?'."""
    chunks = re.split(r"\n+|(?<=\?)\s+|^\s*[*\-•]\s*", question, flags=re.M)
    parts = [c.strip(" *-•") for c in chunks if c and len(c.strip(" *-•").split()) >= 3]
    return parts[:4] or [question.strip()]


_RELATIVE_PERIOD = re.compile(
    r"\b(?:last|past|previous)\s+"
    r"(?:(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+)?"
    r"(days?|weeks?|months?|quarters?|years?)\b", re.I)
_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
                 "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                 "ten": 10, "eleven": 11, "twelve": 12}
_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "quarter": 91, "year": 365}


def requested_period_exceeds(question: str, first_date: str | None, last_date: str | None) -> bool:
    """Whether a requested relative or explicit period exceeds loaded coverage."""
    if not first_date or not last_date:
        return False
    try:
        available_start = date.fromisoformat(first_date[:10])
        available_end = date.fromisoformat(last_date[:10])
    except (TypeError, ValueError):
        return False

    explicit = _explicit_dates(question, available_end.year)
    if explicit and (min(explicit) < available_start or max(explicit) > available_end):
        return True

    match = _RELATIVE_PERIOD.search(question)
    if not match:
        return False
    raw_count = (match.group(1) or "one").lower()
    count = int(raw_count) if raw_count.isdigit() else _NUMBER_WORDS.get(raw_count, 1)
    unit = match.group(2).lower().rstrip("s")
    covered_days = (available_end - available_start).days + 1
    return count * _UNIT_DAYS[unit] > covered_days


_MONTH_NUMBER = {name: number for number, names in enumerate((
    ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"),
    ("may",), ("june", "jun"), ("july", "jul"), ("august", "aug"),
    ("september", "sept", "sep"), ("october", "oct"), ("november", "nov"),
    ("december", "dec"),
), 1) for name in names}
_MONTH_NAMES = "|".join(sorted(_MONTH_NUMBER, key=len, reverse=True))
_DAY_MONTH = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_NAMES})(?:\s+(20\d{{2}}))?\b", re.I)
_MONTH_DAY = re.compile(
    rf"\b({_MONTH_NAMES})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(20\d{{2}}))?\b", re.I)
_ISO_DATE = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b")


def _explicit_dates(question: str, default_year: int) -> list[date]:
    """Extract common explicit date forms for coverage validation."""
    found: list[date] = []
    spans: list[tuple[int, int]] = []
    for match in _ISO_DATE.finditer(question):
        try:
            found.append(date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
            spans.append(match.span())
        except ValueError:
            continue
    remaining = "".join(" " if any(start <= i < end for start, end in spans) else char
                        for i, char in enumerate(question))
    for pattern, day_group, month_group, year_group in (
        (_DAY_MONTH, 1, 2, 3), (_MONTH_DAY, 2, 1, 3),
    ):
        for match in pattern.finditer(remaining):
            try:
                found.append(date(int(match.group(year_group) or default_year),
                                  _MONTH_NUMBER[match.group(month_group).lower()],
                                  int(match.group(day_group))))
            except ValueError:
                continue
    return found


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

DECOMPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "parts": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        "cannot_answer": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"part": {"type": "string"}, "missing": {"type": "string"}},
                      "required": ["part", "missing"]},
        },
    },
    "required": ["parts"],
}

DECOMPOSE_SYSTEM = """You break a business question about shopping-journey data into at most 4
simple, self-contained data questions that together answer it. Each part must be answerable
on its own (repeat the app / user / filter in every part; never write "it" or "them").
The chatbot that answers "we / us / our" means {home_app}.

The data ({first_date} to {last_date}, {journeys} journeys) has per journey: user number,
search phrase, apps used and searches per app, the app of the first search, first/last
search time, status (open/closed) and outcome (purchased / timeout).
It does NOT have: which app the purchase was on, purchase time, price or order value,
product category (only search text), city, device, or any history outside those dates.

Put parts that need missing data in cannot_answer with what is missing. Prefer simple
wordings such as: "purchase share by app", "journeys that start on Amazon",
"average journey duration of purchased journeys on Meesho", "journeys per day on Zepto",
"purchase rate by start app", "users who used 3 or more apps"."""

CRITIC_SCHEMA = {
    "type": "object",
    "properties": {
        "complete": {"type": "boolean"},
        "problems": {"type": "array", "items": {"type": "string"}},
        "follow_up": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
    },
    "required": ["complete"],
}

CRITIC_SYSTEM = """You check whether data answers really answer a business question.
For each part, check that the answer is about the same thing that was asked (same app,
same measure, same filter). A share of ALL purchases is different from an app's own
purchase rate; journeys are different from searches.
Return complete=false if any asked part is unanswered or answered about something else.
In follow_up give at most 2 simple data questions that would fill the gap, only if the
data can answer them. Do not compute anything."""

SYNTH_SYSTEM = """Write the final answer to a business question using ONLY the verified data
answers below. Rules:
- Every number you write must appear in the data answers exactly. Do not calculate new numbers.
- Answer each part of the question directly first, then give the supporting numbers.
- Say clearly which parts cannot be answered from this data and what is missing.
- Plain business language, short paragraphs or bullets, no more than 180 words.
- "we / us / our" means {home_app}."""


def parse_json(raw: str) -> dict:
    raw = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fence:
        raw = fence.group(1)
    start, end = raw.find("{"), raw.rfind("}")
    try:
        return json.loads(raw[start:end + 1] if start >= 0 else raw)
    except json.JSONDecodeError:
        if start < 0:
            raise
        # A model sometimes repeats the object or appends text: use the first complete object.
        value, _ = json.JSONDecoder().raw_decode(raw[start:])
        return value
