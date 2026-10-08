"""Answer rendering and grounding.

* ``render_*`` turn query results into plain-language answers deterministically.
  No language model touches the numbers on this path.
* ``ungrounded_numbers`` is the hallucination guard for every answer a model
  writes: each number in the answer must be traceable to the evidence (exactly,
  after rounding, as a percentage of two evidence numbers, or seconds->minutes).
"""

from __future__ import annotations

import itertools
import re

from .planner import QueryPlan, describe_filters
from .store import QueryResult

LIST_LIMIT = 25
TIE_ID_LIMIT = 100


def _n(value) -> str:
    if value is None:
        return "0"
    if isinstance(value, float):
        return f"{value:,.1f}".rstrip("0").rstrip(".") if not value.is_integer() else f"{int(value):,}"
    return f"{value:,}" if isinstance(value, int) else str(value)


def _plural(count, word: str, plural: str | None = None) -> str:
    return f"{_n(count)} {word if count == 1 else (plural or word + 's')}"


def _scope(plan: QueryPlan, skip_user: bool = False) -> str:
    parts = describe_filters(plan)
    if skip_user:
        parts = [p for p in parts if not p.startswith("for user")]
    return (" " + " ".join(parts)) if parts else ""


def _outcome_label(value) -> str:
    return {"open": "Still open", "purchased": "Ended in purchase", "timeout": "Timed out",
            "closed": "Closed"}.get(str(value), str(value))


def _fmt_time(value) -> str:
    text = str(value or "")
    return text[:16] if len(text) >= 16 else text


# --------------------------------------------------------------------------- #
# Aggregates
# --------------------------------------------------------------------------- #

def _measure_phrase(row: dict, plan: QueryPlan, lead: str) -> str:
    """'70 searches in 35 journeys' with the ranked metric first."""
    j, s = row.get("journeys") or 0, row.get("searches") or 0
    metric = plan.metric or "journeys"
    if metric == "purchase_rate":
        return (f"{_n(row.get('purchase_rate_pct'))}% purchased "
                f"({_n(row.get('purchased'))} of {_plural(j, 'journey')})")
    if metric == "avg_searches":
        return f"{_n(row.get('avg_searches_per_journey'))} searches per journey ({_plural(j, 'journey')})"
    if metric == "avg_duration":
        return f"{_n(row.get('avg_duration_minutes'))} min average ({_plural(j, 'journey')})"
    if metric == "total_duration":
        return f"{_n(row.get('total_duration_minutes'))} total minutes ({_plural(j, 'journey')})"
    if metric == "apps":
        return f"{_plural(row.get('apps'), 'app')} ({_plural(s, 'search', 'searches')} in {_plural(j, 'journey')})"
    if metric == "users":
        return f"{_plural(row.get('users'), 'user')} ({_plural(j, 'journey')})"
    if metric == "searches":
        return f"{_plural(s, 'search', 'searches')} in {_plural(j, 'journey')}"
    return _plural(j, "journey")


def _group_label(group: str, value) -> str:
    if group == "user":
        return f"User {value}"
    if group == "query":
        return f'"{value}"'
    if group == "hour" and value is not None:
        return f"{int(value):02d}:00-{int(value):02d}:59"
    if group == "outcome":
        return _outcome_label(value)
    if group == "apps_per_journey":
        return _plural(value, "app")
    if group == "start_app":
        return f"Started on {value}" if value else "Start app unknown"
    return str(value)


def _value_phrase(plan: QueryPlan, value) -> str:
    metric = plan.metric or "journeys"
    if metric == "purchase_rate":
        return f"{_n(value)}% purchased"
    if metric == "avg_searches":
        return f"{_n(value)} searches per journey"
    if metric == "avg_duration":
        return f"{_n(value)} minutes"
    if metric == "total_duration":
        return f"{_n(value)} total minutes"
    word = {"journeys": ("journey", "journeys"), "searches": ("search", "searches"),
            "users": ("user", "users"), "apps": ("app", "apps")}.get(metric, (metric, metric))
    return f"{_n(value)} {word[0] if value == 1 else word[1]}"


_METRIC_WORD = {"journeys": "journeys", "searches": "searches", "users": "users",
                "apps": "apps", "avg_searches": "searches per journey",
                "avg_duration": "average duration", "total_duration": "total time",
                "purchase_rate": "purchase rate"}


def render_aggregate(plan: QueryPlan, result: QueryResult, tie: dict | None = None) -> str:
    rows = result.dicts()
    if not plan.group_by:
        return _render_total(plan, rows[0] if rows else {})
    if not rows:
        return f"No journeys match{_scope(plan) or ' that question'} in this file."

    group, key = plan.group_by, plan.group_by
    sort_col = {"avg_searches": "avg_searches_per_journey", "avg_duration": "avg_duration_minutes",
                "total_duration": "total_duration_minutes",
                "purchase_rate": "purchase_rate_pct"}.get(plan.metric or "", plan.metric or "journeys")
    lines: list[str] = []

    # Header
    single_user = len(plan.user_ids) == 1
    if group == "app" and single_user and plan.metric in ("searches", "journeys", None):
        header = (f"User {plan.user_ids[0]} searched on {_plural(len(rows), 'app')}"
                  f"{_scope(plan, skip_user=True)}:")
    elif group == "app" and not plan.has_filters() and not plan.limit and len(rows) == result.row_count:
        header = f"The file has {_plural(len(rows), 'app')}, ranked by {_METRIC_WORD.get(plan.metric, 'journeys')}:"
    else:
        order = " (lowest first)" if plan.order == "asc" else ""
        header = (f"{_METRIC_WORD.get(plan.metric, 'journeys').capitalize()} by {group.replace('_', ' ')}"
                  f"{_scope(plan)}{order}:")

    overflow_tie = bool(tie) and tie.get("total", 0) > len(rows)
    if (plan.singular or overflow_tie) and plan.group_by not in ("date", "hour") and sort_col in rows[0]:
        best = rows[0].get(sort_col)
        leaders = [r for r in rows if r.get(sort_col) == best]
        tie_total = tie.get("total", len(leaders)) if tie else len(leaders)
        word = "fewest" if plan.order == "asc" else "most"
        if plan.metric in ("purchase_rate", "avg_duration", "total_duration", "avg_searches"):
            word = "lowest" if plan.order == "asc" else "highest"
        metric_word = _METRIC_WORD.get(plan.metric, "journeys")
        if tie_total <= 1:
            lead = leaders[0]
            lines.append(f"{_group_label(group, lead[key])} has the {word} {metric_word}"
                         f"{_scope(plan)}: {_measure_phrase(lead, plan, group)}.")
        else:
            # A tie is about the ranked metric only; other measures differ per member.
            value = _value_phrase(plan, best)
            noun = {"user": "users", "app": "apps", "query": "search phrases"}.get(group, "groups")
            lines.append(f"{_n(tie_total)} {noun} are tied for the {word} {metric_word}"
                         f"{_scope(plan)}: {value} each.")
            ids = (tie or {}).get("labels") or [r[key] for r in leaders]
            label = {"user": "User ids", "app": "Apps", "query": "Search phrases"}.get(group, "Members")
            shown_ids = ids[:TIE_ID_LIMIT]
            more = f" (first {len(shown_ids)} of {_n(tie_total)})" if tie_total > len(shown_ids) else ""
            lines.append(f"{label}{more}: " + ", ".join(
                (f'"{v}"' if group == "query" else str(v)) for v in shown_ids) + ".")
            if tie_total > len(shown_ids):
                lines.append("The full list is in the result table. Add a filter (app, outcome, "
                             "date) to narrow it down.")
            return "\n".join(lines)
        if plan.metric == "purchase_rate":
            lines.append("Small groups can show extreme rates, so check the journey counts.")
        if len(rows) > len(leaders):
            lines.append("")
            lines.append("Ranking:" if not single_user else header)
    else:
        lines.append(header)

    shown = rows[:LIST_LIMIT]
    for i, row in enumerate(shown, 1):
        lines.append(f"{i}. {_group_label(group, row[key])}: {_measure_phrase(row, plan, group)}")
    total_groups = result.row_count
    if total_groups > len(shown):
        lines.append(f"Showing {len(shown)} of {_n(total_groups)} {group.replace('_', ' ')} groups.")
    if plan.singular and len(rows) == 1 and lines[-1].startswith("1."):
        lines = lines[:1]
    return "\n".join(lines)


def render_share(plan: QueryPlan, result: QueryResult, totals: dict) -> str:
    """Each group's slice of the filtered total, e.g. share of all purchases by app."""
    rows = result.dicts()
    measure = plan.metric if plan.metric in ("journeys", "searches") else "journeys"
    total = totals.get(measure) or 0
    if not rows or not total:
        return f"No journeys{_scope(plan)} were found in this file."
    unit = "journey" if measure == "journeys" else "search"
    units = "journeys" if measure == "journeys" else "searches"
    scope = _scope(plan)
    what = {"purchased": "purchases", "timeout": "timed-out journeys"}.get(plan.outcome or "",
                                                                          f"{units}{scope}")
    group = plan.group_by
    ranked = sorted(rows, key=lambda r: -(r.get(measure) or 0))

    def pct(r):
        return 100.0 * (r.get(measure) or 0) / total

    lines = []
    for focus in plan.focus_apps:
        pos = next((i for i, r in enumerate(ranked) if r[group] == focus), None)
        if pos is None:
            lines.append(f"{focus} has no {what} in this file.")
            continue
        r = ranked[pos]
        lines.append(f"{focus} has {pct(r):.1f}% of all {what} ({_n(r[measure])} of "
                     f"{_plural(total, unit, units)}), ranked {_ordinal(pos + 1)} of {len(ranked)} apps.")
        ahead = ranked[:pos]
        if ahead:
            lines.append(f"Apps ahead of {focus}: " + ", ".join(
                f"{a[group]} {pct(a):.1f}% ({_n(a[measure])})" for a in ahead) + ".")
        else:
            lines.append(f"No app is ahead of {focus}.")
        lines.append("")
    lines.append(f"Share of all {what} ({_plural(total, unit, units)}) by {group}:")
    for i, r in enumerate(ranked[:LIST_LIMIT], 1):
        lines.append(f"{i}. {_group_label(group, r[group])}: {pct(r):.1f}% ({_n(r[measure])})")
    summed = sum(pct(r) for r in rows)
    multi = totals.get("multi_app_journeys") or 0
    if group == "app" and summed > 100.05:
        lines.append(f"Note: {_plural(multi, 'journey')} used more than one app and count for each "
                     f"app they used, so the shares add up to {summed:.1f}%, not 100%. The file does "
                     "not record which app the purchase itself happened on.")
    return "\n".join(lines)


def render_start_funnel(plan: QueryPlan, totals: dict, destinations: list[dict]) -> str:
    start = " / ".join(plan.start_apps)
    n = totals.get("journeys") or 0
    if not n:
        return f"No journeys started on {start}{_scope(plan).replace(' that started on ' + start, '')}."

    def pct(x):
        return f"{100.0 * (x or 0) / n:.1f}%"

    bought = totals.get("purchased") or 0
    stayed_bought = totals.get("bought_start_app_only") or 0
    moved_bought = totals.get("bought_after_moving") or 0
    moved = totals.get("moved") or 0
    other = " ".join(p for p in describe_filters(plan) if not p.startswith("that started on"))
    lines = [
        f"{_plural(n, 'journey')} started on {start}{(' ' + other) if other else ''}.",
        f"- Bought, using only {start}: {_n(stayed_bought)} ({pct(stayed_bought)}). "
        f"These purchases were on {start}, because no other app was used.",
        f"- Bought after also using another app: {_n(moved_bought)} ({pct(moved_bought)}). "
        "The file does not say which app the purchase was on.",
        f"- Total that ended in a purchase: {_n(bought)} ({pct(bought)}).",
        f"- Moved on to at least one other app: {_n(moved)} ({pct(moved)}); "
        f"stayed on {start} only: {_n(totals.get('stayed'))} ({pct(totals.get('stayed'))}).",
        f"- Timed out: {_n(totals.get('timed_out'))} ({pct(totals.get('timed_out'))}); "
        f"still open: {_n(totals.get('still_open'))} ({pct(totals.get('still_open'))}).",
    ]
    if destinations:
        lines.append("Where the journeys that moved went next: " + ", ".join(
            f"{d['app']} ({_plural(d['journeys'], 'journey')}"
            + (f", {_n(d['purchased'])} bought" if d.get("purchased") else "") + ")"
            for d in destinations[:8]) + ".")
    if moved and moved < 30:
        lines.append(f"Only {_plural(moved, 'journey')} moved to another app, so treat the "
                     "split between destination apps as indicative.")
    return "\n".join(lines)


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _render_total(plan: QueryPlan, row: dict) -> str:
    scope = _scope(plan)
    j, s, u = row.get("journeys") or 0, row.get("searches") or 0, row.get("users") or 0
    if j == 0:
        return f"No journeys{scope} were found in this file."
    metric = plan.metric or "journeys"
    if metric == "searches":
        return f"There were {_plural(s, 'search', 'searches')}{scope}, across {_plural(j, 'journey')} by {_plural(u, 'user')}."
    if metric == "users":
        return f"{_plural(u, 'user')} made journeys{scope} ({_plural(j, 'journey')}, {_plural(s, 'search', 'searches')})."
    if metric == "apps":
        return f"{_plural(row.get('apps'), 'app')} appear{scope}."
    if metric == "avg_searches":
        return (f"A journey{scope} has {_n(row.get('avg_searches_per_journey'))} searches on average "
                f"({_plural(s, 'search', 'searches')} over {_plural(j, 'journey')}).")
    if metric == "avg_duration":
        return (f"The average time between the first and last search in a journey{scope} is "
                f"{_n(row.get('avg_duration_minutes'))} minutes (longest: "
                f"{_n(row.get('max_duration_minutes'))} minutes, {_plural(j, 'journey')}). "
                "Journeys with a single search count as 0 minutes.")
    if metric == "total_duration":
        return (f"The total time between first and last searches across journeys{scope} is "
                f"{_n(row.get('total_duration_minutes'))} minutes across {_plural(j, 'journey')}.")
    if metric == "purchase_rate":
        return (f"{_n(row.get('purchase_rate_pct'))}% of journeys{scope} ended in a purchase "
                f"({_n(row.get('purchased'))} of {_plural(j, 'journey')}). The file does not say "
                "what was bought or for how much.")
    detail = f"{_plural(s, 'search', 'searches')} by {_plural(u, 'user')}"
    if not (plan.outcome or plan.status):
        detail += (f"; {_n(row.get('purchased'))} ended in a purchase, "
                   f"{_n(row.get('timed_out'))} timed out, {_n(row.get('open_journeys'))} are still open")
    return f"There are {_plural(j, 'journey')}{scope} ({detail})."


# --------------------------------------------------------------------------- #
# Row lists and entities
# --------------------------------------------------------------------------- #

def render_journey_list(plan: QueryPlan, result: QueryResult, total: int,
                        suggestions: list[str] | None = None) -> str:
    scope = _scope(plan)
    if total == 0:
        text = f"No journeys{scope} were found in this file."
        if suggestions:
            text += " Close matches in the stored searches: " + ", ".join(f'"{s}"' for s in suggestions) + "."
        return text
    rows = result.dicts()
    head = f"Found {_plural(total, 'journey')}{scope}"
    if plan.limit and plan.limit < total:
        head += f" (top {len(rows)} by searches)"
    lines = [head + ":"]
    for row in rows[:LIST_LIMIT]:
        outcome = row["outcome"] or row["status"]
        lines.append(f"- {row['journey_id']} (user {row['user']}): \"{row['query']}\" - "
                     f"{row['apps']}, {_plural(row['searches'], 'search', 'searches')}, "
                     f"{_outcome_label(outcome).lower()}, started {_fmt_time(row['first_search'])}")
    if total > min(len(rows), LIST_LIMIT):
        lines.append(f"Showing {min(len(rows), LIST_LIMIT)} of {_n(total)}. Add a filter to narrow it down.")
    return "\n".join(lines)


def render_user(profile: dict) -> str:
    p = profile
    apps = p["apps"]
    lines = [
        f"User {p['user_id']} made {_plural(p['journeys'], 'journey')} with "
        f"{_plural(p['searches'], 'search', 'searches')} between {_fmt_time(p['first_seen'])} "
        f"and {_fmt_time(p['last_seen'])}.",
        f"Outcomes: {_n(p['open_journeys'])} still open, {_n(p['purchased'])} ended in a purchase, "
        f"{_n(p['timed_out'])} timed out. {_n(p['multi_app'])} used more than one app.",
        f"Apps used ({len(apps)}): " + "; ".join(
            f"{a['app_name']} - {_plural(a['searches'], 'search', 'searches')} in "
            f"{_plural(a['journeys'], 'journey')}" for a in apps) + ".",
        "",
        "Journeys:",
    ]
    journeys = p["journey_list"].dicts()
    for row in journeys[:LIST_LIMIT]:
        outcome = row["outcome"] or row["status"]
        lines.append(f"- {row['journey_id']}: \"{row['query']}\" - {row['apps']}, "
                     f"{_outcome_label(outcome).lower()}, {_fmt_time(row['first_search'])}")
    if p["journeys"] > LIST_LIMIT:
        lines.append(f"Showing {LIST_LIMIT} of {_n(p['journeys'])} journeys; the full list is in the table data.")
    return "\n".join(lines)


def render_journey(d: dict) -> str:
    seconds = d.get("duration_seconds") or 0
    apps = ", ".join(f"{a['app_name']} ({_plural(a['event_count'], 'search', 'searches')})" for a in d["apps"])
    outcome = _outcome_label(d.get("close_reason") or d.get("status")).lower()
    span = f"over {_duration(seconds)}" if seconds >= 1 else "all at the same moment"
    return (f"Journey {d['journey_id']} belongs to user {d['user_assoc_id']}. They looked for "
            f"\"{d['query_cluster_rep']}\" with {_plural(d['total_events'], 'search', 'searches')} "
            f"on {_plural(d['total_apps'], 'app')} - {apps} - {span}, from "
            f"{_fmt_time(d['first_search_time'])} to {_fmt_time(d['last_search_time'])}. "
            f"Outcome: {outcome}.")


def _duration(seconds: float) -> str:
    if seconds < 90:
        return _plural(int(round(seconds)), "second")
    if seconds < 90 * 60:
        return _plural(int(round(seconds / 60)), "minute")
    hours = seconds / 3600
    return f"{hours:.1f} hours" if hours < 48 else _plural(int(round(hours / 24)), "day")


def render_similar(journey_id: str, pairs: list[tuple[str, float]], details: dict) -> str:
    if not pairs:
        return f"No journeys similar to {journey_id} were found."
    lines = [f"Journeys most similar to {journey_id} (by the stored search-meaning vectors):"]
    for jid, score in pairs:
        d = details.get(jid, {})
        lines.append(f"- {jid}: \"{d.get('query_cluster_rep')}\" - {d.get('apps_summary')}, "
                     f"{_outcome_label(d.get('close_reason') or d.get('status')).lower()} "
                     f"(similarity {score:.2f})")
    return "\n".join(lines)


def render_summary(s: dict) -> str:
    return (f"This file has {_plural(s['journeys'], 'journey')} by {_plural(s['users'], 'user')}, "
            f"with {_plural(s['events'], 'search', 'searches')} from {s.get('first_date')} to "
            f"{s.get('last_date')}. {_n(s['cross_app'])} journeys used more than one app. "
            f"{_n(s['purchased'])} ended in a purchase, {_n(s['timed_out'])} timed out and "
            f"{_n(s['open_journeys'])} are still open. Apps ({len(s['apps'])}): {', '.join(s['apps'])}.")


def render_quality(s: dict) -> str:
    quality = s.get("data_quality") or {}
    read = quality.get("records_read", 0)
    loaded = quality.get("records_loaded", s.get("journeys", 0))
    skipped = quality.get("records_skipped", 0)
    warnings = quality.get("warnings", 0)
    lines = [f"Data-quality report: {_n(loaded)} of {_n(read)} records were loaded; "
             f"{_n(skipped)} were skipped and {_n(warnings)} warnings were recorded."]
    issues = quality.get("issues") or []
    if issues:
        lines.append("Issues:")
        lines.extend(f"- {issue}" for issue in issues[:20])
        if len(issues) > 20:
            lines.append(f"Showing 20 of {_n(len(issues))} issues.")
    else:
        lines.append("No load-time data-quality issues were detected.")
    return "\n".join(lines)


def render_unsupported(plan: QueryPlan) -> str:
    what = f" ({plan.reason})" if plan.reason and plan.reason != "fields that are not in this file" else ""
    return ("That information is not in this journey file" + what + ". It records, per journey: "
            "the user number, what they searched for, which apps and how many searches, the "
            "first and last search time, and whether the journey is still open, ended in a "
            "purchase, or timed out. It has no price, order, product, payment, revenue, "
            "location or demographic details.")


HELP_TEXT = ("I answer questions about the loaded journey file. For example:\n"
             "- Which app has the most searches?\n"
             "- Which users have the most journeys?\n"
             "- All apps used by user 5330\n"
             "- How many journeys ended in a purchase on Meesho?\n"
             "- Purchase rate per app\n"
             "- Show journey J000002 / journeys similar to J000002\n"
             "- Journeys for \"modak\"\n"
             "- Show the journey data-quality report\n"
             "Follow-ups like \"this user\", \"the second user\" or \"what about Flipkart?\" use the earlier messages.")


# --------------------------------------------------------------------------- #
# Grounding
# --------------------------------------------------------------------------- #

_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\w])")


def numbers_in(text: str) -> list[float]:
    values = []
    for match in _NUM.finditer(text or ""):
        try:
            values.append(float(match.group(1).replace(",", "")))
        except ValueError:
            pass
    return values


def _variants(value: float) -> set[float]:
    return {round(value, 0), round(value, 1), round(value, 2), float(int(value))}


def ungrounded_numbers(answer: str, evidence: list[str], question: str = "",
                       list_len: int = 10, derived: bool = False) -> list[str]:
    """Numbers in ``answer`` that cannot be traced to the evidence."""
    source = [v for text in evidence for v in numbers_in(text)]
    allowed: set[float] = set()
    for v in source + numbers_in(question):
        allowed |= _variants(v)
        allowed |= _variants(v / 60.0)        # seconds -> minutes
        allowed |= _variants(v / 3600.0)      # seconds -> hours
    if derived:
        # Opt-in only: with many evidence numbers almost any percentage can be
        # "derived", which would let invented figures through.
        small = sorted({v for v in source if 0 < v <= 1_000_000})[:40]
        for a, b in itertools.permutations(small, 2):
            allowed |= _variants(100.0 * a / b)
    allowed |= {0.0, 1.0}
    bad = []
    text = re.sub(r"(?m)^\s*\d{1,3}[.)]\s", " ", answer or "")      # list markers "1. "
    for match in _NUM.finditer(text):
        value = float(match.group(1).replace(",", ""))
        if not (_variants(value) & allowed):
            bad.append(match.group(1))
    return bad


_NUMBER_WORDS = re.compile(
    r"\b(zero|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|thirty|"
    r"forty|fifty|hundred|hundreds|thousand|thousands|dozen|dozens|half|twice|thrice|"
    r"double|triple)\b", re.I)


def plain_summary_problems(summary: str) -> list[str]:
    """A 'simple meaning' summary may explain, but must not state facts itself.

    The exact numbers and ids are always shown underneath from the query
    result, so the summary is rejected if it carries any figure or id of its
    own - that is where rewrites went wrong ("10 searches", "two minutes").
    """
    problems = []
    if re.search(r"\d", summary or ""):
        problems.append("contains digits")
    if _NUMBER_WORDS.search(summary or ""):
        problems.append("contains number words")
    if re.search(r"\b(wow|amazing|incredible)\b", summary or "", re.I):
        problems.append("hype words")
    if len((summary or "").split()) > 70:
        problems.append("too long")
    return problems
