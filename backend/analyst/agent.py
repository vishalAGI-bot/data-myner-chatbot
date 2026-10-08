"""Data analyst agent: question -> right table(s) -> verified numbers -> answer.

Pipeline
  1. Router picks a vetted metric (deterministic, no model) or candidate tables.
  2. Weak match -> the model chooses a metric + parameters, or tables for custom SQL
     (its choice is validated against the catalog).
  3. Custom SQL is written by the model from the chosen tables' documentation,
     checked by the guard, run read-only, and repaired up to twice on errors.
  4. The answer is rendered from the result rows; any model-written narrative
     must only use numbers present in the results (grounding check).
  5. Deep mode splits complex questions into parts, answers each, checks
     coverage, and writes a grounded summary - all inside a 60 s budget.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Callable

import config
import llm
from output_format import OUT_OF_SCOPE_MESSAGE, is_explicitly_out_of_scope
from journey_agent import answer as grounding
from journey_agent.context import split_style
from journey_agent.thinking import (DECOMPOSE_SCHEMA, DEFAULT_THINK_SECONDS, Budget, ThinkingStep,
                                    ThinkingTimeout, looks_complex, parse_json, requested_period_exceeds,
                                    split_parts_without_llm)

from . import guard, metrics as metric_lib
from .conversation import RESOLVE_SCHEMA, RESOLVE_SYSTEM, Memory, needs_context
from .catalog import Catalog, MetricDoc
from .router import Route, Router
from .settings import Settings
from .sources import Result, Source, SourceError

log = logging.getLogger("analyst.agent")
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="analyst-llm")


@dataclass
class AnalystAnswer:
    answer: str
    route: str = "metric"               # metric | proxy | sql | deep | data_gap | clarify | unsupported | out_of_scope | help | error | timeout
    source: str = "rules"               # rules | llm
    resolved: str = ""
    metric: str | None = None
    params: dict = field(default_factory=dict)
    tables: list[str] = field(default_factory=list)
    sql: str | None = None
    table: dict | None = None
    notes: list[str] = field(default_factory=list)
    thinking: list[dict] = field(default_factory=list)
    parts: list[dict] = field(default_factory=list)
    verified: bool = True
    timed_out: bool = False
    depth: str = "fast"
    latency_ms: int = 0
    state: dict = field(default_factory=dict)       # sent back by the client for follow-ups
    suggested_questions: list[str] = field(default_factory=list)
    reason: str = ""                    # why a question could not be answered (shown to the user)


class _ReviewRejected(RuntimeError):
    """OpenAI's pre-execution reviewer objected to a query the guard already approved.

    Kept apart from GuardError on purpose. The guard and the database enforce what is *allowed*;
    the reviewer offers an opinion about business logic and is sometimes wrong. Conflating the two
    meant a debatable objection blocked as hard as a blocked column, and the user got no answer at
    all - so a query that ran read-only is now shown, flagged, rather than thrown away.
    """


@dataclass
class _Run:
    budget: Budget
    trace: list[ThinkingStep] = field(default_factory=list)
    on_event: Callable[[dict], None] | None = None
    memory: "Memory | None" = None


_RUN: contextvars.ContextVar[_Run | None] = contextvars.ContextVar("analyst_run", default=None)

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["metric", "sql", "clarify", "unsupported", "out_of_scope"]},
        "metric": {"type": "string"},
        "params": {"type": "object"},
        "tables": {"type": "array", "items": {"type": "string"}},
        "message": {"type": "string"},
    },
    "required": ["action"],
}

SQL_SCHEMA = {
    "type": "object",
    "properties": {"sql": {"type": "string"}, "explanation": {"type": "string"}},
    "required": ["sql"],
}

PLAN_SYSTEM = """You route a business question about shopping-app data to the right analysis.
Prefer a vetted metric when one fits (exact, tested). Choose "sql" only when no metric fits.
Choose "unsupported" when the question is about business data but needs fields the dataset does not
contain. Choose "out_of_scope" for code, project/source files, creative writing and unrelated general
knowledge; never answer such a request. Choose "clarify" when a data-analysis question is ambiguous.
"we / us / our" means {home_app}. Data covers {first_day} to {last_day}.
No date range given means the whole data coverage; never clarify or refuse only because dates are missing.
"Day wise / daywise / daily / week wise / weekly" (any spelling) asks for one row per period: choose a metric
only if it returns that grain and grouping, otherwise "sql". For "each app / by app", a journey counts for every
app used in it (once per app); do not ask to clarify that.

Vetted metrics:
{metrics}

Tables:
{tables}

Glossary:
{glossary}

Reply with JSON: {{"action": ..., "metric": name, "params": {{...}}, "tables": [...], "message": "..."}}.
Use exact app names from: {apps}."""

SQL_SYSTEM = """You write ONE PostgreSQL SELECT query that answers the question from these tables only.
{schema}

Rules:
- Use only the tables and columns listed. Quote the table "order" as "order".
- journey_window: always filter window_hours (default {window}). For rates count finished windows only:
  jw.window_end <= (SELECT MAX(lw.window_start) FROM journey_window lw). Never use is_final (stale flag).
- Join app_master to show app names instead of ids. "we/us/our" = {home_app}.
- Never invent a mapping between an identifier in the question and a column that does not exist. Apps have
  no code or site number: match them on app_master.app_name or app_master.id, and always return app_name so
  the answer shows which app was actually used.
- Never SELECT *; never return columns marked "do not select"; personal columns only inside aggregates.
- Aggregate on large tables (testing_accessibility_infos); add a LIMIT large enough for the complete answer
  (at most {max_rows} rows).
- session_master: when a CTE derives columns from split_part(session_id, ...), never CROSS JOIN or join a
  scalar aggregate of that CTE back onto it. PostgreSQL misjudges its size and re-runs the aggregate once per
  row. Read the aggregate as an uncorrelated subquery - (SELECT MAX(x) FROM cte) - and, where you need a
  per-user figure beside per-session rows, aggregate to one row per group first and use a window function.
- Name result columns clearly (e.g. journeys, conversion_pct).
- When the question asks for a LIST or DETAILS of entities (users, journeys, orders, products), the FIRST
  selected column must identify the entity: user_master.id for a user, the journey id for a journey, the app
  name for an app. Attributes such as city, gender or device with no identifier beside them cannot be acted
  on and do not answer "give me the details of X".
- A comparison question (vs, compared with, always, usual, spike, dominant, grown) needs its baseline: return the
  named entities AND their peers (other apps / categories), and the whole data coverage for always/usual/spike
  questions, not only the named entity or period.
Business terms:
{glossary}
{time_rules}
Reply with JSON: {{"sql": "...", "explanation": "one sentence"}}"""

DEEP_SQL_RULES = """

Additional Deep-analysis correctness rules:
- For a date range on a journey analysis, select journeys whose first_search_time is in the half-open range
  [start date, day after end date), then use each selected journey's complete events, duration, app involvement
  and close_reason. Do not clip a journey's events or recompute its duration at the date boundaries. Only filter
  cross_app_journey_event.search_time directly when the user explicitly asks for events/searches occurring in
  the period rather than journeys from the period.
- cross_app_journey is one row per journey. Journey duration is
  EXTRACT(EPOCH FROM (last_search_time - first_search_time)) / 60.0; never use
  journey_window.window_hours as duration (it is a purchase-attribution setting).
- cross_app_journey_event is one-to-many. Calculate journey count, journey duration and purchase
  rate at one-row-per-journey grain before joining event rows, or aggregate events in a separate CTE.
  Never multiply parent journey measures through an event join.
- Use COUNT(DISTINCT journey id) for journey counts whenever a query includes a one-to-many join.
- For a comparison "by app", an app's population is the DISTINCT journeys that contain an event on that
  app. A multi-app journey may belong to multiple app groups, but only once within each app. Search count is
  the app-attributed event count; duration is averaged once per distinct journey involving that app;
  multi-app usage counts those distinct journeys with total_apps > 1; purchase-closure rate is purchased
  distinct journeys divided by all distinct journeys involving that app unless the user explicitly asks
  "among closed journeys". Other apps in such a journey are expected and do not contaminate the requested
  app's population. Do not silently substitute first_search_app_id attribution.
- Resolve a one-level sub-app to its main app with COALESCE(parent_app.id, event_app.id) and
  COALESCE(parent_app.app_name, event_app.app_name), using event_app.parent_id = parent_app.id. This mapping
  assigns every event to exactly one app family. Use explicit column lists in every SELECT, including CTEs.
- Filter requested apps on the RESOLVED family (COALESCE(parent_app.app_name, event_app.app_name) IN (...)),
  never on event_app alone, so sub-app events (for example Amazon Now under Amazon) count for their main app.
- Recommended shape: (1) resolved_events CTE: one row per event with journey id and resolved family id/name;
  (2) search_count = COUNT(*) of resolved_events grouped by family directly (never join events to a
  journey-membership CTE, which repeats events); (3) membership CTE = SELECT DISTINCT journey id, family from
  resolved_events; (4) join membership to cross_app_journey (one row per journey) for journey count, average
  duration, total_apps > 1 and close_reason; (5) join the two aggregates on family.
- Keep every requested app/date/filter identical across all measures. Return the requested measures plus
  only supporting columns (numerator/denominator behind a rate, a period's first_day/last_day).
"""

NARRATIVE_SYSTEM = """Answer the business question in at most 120 words using ONLY the result rows below.
Copy numbers exactly as they appear; do not calculate new numbers. Lead with the direct answer.
Mention a caveat only if it is given. "we / us / our" means {home_app}.
The complete result table is shown to the user directly below your text: NEVER reproduce the table, use
markdown tables, or list every row. For a day-wise / week-wise breakdown, describe the overall pattern in at most
two sentences; name a highest or lowest value for ONE measure only after checking every row, and never claim
that several apps or measures share a peak. Always finish your last sentence."""

DECOMPOSE_SYSTEM = """Split a business question about shopping-app data into at most 4 simple, self-contained
data questions that together answer it (repeat the app, period and filter in every part; keep the user's
wording such as "only", "last 24 hours"). A single simple question stays as ONE part.
Every part must be executable independently: never refer to "those users", "the results above", or an
entity that another part must discover first. When a ranking asks for several columns about the same rows
(for example top users plus their journey count, apps and purchase rate), keep it as ONE custom-query part.
Likewise, keep a comparison of several apps across several metrics as ONE custom-query part.
Vetted analyses:
{metrics}
Tables (a part may also be answered with a custom query on these):
{tables}
The data covers {first_day} to {last_day} only and has no city tier. "we/us/our" = {home_app}.
Only put a part in cannot_answer when NO table above holds that information (say what is missing).
Reply with JSON: {{"parts": [...], "cannot_answer": [{{"part": "...", "missing": "..."}}]}}"""

DEEP_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["metric", "custom_sql", "decompose", "unsupported"]},
        "metric": {"type": "string"},
        "params": {"type": "object"},
        "tables": {"type": "array", "items": {"type": "string"}},
        "parts": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        "message": {"type": "string"},
        "reason": {"type": "string"},
        "interpretation": {"type": "string"},
    },
    "required": ["action", "reason", "interpretation"],
}

DEEP_PLAN_SYSTEM = """You are the lead analyst deciding the complete execution strategy for one Deep analysis.
Use a vetted metric only when it answers EVERY requested measure, entity, filter and date. Choose custom_sql
for a cohesive ranking/comparison needing several columns or joins. Choose decompose only for independent
questions; every part must be executable alone and must repeat its own entities, dates and filters. Never use
phrases such as "those users" or depend on another part's result. Choose unsupported only when required fields
do not exist and the requested measure cannot be derived from the documented fields. Treat timestamps,
statuses, counts and joins as usable inputs for derived measures. Do not calculate or answer the question.
For a dated journey analysis, the date range selects journeys by first_search_time and all measures then use
those complete journeys. For "by app", attribute a journey to every app represented by its events, once per
app; do not substitute first_search_app_id. Purchase-closure rate comes from close_reason = 'purchased', not
journey_window. Select the tables needed to apply these definitions.
A comparison or judgement question (vs, compared with, always, usual, spike, dominant, grown) needs its baseline in
the result. For always/usual/spike/grown/trend questions never restrict the dates to the named period: use the whole
coverage, the named period is compared with the earlier days afterwards. Vetted by-app and by-category metrics already
keep the peers (named apps are highlighted rather than filtered, the category trend always shows the top categories,
and a share is always measured against all apps), so prefer them over custom_sql for comparisons.
A day-wise / week-wise / month-wise request needs a result with one row per period (and per requested group
such as app). A vetted metric qualifies only if it already returns that exact period grain and grouping;
otherwise choose custom_sql. Keep a period breakdown of several measures as ONE custom_sql part.
Misspellings such as "daywiese" or "week wiese" mean day-wise / week-wise. When no dates are given, the whole data
coverage is intended: a missing date range is NEVER a reason for unsupported.
Ambiguous or loosely worded questions ("total user visit", "how many users") are NOT unsupported: resolve every
ambiguous term with the glossary (otherwise the most common business reading), default to all apps and the whole
data coverage, and record the resolution in "interpretation" (one or two sentences naming each measure, its
table/definition, the entities and the period). The query writer and reviewer must follow your interpretation.
Plan ONLY the current question. Any reference to an earlier turn has already been resolved into it, so take it
literally: never carry over measures, grains, apps or dates it does not name. Earlier turns are shown only as
background for wording.
A keyword-match hint may be given; use it when it answers the current question, ignore it otherwise.

Glossary:
{glossary}

Vetted metrics:
{metrics}

Available tables:
{tables}

Complete allowed schema and joins:
{schema}

Data coverage: {first_day} to {last_day} (journey data). "This week" means the calendar week from Monday to the
latest data day ({week_start} to {last_day}); "last 7 days" means the 7 days ending on {last_day}. Session data (session_master) can cover a longer
period; for session-based measures say "all loaded session data" instead of naming dates. "we/us/our" means
{home_app}.
Reply with the required JSON strategy. Use exact metric and table names from the menus."""

SQL_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "blocking_issues": {"type": "array", "items": {"type": "string"}},
        "minor_issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["blocking_issues", "minor_issues"],
}

SQL_REVIEW_SYSTEM = """Review a proposed read-only analytics SQL query before execution.
Check that it answers every requested metric with the exact entities, date range and filters; uses the correct
grain; does not multiply parent measures through one-to-many joins; calculates duration from first/last search
timestamps; and labels units accurately. A syntactically valid query is invalid if its business logic is wrong.

The business definitions below are AUTHORITATIVE. The query writer received the same text. A query that follows
them is correct: do not invent stricter or different definitions, and do not call a behaviour these definitions
require "bias" or "contamination". In particular, a multi-app journey appearing once in EACH app's group (and
contributing its full duration to each of those app averages) is the required behaviour, not double counting;
double counting means the same journey counted more than once WITHIN one app group. Multi-app usage for an app
means that app's distinct journeys with cross_app_journey.total_apps > 1 (any apps, not only the requested ones).
{rules}
{time_rules}
Extra review conventions:
- A lead-analyst interpretation, when given, settles every ambiguous term (for example what a "visit" is).
  Check that the SQL implements the question AS INTERPRETED; never reject it for using that interpretation and
  never switch to a different reading between reviews.
- The SQL only has to return every requested measure per requested entity/period. Judgements such as "which
  app performed best", rankings, winners or explanations are written afterwards from the result rows: never
  block a query for not computing them.
- In a dated journey analysis, search count = events of the journeys selected by first_search_time (their
  complete events), not events filtered by their own search_time, unless the user asks for searches occurring
  in the period.
- When the user gives a month and day without a year, the year of the active data coverage is valid.
- When the user gives no dates, no date filter (or a filter equal to the coverage) is valid.
- Reject SELECT * in any query or CTE.
Classify every problem you find:
- blocking_issues: the query would return WRONG, MISSING or MISLEADING numbers for what was asked (wrong grain,
  inflated counts, wrong population/date/app filter, wrong formula, a requested measure or entity absent,
  truncated period grid, SELECT *).
- minor_issues: anything that does not change the requested numbers (an extra supporting column, column naming,
  ordering, an app with no rows being absent rather than shown as zero, style).
Leave blocking_issues empty when the numbers would be correct. Each issue is one specific sentence.
Do not write replacement SQL. Reply only with the required JSON review."""

ANSWER_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "valid": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["valid", "issues"],
}

ANSWER_REVIEW_SYSTEM = """Verify an analytics answer against the executed result rows.
It must answer the user's request, preserve entity/metric meanings and units, and contain no factual claim or
number unsupported by the rows. The result rows given are the COMPLETE query output, and the user sees the full
table next to the answer; an answer that lists every row, groups rows by period, or says the full table is
attached is not truncated. Judge completeness on the whole answer: a short summary followed by a complete
"Result" section is complete. Numbers rounded to one decimal place (22.222 -> 22.2%) and thousands separators
are faithful. In a week-wise result, week_start may be the Monday before the data begins; first_day/last_day
show the covered days of a partial week, which is correct. The stated data coverage describes journey data;
session-based results can legitimately span a longer period, and their period_start/period_end columns are
authoritative. A "Comparison (computed from the rows)" section holds averages, ranges and ranks calculated in
code from the complete rows: treat them as correct and judge whether the "Answer:" verdict follows from them and the
rows. Report an issue only when the answer states a wrong number or claim, or misses what was asked; a claim you
re-check and find correct is not an issue. Do not rewrite the answer. Reply only with the required JSON review."""

SYNTH_SYSTEM = """Write the final answer to a business question using ONLY the verified data answers below.
Every number must appear in them exactly; do not calculate new numbers. Answer each part directly, then give
supporting numbers. Say which parts cannot be answered and why. At most 200 words. "we/us/our" = {home_app}.
Each "Complete result" is the whole query output: never say the data is partial, cut off or missing rows
unless it says "more rows exist". Name every item when the question asks for a list."""

COMPARE_SYSTEM = """Answer a comparison question about shopping-app data for a business reader in 2 to 4 sentences.
Start with a direct verdict that answers the question as asked (for example "Not a spike:", "Yes:", "Meesho is
faster:"), then give the two or three numbers that prove it. Copy every number exactly from the computed comparison
facts or the result rows; never calculate a new number. If a caveat limits the comparison (for example the
categories changed during the period), say so in one short sentence. Do not list the rows, describe the table, or
use markdown headings or tables. "we / us / our" means {home_app}. Always finish your last sentence."""

SIMPLE_SYSTEM = """Explain what the data answer below MEANS for a non-technical shop owner, in one to three
short sentences (a journey is one product hunt, apps are shopping apps, leakage means buying on another app).
Do NOT write any number, digit, percentage, date or id, not even as words; the exact figures are shown below
your text. No exclamation marks."""

# The reviewer keeps reading the answer's row limit as missing data, even when the complete table is
# rendered beside it. When the result is genuinely whole, such an objection is false by construction.
_COMPLETENESS_COMPLAINT = re.compile(
    r"\btruncat\w*|\bonly the first\b|\bnot attached\b|\bfull table\b|\blists only\b", re.I)

# Comparison and judgement questions get computed comparison facts and a written verdict, not only rows.
_COMPARISON = re.compile(
    r"\b(compar\w*|vs\.?|versus|than|always|usual(?:ly)?|normal(?:ly)?|typical(?:ly)?|spikes?|spiking|dominan\w*|"
    r"dominat\w*|ahead|behind|gr[eo]w\w*|shr[iu]nk\w*|increas\w*|decreas\w*|declin\w*|higher|lower|better|worse|"
    r"faster|slower|differ\w*|steady|stable|consistent\w*|outperform\w*)\b", re.I)
# ...of which these judge the present against the past, so they need the whole period as the baseline.
_BASELINE = re.compile(r"\b(always|usual(?:ly)?|normal(?:ly)?|typical(?:ly)?|spikes?|spiking|steady|stable|"
                       r"consistent\w*|gr[eo]w\w*|shr[iu]nk\w*|increas\w*|decreas\w*|declin\w*|over time|"
                       r"histor\w*)\b", re.I)
_BUCKET = re.compile(r"^(other\b|uncategori[sz]ed$|unknown)", re.I)         # catch-all groups, never "the leader"
_COUNT_LIKE = re.compile(r"pct|share|journeys|count|purchases|sessions|users|visits|searches|orders|converted", re.I)
_RANKING = re.compile(r"\b(ahead|behind|rank\w*|beat\w*|leader\w*|top \w+ apps?|who is winning)\b", re.I)
_PERIOD_WORDS = re.compile(r"\b(months?|years?|quarter|always|historically|over time|last month|"
                           r"month over month|mom|yoy|since launch)\b", re.I)
_CURRENT_WORDS = re.compile(r"\b(today|right now|currently|current|latest|as of now)\b", re.I)
# "day wise", "daywise", "day-wiese", "daily", "per week", "week by week", "date wise" ...
_TIME_GRAIN = (
    ("day", re.compile(r"\b(?:day|date)\s*-?\s*wie?se?\b|\bdaily\b|\b(?:per|each|every|by)\s+(?:day|date)\b|"
                       r"\bday\s+by\s+day\b|\bday\s+on\s+day\b|\bdate\s*-?\s*level\b", re.I)),
    ("week", re.compile(r"\bweek\s*-?\s*wie?se?\b|\bweekly\b|\b(?:per|each|every|by)\s+week\b|"
                        r"\bweek\s+(?:by|on|over)\s+week\b|\bwow\b", re.I)),
    ("month", re.compile(r"\bmonth\s*-?\s*wie?se?\b|\bmonthly\b|\b(?:per|each|every|by)\s+month\b", re.I)),
)
_BY_APP = re.compile(r"\b(?:each|every|per|by)\s+apps?\b|\bapp\s*-?\s*wie?se?\b|\bapps?\b.*\b(?:vs|versus|compare)\b",
                     re.I)
# "Add subcategory and master category to the table" asks to EXTEND the previous result. A vetted
# analysis returns a fixed set of columns and cannot be extended, so it answered "the results provide
# search categories but not subcategory or master category" instead of joining the taxonomy.
_ADD_COLUMN = re.compile(
    r"^\s*(?:and\s+|also\s+)?(?:add|include|append|bring in)\b"
    r"|\b(?:add|include|append)\b[^.?!]{0,40}\b(?:to|in)\s+the\s+(?:table|result|results|list|output)\b"
    r"|\b(?:along\s+with|together\s+with|as\s+well\s+as)\b[^.?!]{0,40}\bcolumns?\b"
    r"|\b(?:with|show)\b[^.?!]{0,30}\bextra\s+columns?\b",
    re.I)

# A request for the users themselves, not a figure about them. No vetted analysis returns one row
# per user, so these must become a custom query instead of a confident answer about something else.
_BY_USER = re.compile(
    r"\b(?:each|every|per|by)\s+users?\b"
    r"(?!\s*(?:attribute|segment|type|group|tier|age|city|state|zone|gender|device))"
    r"|\buser\s*-?\s*wie?se?\b"
    r"|\busers?\s+(?:list|ids?|names?)\b"
    r"|\blist\s+(?:of\s+)?(?:the\s+)?users?\b"
    r"|\bwhich\s+users?\b"
    r"|\btop\s+\d+\s+users?\b"
    r"|\busers?\s+who\s+\w+\s+(?:most|maximum)\b",
    re.I)
_PERIOD_COLUMNS = {"day", "date", "dt", "week", "week_start", "week_end", "month", "month_start", "period",
                   "search_date", "journey_date", "event_date", "order_date", "first_day", "last_day",
                   "period_start", "period_end", "month_end", "start_date", "end_date"}

TIME_GRAIN_RULES = """
Time-grain rules (day-wise / week-wise / month-wise questions):
- Day-wise: CAST(<timestamp> AS date) AS day. Week-wise: CAST(DATE_TRUNC('week', <timestamp>) AS date) AS week_start
  (ISO weeks start Monday) plus MIN(CAST(<timestamp> AS date)) AS first_day and MAX(...) AS last_day so partial
  first/last weeks are visible. Month-wise: CAST(DATE_TRUNC('month', <timestamp>) AS date) AS month_start.
- Bucket journeys AND their searches/events by the journey's cross_app_journey.first_search_time, so every
  measure in a row describes the same journeys (search count = events of the journeys that started in that
  period). Bucket by cross_app_journey_event.search_time only when the user explicitly asks about searches made
  on each day/week rather than journeys. For sessions use the session start.
- The period column is the FIRST result column; a group column (such as app) comes second; order by period
  ascending, then group. Return EVERY period in the requested range (the whole data coverage when no dates
  are given) and every requested group: never keep only the top rows. The LIMIT must fit the complete
  period x group grid (at most {max_rows} rows).
- When no dates are given, do not add a date filter; all loaded data of the queried table is intended (session
  data can span a longer period than the journey coverage).
"""


def requested_time_grain(question: str) -> str | None:
    """'day' | 'week' | 'month' when the question asks for a per-period breakdown."""
    for grain, pattern in _TIME_GRAIN:
        if pattern.search(question):
            return grain
    return None


def _outer_select(sql: str) -> tuple[list[str], str]:
    """Output column expressions of the outermost SELECT, and the SQL after its FROM."""
    outer = re.split(r"^SELECT\b", sql or "", flags=re.M | re.I)[-1]
    select_list, rest = (re.split(r"^FROM\b", outer, maxsplit=1, flags=re.M | re.I) + [""])[:2]
    return _split_top_level(select_list), rest


def _metric_grain(metric: MetricDoc | None) -> str | None:
    """Period grain of the metric's OUTPUT (a CAST(... AS date) inside a CTE is not a day column)."""
    if metric is None:
        return None
    for item in _outer_select(metric.sql)[0]:
        named = re.search(r"\bAS\s+(\w+)\s*$", item, re.I) or re.fullmatch(r"\s*(?:\w+\.)?(\w+)\s*", item)
        alias = named.group(1).lower() if named else ""
        if re.search(r"DATE_TRUNC\(\s*'week'", item, re.I) or alias.startswith("week"):
            return "week"
        if re.search(r"DATE_TRUNC\(\s*'month'", item, re.I) or alias.startswith("month"):
            return "month"
        if alias in ("day", "date", "dt"):
            return "day"
    return None


def _metric_by_app(metric: MetricDoc | None) -> bool:
    """Whether the metric returns one row per app (its outer GROUP BY includes an app column)."""
    return _metric_groups_by(metric, r"app|competitor", focus="app")


def _metric_groups_by(metric: MetricDoc | None, pattern: str, focus: str | None = None) -> bool:
    """Whether the metric's OUTPUT has one row per entity matching ``pattern``.

    A measure named after the entity does not count: "COUNT(DISTINCT user_key) AS users" is one
    number about users, not a row per user. Only the outer GROUP BY (or a plain listing) does.
    """
    if metric is None:
        return False
    if focus and metric.focus_parameter == focus:
        return True
    items, rest = _outer_select(metric.sql)
    group = re.search(r"\bGROUP\s+BY\b(.*?)(?:\bHAVING\b|\bORDER\s+BY\b|\bLIMIT\b|$)", rest, re.I | re.S)
    if not group:                                     # a plain listing is per app when it lists apps
        return bool(items) and not re.search(r"\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\(", items[0], re.I) \
            and bool(re.search(pattern, items[0], re.I))
    keys = [items[int(k) - 1] if k.isdigit() and 0 < int(k) <= len(items) else k
            for k in (part.strip() for part in _split_top_level(group.group(1)))]
    return any(re.search(pattern, key, re.I) for key in keys)


def _split_top_level(text: str) -> list[str]:
    """Split a SQL list on commas that are not inside parentheses."""
    parts, depth, current = [], 0, []
    for char in text:
        depth += (char == "(") - (char == ")")
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _grain_mismatch(question: str, metric: MetricDoc | None, original: str = "") -> bool:
    """A vetted metric must produce the requested period grain and breakdown to answer the question.

    ``original`` is the message as typed, before follow-up resolution rewrote it: resolving turns
    "Add subcategory and master category to the table" into a complete question that no longer reads
    as an instruction to extend the previous result, which is exactly what has to be detected.
    """
    if _BY_APP.search(question) and not _metric_by_app(metric):
        return True
    if _BY_USER.search(question) and not _metric_groups_by(metric, r"\buser"):
        return True
    if _ADD_COLUMN.search(question) or (original and _ADD_COLUMN.search(original)):
        return True          # a vetted analysis has fixed columns; extending one needs its own query
    grain = requested_time_grain(question)
    return grain is not None and _metric_grain(metric) != grain



def _custom_sql_semantic_error(question: str, sql: str) -> str | None:
    """Reject common SQL that is valid syntactically but answers the wrong metric."""
    low_question, low_sql = question.lower(), sql.lower()
    asks_duration = bool(re.search(
        r"\b(?:spent|spend|duration|journey time|total time|average time|avg time)\b(?![\s-]*to\b)", low_question))
    if asks_duration:
        if "first_search_time" not in low_sql or "last_search_time" not in low_sql:
            return ("journey duration must be calculated from last_search_time - first_search_time "
                    "at one-row-per-journey grain; window_hours is only an attribution setting")
    asks_purchase_rate = bool(re.search(
        r"\b(?:purchase|purchased|conversion)[- ]?(?:closure )?(?:rate|percentage|pct)\b|"
        r"\bpercentage\b.{0,60}\b(?:purchase|purchased)\b", low_question))
    if asks_purchase_rate and not ("close_reason" in low_sql and "purchased" in low_sql):
        return "purchase-closure percentage must use close_reason = 'purchased' at journey grain"
    return None


def comparison_facts(table: dict | None, question: str, focus: list[str],
                     recent: tuple[str | None, str | None] = (None, None), baseline: bool = False,
                     comparable_from: str | None = None) -> list[str]:
    """Comparison facts computed in code from a complete result table (one decimal, so a verdict can quote them).

    Over time (a period column, usually with a group such as app or category): each focus entity's average in
    the recent period vs the earlier days, its range and how often it ranked first, plus the overall leaders.
    Without a period: each focus entity's value and rank, and the differences between the focus entities.
    """
    if not table or not table.get("rows"):
        return []
    cols, rows = table["columns"], table["rows"]

    def numeric(i: int) -> bool:
        vals = [r[i] for r in rows if r[i] is not None]
        return bool(vals) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals)

    def is_day(v) -> bool:
        return v is not None and bool(_ISO_DAY.match(str(v)))

    period = 0 if all(is_day(r[0]) for r in rows) else None
    nums = [i for i in range(len(cols)) if i != period and numeric(i)]
    if not nums:
        return []
    measure = next((i for i in nums if re.search(r"pct|share|rate|avg|average|median", cols[i], re.I)), nums[0])
    group = next((i for i in range(len(cols)) if i != period and i not in nums
                  and not all(is_day(r[i]) for r in rows if r[i] is not None)), None)
    name = _label(cols[measure])
    pct = cols[measure].endswith("_pct")

    def num(v: float) -> str:
        return f"{v:,.1f}".rstrip("0").rstrip(".")

    def val(v: float) -> str:
        return num(v) + ("%" if pct else "")

    low_q = question.lower()
    names = list(dict.fromkeys(str(r[group]) for r in rows)) if group is not None else []
    named = [n for n in names if not _BUCKET.match(n)]
    wanted = list(focus) + [n for n in named if len(n) > 3 and n.lower() in low_q]
    targets: list[str] = []
    for f in wanted:
        fl = str(f).lower()
        hit = next((n for n in named if n.lower() == fl), None) or next(
            (n for n in named if fl in n.lower() or n.lower() in fl), None)
        if hit and hit not in targets:
            targets.append(hit)
    group_name = _label(cols[group]) if group is not None else ""

    if period is None:
        if group is None:
            return []
        values = {str(r[group]): float(r[measure]) for r in rows if isinstance(r[measure], (int, float))}
        ordered = sorted((n for n in values if n in named), key=lambda n: -values[n])
        facts = []
        for t in targets[:4]:
            if t not in values:
                continue
            line = f"{t}: {name} {val(values[t])}"
            if t in ordered and len(ordered) > 1:
                higher = sum(values[n] > values[t] for n in ordered)
                lower = sum(values[n] < values[t] for n in ordered)
                line += (f"; {higher} of the other {len(ordered) - 1} {group_name} values are higher and {lower} lower"
                         f" (highest: {ordered[0]} {val(values[ordered[0]])}, lowest: {ordered[-1]} "
                         f"{val(values[ordered[-1]])})")
            facts.append(line + ".")
        first = targets[0] if targets else None
        for t in targets[1:4]:
            if first in values and t in values:
                diff = values[first] - values[t]
                facts.append(f"{first} minus {t}: {num(diff)}" + (" percentage points" if pct else f" {name}") + ".")
        return facts

    days = sorted({str(r[period])[:10] for r in rows})
    series: dict[str, dict[str, float]] = {}
    for r in rows:
        if isinstance(r[measure], (int, float)) and not isinstance(r[measure], bool):
            key = str(r[group]) if group is not None else name
            series.setdefault(key, {})[str(r[period])[:10]] = float(r[measure])
    by_category = group is not None and "categor" in cols[group].lower()
    comparable = [d for d in days if not (by_category and comparable_from) or d >= comparable_from]
    if not comparable:
        return []
    lo, hi = recent
    if lo or hi:
        recent_days = [d for d in comparable if (not lo or d >= lo) and (not hi or d <= hi)]
    else:
        recent_days = comparable[-7:] if baseline else []
    earlier = [d for d in comparable if recent_days and d < recent_days[0]]
    zero = 0.0 if _COUNT_LIKE.search(cols[measure]) else None      # a missing day is 0 journeys / 0% share

    def value(s: str, d: str) -> float | None:
        return series.get(s, {}).get(d, zero)

    def avg(s: str, ds: list[str]) -> float | None:
        xs = [v for v in (value(s, d) for d in ds) if v is not None]
        return sum(xs) / len(xs) if xs else None

    def span(ds: list[str]) -> str:
        return f"{ds[0]} to {ds[-1]}" if len(ds) > 1 else ds[0]

    ranked = [n for n in (named if group is not None else list(series)) if n in series]
    leaders = sorted(ranked, key=lambda s: -(avg(s, comparable) or 0.0))
    if not targets:
        targets = leaders[:1]
    facts = []
    if by_category and comparable_from and days[0] < comparable_from:
        facts.append(f"Only days from {comparable_from} are compared: the search categories changed that day, "
                     "so earlier days are not like-for-like.")
    for t in targets[:3]:
        a, b = avg(t, recent_days), avg(t, earlier)
        if recent_days and earlier and a is not None and b is not None:
            facts.append(f"{t}: average {name} {val(a)} in {span(recent_days)} ({len(recent_days)} days) vs "
                         f"{val(b)} on the {len(earlier)} earlier days ({span(earlier)}).")
        points = [(v, d) for d in comparable if (v := value(t, d)) is not None]
        if points:
            (mn, dmn), (mx, dmx) = min(points), max(points)
            facts.append(f"{t}: {name} ranged from {val(mn)} ({dmn}) to {val(mx)} ({dmx}) "
                         f"over {len(comparable)} days ({span(comparable)}).")
        if group is not None and len(ranked) > 1:
            days_with = [d for d in comparable if value(t, d) is not None]
            top = [d for d in days_with if all((value(s, d) or 0.0) <= value(t, d) for s in ranked if s != t)]
            buckets = [n for n in names if _BUCKET.match(n)]
            line = f"{t} had the highest {name} of any single {group_name} on {len(top)} of {len(days_with)} days"
            if recent_days:
                line += f" ({sum(d in top for d in recent_days)} of {len(recent_days)} in {span(recent_days)})"
            facts.append(line + (f", not counting the {' and '.join(buckets)} groups" if buckets else "") + ".")
    if group is not None and len(leaders) > 1:
        facts.append(f"Highest average {name} over {span(comparable)}: "
                     + ", ".join(f"{s} {val(avg(s, comparable) or 0.0)}" for s in leaders[:3]) + ".")
    return facts


def _extract_sql(raw: str) -> str:
    """The "sql" field of the model reply, even when the surrounding JSON is slightly malformed."""
    try:
        return str(parse_json(raw).get("sql", ""))
    except (ValueError, json.JSONDecodeError, AttributeError):
        pass
    match = re.search(r'"sql"\s*:\s*"((?:[^"\\]|\\.)*)"', raw, re.S)
    if match:
        try:
            return json.loads(f'"{match.group(1)}"')
        except json.JSONDecodeError:
            return match.group(1).replace("\\n", "\n").replace('\\"', '"')
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", raw, re.S | re.I)
    return fenced.group(1).strip() if fenced else raw


_ACTION_ALIASES = {"sql": "custom_sql", "custom": "custom_sql", "custom_query": "custom_sql", "query": "custom_sql",
                   "vetted_metric": "metric", "vetted": "metric", "split": "decompose", "parts": "decompose"}


def _normalise_plan(plan, catalog: Catalog) -> dict:
    """Accept a strategy that is nested, uses an action synonym, or omits the action but is unambiguous."""
    if not isinstance(plan, dict):
        return {}
    if "action" not in plan:
        nested = [v for v in plan.values() if isinstance(v, dict) and ("action" in v or "metric" in v or "tables" in v)]
        if len(nested) == 1:
            plan = {**{k: v for k, v in plan.items() if not isinstance(v, dict)}, **nested[0]}
    action = str(plan.get("action") or "").strip().lower()
    action = _ACTION_ALIASES.get(action, action)
    if not action:
        if str(plan.get("metric") or "") in catalog.metrics:
            action = "metric"
        elif plan.get("parts"):
            action = "decompose"
        elif plan.get("tables"):
            action = "custom_sql"
    return {**plan, "action": action or None}


class AnalystAgent:
    def __init__(self, source: Source, catalog: Catalog, settings: Settings, use_llm: bool = True):
        self.source = source
        self.cat = catalog
        self.s = settings
        self.router = Router(catalog)
        self.use_llm = use_llm

    # ================================================================ public
    def ask(self, message: str, history: list[dict] | None = None, state: dict | None = None,
            depth: str = "auto", max_seconds: float | None = None,
            on_event: Callable[[dict], None] | None = None,
            cancel: threading.Event | None = None) -> AnalystAnswer:
        started = time.perf_counter()
        run = _Run(Budget(max_seconds or DEFAULT_THINK_SECONDS, cancel), on_event=on_event)
        token = _RUN.set(run)
        memory: Memory | None = None
        question = message
        try:
            question, style = split_style(message)
            history = [{**h, "content": split_style(str(h.get("content")))[0] if h.get("role") == "user"
                        else h.get("content")} for h in (history or []) if h.get("content")][-12:]
            if not question.strip():
                result = AnalystAnswer("Please ask a question about the data.", route="help")
            else:
                memory = Memory.from_state(state)
                run.memory = memory
                asked = question.strip()
                question, continues = self._contextualize(asked, history, memory)
                result = self._ask(question, history, state or {}, depth,
                                   memory if continues else None, original=asked)
                if question != asked:
                    result.notes.insert(0, f"Read as: {question}")
                if result.route == "out_of_scope":
                    result.answer = OUT_OF_SCOPE_MESSAGE
                    result.verified = False
                    result.suggested_questions = result.suggested_questions or self._available_questions()
                elif result.route == "unsupported":
                    result.answer = ("Not available from the current data. "
                                     + (f"{result.reason.rstrip('.')}." if result.reason else
                                        "The dataset does not contain the fields needed for that analysis.")
                                     + "\n\nTry one of the available questions below.")
                    result.route = "data_gap"
                    result.verified = False
                    result.suggested_questions = result.suggested_questions or self._available_questions()
                elif result.route == "data_gap":
                    first, last = self.cat.date_range
                    coverage = f" The available data covers {first} to {last}." if first and last else ""
                    result.answer = ("Not available from the current data." + coverage
                                     + "\n\nTry one of the available questions below.")
                    result.verified = False
                    result.suggested_questions = result.suggested_questions or self._available_questions()
                if style == "simple" and result.route in ("metric", "sql", "deep"):
                    result = self._simplify(result)
        except (ValueError, json.JSONDecodeError) as exc:
            self._emit("verify", f"OpenAI returned an invalid Deep-analysis plan: {exc}", ok=False)
            result = AnalystAnswer(
                "OpenAI could not produce a valid Deep-analysis plan for this question. Try rephrasing "
                "the requested metrics, entities, and date range.", route="error", source="llm",
                resolved=question, depth="deep", verified=False)
        except ThinkingTimeout as exc:
            self._emit("stop", f"Stopped: {exc}", ok=False)
            result = AnalystAnswer(f"I stopped because {exc} before I had a verified answer.",
                                   route="timeout", timed_out=True)
        except llm.OllamaError as exc:
            result = AnalystAnswer("This question needs the language model, which is not reachable "
                                   f"({exc}). Try one of the built-in analyses (see Try asking).",
                                   route="error")
        finally:
            _RUN.reset(token)
        if memory is not None:
            memory.remember(question, result)
            result.state = {**(result.state or {}), **memory.to_state()}
        result.thinking = [s.as_dict() for s in run.trace]
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        log.info("route=%s metric=%s tables=%s latency_ms=%s", result.route, result.metric,
                 result.tables, result.latency_ms)
        return result

    def _ask(self, question: str, history: list[dict], state: dict, depth: str,
             continuation: Memory | None = None, original: str = "") -> AnalystAnswer:
        if is_explicitly_out_of_scope(question):
            self._emit("plan", "The request is outside the data-analysis scope", ok=False)
            return AnalystAnswer(
                OUT_OF_SCOPE_MESSAGE, route="out_of_scope", source="rules", resolved=question,
                verified=False, suggested_questions=self._available_questions())
        if requested_period_exceeds(question, self.cat.date_range[0], self.cat.date_range[1]):
            self._emit("plan", "The requested period extends beyond the available data", ok=False)
            return AnalystAnswer(
                "The requested period is not fully available. The available data covers "
                f"{self.cat.date_range[0]} to {self.cat.date_range[1]}.",
                route="data_gap", resolved=question,
                verified=False, suggested_questions=self._available_questions())
        route, ex = self.router.route(question, state)
        route = self._continue_previous(route, continuation, question)
        grain = requested_time_grain(question)
        if route.kind == "metric" and _grain_mismatch(question, route.metric, original):
            # e.g. "week wise purchase rate by app" keyword-matched a whole-period metric:
            # answering it would silently drop the requested period breakdown.
            wanted = " and ".join(x for x in (f"{grain}-wise" if grain else "",
                                              "per-app" if _BY_APP.search(question) else "",
                                              "per-user" if _BY_USER.search(question) else "",
                                              "extra-column" if _ADD_COLUMN.search(question) or (
                                                  original and _ADD_COLUMN.search(original)) else "") if x)
            self._emit("understand", f"A {wanted} breakdown was requested; "
                                     f"'{route.metric.title}' does not return it")
            route = Route("sql", route.metric, route.params, route.tables, 0.0,
                          f"{wanted} breakdown needs its own query", focus=route.focus, notes=route.notes)
        confident = route.kind == "metric" and route.confidence >= 0.6
        rule_parts = self._split_by_rules(question, route)
        result: AnalystAnswer | None = None
        deep = depth == "deep" or (depth == "auto" and (
            looks_complex(question, confident) or len(rule_parts) > 1
            or (grain is not None and not confident and config.LLM_PROVIDER == "openai")))
        # Deep mode must not make one confidently matched vetted analysis less
        # accurate. Decomposition is useful only for independent subquestions.
        if deep and depth != "deep" and confident and len(rule_parts) == 1 and not looks_complex(question, False):
            self._emit("plan", f"One vetted analysis covers the question: {route.metric.title}")
            result = self._answer_one(question, history, state, route, original=original)
            result.depth = "deep"
            if result.table and result.table.get("rows"):
                self._emit("verify", "Result came from a vetted read-only query")
            result.notes.extend(n for n in route.notes if n not in result.notes)
            deep = False
        if deep:
            result = self._deep(question, history, original)
            result.notes.extend(n for n in route.notes if n not in result.notes)
        elif result is None:
            result = self._answer_one(question, history, state, route, original=original)
        if not deep:
            self._compare(question, result)
        if (_PERIOD_WORDS.search(question) and grain is None and self.cat.date_range[0]
                and result.route != "data_gap"):
            result.notes.append(f"The database covers {self.cat.date_range[0]} to {self.cat.date_range[1]} only, "
                                "so longer-term trends cannot be measured yet.")
        if _CURRENT_WORDS.search(question) and self.cat.date_range[1]:
            result.notes.append(f"‘Current’ means the latest available company data, through "
                                f"{self.cat.date_range[1]}; it does not mean the calendar date.")
        if result.notes and result.route not in ("clarify", "help"):
            result.answer += "\n\nNote: " + " ".join(dict.fromkeys(result.notes))
        return result

    def _continue_previous(self, route: Route, continuation: Memory | None, question: str) -> Route:
        """Keep a follow-up on the analysis it is following up on.

        Changing one setting ("what is the result with 24 hr window?") is the same analysis again,
        but the keyword router sees a fresh question and can match something unrelated - a 24-hour
        custom purchase query came back as "Active users by app". When the question only makes sense
        because of the previous turn, that turn's approach wins over a keyword match.
        """
        prev = continuation.last if continuation is not None else None
        if not prev or not prev.get("answered"):
            return route
        if route.kind == "sql" and prev.get("sql") and not route.prior_sql:
            # Already heading for a custom query - typically because the follow-up asks for a shape no
            # vetted analysis returns. Hand over the previous query (a metric's rendered SQL counts) so
            # the new one keeps its definitions instead of inventing its own period and counting rule,
            # which is how "add user id" came back with different visit numbers.
            self._emit("plan", "Building on the query behind the previous answer")
            return replace(route, prior_sql=prev["sql"])
        if prev.get("route") in ("sql", "deep") and prev.get("tables") and prev.get("sql"):
            self._emit("plan", "Following up on the previous custom query, with the new setting applied")
            return Route("sql", tables=list(prev["tables"]), confidence=1.0,
                         reason="continues the previous custom query", source="rules",
                         prior_sql=prev.get("sql") or "")
        # A vetted analysis is deliberately NOT carried over here. The router has its own follow-up
        # path for that (it reuses state["metric"] for a fragment such as "what about Flipkart?"),
        # and forcing it whenever the router was unsure answered "give me the top 10 users" with the
        # same one-row-per-app table three times over.
        return route

    # ============================================================ one query
    def _answer_one(self, question: str, history: list[dict], state: dict, route: Route | None = None,
                    allow_narrative: bool = True, original: str = "") -> AnalystAnswer:
        if route is None:
            route, _ = self.router.route(question, state)
        notes = list(route.notes)
        if route.kind == "metric":
            self._emit("plan", f"Using the vetted analysis: {route.metric.title}")
            source = "llm" if route.source == "llm" else "rules"
            return self._run_metric(route.metric, route.params, route.focus, notes, source, question,
                                    narrate=allow_narrative)

        # Weak match: ask the model to choose (validated), else explain.
        if self.use_llm:
            try:
                plan = self._llm_plan(question, history, route)
            except (llm.OllamaError, ThinkingTimeout):
                plan = None
            if plan:
                action = plan.get("action")
                if (action == "metric" and plan.get("metric") in self.cat.metrics and self.s.allow_free_sql
                        and _grain_mismatch(question, self.cat.metrics[plan["metric"]], original)):
                    self._emit("plan", "The chosen analysis lacks the requested period breakdown; "
                                       "writing a custom query instead")
                    action = "sql"
                    plan["tables"] = plan.get("tables") or self.cat.metrics[plan["metric"]].tables
                if action == "metric" and plan.get("metric") in self.cat.metrics:
                    metric = self.cat.metrics[plan["metric"]]
                    params, focus, per_app = self._model_metric_params(metric, plan.get("params"), question)
                    self._emit("plan", f"Model chose the vetted analysis: {metric.title}")
                    if per_app:
                        return AnalystAnswer(
                            f"{metric.title} covers one app at a time. Ask about {', '.join(per_app)} one by one, "
                            "or use Deep thinking to compare them.", route="clarify", source="llm",
                            metric=metric.name)
                    return self._run_metric(metric, params, focus, notes, "llm", question,
                                            narrate=allow_narrative)
                if action == "unsupported":
                    return AnalystAnswer(plan.get("message") or "The data cannot answer that question.",
                                         route="unsupported", source="llm", notes=notes)
                if action == "out_of_scope":
                    return AnalystAnswer(OUT_OF_SCOPE_MESSAGE, route="out_of_scope", source="llm",
                                         notes=notes, verified=False)
                if action == "clarify" and plan.get("message"):
                    return AnalystAnswer(plan["message"], route="clarify", source="llm")
                tables = [t for t in plan.get("tables") or [] if t in self.cat.allowed_tables] or route.tables
                if self.s.allow_free_sql:
                    return self._free_sql(question, history, tables, route, notes,
                                          narrate=allow_narrative)
        if route.metric is not None and route.confidence >= 0.35:
            # Offline and a plausible (not certain) metric: answer it, but say what was assumed.
            notes.append(f"I read this as '{route.metric.title}'. If you meant something else, rephrase it.")
            return self._run_metric(route.metric, route.params, route.focus, notes, "rules", question,
                                    narrate=allow_narrative)
        suggestions = [self.cat.metrics[n].title for n in ([route.metric.name] if route.metric else []) + route.alternatives
                       if n in self.cat.metrics][:4]
        return AnalystAnswer(
            "I could not match this to a built-in analysis, and custom queries need the language model"
            + (" (not reachable right now)." if self.use_llm else ".")
            + (" Closest analyses: " + "; ".join(suggestions) + "." if suggestions else ""),
            route="help")

    # --------------------------------------------------------------- metric
    def _run_metric(self, metric: MetricDoc, params: dict, focus: list[str], notes: list[str],
                    source: str, question: str, narrate: bool = True) -> AnalystAnswer:
        data_issue = self.cat.metric_issues.get(metric.name)
        if data_issue:
            # A useful behavioural proxy exists for share questions, but it is
            # never silently presented as an order or market-share measure.
            if metric.name == "purchase_share_by_app":
                proxy = self.cat.metrics.get("checkout_confirmation_share_by_app")
                if proxy is not None and proxy.available:
                    proxy_params = {k: v for k, v in params.items() if proxy.param(k)}
                    proxy_answer = self._run_metric(
                        proxy, proxy_params, focus,
                        notes + [data_issue,
                                 "Showing checkout-confirmation share as a directional proxy, not verified purchase share."],
                        source, question)
                    proxy_answer.route = "proxy"
                    proxy_answer.answer = (
                        "Verified purchase share is unavailable because purchase outcomes are not linked. "
                        "Here is the directional checkout-confirmation signal instead:\n\n" + proxy_answer.answer)
                    return proxy_answer
            return AnalystAnswer(
                f"I cannot give a reliable {metric.title.lower()} yet. {data_issue}",
                route="data_gap", source=source, resolved=metric.title, metric=metric.name,
                params=params, tables=metric.tables, notes=notes, verified=False,
                state={"metric": metric.name, "params": params, "tables": metric.tables})
        try:
            resolved = metric_lib.resolve_params(metric, params, self.cat)
            sql = metric_lib.render(metric, resolved)
            checked = guard.check(sql, self.cat, dialect=self.source.dialect, max_rows=self.s.max_rows,
                                  schema=self.s.db_schema)
        except (metric_lib.MetricError, guard.GuardError) as exc:
            return AnalystAnswer(f"I need a bit more detail: {exc}", route="clarify", metric=metric.name)
        self._emit("query", f"Running {metric.title}" + (f" ({metric_lib.describe_params(metric, resolved)})"
                                                            if resolved else ""))
        try:
            result = self._execute(checked.sql)
        except SourceError as exc:
            self._emit("verify", f"The database rejected the query: {str(exc)[:300]}", ok=False)
            return AnalystAnswer(f"The {metric.title.lower()} query failed on the database ({str(exc)[:200]}). "
                                 "Try again, or narrow the period or apps.", route="error", source=source,
                                 resolved=metric.title, metric=metric.name, params=resolved,
                                 tables=checked.tables, sql=checked.sql, notes=notes, verified=False)
        if metric.caveats:
            notes.append(metric.caveats)
        if self.cat.metric_notes.get(metric.name):
            notes.append(self.cat.metric_notes[metric.name])
        rendered = render_result(result, title=metric.title, detail=metric_lib.describe_params(metric, resolved),
                                 focus=focus, ahead=bool(_RANKING.search(question)),
                                 hours=resolved.get("dimension") == "hour_of_day",
                                 highlight=list(focus) + ([resolved["category"]] if resolved.get("category") else []))
        # The figures come from the query that just ran; the model only words them. A comparison
        # question is left alone here because _compare writes its verdict above the rows instead.
        narrative = ""
        if narrate and self.use_llm and not llm.recently_unavailable() and not _COMPARISON.search(question):
            narrative = self._narrate(question, result, notes)[0]
        text = f"{narrative}\n\n{rendered}" if narrative else rendered
        return AnalystAnswer(text, route="metric", source=source, resolved=metric.title, metric=metric.name,
                             params=resolved, tables=checked.tables, sql=checked.sql,
                             table=result.as_table(), notes=notes, verified=bool(result.rows),
                             state={"metric": metric.name, "params": resolved, "tables": checked.tables})

    # Time kept back from a query so there is room left to render, ground-check and
    # (in Deep mode) verify the answer instead of timing out with rows in hand.
    QUERY_RESERVE_S = 5

    def _execute(self, sql: str) -> Result:
        run = _RUN.get()
        timeout_ms = None
        if run is not None:
            if run.budget.no_llm:
                timeout_ms = 3000                     # post-deadline rule answers: short, fixed limit
            else:
                run.budget.check(0)
                reserve = min(self.QUERY_RESERVE_S, run.budget.seconds * 0.1)
                timeout_ms = int(max(1000, (run.budget.remaining() - reserve) * 1000))
        return self.source.query(sql, max_rows=self.s.max_rows, timeout_ms=timeout_ms)

    # ------------------------------------------------------------- free SQL
    def _free_sql(self, question: str, history: list[dict], tables: list[str], route: Route,
                  notes: list[str], strict_deep: bool = False, feedback: str = "",
                  interpretation: str = "", narrate: bool = True) -> AnalystAnswer:
        tables = list(dict.fromkeys(tables))[:5]
        if "app_master" not in tables and "app_master" in self.cat.allowed_tables:
            tables.append("app_master")
        example = ""
        if route.prior_sql:
            # Same analysis, one thing changed: keeping the previous query's definitions is the
            # whole point, so it takes precedence over a vetted example.
            example = ("\nThis query answered the previous question in this conversation. The user is "
                       "changing one thing about it: keep its tables, joins, definitions and filters, "
                       f"and change only what the current question changes.\n{route.prior_sql[:1800]}\n")
        elif route.metric is not None:
            example = f"\nA vetted query on similar data (adapt, do not copy blindly):\n{route.metric.sql[:1500]}\n"
        system = SQL_SYSTEM.format(schema=self.cat.schema_card(tables), window=self.s.default_window_hours,
                                   home_app=self.s.home_app, max_rows=self.s.max_rows,
                                   glossary=self.cat.glossary[:4000],
                                   time_rules=TIME_GRAIN_RULES.format(max_rows=self.s.max_rows))
        if strict_deep:
            system += DEEP_SQL_RULES
        prompt = self._history_text(history) + f"Question: {question}\nJourney data covers {self.cat.date_range[0]} " \
                 f"to {self.cat.date_range[1]}; session data can start earlier. \"This week\" = {self._week_start()} " \
                 f"to {self.cat.date_range[1]}.{example}"
        if interpretation:
            prompt += f"\nLead analyst's interpretation (follow it exactly): {interpretation}"
        self._emit("plan", f"Writing a custom query on: {', '.join(tables)}")
        last_error = ""
        reviewed_out: tuple | None = None       # last query the guard passed but the reviewer did not
        flagged: list[str] = []                 # every issue so far, so a repair never reintroduces one
        attempts = 4 if strict_deep else 3
        for attempt in range(attempts):
            raw = self._llm(system=system, user=prompt + feedback, model=config.SQL_MODEL,
                            max_tokens=2200 if strict_deep else 1000,
                            schema=SQL_SCHEMA, cap=25)
            sql = _extract_sql(raw)
            try:
                checked = guard.check(sql, self.cat, dialect=self.source.dialect, max_rows=self.s.max_rows,
                                      schema=self.s.db_schema)
                if strict_deep:
                    unexpected = [table for table in checked.tables if table not in tables]
                    if unexpected:
                        raise guard.GuardError(
                            "query used tables outside the selected Deep-analysis scope: "
                            + ", ".join(unexpected))
                    semantic_error = _custom_sql_semantic_error(question, checked.sql)
                    if semantic_error:
                        raise guard.GuardError(semantic_error)
                    review_issues = self._openai_review_sql(question, checked.sql, tables, interpretation)
                    if review_issues:
                        raise _ReviewRejected("OpenAI SQL review: " + "; ".join(review_issues[:4]))
                    self._emit("verify", "OpenAI approved the query logic before execution")
                self._emit("query", f"Running the custom query (attempt {attempt + 1})")
                result = self._execute(checked.sql)
            except (_ReviewRejected, guard.GuardError, SourceError) as exc:
                last_error = str(exc)
                if isinstance(exc, _ReviewRejected):
                    # Guard-approved and runnable: keep it as a fallback in case nothing is approved.
                    reviewed_out = (checked, last_error)
                self._emit("verify", f"Query rejected: {last_error[:500]}", ok=False)
                flagged.append(last_error)
                earlier = "".join(f"\n- {issue}" for issue in flagged[:-1])
                feedback = (f"\n\nYour previous SQL:\n{sql}\nfailed with: {last_error}"
                            + (f"\nEarlier attempts were also rejected for these reasons; keep all of them fixed:"
                               f"{earlier}" if earlier else "")
                            + "\nFix it and reply with JSON.")
                continue
            narrative, grounded = (("", True) if not narrate or (self.use_llm and _COMPARISON.search(question))
                                   else self._narrate(question, result, notes))
            table_text = render_result(result, title="Result", detail="", focus=[],
                                       max_lines=50 if strict_deep else 10)
            text = (narrative + "\n\n" + table_text) if narrative else table_text
            return AnalystAnswer(text, route="sql", source="llm", resolved="custom query", tables=checked.tables,
                                 sql=checked.sql, table=result.as_table(), notes=notes, verified=grounded,
                                 state={"tables": checked.tables, "sql": checked.sql})
        if reviewed_out is not None:
            # Every attempt was turned down by the reviewer alone. Running the last one and flagging it
            # beats answering nothing: the guard allowed it, the session is read-only, and the user can
            # see both the figures and what the review objected to.
            checked, why = reviewed_out
            self._emit("verify", "No query satisfied the reviewer; running the last guard-approved one "
                                 "and flagging it for checking", ok=False)
            try:
                result = self._execute(checked.sql)
            except SourceError as exc:
                last_error = str(exc)
            else:
                if result.rows:
                    detail = why.removeprefix("OpenAI SQL review: ").rstrip(".")
                    narrative = ""
                    if narrate and self.use_llm:
                        narrative = self._narrate(question, result, notes)[0]
                    table_text = render_result(result, title="Result", detail="", focus=[],
                                               max_lines=50 if strict_deep else 10)
                    body = f"{narrative}\n\n{table_text}" if narrative else table_text
                    return AnalystAnswer(
                        f"**Check before relying on this.** The review flagged: {detail}.\n\n{body}",
                        route="sql", source="llm", resolved="custom query", tables=checked.tables,
                        sql=checked.sql, table=result.as_table(), notes=notes + [f"Review: {detail}."],
                        verified=False, state={"tables": checked.tables, "sql": checked.sql})
        return AnalystAnswer(f"I could not write a valid query for this ({last_error}). Try rephrasing, or ask "
                             "about a specific app, metric or time range.", route="error", source="llm",
                             tables=tables)

    def _compare(self, question: str, result: AnalystAnswer) -> None:
        """Comparison questions: computed comparison facts and a written verdict above the rows."""
        if not (_COMPARISON.search(question) and result.table and result.table.get("rows")
                and result.route in ("metric", "proxy", "sql")):
            return
        ex = self.router.extract(question)
        focus = list(ex.apps) + [v for v in (ex.category, result.params.get("category")) if v]
        facts = comparison_facts(result.table, question, focus, (ex.date_from, ex.date_to),
                                 baseline=bool(_BASELINE.search(question)),
                                 comparable_from=self.cat.category_comparable_from)
        if facts:
            self._emit("verify", f"Computed {len(facts)} comparison facts from the {_table_size(result.table)}")
        verdict = self._verdict(question, result, facts) if self.use_llm else ""
        head = ([f"**Answer:** {verdict}"] if verdict else []) + (
            ["Comparison (computed from the rows):\n" + "\n".join(f"- {f}" for f in facts)] if facts else [])
        if head:
            # A proxy's disclaimer ("Verified purchase share is unavailable...") must stay the first thing read.
            lead, _, rest = result.answer.partition("\n\n") if result.route == "proxy" else ("", "", result.answer)
            result.answer = "\n\n".join(([lead] if lead else []) + head + [rest])

    def _verdict(self, question: str, result: AnalystAnswer, facts: list[str]) -> str:
        """The model's direct answer to a comparison, kept only if every number is in the facts or rows."""
        rows = _table_text(result.table, limit=200)
        caveats = " ".join(dict.fromkeys(result.notes)) or "none"
        settings = ", ".join(f"{k} = {v}" for k, v in result.params.items()) or "none"
        try:
            text = self._llm(
                system=COMPARE_SYSTEM.format(home_app=self.s.home_app),
                user=(f"Question: {question}\n\nComputed comparison facts:\n"
                      + ("\n".join(f"- {f}" for f in facts) or "(none)")
                      + f"\n\nAnalysis settings: {settings}\nCaveats: {caveats}"
                      + f"\n\nResult rows ({_table_size(result.table)}):\n{rows}"),
                model=config.ANSWER_MODEL, max_tokens=300, cap=20, reserve=4).strip()
        except (llm.OllamaError, ThinkingTimeout):
            return ""
        text = re.sub(r"^\W*answer\W*", "", text, flags=re.I).strip()
        if not text or "|" in text or not re.search(r"[.!?)%\"']\s*$", text):
            self._emit("verify", "Comparison verdict dropped: it was cut off or tried to copy the table", ok=False)
            return ""
        bad = grounding.ungrounded_numbers(text, ["\n".join(facts), rows, caveats, settings], question)
        if bad:
            self._emit("verify", f"Comparison verdict rejected, numbers not in the facts or rows: {', '.join(bad[:5])}",
                       ok=False)
            return ""
        self._emit("verify", "Comparison verdict checked: every number matches the computed facts or rows")
        return text

    def _narrate(self, question: str, result: Result, notes: list[str]) -> tuple[str, bool]:
        if not result.rows or not self.use_llm:
            return "", True
        evidence = result.to_text(self.s.max_rows)
        try:
            text = self._llm(system=NARRATIVE_SYSTEM.format(home_app=self.s.home_app),
                             user=f"Question: {question}\n\nResult:\n{evidence}\n\nCaveats: {' '.join(notes) or 'none'}",
                             model=config.ANSWER_MODEL, max_tokens=300, cap=20).strip()
        except (llm.OllamaError, ThinkingTimeout):
            return "", True
        if "|" in text or not re.search(r"[.!?)%\"']\s*$", text):
            self._emit("verify", "Summary dropped: it was cut off or tried to copy the table", ok=False)
            return "", True
        bad = grounding.ungrounded_numbers(text, [evidence], question)
        if bad:
            self._emit("verify", f"Summary rejected, numbers not in the result: {', '.join(bad[:5])}", ok=False)
            return "", True
        self._emit("verify", "Summary checked: every number matches the result")
        return text, True

    def _llm_plan(self, question: str, history: list[dict], route: Route) -> dict | None:
        self._emit("understand", "Asking the model which analysis fits")
        raw = self._llm(
            system=PLAN_SYSTEM.format(home_app=self.s.home_app, first_day=self.cat.date_range[0],
                                      last_day=self.cat.date_range[1], metrics=self.cat.metric_menu(),
                                      tables=self.cat.table_menu(), glossary=self.cat.glossary[:4000],
                                      apps=", ".join(self.cat.apps)),
            user=self._history_text(history) + f"Question: {question}\nKeyword guess: "
                 f"{route.metric.name if route.metric else 'none'} {route.params}",
            model=config.AGENT_MODEL, max_tokens=300, schema=PLAN_SCHEMA, cap=20)
        try:
            return parse_json(raw)
        except (ValueError, json.JSONDecodeError):
            return None

    def _openai_review_sql(self, question: str, sql: str, tables: list[str], interpretation: str = "") -> list[str]:
        """Return OpenAI's semantic issues for a proposed Deep query."""
        raw = self._llm(
            system=SQL_REVIEW_SYSTEM.format(
                rules=DEEP_SQL_RULES, time_rules=TIME_GRAIN_RULES.format(max_rows=self.s.max_rows)),
            user=(f"Question: {question}\n"
                  + (f"Lead analyst's interpretation (authoritative for ambiguous wording): {interpretation}\n"
                     if interpretation else "")
                  + f"Business terms:\n{self.cat.glossary[:4000]}\n\nAllowed table documentation:\n"
                  f"{self.cat.schema_card(tables)}\n\n"
                  f"Active data coverage: {self.cat.date_range[0]} to {self.cat.date_range[1]}.\n\n"
                  f"Proposed SQL:\n{sql}"),
            model=config.AGENT_MODEL, max_tokens=350, schema=SQL_REVIEW_SCHEMA, cap=15, reserve=5)
        review = parse_json(raw)
        minor = [str(x) for x in review.get("minor_issues") or [] if str(x).strip()]
        if minor:
            self._emit("verify", "OpenAI minor review notes (not blocking): " + "; ".join(minor[:3]))
        return [str(x) for x in review.get("blocking_issues") or [] if str(x).strip()]

    def _openai_verify_answer(self, question: str, result: AnalystAnswer, interpretation: str = "") -> list[str]:
        """Return OpenAI's grounding/coverage issues for one completed Deep answer."""
        table = result.table or {}
        total = table.get("total_rows", len(table.get("rows") or []))
        raw = self._llm(
            system=ANSWER_REVIEW_SYSTEM,
            user=(f"Question: {question}\n"
                  + f"How this is displayed: the complete {total}-row result is rendered as its own table "
                    "directly below the answer text, which the user reads together. The answer text "
                    "deliberately summarises and lists only its first lines. That is the intended format: "
                    "never report it as truncated, incomplete or missing rows.\n"
                  + (f"Lead analyst's interpretation (authoritative for ambiguous wording): {interpretation}\n"
                     if interpretation else "")
                  + (f"Data caveats from the automated data-quality checks (facts about the data): "
                     f"{' '.join(dict.fromkeys(result.notes))}\n" if result.notes else "")
                  + f"\nExecuted SQL:\n{result.sql or '(vetted metric query)'}"
                  f"\n\nResult rows ({_table_size(result.table)}):\n"
                  f"{_table_text(result.table, limit=self.s.max_rows)[:60000]}"
                  f"\n\nProposed answer:\n{result.answer[:30000]}"),
            model=config.ANSWER_MODEL, max_tokens=400, schema=ANSWER_REVIEW_SCHEMA, cap=20, reserve=3)
        review = parse_json(raw)
        if review.get("valid"):
            return []
        issues = [str(x) for x in review.get("issues") or []]
        if not table.get("truncated"):
            kept = [i for i in issues if not _COMPLETENESS_COMPLAINT.search(i)]
            if len(kept) < len(issues):
                self._emit("verify", "Ignoring the review's completeness objection: every row of the "
                                     "result is in the table shown to the user")
            issues = kept
        return issues

    # ========================================================== deep mode
    def _deep(self, question: str, history: list[dict], original: str = "") -> AnalystAnswer:
        budget = _RUN.get().budget
        parts: list[str] = []
        routes: dict[str, Route] = {}
        custom_tables: dict[str, list[str]] = {}
        cannot: list[dict] = []
        results: list[tuple[str, AnalystAnswer]] = []
        timed_out = None
        if config.LLM_PROVIDER != "openai":
            return AnalystAnswer(
                "Deep analysis requires the OpenAI provider. Select OpenAI in Connection settings, "
                "test the connection, and try again.", route="error", source="rules", resolved=question,
                depth="deep", verified=False)
        try:
            self._emit("understand", "OpenAI is choosing the complete Deep-analysis strategy")
            decision = self._deep_plan(question, history)
            action = decision.get("action")
            interpretation = str(decision.get("interpretation") or "").strip()
            self._emit("plan", "OpenAI decision: " + str(decision.get("reason") or action))
            if interpretation:
                self._emit("plan", "OpenAI interpretation: " + interpretation)
            # The keyword router has to drop a metric that cannot produce the requested shape; Deep
            # mode checked only that the metric existed, so OpenAI could pick "Top users by visits"
            # for "and add user id" and answer with a different table altogether.
            if action == "metric":
                picked = self.cat.metrics.get(str(decision.get("metric") or ""))
                if picked is not None and picked.available and _grain_mismatch(question, picked, original):
                    self._emit("plan", f"'{picked.title}' does not return what was asked for; "
                                       "writing a custom query instead", ok=False)
                    action = "custom_sql"
                    decision = {**decision, "tables": decision.get("tables") or list(picked.tables)}
            if action == "unsupported":
                cannot = [{"part": question,
                           "missing": decision.get("message") or decision.get("reason") or "required data"}]
            elif action == "metric":
                metric = self.cat.metrics.get(str(decision.get("metric") or ""))
                if metric is None or not metric.available:
                    raise ValueError("OpenAI selected an unavailable metric")
                params, focus, per_app = self._model_metric_params(metric, decision.get("params"), question)
                if per_app:
                    # A single-app analysis asked for several apps: one vetted run per app.
                    for app in per_app[:4]:
                        label = f"{metric.title} for {app}"
                        parts.append(label)
                        routes[label] = Route("metric", metric, {**params, "app": app}, metric.tables, 1.0,
                                              "OpenAI Deep strategy, one run per app", source="llm")
                else:
                    parts = [question]
                    routes[question] = Route("metric", metric, params, metric.tables, 1.0,
                                             "OpenAI Deep strategy", source="llm", focus=focus)
            elif action == "custom_sql":
                parts = [question]
                selected = [str(t) for t in decision.get("tables") or []
                            if str(t) in self.cat.allowed_tables]
                if not selected:
                    raise ValueError("OpenAI did not select tables for the custom analysis")
                # Journey events are only meaningful with their parent journey (period bucketing,
                # dated selection, close_reason), so the parent table is always in scope with them.
                if ("cross_app_journey_event" in selected and "cross_app_journey" not in selected
                        and "cross_app_journey" in self.cat.allowed_tables):
                    selected.insert(selected.index("cross_app_journey_event"), "cross_app_journey")
                custom_tables[question] = selected[:5]
            elif action == "decompose":
                parts = [str(p).strip() for p in decision.get("parts") or []
                         if str(p).strip()][:4]
                if not parts:
                    raise ValueError("OpenAI returned an empty Deep-analysis plan")
            else:
                raise ValueError("OpenAI returned an invalid Deep-analysis action")
            parts = parts[:4]
            self._emit("plan", "Plan: " + " | ".join(f"{i}. {p}" for i, p in enumerate(parts, 1)))
            for c in cannot:
                self._emit("plan", f"Not in the data: {c['part']} ({c.get('missing', '')})", ok=False)
            if cannot:
                self._emit("write", "The requested data is unavailable; suggesting available analyses", ok=False)
                return AnalystAnswer(
                    "Not available from the current data. The dataset does not contain the fields "
                    "needed for that analysis.\n\nTry one of the available questions below.",
                    route="unsupported", source="llm" if self.use_llm else "rules",
                    resolved=question, depth="deep", verified=False, reason=str(cannot[0].get("missing") or ""),
                    suggested_questions=self._available_questions())
            for part in parts:
                budget.check(reserve=6)
                self._emit("query", f"Part {len(results) + 1}: {part}")
                planned = routes.get(part)
                if part in custom_tables:
                    tables = custom_tables[part]
                    forced = Route("sql", tables=tables, confidence=1.0,
                                   reason="OpenAI Deep custom query", source="llm")
                    self._emit("plan", "OpenAI selected a custom joined query")
                    sub = self._free_sql(part, history, tables, forced, [],
                                         strict_deep=True, interpretation=interpretation)
                    hint = None if sub.table and sub.table.get("rows") else self._hint_route(part, original)
                    if hint is not None:
                        # OpenAI could not settle a custom query: use the closest vetted (tested) analysis;
                        # OpenAI still verifies its answer below.
                        self._emit("plan", "No custom query was approved; using the closest vetted analysis: "
                                           + hint.metric.title, ok=False)
                        sub = self._run_metric(hint.metric, hint.params, hint.focus, list(hint.notes), "llm",
                                               part, narrate=False)
                else:
                    # For decomposed parts, do not seed the deterministic router: OpenAI chooses
                    # the vetted metric or custom tables for each independent part.
                    openai_route = planned or Route("sql", tables=[], confidence=0.0,
                                                    reason="OpenAI must choose", source="llm")
                    sub = self._answer_one(part, history, {}, openai_route, allow_narrative=False,
                                           original=original)
                self._compare(part, sub)
                if interpretation and len(parts) == 1:
                    sub.notes.insert(0, f"Interpreted as: {interpretation}")
                if sub.table and sub.table.get("rows"):
                    issues = self._openai_verify_answer(part, sub, interpretation)
                    summary, sep, table_only = sub.answer.partition("\n\nResult:\n")
                    if issues and sep and summary.strip():
                        # The rendered table is copied from the rows; only the model's summary sentence can
                        # misstate a comparison. Drop it and verify the table alone before touching the SQL.
                        self._emit("verify", "OpenAI rejected the summary: " + "; ".join(issues[:2])
                                   + ". Re-checking the table without it", ok=False)
                        sub.answer = "Result:\n" + table_only
                        issues = self._openai_verify_answer(part, sub, interpretation)
                    if issues and sub.route == "sql" and budget.remaining() > 18:
                        self._emit("verify", "OpenAI rejected the answer: " + "; ".join(issues[:3])
                                   + ". Repairing the query once", ok=False)
                        repair_tables = custom_tables.get(part) or sub.tables
                        repaired = self._free_sql(
                            part, history, repair_tables,
                            Route("sql", tables=repair_tables, confidence=1.0,
                                  reason="OpenAI verification repair", source="llm"),
                            [], strict_deep=part in custom_tables,
                            feedback=(f"\n\nA previous query:\n{sub.sql}\nran, but OpenAI verification of its "
                                      "answer found: " + "; ".join(issues[:4])
                                      + "\nWrite a corrected query and reply with JSON."))
                        if repaired.table and repaired.table.get("rows"):
                            sub = repaired
                            self._compare(part, sub)
                            issues = self._openai_verify_answer(part, sub, interpretation)
                    if issues:
                        sub.verified = False
                        sub.notes.extend("OpenAI verification: " + issue for issue in issues)
                        self._emit("verify", "OpenAI rejected the answer: " + "; ".join(issues[:3]), ok=False)
                    else:
                        self._emit("verify", "OpenAI verified the answer against the query result")
                results.append((part, sub))
        except ThinkingTimeout as exc:
            timed_out = str(exc)
            self._emit("stop", f"Stopped: {exc}. Using the parts finished so far.", ok=False)

        good = [(p, r) for p, r in results
                if r.route in ("metric", "proxy", "sql") and r.table is not None
                and bool(r.table.get("rows")) and r.verified]
        # Rows that ran read-only but that OpenAI did not approve are still shown, clearly flagged,
        # instead of discarding a complete result table.
        flagged = [(p, r) for p, r in results
                   if (p, r) not in good and r.route in ("metric", "proxy", "sql") and r.table is not None
                   and bool(r.table.get("rows"))]
        for _, r in flagged:
            if r.answer.startswith("**Check before relying on this."):
                continue          # already carries its own, more specific warning
            issues = [n.removeprefix("OpenAI verification: ") for n in r.notes
                      if n.startswith("OpenAI verification: ")]
            r.answer = ("**Check before relying on this.** OpenAI's review flagged: "
                        + ("; ".join(issue.rstrip(".") for issue in issues[:2]) if issues else "a possible mismatch")
                        + ".\n\n" + r.answer)
        if len(parts) == 1 and len(good) == 1 and not cannot and not timed_out:
            answer = good[0][1]
            answer.depth = "deep"
            self._emit("verify", f"Single-part question: answered directly from the query ({budget.elapsed:.0f}s)")
            return answer
        if len(parts) == 1 and not good and len(flagged) == 1 and not cannot and not timed_out:
            answer = flagged[0][1]
            answer.depth = "deep"
            answer.verified = False
            answer.notes = [n for n in answer.notes if not n.startswith("OpenAI verification: ")]
            return answer
        if len(parts) == 1 and len(results) == 1 and not good and not flagged and not cannot and not timed_out:
            # The only part produced no rows: its own message (a clarification, a database error,
            # an empty result) tells the user why, unlike a generic "could not answer".
            answer = results[0][1]
            answer.depth = "deep"
            answer.verified = False
            return answer
        # Assemble deterministic per-part answers. Numeric-token checks cannot
        # detect entity swaps (for example assigning Flipkart's value to
        # Amazon) or false non-numeric rankings, so synthesized prose cannot be
        # labelled verified safely.
        text = "\n\n".join(
            f"{i}. {p}\n" + (r.answer if (p, r) in good or (p, r) in flagged
                             else f"Not answered: {r.answer.strip() or 'no result'}")
            for i, (p, r) in enumerate(results, 1)) or "I could not answer any part of this question from the data."
        if good:
            self._emit("verify", "Final answer assembled directly from the vetted query results")
        extra = []
        if cannot:
            extra.append("Some requested information is not available in the current data.")
        failed = [p for p, r in results if (p, r) not in good]
        if timed_out:
            extra.append("The analysis stopped before every requested part was completed.")
        first = good[0][1] if good else None
        return AnalystAnswer(
            text + ("\n\n" + "\n".join(extra) if extra else ""),
            route="deep", source="llm" if self.use_llm else "rules",
            resolved=" | ".join(parts), metric=first.metric if first else None,
            tables=list(dict.fromkeys(t for _, r in good for t in r.tables)), sql=first.sql if first else None,
            table=first.table if first else None, timed_out=bool(timed_out), depth="deep",
            verified=bool(good) and not (cannot or failed or timed_out),
            notes=([f"cannot answer: {c['part']}" for c in cannot]
                   + [f"not verified: {part}" for part in failed]
                   + (["analysis timed out before completion"] if timed_out else [])),
            state=first.state if len(good) == 1 and first else {},
            parts=[{"question": p, "understood_as": r.resolved, "answer": r.answer, "sql": r.sql,
                    "table": r.table, "verified": (p, r) in good} for p, r in results])

    def _deep_plan(self, question: str, history: list[dict]) -> dict:
        """OpenAI's strategy JSON, normalised; asks once more when the reply has no usable action."""
        system = DEEP_PLAN_SYSTEM.format(
            metrics=self.cat.metric_menu(), tables=self.cat.table_menu(),
            schema=self.cat.schema_card(sorted(self.cat.allowed_tables), with_notes=False),
            first_day=self.cat.date_range[0], last_day=self.cat.date_range[1], week_start=self._week_start(),
            home_app=self.s.home_app, glossary=self.cat.glossary[:4000])
        user = self._history_text(history) + f"Current question: {question}" + self._route_hint(question)
        decision: dict = {}
        for attempt in range(2):
            raw = self._llm(system=system, user=user, model=config.AGENT_MODEL, max_tokens=700,
                            schema=DEEP_PLAN_SCHEMA, cap=20, reserve=10)
            try:
                decision = _normalise_plan(parse_json(raw), self.cat)
            except (ValueError, json.JSONDecodeError):
                decision = {}
            if decision.get("action") in ("metric", "custom_sql", "decompose", "unsupported"):
                return decision
            self._emit("plan", "OpenAI's strategy had no usable action; asking again", ok=False)
            user += ("\n\nYour previous reply was not a valid strategy. Reply with ONE JSON object whose \"action\" "
                     "is metric, custom_sql, decompose or unsupported, plus reason and interpretation.")
        return decision

    def _model_metric_params(self, metric: MetricDoc, raw, question: str) -> tuple[dict, list[str], list[str]]:
        """A model's parameters for ``metric``: (params, apps to highlight, apps that need one run each).

        Models write "our X vs Meesho and Flipkart" as ONE app parameter holding several apps ("Amazon,
        Meesho, Flipkart" or a list), which matches no app. A by-app metric answers a comparison with
        every app shown and the named ones highlighted, exactly like the rule router; a single-app
        metric is run once per app.
        """
        params = {k: v for k, v in (raw if isinstance(raw, dict) else {}).items() if metric.param(k)}
        _, focus = self.router._params_for(metric, self.router.extract(question), question)
        per_app: list[str] = []
        for p in metric.params:
            if p.type != "app" or p.name not in params:
                continue
            apps = self._app_values(params[p.name])
            if p.name == "app" and metric.focus_parameter == "app" and (len(apps) > 1 or focus):
                params.pop("app")
                focus = list(dict.fromkeys(focus + apps))
            elif p.name == "app" and len(apps) > 1:
                params.pop("app")
                per_app = apps
            elif len(apps) == 1:
                params[p.name] = apps[0]
        if "trend" in metric.name and _BASELINE.search(question):
            # "Always this high, or a spike this week?" needs every day as the baseline, not just that week.
            params.pop("date_from", None)
            params.pop("date_to", None)
        return params, focus, per_app

    def _app_values(self, value) -> list[str]:
        """App names in a model parameter: a list, or a string such as "Amazon, Meesho and Flipkart"."""
        known = {a.lower(): a for a in self.cat.apps}
        home = self.cat.home_app or "HOME"
        names: list[str] = []
        for item in value if isinstance(value, (list, tuple)) else [value]:
            text = str(item or "").strip()
            pieces = [text] if text.lower() in known else re.split(
                r"\s*(?:,|;|/|&|\band\b|\bvs\.?|\bversus\b)\s*", text, flags=re.I)
            for piece in (x.strip() for x in pieces):
                if piece.lower() in ("home", "we", "us", "our", "ours", "our app"):
                    names.append(home)
                elif piece:
                    names.append(known.get(piece.lower(), piece))     # unknown names fail validation later
        return list(dict.fromkeys(names))

    def _week_start(self) -> str:
        """Monday of the latest data week: the same "this week" the router uses for dates and comparisons."""
        anchor = self.router._anchor()
        return (anchor - timedelta(days=anchor.weekday())).isoformat()

    def _hint_route(self, question: str, original: str = "") -> Route | None:
        """The keyword router's vetted metric for a question, when it plausibly answers it."""
        route, _ = self.router.route(question)
        metric = route.metric
        if (metric is None or route.confidence < 0.35 or not metric.available
                or metric.name in self.cat.metric_issues or _grain_mismatch(question, metric, original)):
            return None
        return route

    def _route_hint(self, question: str) -> str:
        route = self._hint_route(question)
        if route is None:
            return ""
        return (f"\nKeyword-match hint: vetted metric '{route.metric.name}' ({route.metric.title}), "
                f"params {route.params}, confidence {route.confidence:.2f}.")

    def _deep_query_tables(self, question: str, route: Route) -> list[str]:
        """Choose rich but bounded table context for a Deep-mode joined query."""
        preferred: list[str] = []
        low = question.lower()
        if re.search(r"\b(journey|journeys|purchase|purchased|multi[- ]app|search|user)\b", low):
            preferred.extend(("cross_app_journey", "cross_app_journey_event"))
        preferred.extend(route.tables)
        if re.search(r"\b(session|sessions|screen|screens)\b", low):
            preferred.append("session_master")
        preferred.append("app_master")
        return [name for name in dict.fromkeys(preferred)
                if name in self.cat.allowed_tables][:5]

    def _normalise_deep_part(self, item) -> tuple[str, Route | None] | None:
        """Accept string plans and defensively handle model metric objects.

        Some local models return ``{"part": metric_name, "params": ...}``
        despite the requested schema. Stringifying that dictionary previously
        turned the word ``part`` into a bogus category filter.
        """
        if isinstance(item, str):
            text = item.strip()
            return (text, None) if text else None
        if not isinstance(item, dict):
            return None
        question = str(item.get("question") or "").strip()
        metric_name = str(item.get("metric") or item.get("part") or "").strip()
        metric = self.cat.metrics.get(metric_name)
        if metric is None:
            return (question, None) if question else None
        params = {k: (self.s.home_app if isinstance(v, str) and v.upper() == "HOME" else v)
                  for k, v in (item.get("params") or {}).items() if metric.param(k)}
        label = question or metric.title
        if not question:
            qualifiers = [str(params[k]) for k in ("app", "from_app", "to_app") if params.get(k)]
            if qualifiers:
                label += " for " + " → ".join(qualifiers)
        return label, Route("metric", metric, params, metric.tables, 1.0,
                            "structured deep-plan metric", source="llm")

    def _available_questions(self, limit: int = 4) -> list[str]:
        """Offer only analyses whose required tables are currently ready."""
        questions: list[str] = []
        for name, metric in self.cat.metrics.items():
            if not metric.available or name in self.cat.metric_issues:
                continue
            title = str(metric.title).strip()
            if title and title not in questions:
                questions.append(title)
            if len(questions) >= limit:
                break
        return questions

    def _split_by_rules(self, question: str, route: Route | None = None) -> list[str]:
        return [text for text, _ in self._rule_parts(question, route)]

    def _rule_parts(self, question: str, route: Route | None = None) -> list[tuple[str, Route | None]]:
        """Split without the model: bullet/line/'?' parts, or one part per requested dimension."""
        parts = split_parts_without_llm(question)
        if len(parts) > 1:
            return [(p, None) for p in parts]
        if route is None:
            route, _ = self.router.route(question)
        dim = route.metric.param("dimension") if route.metric else None
        if dim:
            low = question.lower()
            names = [("device", "device_model"), ("time of day", "hour_of_day"), ("hour", "hour_of_day"),
                     ("age", "age_group"), ("gender", "gender"), ("zone", "zone"), ("state", "state"),
                     ("loyalty", "tier"), ("city", "city")]
            wanted = []
            for word, value in names:
                if word == "city" and "city tier" in low:
                    continue                       # city tier is not recorded; do not substitute city
                if re.search(rf"\b{word}\b", low) and value in dim.values and value not in wanted:
                    wanted.append(value)
            if len(wanted) > 1:
                out = []
                for w in wanted:
                    params = {**route.params, "dimension": w}
                    label = f"{route.metric.title.split(' by ')[0]} by {w.replace('_', ' ')}" + (
                        f" ({params['app']})" if params.get("app") else "")
                    out.append((label, Route("metric", route.metric, params, route.metric.tables, 1.0,
                                             "split by dimension", focus=route.focus)))
                return out
        return [(question, None)]

    def _simplify(self, result: AnalystAnswer) -> AnalystAnswer:
        """Plain-language intro with NO numbers above the exact figures (it cannot change them)."""
        if not self.use_llm:
            return result
        try:
            text = self._llm(system=SIMPLE_SYSTEM, user=result.answer[:3000], model=config.ANSWER_MODEL,
                             max_tokens=160, cap=15).strip()
        except (llm.OllamaError, ThinkingTimeout):
            return result
        problems = grounding.plain_summary_problems(text)
        if text and not problems:
            result.answer = f"In simple words: {text}\n\n{result.answer}"
        else:
            self._emit("verify", f"Simple intro rejected ({', '.join(problems)})", ok=False)
        return result

    # ====================================================== conversation
    def _contextualize(self, question: str, history: list[dict], memory: Memory) -> tuple[str, bool]:
        """Rewrite a follow-up into a question that stands on its own.

        Doing this once, before routing, means the router, Deep mode and the SQL writer all receive
        a complete question and need no conversation handling of their own - which is why Deep mode
        used to lose follow-ups entirely.

        This runs on every turn that has history behind it. Deciding in advance whether a message
        "looks like" a follow-up kept missing ones that carried no pronoun at all ("what is the
        result with 24 hr window?"), and the cost of asking is one small call that returns the
        question unchanged when it already stands alone. Returns (question, continues_previous).
        """
        if not memory:
            return question, False
        # "the second one" resolves from the stored result rows without a model call.
        entity = memory.ordinal_entity(question)
        if not self.use_llm:
            if entity:
                self._emit("understand", f"Reading '{question}' as being about {entity}")
                return f"{memory.last.get('question', question)} for {entity}", True
            return question, needs_context(question, True)
        try:
            raw = self._llm(
                system=RESOLVE_SYSTEM,
                user=memory.prompt() + self._recent_text(history)
                     + f"New message: {question}\n"
                     + (f"(An ordinal in it points to: {entity})\n" if entity else "")
                     + "Rewrite it as a standalone question.",
                model=config.AGENT_MODEL, max_tokens=200, schema=RESOLVE_SCHEMA, cap=15, reserve=2)
            plan = parse_json(raw)
        except (llm.OllamaError, ThinkingTimeout, ValueError, json.JSONDecodeError):
            self._emit("understand", "Could not use the earlier turns; reading the question as written", ok=False)
            return question, False
        rewritten = str(plan.get("question") or "").strip()
        # A rewrite that drops the question or balloons into an instruction is not trustworthy.
        if not rewritten or len(rewritten) > 400 or not plan.get("used_context", True):
            return question, False
        if rewritten.lower() == question.lower().strip():
            return question, False
        self._emit("understand", f"Follow-up resolved from the conversation: {rewritten}")
        # Only a rewrite that actually pulled something in counts as continuing the previous turn.
        return rewritten, True

    # ================================================================ utils
    def _recent_text(self, history: list[dict]) -> str:
        """The last couple of raw turns, for wording the structured memory does not capture."""
        if not history:
            return ""
        lines = [f"{'User' if h.get('role') == 'user' else 'Assistant'}: {str(h.get('content'))[:200]}"
                 for h in history[-2:]]
        return "Last messages verbatim:\n" + "\n".join(lines) + "\n\n"

    def _history_text(self, history: list[dict], memory: Memory | None = None) -> str:
        """Conversation context for a prompt.

        The structured memory is preferred: it carries the previous result's columns and real rows,
        where the rendered answer text was clipped to 300 characters and lost them.
        """
        if memory is None:
            run = _RUN.get()
            memory = run.memory if run is not None else None
        if memory is not None and memory:
            return memory.prompt()
        if not history:
            return ""
        lines = [f"{'User' if h.get('role') == 'user' else 'Assistant'}: {str(h.get('content'))[:300]}"
                 for h in history[-6:]]
        return "Recent conversation:\n" + "\n".join(lines) + "\n\n"

    def _emit(self, stage: str, text: str, ok: bool = True) -> None:
        run = _RUN.get()
        if run is None:
            return
        step = ThinkingStep(stage, text, int(run.budget.elapsed * 1000), ok)
        run.trace.append(step)
        if run.on_event:
            try:
                run.on_event({"type": "thinking", **step.as_dict()})
            except Exception:
                log.debug("on_event failed", exc_info=True)

    def _llm(self, reserve: float = 1.0, cap: float | None = None, **kwargs) -> str:
        if not self.use_llm:
            raise llm.OllamaError("language model disabled")
        run = _RUN.get()
        if run is None:
            return llm.complete(**kwargs)
        if run.budget.no_llm:
            raise ThinkingTimeout("the thinking limit was reached")
        run.budget.check(reserve)
        allowed = run.budget.remaining() - run.budget.scaled(reserve)
        if cap:
            allowed = min(allowed, cap)
        future = _POOL.submit(llm.complete, timeout=allowed, **kwargs)
        while True:
            try:
                return future.result(timeout=0.25)
            except FutureTimeout:
                if run.budget.cancelled or run.budget.remaining() <= 0:
                    future.cancel()
                    run.budget.check(0)
                    raise ThinkingTimeout("the thinking limit was reached")
            except llm.OllamaError:
                run.budget.check(reserve)
                raise


# --------------------------------------------------------------------------- #
# Deterministic rendering
# --------------------------------------------------------------------------- #

def _fmt(col: str, value) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        text = f"{value:,.1f}".rstrip("0").rstrip(".") if isinstance(value, float) and not float(value).is_integer() \
            else f"{int(value):,}"
        return text + ("%" if col.endswith("_pct") else "")
    if isinstance(value, list):
        return ", ".join(str(v) for v in value[:5]) + ("…" if len(value) > 5 else "")
    return str(value)


def _label(col: str) -> str:
    return col.replace("_pct", "").replace("_", " ")


def _table_size(table: dict | None) -> str:
    if not table:
        return "no rows"
    total = table.get("total_rows", len(table.get("rows", [])))
    return f"{total} rows" + (", more rows exist" if table.get("truncated") else ", all rows")


def _table_text(table: dict | None, limit: int = 80) -> str:
    """Every row (up to ``limit``) for the model, so it never mistakes a shortened list for missing data."""
    if not table or not table.get("rows"):
        return "(empty)"
    cols = table["columns"]
    lines = [" | ".join(cols)] + [" | ".join("" if v is None else str(v) for v in r) for r in table["rows"][:limit]]
    if len(table["rows"]) > limit:
        lines.append(f"... {len(table['rows']) - limit} more rows")
    return "\n".join(lines)


_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]00:00(?::00)?(?:[+-]\d{2}(?::?\d{2})?|Z)?)?$")


def _is_period_column(result: Result) -> bool:
    """First column holds calendar periods (day / week / month), so every row must be shown."""
    if not result.columns or not result.rows:
        return False
    if result.columns[0].lower() in _PERIOD_COLUMNS:
        return True
    sample = [r[0] for r in result.rows[:20] if r and r[0] is not None]
    return bool(sample) and all(hasattr(v, "isoformat") or _ISO_DAY.match(str(v)) for v in sample)


def _period_label(value) -> str:
    text = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return text[:10] if _ISO_DAY.match(text) else text


def _render_periods(result: Result, head: str, highlight: list[str] | None = None) -> str:
    """Complete period breakdown. With a group column (e.g. app), one line per period lists every group."""
    cols, rows = result.columns, result.rows
    key = cols[0]
    group_idx = next((i for i in range(1, len(cols))
                      if all(r[i] is None or (isinstance(r[i], str) and not _ISO_DAY.match(r[i])) for r in rows)
                      and cols[i].lower() not in _PERIOD_COLUMNS), None)
    extra = [i for i in range(1, len(cols)) if i != group_idx and (    # week first/last day
        cols[i].lower() in _PERIOD_COLUMNS
        or (any(r[i] is not None for r in rows)
            and all(v is None or hasattr(v, "isoformat") or _ISO_DAY.match(str(v)) for v in (r[i] for r in rows))))]
    measures = [i for i in range(1, len(cols)) if i != group_idx and i not in extra]
    prefix = "Week of " if key.lower().startswith("week") else ""
    lines = [head]
    if group_idx is None:
        for r in rows:
            span = f" ({' to '.join(_period_label(r[i]) for i in extra)})" if extra else ""
            lines.append(f"{prefix}{_period_label(r[0])}{span}: "
                         + ", ".join(f"{_label(cols[i])} {_fmt(cols[i], r[i])}" for i in measures))
    else:
        periods: dict[str, list] = {}
        for r in rows:
            periods.setdefault(_period_label(r[0]), []).append(r)
        groups = {str(r[group_idx]) for r in rows}
        # A named entity (e.g. the asked-about category) is listed alone; every row stays in the table.
        wanted = {g for g in groups if any(h and h.lower() in g.lower() for h in highlight or [])}
        missing = [label for label, members in periods.items()
                   if wanted and not any(str(m[group_idx]) in wanted for m in members)]
        for label, members in periods.items():
            members = [m for m in members if str(m[group_idx]) in wanted] if wanted else members
            if not members:
                continue
            span = ""
            if extra:
                firsts = [_period_label(m[extra[0]]) for m in members if m[extra[0]] is not None]
                lasts = [_period_label(m[extra[-1]]) for m in members if m[extra[-1]] is not None]
                if firsts and lasts:
                    span = f" ({min(firsts)} to {max(lasts)})"
            # Blank line first: in Markdown a line right after a list item continues that item.
            lines.extend(["", f"{prefix}{label}{span}:"])
            for m in members:
                lines.append(f"  - {m[group_idx] if m[group_idx] is not None else 'Unknown'}: "
                             + ", ".join(f"{_label(cols[i])} {_fmt(cols[i], m[i])}" for i in measures))
        if wanted:
            if missing:
                lines.extend(["", f"No rows for {', '.join(sorted(wanted))} on: {', '.join(missing)}."])
            lines.extend(["", f"Listing {', '.join(sorted(wanted))} only; the table has all {len(groups)} "
                              f"{_label(cols[group_idx])} values for every {cols[0].replace('_', ' ')}."])
        else:
            lines.extend(["", f"{len(periods)} periods x {len(groups)} {_label(cols[group_idx])} values, all rows shown."])
    if result.truncated:
        lines.append("The query hit the row limit; narrow the date range or the apps to see every period.")
    return "\n".join(lines)


def render_result(result: Result, title: str, detail: str, focus: list[str], max_lines: int = 15,
                  ahead: bool = False, hours: bool = False, highlight: list[str] | None = None) -> str:
    head = f"{title}" + (f" ({detail})" if detail else "") + ":"
    if not result.rows:
        return f"{head}\nNo rows matched. The filters may be too narrow for the data available."
    if not hours and _is_period_column(result) and len(result.columns) > 1:
        return _render_periods(result, head, highlight)
    cols = result.columns
    lines = [head]
    if len(result.rows) == 1 and len(cols) > 2:
        lines.append(" · ".join(f"{_label(c)}: {_fmt(c, v)}" for c, v in zip(cols, result.rows[0])))
        return "\n".join(lines)
    key, measures = cols[0], cols[1:]
    rows = result.dicts()
    # A rank only means something for the measure the rows are actually sorted by.
    order_by = next((c for c in measures if all(isinstance(r[c], (int, float)) for r in rows)
                     and all(a[c] >= b[c] for a, b in zip(rows, rows[1:]))), None)
    for app in focus:
        idx = next((i for i, r in enumerate(rows) if str(r[key]).lower() == app.lower()), None)
        if idx is None:
            lines.append(f"{app}: no rows in this result.")
            continue
        r = rows[idx]
        figures = ", ".join(f"{_label(c)} {_fmt(c, r[c])}" for c in measures)
        lines.append(f"{app}: {figures}" + (f" - ranked {idx + 1} of {len(rows)}{'+' if result.truncated else ''} "
                                            f"by {_label(order_by)}" if order_by else "") + ".")
        if ahead and 0 < idx <= 5 and len(focus) == 1:
            lines.append("Ahead of " + app + ": " + ", ".join(
                f"{rows[i][key]} ({_fmt(measures[0], rows[i][measures[0]])}"
                + (f", {_fmt(measures[-1], rows[i][measures[-1]])}" if len(measures) > 1 else "") + ")"
                for i in range(idx)) + ".")
    if focus:
        lines.append("")
    if len(rows) <= 30:
        max_lines = max(max_lines, len(rows))          # short lists are shown in full
    for i, r in enumerate(rows[:max_lines], 1):
        label = _fmt(key, r[key])
        if hours and str(r[key]).isdigit():
            label = f"{int(r[key]):02d}:00-{int(r[key]):02d}:59"
        lines.append(f"{i}. {label}: " + ", ".join(f"{_label(c)} {_fmt(c, r[c])}" for c in measures))
    if len(rows) > max_lines or result.truncated:
        lines.append(f"Showing {min(max_lines, len(rows))} of {len(rows)}{'+' if result.truncated else ''} rows; "
                     "the complete table is displayed below this answer.")
    return "\n".join(lines)
