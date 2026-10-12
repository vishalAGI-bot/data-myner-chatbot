"""Question understanding with OpenAI, live, through a tool call.

With the OpenAI provider, no keyword list or regular expression decides what a question means. One forced call
of the ``understand_question`` tool reads the latest message with the conversation and the live facts of the
database (data coverage, apps, sub-apps, categories, vetted analyses, tables, business terms) and returns:
the question rephrased so it stands on its own, what kind of question it is, who "we" is, the period as dates,
the apps, categories, splits and minimum group size, and the vetted analysis or tables that answer it.

It also breaks a long request down - main goal, individual questions, constraints, expected output - and marks
a part unclear only when an ambiguity would change the answer and no default settles it. Each unclear part comes
with one focused question, why it matters, and 2-4 options drawn from the user's words and the live data.

Code only validates that reading against the live data - app, category, analysis and table names must exist,
dates must be real - and never reinterprets the words itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

INTENTS = ("analysis", "multi_part", "brief", "clarify", "unsupported", "out_of_scope")
GRAINS = ("day", "week", "month")
MAX_PARTS = 12                    # questions read from one message; more are counted and reported, never dropped
PART_STATUSES = ("answer", "clarify", "unsupported", "out_of_scope")
# Measures a brief can be asked to analyse; the brief says which it covers and how.
MEASURES = ("visitors", "purchasers", "conversion", "retention", "cross_app_switching", "purchase_share",
            "order_value", "time_to_purchase")
MAX_CLARIFICATIONS = 3
_OTHER = re.compile(r"^\s*(other|others|something else|none of (these|the above))\b", re.I)    # the UI adds "Other"

TOOL_NAME = "understand_question"
TOOL_DESCRIPTION = ("Record how the user's latest message must be understood: the question rephrased so it stands "
                    "on its own, its kind, and the apps, period, categories, splits and analysis it refers to.")

PARAMETERS = {
    "type": "object",
    "properties": {
        "rephrased_question": {
            "type": "string",
            "description": "The latest message as ONE standalone, explicit question: app names instead of we/us/our, "
                           "the measure, the exact period as ISO dates, the filters and the splits. Typos fixed. "
                           "For a brief or several asks, one sentence covering all of it."},
        "intent": {"type": "string", "enum": list(INTENTS)},
        "goal": {"type": "string", "description": "The user's main goal, in one sentence."},
        "parts": {"type": "array", "maxItems": MAX_PARTS, "items": {"type": "object", "properties": {
            "question": {"type": "string", "description": "One individual question, standalone and explicit."},
            "status": {"type": "string", "enum": list(PART_STATUSES),
                       "description": "answer = can be answered now; clarify = needs the user's input (add a "
                                      "clarification for it); unsupported = the data does not hold it; "
                                      "out_of_scope = not about this data."},
            "reason": {"type": "string", "description": "unsupported / out_of_scope: why, in one sentence."},
            "depends_on": {"type": "array", "items": {"type": "integer"},
                           "description": "Earlier part numbers whose RESULT this part needs."},
            "kind": {"type": "string", "enum": ["analysis", "brief"]},
            "metric": {"type": "string", "description": "Vetted analysis that answers this part exactly, or empty."},
            "metric_params": {"type": "object", "description": "Its parameters, with this part's own period."},
            "tables": {"type": "array", "items": {"type": "string"},
                       "description": "Tables a custom query needs when no vetted analysis fits."},
            "time_grain": {"type": "string", "enum": ["", *GRAINS]},
            "per_app": {"type": "boolean"}, "per_user": {"type": "boolean"}},
            "required": ["question", "status"]},
            "description": "Every individual question in the message, in order; a single question is one part."},
        "more_questions": {"type": "integer",
                           "description": "How many further questions the message has beyond the parts listed."},
        "constraints": {"type": "array", "items": {"type": "string"},
                        "description": "Conditions the answer must respect: period, apps, categories, minimum "
                                       "group size, format."},
        "expected_output": {"type": "string",
                            "description": "What the user expects back: a number, a ranking, a table, a "
                                           "comparison, an executive summary, charts ..."},
        "clarifications": {"type": "array", "maxItems": MAX_CLARIFICATIONS, "items": {"type": "object", "properties": {
            "part": {"type": "integer", "description": "1-based number of the part it blocks; 0 = the whole request."},
            "question": {"type": "string", "description": "One focused question to the user."},
            "why": {"type": "string", "description": "One short sentence: what changes depending on the answer."},
            "options": {"type": "array", "minItems": 2, "maxItems": 4, "items": {"type": "string"},
                        "description": "2-4 short, distinct choices from the user's words and the live data."}},
            "required": ["part", "question", "why", "options"]}},
        "message": {"type": "string",
                    "description": "clarify: the one question to ask back. unsupported: what data is missing."},
        "focus_app": {"type": "string", "description": "Who 'we / us / our' is (exact app name)."},
        "apps": {"type": "array", "items": {"type": "string"},
                 "description": "Apps the question is about or compares, exact names."},
        "date_from": {"type": "string", "description": "ISO date, or empty when no period is named."},
        "date_to": {"type": "string", "description": "ISO date, or empty when no period is named."},
        "window_hours": {"type": "integer", "enum": [24, 48, 72],
                         "description": "Purchase window when the question names one."},
        "time_grain": {"type": "string", "enum": ["", *GRAINS],
                       "description": "day / week / month when one row per period is asked for, else empty."},
        "per_app": {"type": "boolean", "description": "One result per app is asked for."},
        "per_user": {"type": "boolean", "description": "A list of individual users is asked for."},
        "adds_columns": {"type": "boolean",
                         "description": "The message asks to add columns to the previous result."},
        "follows_previous": {"type": "boolean",
                             "description": "The message changes or extends the previous question."},
        "categories": {"type": "array", "items": {"type": "object", "properties": {
            "asked": {"type": "string"}, "matches": {"type": "array", "items": {"type": "string"}}},
            "required": ["asked", "matches"]},
            "description": "Each product category the user names, with the names from the category list that "
                           "mean the same products ([] when none does)."},
        "breakdowns": {"type": "array", "items": {"type": "string"},
                       "description": "Brief splits, from the allowed list; crossed pairs joined with '+'."},
        "min_users": {"type": "integer", "description": "Smallest group size allowed (distinct users), 0 if none."},
        "measures": {"type": "array", "items": {"type": "string", "enum": list(MEASURES)},
                     "description": "The measures the user asks to analyse, when they name any."},
        "unavailable": {"type": "array", "items": {"type": "object", "properties": {
            "asked": {"type": "string"}, "reason": {"type": "string"}}},
            "description": "Requested splits or measures the data does not hold."},
        "metric": {"type": "string",
                   "description": "The vetted analysis that answers the question exactly, or empty."},
        "metric_params": {"type": "object", "description": "Parameters for that vetted analysis."},
        "tables": {"type": "array", "items": {"type": "string"},
                   "description": "Tables a custom query needs when no vetted analysis fits."},
    },
    "required": ["rephrased_question", "intent"],
}

SYSTEM = """You are the question-understanding step of a read-only analytics assistant over shopping-app data.
Read the user's LATEST message together with the conversation, and call understand_question once. Do not answer,
analyse or compute anything.

- rephrased_question: rewrite the latest message as one standalone, explicit question. Replace "we / us / our"
  with the app name, earlier references ("it", "those", "the second one", "same for Meesho") with what they point
  to, and relative periods with ISO dates. Fix typos. Keep the user's intent exactly: never add a measure, filter,
  split or period they did not ask for, and never narrow one they left open.
- intent: analysis = one result answers it. multi_part = several independent questions. brief = an executive
  summary or performance review of an app across several splits (what went well, where it stumbled, where
  competitors did better). clarify = no part can be answered before the user replies. unsupported = it needs
  data that does not exist: say what in message. out_of_scope = not about this data (code, project files,
  general knowledge, creative writing).
- Break the request down: goal (the main aim), parts, constraints and expected_output. parts lists EVERY
  individual question in the message, in order (at most {max_parts}; count any further ones in more_questions).
  Never drop or merge unrelated questions, but keep a ranking with several columns, or one comparison of several
  measures, as ONE part; an executive-summary brief is one part with kind "brief". Write each part as a standalone,
  explicit question carrying its own apps, period and filters. For each part give its status, and for status
  answer the vetted metric (with metric_params) or the tables, its time_grain, per_app and per_user, as for the
  whole question. depends_on lists earlier part numbers only when the part needs their RESULT (for example "the
  top categories of the app with the highest conversion in part 1"); such a part says what it takes from them.
- Ask only when it matters. A long message is never by itself a reason to ask. Mark a part clear=false only when
  an ambiguity would materially change its answer AND neither the conversation nor these defaults settle it: no
  period named = the whole coverage; "we" = the asker's company, else {home_app}; purchase window = 24 hours;
  business terms as defined below. Never ask for something the conversation already gives.
- For each unclear part add one clarification: the part number (0 when the whole request depends on it), one
  focused question, why it matters in one short sentence, and 2 to 4 short, distinct options taken from the
  user's words and the live data (app, category, period or measure names). At most 3 clarifications. Do not
  add an "Other" option; the user is always offered one.
- When the latest message answers questions asked earlier in the conversation, apply the answers to the earlier
  request and rephrase that request: the parts still unanswered, plus any part the answers change.
- focus_app: the company the asker says they work for, in this message or earlier in the conversation;
  otherwise {home_app}.
- Period: the data covers {first_day} to {last_day}; "today" is {last_day}, "yesterday" is {yesterday},
  "this week" is {week_start} to {last_day}, "last N days" ends on {last_day}. Leave both dates empty when
  no period is named (the whole coverage is meant).
- metric: name a vetted analysis only when it answers EVERY requested measure, split and period grain, with
  metric_params using its parameter names; otherwise leave it empty and list the tables a custom query needs.
- categories: fill whenever the message names product categories.
- For a brief, also fill breakdowns (from: {breakdowns}; "requested_category" splits by the asked categories;
  crossed pairs such as "requested_category+gender" for "interlocking" or "micro"), min_users, measures and
  unavailable. City tier is not recorded; user_master.tier is a loyalty tier.
- Every result is a table the user can show as a chart, so graphs and charts are always available: never list
  them as unavailable.

Apps: {apps}
Sub-apps (part of their main app, never its competitor): {sub_apps}

Vetted analyses:
{metrics}

Tables:
{tables}

Category list (master category: categories):
{categories}

Business terms:
{glossary}"""


@dataclass
class Part:
    """One individual question of a request, with OpenAI's reading of it (validated against the live data)."""
    number: int
    question: str
    status: str = "answer"                    # answer | clarify | unsupported | out_of_scope
    reason: str = ""
    depends_on: list[int] = field(default_factory=list)
    kind: str = "analysis"                    # analysis | brief
    metric: str | None = None
    metric_params: dict = field(default_factory=dict)
    tables: list[str] = field(default_factory=list)
    time_grain: str | None = None
    per_app: bool = False
    per_user: bool = False


@dataclass
class Understanding:
    question: str
    intent: str
    message: str = ""
    focus_app: str | None = None
    apps: list[str] = field(default_factory=list)
    date_from: str | None = None
    date_to: str | None = None
    window_hours: int | None = None
    time_grain: str | None = None
    per_app: bool = False
    per_user: bool = False
    adds_columns: bool = False
    follows_previous: bool = False
    categories: list[dict] = field(default_factory=list)
    breakdowns: list[str] = field(default_factory=list)
    min_users: int | None = None
    measures: list[str] = field(default_factory=list)
    unavailable: list = field(default_factory=list)
    metric: str | None = None
    metric_params: dict = field(default_factory=dict)
    tables: list[str] = field(default_factory=list)
    goal: str = ""
    parts: list["Part"] = field(default_factory=list)
    more_questions: int = 0                                  # further questions beyond MAX_PARTS
    constraints: list[str] = field(default_factory=list)
    expected_output: str = ""
    clarifications: list[dict] = field(default_factory=list)  # {"part", "question", "why", "options"}
    outside_coverage: bool = False
    dropped: list[str] = field(default_factory=list)       # names OpenAI used that the live data does not have

    @property
    def clear_parts(self) -> list[str]:
        return [p.question for p in self.parts if p.status == "answer"]

    @property
    def blocked(self) -> bool:
        """Nothing can be answered (or reported) before the user replies."""
        return bool(self.clarifications) and (
            (self.intent == "clarify" and not self.parts) or any(c["part"] == 0 for c in self.clarifications)
            or (bool(self.parts) and all(p.status == "clarify" for p in self.parts)))

    @property
    def several(self) -> bool:
        """Several questions, or one that cannot simply be answered: handled part by part."""
        return (len(self.parts) > 1 or self.more_questions > 0
                or any(p.status != "answer" for p in self.parts))

    def request(self) -> dict:
        """The breakdown shown to the user as "Your request"."""
        return {"goal": self.goal, "parts": [p.question for p in self.parts], "constraints": self.constraints,
                "expected_output": self.expected_output}



    def scope_plan(self) -> dict:
        """The brief scope in the shape report.build_scope validates."""
        return {"focus_app": self.focus_app or "", "date_from": self.date_from or "", "date_to": self.date_to or "",
                "categories": self.categories, "breakdowns": self.breakdowns, "min_users": self.min_users or 0,
                "unavailable": self.unavailable}

    def summary(self) -> str:
        bits = [f"intent {self.intent.replace('_', ' ')}"]
        if self.focus_app:
            bits.append(f"'we' = {self.focus_app}")
        if self.apps:
            bits.append("apps " + ", ".join(self.apps))
        if self.date_from or self.date_to:
            bits.append(f"period {self.date_from or '…'} to {self.date_to or '…'}")
        if self.window_hours:
            bits.append(f"{self.window_hours}-hour window")
        if self.time_grain:
            bits.append(f"one row per {self.time_grain}")
        if self.metric:
            bits.append(f"analysis {self.metric}")
        elif self.tables:
            bits.append("custom query on " + ", ".join(self.tables))
        return "; ".join(bits)


def _iso(value) -> str | None:
    try:
        return date.fromisoformat(str(value or "").strip()[:10]).isoformat()
    except ValueError:
        return None


def _part(number: int, raw, *, metrics: dict, tables: set[str], dropped: list[str]) -> Part:
    """One part, kept only where it matches the live data. ``depends_on`` may only name earlier parts."""
    if not isinstance(raw, dict):
        return Part(number, str(raw).strip()[:500])
    status = str(raw.get("status") or "").strip().lower()
    if status not in PART_STATUSES:                       # older readings mark parts only clear / not clear
        status = "clarify" if raw.get("clear") is False else "answer"
    metric = str(raw.get("metric") or "").strip()
    if metric and not (metric in metrics and metrics[metric].available):
        dropped.append(f"analysis '{metric}' (part {number})")
        metric = ""
    grain = str(raw.get("time_grain") or "").lower()
    return Part(
        number=number, question=str(raw.get("question")).strip()[:500], status=status,
        reason=str(raw.get("reason") or "").strip()[:300],
        depends_on=sorted({d for d in raw.get("depends_on") or [] if isinstance(d, int) and 1 <= d < number}),
        kind="brief" if raw.get("kind") == "brief" else "analysis", metric=metric or None,
        metric_params=raw.get("metric_params") if isinstance(raw.get("metric_params"), dict) else {},
        tables=[t for t in (str(x).strip() for x in raw.get("tables") or []) if t in tables],
        time_grain=grain if grain in GRAINS else None,
        per_app=bool(raw.get("per_app")), per_user=bool(raw.get("per_user")))


def parse(args: dict, *, apps: list[str], metrics: dict, tables: set[str], date_range: tuple,
          fallback_question: str) -> Understanding:
    """OpenAI's tool arguments, kept only where they match the live data."""
    known = {a.lower(): a for a in apps}
    dropped: list[str] = []

    def app(name) -> str | None:
        found = known.get(str(name or "").strip().lower())
        if name and not found:
            dropped.append(f"app '{name}'")
        return found

    intent = str(args.get("intent") or "").strip().lower()
    u = Understanding(
        question=str(args.get("rephrased_question") or "").strip()[:1000] or fallback_question,
        intent=intent if intent in INTENTS else "analysis",
        message=str(args.get("message") or "").strip()[:600],
        focus_app=app(args.get("focus_app")),
        apps=[a for a in dict.fromkeys(app(n) for n in args.get("apps") or [] if str(n).strip()) if a],
        date_from=_iso(args.get("date_from")), date_to=_iso(args.get("date_to")),
        window_hours=args.get("window_hours") if args.get("window_hours") in (24, 48, 72) else None,
        time_grain=str(args.get("time_grain") or "").lower() if str(args.get("time_grain") or "").lower() in GRAINS
        else None,
        per_app=bool(args.get("per_app")), per_user=bool(args.get("per_user")),
        adds_columns=bool(args.get("adds_columns")), follows_previous=bool(args.get("follows_previous")),
        categories=[c for c in args.get("categories") or [] if isinstance(c, dict)],
        breakdowns=[str(b) for b in args.get("breakdowns") or []],
        min_users=args.get("min_users") if isinstance(args.get("min_users"), int) and args["min_users"] > 0 else None,
        measures=[m for m in dict.fromkeys(str(x) for x in args.get("measures") or []) if m in MEASURES],
        unavailable=list(args.get("unavailable") or []),
        metric_params=args.get("metric_params") if isinstance(args.get("metric_params"), dict) else {})
    metric =str(args.get("metric") or "").strip()
    if metric and metric in metrics and metrics[metric].available:
        u.metric = metric
    elif metric:
        dropped.append(f"analysis '{metric}'")
    u.tables = [t for t in (str(x).strip() for x in args.get("tables") or []) if t in tables]
    dropped += [f"table '{t}'" for t in (str(x).strip() for x in args.get("tables") or []) if t and t not in tables]
    first, last = (str(d)[:10] if d else None for d in date_range)
    if first and last and ((u.date_from and u.date_from < first) or (u.date_to and u.date_to > last)):
        u.outside_coverage = True
    u.goal = str(args.get("goal") or "").strip()[:300]
    u.constraints = [str(c).strip()[:200] for c in args.get("constraints") or [] if str(c).strip()][:8]
    u.expected_output = str(args.get("expected_output") or "").strip()[:200]
    listed = [x for x in args.get("parts") or []
              if str(x.get("question") if isinstance(x, dict) else x or "").strip()]
    for raw in listed[:MAX_PARTS]:
        u.parts.append(_part(len(u.parts) + 1, raw, metrics=metrics, tables=tables, dropped=dropped))
    more = args.get("more_questions")
    u.more_questions = max(0, len(listed) - MAX_PARTS) + (more if isinstance(more, int) and more > 0 else 0)
    for item in args.get("clarifications") or []:
        if not isinstance(item, dict) or not str(item.get("question") or "").strip():
            continue
        options = [o for o in dict.fromkeys(str(x).strip()[:120] for x in item.get("options") or [])
                   if o and not _OTHER.match(o)][:4]
        number = item.get("part") if isinstance(item.get("part"), int) else 0
        u.clarifications.append({
            "part": number if 0 <= number <= len(u.parts) else 0,
            "question": str(item["question"]).strip()[:300], "why": str(item.get("why") or "").strip()[:300],
            "options": options})
    u.clarifications = u.clarifications[:MAX_CLARIFICATIONS]
    if u.intent == "clarify" and not u.clarifications:
        u.clarifications = [{"part": 0, "question": u.message or "Could you say which measure, apps and period "
                                                                   "you mean?", "why": "", "options": []}]
    u.dropped = dropped
    return u
