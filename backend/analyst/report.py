"""Executive-summary questions: one brief, many small exact analyses.

"I'm Flipkart's insights lead: how did we do yesterday in beauty and smartphones, by category, city, age and
gender - what went well, where did we stumble, where did competitors do better" is not one question. Answered
as one model-written query it is slow to write, gets cut off at the output limit and fails review, and the
user gets nothing. Here it becomes a scope (who "we" is, the period, the categories, the smallest group
allowed, the breakdowns) and one run of the vetted segment scorecard per breakdown. The findings - where we
lead, where a competitor converts better, where we are weak - are computed from the rows in code. A model only
words them, and every sentence it writes must cite findings and repeat only their numbers and names.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from sqlglot import exp

try:
    from rapidfuzz import fuzz, process
except ImportError:  # pragma: no cover - rapidfuzz is in requirements.txt
    fuzz = process = None

from journey_agent import answer as grounding

from .understanding import plain

SCORECARD = "segment_scorecard"
DEFAULT_MIN_USERS = 30
MAX_BREAKDOWNS = 8
GAP_PTS = 1.0               # a conversion difference smaller than this is too small to act on
WEAK_PTS = 2.0              # a segment this far below the rest of our journeys is a weak spot
TOP_FINDINGS = 3            # per kind and breakdown; the full tables are always shown
FDR = 0.05                  # false-discovery rate across every comparison in one brief (Benjamini-Hochberg)
CATEGORY_DIMS = {"requested_category", "master_category", "category", "sub_category"}
# Catch-all groups describe no product or person anyone can act on, so they never become findings.
_CATCH_ALL = re.compile(r"^(?:uncategori[sz]ed|unknown|unknown app|others?|all others)$", re.I)

# Breakdowns the scorecard can run, in the order a reader expects them.
BREAKDOWNS = ("overall", "requested_category", "master_category", "category", "gender", "age_group", "city",
              "tier", "state", "zone", "device_model")
LABELS = {"overall": "Overall", "requested_category": "Asked category", "master_category": "Master category",
          "category": "Category", "sub_category": "Sub-category", "gender": "Gender", "age_group": "Age group",
          "city": "City", "tier": "Loyalty tier", "state": "State", "zone": "Zone", "device_model": "Device"}

_REPORT_WORDS = re.compile(
    r"\b(executive summary|summary|summari[sz]e|report|brief(?:ing)?|deck|scorecard|insights?|went well|"
    r"did well|stumbled|did better|doing better|how did (?:we|i) (?:do|perform|fare)|how (?:we|did we) performed|"
    r"perform(?:ed|ance)?|wins? and loss(?:es)?|strengths? and weakness(?:es)?)\b", re.I)
_CHARTS = re.compile(r"\b(graphs?|charts?|visuali[sz]\w*|plots?)\b", re.I)
_CITY_TIER = re.compile(r"\bcity[- ]?tiers?\b|\btier[- ]?[123]\s+cit(?:y|ies)\b", re.I)
_DIMENSION_WORDS = (
    ("requested_category", r"\bcategor(?:y|ies)\b"),
    ("gender", r"\bgenders?\b|\b(?:fe)?males?\b|\b(?:wo)?men\b"),
    ("age_group", r"\bages?\b|\bage[- ]groups?\b"),
    ("city", r"\bcit(?:y|ies)\b"),
    ("tier", r"\bloyalty\b|\btiers?\b"),
    ("state", r"\bstates?\b"),
    ("zone", r"\bzones?\b|\bregions?\b"),
    ("device_model", r"\bdevices?\b|\bphone models?\b|\bhandsets?\b"),
)
_INTERLOCK = re.compile(r"\binterlock\w*|\bcross[- ]?(?:tab\w*|cut\w*|section\w*)|\bcombinations?\b|\bmicro\b", re.I)
_MIN_USERS = re.compile(
    r"(?:min(?:imum)?|at\s*least|no\s+(?:less|fewer)\s+than|>=?)\s*(?:of\s+)?(\d{1,6})\s*"
    r"(?:people|persons?|users?|shoppers?|respondents?|customers?|panel(?:l)?ists?|members?|sample)"
    r"|\b(?:base|sample)(?:\s+size)?\s+(?:should|must|has to|needs? to)?\s*(?:be\s+)?(?:a\s+)?"
    r"(?:min(?:imum)?|at\s*least)\s*(?:of\s+)?(\d{1,6})", re.I)
_PERSONA_LEAD = r"(?:\bi\s*am|\bi'm|\bim|\bam|\bwe\s+are|\bwe're|\bwe\s+at|\bhere\s+at|\bwork(?:ing)?\s+(?:at|for|in))"
# "Flipkart's insights lead", "Meesho's category team" - a role noun, not just "Flipkart's insights?".
_PERSONA_ROLE = r"(?:[a-z&-]+\s+){0,2}(?:lead|head|manager|director|team|analyst|vp|ceo|cxo|owner)\b"


# --------------------------------------------------------------------------- #
# Reading the brief
# --------------------------------------------------------------------------- #

def stated_home_app(question: str, apps: list[str]) -> str | None:
    """The app the asker speaks for, when they say so ("am Flipkart's insights lead", "we at Meesho")."""
    low = question.lower()
    found = []
    for app in sorted(apps, key=len, reverse=True):
        name = re.escape(app.lower())
        lead = rf"{_PERSONA_LEAD}\s+(?:at\s+|with\s+|from\s+|for\s+)?(?:the\s+|a\s+|an\s+)?{name}(?:'s|’s)?\b"
        role = rf"\b{name}(?:'s|’s)\s+{_PERSONA_ROLE}"
        ours = rf"\b(?:our|my)\s+(?:app|company|platform|brand|business)\s*(?:,|is|-|:)?\s*{name}\b"
        if any(re.search(rx, low) for rx in (lead, role, ours)):
            if not any(app.lower() in other.lower() for other in found):   # "Amazon" inside "Amazon Now"
                found.append(app)
    return found[0] if len(found) == 1 else None


def subject_app(question: str, apps: list[str]) -> str | None:
    """The app a brief is about when the question names it ("how did Amazon perform", "Meesho's performance")."""
    low = question.lower()
    found = []
    for app in sorted(apps, key=len, reverse=True):
        name = re.escape(app.lower())
        how = (rf"\bhow\s+(?:(?:did|does|do|has|is|was)\s+)?(?:the\s+)?{name}(?:'s|’s)?\s+"
               rf"(?:perform\w*|do(?:ing)?|did|fare\w*)")
        if re.search(how, low) or re.search(rf"\b{name}(?:'s|’s)?\s+performance\b", low):
            if not any(app.lower() in other.lower() for other in found):
                found.append(app)
    return found[0] if len(found) == 1 else None


def min_users_from(question: str) -> int | None:
    m = _MIN_USERS.search(question)
    return int(m.group(1) or m.group(2)) if m else None


def breakdowns_from(question: str) -> list[str]:
    text = _CITY_TIER.sub(" ", question)            # "city tiers" is neither a city nor a loyalty-tier split
    wanted = [dim for dim, rx in _DIMENSION_WORDS if re.search(rx, text, re.I)]
    if _INTERLOCK.search(question):
        for a, b in (("requested_category", "gender"), ("requested_category", "age_group"), ("gender", "age_group")):
            if a in wanted and b in wanted:
                wanted.append(f"{a}+{b}")
    return wanted


def looks_like_report(question: str) -> bool:
    """An executive-summary style brief: report wording plus several breakdowns, or a long report request."""
    if not _REPORT_WORDS.search(question):
        return False
    plain = [d for d in breakdowns_from(question) if "+" not in d]
    return len(plain) >= 2 or len(question.split()) >= 40


def city_tier_requested(question: str) -> bool:
    return bool(_CITY_TIER.search(question))


# --------------------------------------------------------------------------- #
# Categories: the asker's words -> exact values in the data
# --------------------------------------------------------------------------- #

def _norm(text: str) -> str:
    text = str(text).lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]", "", text)


def _singular(text: str) -> str:
    return text[:-1] if text.endswith("s") and len(text) > 4 else text


def resolve_category(asked: str, levels: dict[str, set[str]]) -> tuple[list[str], list[str]]:
    """(exact values meaning ``asked``, closest suggestions when there are none)."""
    want = _singular(_norm(asked))
    if not want:
        return [], []
    exact = [v for v in levels if _singular(_norm(v)) == want]
    if exact:
        return exact[:1], []
    if process is None:
        return [], []
    names = list(levels)
    keys = [_singular(_norm(v)) for v in names]
    best = process.extractOne(want, keys, scorer=fuzz.ratio)
    if best and best[1] >= 90:
        return [names[best[2]]], []
    close = process.extract(asked.lower(), [v.lower() for v in names], scorer=fuzz.WRatio, limit=3)
    return [], [names[i] for _, score, i in close if score >= 55]


def categories_in_text(question: str, levels: dict[str, set[str]], apps: list[str]) -> list[str]:
    """Category names written out in the question (no model): longest first, never part of a longer match."""
    low = question.lower()
    app_names = {a.lower() for a in apps}
    taken: list[tuple[int, int]] = []
    found = []
    for value in sorted(levels, key=len, reverse=True):
        v = value.lower()
        if len(v) < 5 or v in app_names or v in {"other", "others", "unknown", "uncategorised", "uncategorized"}:
            continue
        spellings = {v, re.sub(r"\s+", " ", v.replace("&", " and "))}          # "Beauty & Care" ~ "beauty and care"
        hit = None
        for spelling in spellings:
            for m in re.finditer(rf"(?<![a-z]){re.escape(spelling)}(?![a-z])", low):
                if not any(a <= m.start() < b or a < m.end() <= b for a, b in taken):
                    hit = (m.start(), m.end())
                    break
            if hit:
                break
        if hit:
            taken.append(hit)
            found.append(value)
    return found


def to_paths(names: list[str], paths: list[tuple[str, str, str]]) -> tuple[list[str], list[str], list[str]]:
    """Exact names -> taxonomy paths ("Master", "Master > Category", "Master > Category > Sub").

    A category name can recur under several masters ("Others" under Beauty and under Electronics). Matching
    the bare name would pull every one of them in, so such a name is tied to the master its sibling names
    belong to, or left out when that is not clear. Returns (paths, display labels, names left out)."""
    masters = {m.lower(): m for m, _, _ in paths}
    categories: dict[str, set[tuple[str, str]]] = {}
    subs: dict[str, set[tuple[str, str, str]]] = {}
    for m, c, sc in paths:
        categories.setdefault(c.lower(), set()).add((m, c))
        subs.setdefault(sc.lower(), set()).add((m, c, sc))
    resolved: list[tuple[str, str, str]] = []           # (path, label, master)
    pending: list[tuple[str, list[tuple[str, str, str]]]] = []
    for name in names:
        key = str(name).strip().lower()
        if key in masters:
            resolved.append((masters[key], masters[key], masters[key]))
            continue
        options = ([(f"{m} > {c}", c, m) for m, c in sorted(categories[key])] if key in categories else
                   [(f"{m} > {c} > {sc}", sc, m) for m, c, sc in sorted(subs.get(key, ()))])
        if len(options) == 1:
            resolved.append(options[0])
        elif options:
            pending.append((str(name), options))
    siblings = {m for _, _, m in resolved}
    left_out = []
    for name, options in pending:
        fit = [o for o in options if o[2] in siblings]
        if len(fit) == 1:
            path, lab, master = fit[0]
            resolved.append((path, f"{lab} (in {master})", master))
        else:
            left_out.append(name)
    resolved = list(dict.fromkeys(resolved))
    return [r[0] for r in resolved], [r[1] for r in resolved], left_out


def category_menu(tree: dict[str, list[str]], limit: int = 6000) -> str:
    lines, size = [], 0
    for master, cats in sorted(tree.items()):
        line = f"- {master}: {', '.join(cats)}"
        if size + len(line) > limit:
            lines.append("- ... (list shortened)")
            break
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines) or "(no category list available)"


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #

@dataclass
class Scope:
    focus_app: str
    focus_reason: str = ""
    date_from: str | None = None
    date_to: str | None = None
    window_hours: int = 24
    min_users: int = DEFAULT_MIN_USERS
    min_users_stated: bool = False
    category_map: dict[str, list[str]] = field(default_factory=dict)    # asked -> taxonomy paths
    category_labels: dict[str, str] = field(default_factory=dict)       # asked -> how the mapping reads
    unresolved: dict[str, list[str]] = field(default_factory=dict)      # asked -> closest values
    ambiguous: dict[str, list[str]] = field(default_factory=dict)       # asked -> names under several masters
    category_notes: list[str] = field(default_factory=list)
    breakdowns: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)

    @property
    def categories(self) -> list[str]:
        return list(dict.fromkeys(v for values in self.category_map.values() for v in values))

    def period(self) -> str:
        if self.date_from and self.date_to:
            return self.date_from if self.date_from == self.date_to else f"{self.date_from} to {self.date_to}"
        if self.date_from:
            return f"from {self.date_from}"
        if self.date_to:
            return f"until {self.date_to}"
        return "all loaded data"

    def params(self) -> dict:
        params = {"window_hours": self.window_hours, "min_users": self.min_users}
        if self.date_from:
            params["date_from"] = self.date_from
        if self.date_to:
            params["date_to"] = self.date_to
        if self.category_map:
            params["categories"] = self.categories
        return params


def _iso(value, first: str | None, last: str | None) -> str | None:
    try:
        day = date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
    if first and last and not (str(first)[:10] <= day.isoformat() <= str(last)[:10]):
        return None
    return day.isoformat()


def _mapping_label(asked: str, labels: list[str]) -> str:
    if len(labels) == 1 and _singular(_norm(labels[0])) == _singular(_norm(asked)):
        return labels[0]
    text = f"{asked} = {', '.join(labels)}"
    if len(labels) == 1 and (fuzz is None or fuzz.ratio(_norm(asked), _norm(labels[0])) < 90):
        text += " (closest category in the data, not an exact match)"
    return text


def build_scope(question: str, plan: dict | None, *, apps: list[str], home_app: str, stated_app: str | None,
                levels: dict[str, set[str]], extracted, date_range: tuple, carried_app: str | None = None,
                paths: list[tuple[str, str, str]] | None = None, use_rules: bool = True) -> Scope:
    """Rules first (they are exact), then the model's reading for what rules cannot see, all validated.
    With ``use_rules=False`` (OpenAI understood the question) only the model's reading is used.

    Who the brief is about: the app the asker says they work for in this message, else the app the question
    asks about ("how did Amazon perform"), else the model's reading, else who "we" was earlier in the chat."""
    plan = plan if isinstance(plan, dict) else {}
    known = {a.lower(): a for a in apps}
    first, last = (str(d)[:10] if d else None for d in date_range)

    planned_app = known.get(str(plan.get("focus_app") or "").strip().lower())
    named = subject_app(question, apps) if use_rules else None
    if stated_app:
        scope = Scope(stated_app, f"you wrote as {stated_app}")
    elif named:
        scope = Scope(named, "named in your question")
    elif planned_app and planned_app != carried_app:
        scope = Scope(planned_app, "read from your question" if use_rules else "read by OpenAI from your question")
    elif carried_app:
        scope = Scope(carried_app, f"you wrote as {carried_app} earlier in this chat")
    else:
        scope = Scope(home_app, "the configured home app")

    # Period: the router's reading of "yesterday", "last 7 days", "13 Sep" is exact; the model only fills a gap.
    if extracted is not None and (extracted.date_from or extracted.date_to):
        scope.date_from, scope.date_to = extracted.date_from, extracted.date_to
    else:
        scope.date_from = _iso(plan.get("date_from"), first, last)
        scope.date_to = _iso(plan.get("date_to"), first, last) or scope.date_from
    if extracted is not None and extracted.window_hours:
        scope.window_hours = extracted.window_hours

    stated = min_users_from(question) if use_rules else None
    planned = plan.get("min_users")
    if stated:
        scope.min_users, scope.min_users_stated = stated, True
    elif isinstance(planned, int) and 1 <= planned <= 100_000:
        scope.min_users, scope.min_users_stated = planned, True

    # Categories: the model names what was asked and proposes exact values; only values that exist are used,
    # and each becomes a taxonomy path so a name shared by several parents cannot pull in the wrong products.
    asked_items = plan.get("categories") if isinstance(plan.get("categories"), list) else []
    exact = {v.lower(): v for v in levels}
    wanted_names: list[tuple[str, list[str]]] = []
    for item in asked_items:
        asked = str(item.get("asked") if isinstance(item, dict) else item or "").strip()
        if not asked:
            continue
        proposed = item.get("matches") if isinstance(item, dict) and isinstance(item.get("matches"), list) else []
        values = list(dict.fromkeys(exact[str(m).strip().lower()] for m in proposed
                                    if str(m).strip().lower() in exact))
        if not values:
            values, suggestions = resolve_category(asked, levels)
            if not values:
                scope.unresolved[asked] = suggestions
                continue
        wanted_names.append((asked, values))
    if not asked_items and use_rules:
        wanted_names = [(value, [value]) for value in categories_in_text(question, levels, apps)]
    for asked, values in wanted_names:
        selected, labels, left_out = to_paths(values, paths or [])
        if left_out:
            scope.ambiguous[asked] = left_out
            scope.category_notes.append(
                f"'{', '.join(left_out)}' occurs under several master categories, so it was left out of "
                f"'{asked}' rather than counting the wrong products.")
        if selected:
            scope.category_map[asked] = selected
            scope.category_labels[asked] = _mapping_label(asked, labels)
        elif not left_out:
            scope.unresolved[asked] = []

    # Breakdowns: everything the asker named, plus what the model read; overall always comes first.
    wanted = ["overall"] + (breakdowns_from(question) if use_rules else [])
    for b in plan.get("breakdowns") or []:
        b = str(b).strip()
        if all(part in BREAKDOWNS or part == "sub_category" for part in b.split("+")) and b.count("+") <= 1:
            wanted.append(b)
    if not scope.category_map:
        wanted = [b.replace("requested_category", "category") for b in wanted]
    seen: list[str] = []
    for b in wanted:
        if b not in seen:
            seen.append(b)
    singles = [b for b in seen if "+" not in b]
    pairs = [b for b in seen if "+" in b]
    scope.breakdowns = (sorted(singles, key=lambda b: BREAKDOWNS.index(b) if b in BREAKDOWNS else 99)
                        + pairs)[:MAX_BREAKDOWNS]

    if use_rules and city_tier_requested(question):
        scope.unavailable.append("City tier is not recorded in the data (the recorded tier is a loyalty tier: "
                                 "Gold, Silver or Bronze), so there is no city-tier breakdown.")
    for item in plan.get("unavailable") or []:
        text = (f"{item.get('asked')}: {item.get('reason')}" if isinstance(item, dict) else str(item)).strip()
        if _CHARTS.search(text.split(":")[0]):
            continue                          # every table has a chart; saying otherwise would be false
        if text and not (use_rules and "city tier" in text.lower()) and text not in scope.unavailable:
            scope.unavailable.append(text)
    return scope


def category_clarifications(scope: Scope, paths: list[tuple[str, str, str]]) -> list[dict]:
    """Ask about the categories the brief had to leave out, with real names from the data as the options."""
    out = []
    for asked, close in scope.unresolved.items():
        out.append({"part": 1, "question": f"Which category did you mean by '{asked}'?",
                    "why": "No category in the data has that name, so it was left out of this brief.",
                    "options": list(close)[:4]})
    for asked, names in scope.ambiguous.items():
        for name in names:
            masters = sorted({m for m, c, sc in paths if name.lower() in (c.lower(), sc.lower())})
            out.append({"part": 1, "question": f"Which '{name}' did you mean in '{asked}'?",
                        "why": "Several master categories have one with this name, so it was left out.",
                        "options": [f"{name} in {m}" for m in masters][:4]})
    return out[:3]


def requested_category_sql(category_map: dict[str, list[str]]) -> str:
    """A CASE that labels each journey with the asked-for category it belongs to (paths already validated)."""
    def lit(v: str) -> str:
        return exp.Literal.string(str(v)).sql(dialect="postgres")

    whens = []
    for asked, paths in category_map.items():
        label = paths[0].split(" > ")[-1] if len(paths) == 1 else asked.strip().capitalize()
        options = ", ".join(lit(v) for v in paths)
        whens.append(f"WHEN cat.master_category IN ({options}) "
                     f"OR CONCAT(cat.master_category, ' > ', cat.category) IN ({options}) "
                     f"OR CONCAT(cat.master_category, ' > ', cat.category, ' > ', cat.sub_category) IN ({options}) "
                     f"THEN {lit(label)}")
    return "CASE " + " ".join(whens) + " ELSE 'Other' END"


def label(breakdown: str) -> str:
    return " × ".join(LABELS.get(part, part.replace("_", " ").capitalize()) for part in breakdown.split("+"))


# --------------------------------------------------------------------------- #
# Findings, computed from the rows
# --------------------------------------------------------------------------- #

@dataclass
class Finding:
    id: str
    kind: str                 # lead | behind | weak | coverage
    breakdown: str
    segment: str
    text: str
    apps: list[str]
    score: float = 0.0        # the gap in points; negative = behind or weak
    p: float | None = None    # two-sided Fisher's exact test; None for coverage notes


def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _pct(value) -> str:
    return f"{value:g}%"


def _count(value) -> str:
    return f"{int(value):,}"


def _p_text(p: float) -> str:
    return "p < 0.001" if p < 0.001 else f"p = {p:.3f}"


def fisher_p(x1: int, n1: int, x2: int, n2: int) -> float:
    """Two-sided Fisher's exact test: could x1 of n1 vs x2 of n2 conversions differ by chance alone?

    Exact rather than a normal approximation, because these rates are a few percent of a few dozen journeys."""
    x1, n1, x2, n2 = int(x1), int(n1), int(x2), int(n2)
    total, hits = n1 + n2, x1 + x2
    if min(n1, n2) <= 0 or hits in (0, total):
        return 1.0
    lg = math.lgamma

    def log_pmf(k: int) -> float:            # hypergeometric: k of the hits fall in the first group
        return (lg(hits + 1) - lg(k + 1) - lg(hits - k + 1) + lg(total - hits + 1) - lg(n1 - k + 1)
                - lg(total - hits - n1 + k + 1) - lg(total + 1) + lg(n1 + 1) + lg(total - n1 + 1))

    observed = log_pmf(x1)
    low, high = max(0, n1 - (total - hits)), min(hits, n1)
    p = sum(math.exp(v) for v in (log_pmf(k) for k in range(low, high + 1)) if v <= observed + 1e-7)
    return min(1.0, p)


_NEEDED = {"segment", "app", "users", "journeys", "converted", "conversion_pct", "leaked_pct", "purchase_share_pct"}


def _groups(columns: list[str], rows: list[list], focus: str,
            family: dict[str, str]) -> list[tuple[str, dict | None, list[dict]]]:
    """(segment, our row or None, competitor rows) per segment. Sub-apps belong to their main app, so Flipkart
    Minutes is never Flipkart's competitor."""
    if not _NEEDED <= set(columns):
        return []

    def fam(app: str) -> str:
        return family.get(app, app).lower()

    by_segment: dict[str, list[dict]] = {}
    for r in rows:
        row = dict(zip(columns, r))
        by_segment.setdefault(str(row["segment"]), []).append(row)
    out = []
    for segment, group in by_segment.items():
        mine = next((r for r in group if str(r["app"]).lower() == focus.lower()
                     and _num(r["conversion_pct"]) is not None), None)
        rivals = [r for r in group if fam(str(r["app"])) != fam(focus) and str(r["app"]) != "Unknown app"
                  and _num(r["conversion_pct"]) is not None]
        out.append((segment, mine, rivals))
    return out


def compared_groups(columns: list[str], rows: list[list], focus: str, family: dict[str, str]) -> int:
    """Groups where we AND at least one competitor reached the user base: the only ones a gap can come from."""
    return sum(1 for _, mine, rivals in _groups(columns, rows, focus, family) if mine is not None and rivals)


def _catch_all(segment: str) -> bool:
    return any(_CATCH_ALL.match(part.strip()) for part in str(segment).split("·"))


def findings(breakdown: str, columns: list[str], rows: list[list], focus: str,
             family: dict[str, str], min_users: int, overall: dict | None = None) -> list[Finding]:
    """Candidate leads, gaps and weak spots for ``focus`` in one breakdown, each with its p-value
    (``keep_significant`` decides which are reported). Every number is copied from a row, or is a
    difference or a rest-of-total shown beside the numbers it comes from.

    Like for like: within a category every competitor there is a fair benchmark. Across categories (overall,
    gender, age ...) conversion mostly reflects what an app sells - a grocery app converts more often than a
    fashion app - so the benchmark is the biggest competitor in the group, not the highest rate."""
    groups = _groups(columns, rows, focus, family)
    by_category = any(part in CATEGORY_DIMS for part in breakdown.split("+"))
    out: list[Finding] = []
    missing = alone = 0
    name = label(breakdown)
    for segment, mine, rivals in groups:
        if mine is None:
            missing += 1
            continue
        if not rivals:
            alone += 1
        if _catch_all(segment):
            continue
        where = "Overall" if breakdown == "overall" else segment if by_category else f"{name} {segment}"
        conv, x, n = _num(mine["conversion_pct"]), int(mine["converted"] or 0), int(mine["journeys"])
        if rivals:
            best = max(rivals, key=lambda r: (_num(r["conversion_pct"]), _num(r["journeys"]) or 0))
            biggest = max(rivals, key=lambda r: (_num(r["journeys"]) or 0, _num(r["conversion_pct"])))
            for rival in (rivals if by_category else [biggest]):
                gap = round(conv - _num(rival["conversion_pct"]), 1)
                if abs(gap) < GAP_PTS or (gap > 0 and by_category and rival is not best):
                    continue                  # a lead in a category has to be a lead over its best competitor
                p = fisher_p(x, n, rival["converted"] or 0, rival["journeys"])
                role = ("the biggest competitor here" if not by_category else
                        "the best-converting competitor here" if rival is best else "a competitor here")
                text = (f"{where}: {focus} converted {_pct(conv)} ({_count(x)} of {_count(n)} journeys, "
                        f"{_count(mine['users'])} users) vs {_pct(rival['conversion_pct'])} "
                        f"({_count(rival['converted'] or 0)} of {_count(rival['journeys'])}) for {rival['app']}, "
                        f"{role} ({gap:+.1f} pts, {_p_text(p)}); purchase share {focus} "
                        f"{_pct(mine['purchase_share_pct'] or 0)} vs {rival['app']} "
                        f"{_pct(rival['purchase_share_pct'] or 0)}.")
                out.append(Finding("", "lead" if gap > 0 else "behind", breakdown, segment, text,
                                   [focus, str(rival["app"])], gap, p))
        if overall and breakdown != "overall" and _num(overall.get("journeys")):
            rest_x = int(overall.get("converted") or 0) - x
            rest_n = int(overall["journeys"]) - n
            if rest_n > 0 and rest_x >= 0:
                rest = round(100.0 * rest_x / rest_n, 1)
                delta = round(conv - rest, 1)
                if delta <= -WEAK_PTS:
                    p = fisher_p(x, n, rest_x, rest_n)
                    out.append(Finding(
                        "", "weak", breakdown, segment,
                        f"{where}: {focus} converted {_pct(conv)} ({_count(x)} of {_count(n)} journeys, "
                        f"{_count(mine['users'])} users) vs {_pct(rest)} in the rest of its journeys "
                        f"({delta:+.1f} pts, {_p_text(p)}); {_pct(mine['leaked_pct'] or 0)} of these journeys "
                        f"ended in a purchase on another app.", [focus], delta, p))
    if missing and breakdown != "overall":
        out.append(Finding("", "coverage", breakdown, "", (
            f"{name}: {focus} has fewer than {min_users} users (or no journeys) in {missing} of "
            f"{len(groups)} groups in the result, so those groups are not compared."), [focus]))
    if alone and breakdown == "overall":
        mine = groups[0][1]
        out.append(Finding("", "coverage", breakdown, "", (
            f"Overall: no competitor reached {min_users} users, so {focus} ({_pct(mine['conversion_pct'])} of "
            f"{_count(mine['journeys'])} journeys converted, {_count(mine['users'])} users) is not compared."),
            [focus]))
    elif alone:
        out.append(Finding("", "coverage", breakdown, "", (
            f"{name}: no competitor reached {min_users} users in {alone} of the {len(groups) - missing} groups "
            f"where {focus} did, so those groups are not compared."), [focus]))
    return out


def keep_significant(found: list[Finding], q: float = FDR) -> tuple[list[Finding], int]:
    """The candidates that are unlikely to be chance, controlling the false-discovery rate across every
    comparison in the brief (Benjamini-Hochberg): one brief tests dozens of groups, and at p < 0.05 each,
    some would look real by luck alone. Keeps the largest significant gap per group and kind, then the
    strongest few per breakdown. Returns (kept findings with the coverage notes, gaps left out as chance)."""
    tested = sorted((f for f in found if f.p is not None), key=lambda f: f.p)
    cutoff = max((i for i, f in enumerate(tested, 1) if f.p <= q * i / len(tested)), default=0)
    best: dict[tuple, Finding] = {}
    for f in tested[:cutoff]:
        key = (f.breakdown, f.segment, f.kind)
        if key not in best or abs(f.score) > abs(best[key].score):
            best[key] = f
    order = {b: i for i, b in enumerate(dict.fromkeys(f.breakdown for f in found))}
    kinds = ("lead", "behind", "weak", "coverage")
    kept: list[Finding] = []
    for b in order:
        for kind in kinds[:3]:
            kept += sorted((f for f in best.values() if f.breakdown == b and f.kind == kind),
                           key=lambda f: -abs(f.score))[:TOP_FINDINGS]
        kept += [f for f in found if f.breakdown == b and f.kind == "coverage"]
    return kept, len(tested) - cutoff


# Measures the scorecard does not hold, answered by a vetted session analysis (sessions carry no category).
SESSION_MEASURES = {"visitors": ("active_users_by_app", "Visitors (app sessions)"),
                    "cross_app_switching": ("competitor_switch_by_app", "Switching to a competitor app (sessions)")}


def _same_app(a, b) -> bool:
    return re.sub(r"[^a-z0-9]", "", str(a).lower()) == re.sub(r"[^a-z0-9]", "", str(b).lower())


def session_fact(measure: str, columns: list[str], rows: list[list], focus: str) -> Finding | None:
    """One context line on visitors or competitor switching for ``focus``, copied from the session rows."""
    table = [dict(zip(columns, r)) for r in rows]
    mine = next((r for r in table if _same_app(r.get("app"), focus)), None)
    if measure == "visitors" and {"app", "users", "sessions"} <= set(columns) and table:
        if mine is None:
            return Finding("", "context", "visitors", "", f"Visitors: {focus} has no app sessions in this period.",
                           [focus])
        rank = next(i for i, r in enumerate(table, 1) if r is mine)
        top = table[0]
        text = (f"Visitors (all categories): {focus} had {_count(mine['users'])} visitors in "
                f"{_count(mine['sessions'])} sessions, {_pct(mine.get('pct_of_active_users') or 0)} of all active "
                f"users, ranked {rank} of {len(table)} apps")
        text += "." if rank == 1 else f"; {top['app']} had the most ({_count(top['users'])})."
        return Finding("", "context", "visitors", "", text, list(dict.fromkeys([focus, str(top["app"])])))
    if measure == "cross_app_switching" and {"app", "sessions", "switch_pct"} <= set(columns) and table:
        if mine is None:
            return Finding("", "context", "switching", "", f"Switching: {focus} has no app sessions in this period.",
                           [focus])
        rated = [r for r in table if _num(r.get("switch_pct")) is not None]
        low, high = min(rated, key=lambda r: r["switch_pct"]), max(rated, key=lambda r: r["switch_pct"])
        return Finding("", "context", "switching", "", (
            f"Switching (all categories): in {_pct(mine['switch_pct'])} of {focus}'s {_count(mine['sessions'])} "
            f"sessions the user opened a competitor app within 5 minutes; lowest {low['app']} "
            f"{_pct(low['switch_pct'])}, highest {high['app']} {_pct(high['switch_pct'])}."),
            list(dict.fromkeys([focus, str(low["app"]), str(high["app"])])))
    return None


def directional(candidates: list[Finding], kept: list[Finding], limit: int = 5) -> list[Finding]:
    """The largest gaps that did not pass the significance test, labelled as such. They may motivate a test,
    never a claim."""
    shown = {id(f) for f in kept}
    out = []
    for f in sorted((f for f in candidates if f.p is not None and id(f) not in shown), key=lambda f: -abs(f.score)):
        if any((o.breakdown, o.segment) == (f.breakdown, f.segment) for o in out):
            continue
        out.append(replace(f, kind="directional",
                           text=f.text.rstrip(".") + "; not statistically reliable at this sample size."))
        if len(out) == limit:
            break
    return out


def number_findings(all_findings: list[Finding]) -> list[Finding]:
    for i, f in enumerate(all_findings, 1):
        f.id = f"F{i}"
    return all_findings


def period_words(scope: Scope, last_day: str | None) -> str:
    """How a follow-up question should name the brief's period, so the router reads it back the same way."""
    if scope.date_from and scope.date_from == scope.date_to and last_day:
        try:
            gap = (date.fromisoformat(str(last_day)[:10]) - date.fromisoformat(scope.date_from)).days
        except ValueError:
            gap = None
        if gap in (0, 1):
            return "today" if gap == 0 else "yesterday"
    if scope.date_from and scope.date_from == scope.date_to:
        return f"on {scope.date_from}"
    if scope.date_from and scope.date_to:
        return f"from {scope.date_from} to {scope.date_to}"
    return "over all loaded data"


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def wider_questions(scope: Scope, date_range: tuple) -> list[str]:
    """The same brief with a bigger base: a longer period, every category, or a smaller minimum group.
    Every asked category and split is kept, worded so the brief reader takes it back the same way."""
    first, last = (str(d)[:10] if d else None for d in date_range)
    words = {"requested_category": "category", "master_category": "category", "sub_category": "category"}
    splits = list(dict.fromkeys(words.get(b, label(b).lower()) for b in scope.breakdowns
                                if "+" not in b and b != "overall")) or ["category"]
    by = _and(splits)
    asked = list(scope.category_map) + list(scope.unresolved)
    within = f" in {_and(asked)}" if asked else ""
    base = f"minimum {scope.min_users} users per group"
    head = f"Executive summary of how {scope.focus_app} performed"
    period = period_words(scope, last)
    out = []
    if scope.date_from and scope.date_to:
        days = (date.fromisoformat(scope.date_to) - date.fromisoformat(scope.date_from)).days + 1
        covered = ((date.fromisoformat(last) - date.fromisoformat(first)).days + 1) if first and last else 0
        longer = ("in the last 7 days" if days <= 3 and covered >= 7 else          # at least double the period
                  "in the last 30 days" if days <= 14 and covered >= 30 else "over all loaded data")
        out.append(f"{head} {longer}{within} vs competitors, by {by}, {base}")
    if asked:
        out.append(f"{head} {period} across all categories vs competitors, by {by}, {base}")
    if scope.min_users > 10:
        out.append(f"{head} {period}{within} vs competitors, by {by}, minimum 10 users per group")
    return out[:4]


def focus_row(columns: list[str], rows: list[list], focus: str) -> dict | None:
    for r in rows:
        row = dict(zip(columns, r))
        if str(row.get("app", "")).lower() == focus.lower():
            return row
    return None


# --------------------------------------------------------------------------- #
# The written summary and its checks
# --------------------------------------------------------------------------- #

SUMMARY_KINDS = (("went_well", "What went well"), ("stumbled", "Where we stumbled"),
                 ("competitors_better", "Where competitors did better"), ("actions", "Suggested actions"))

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {kind: {"type": "array", "maxItems": 5 if kind == "actions" else 4, "items": {
        "type": "object",
        "properties": {"text": {"type": "string"}, "findings": {"type": "array", "items": {"type": "string"}}},
        "required": ["text", "findings"]}} for kind, _ in SUMMARY_KINDS},
    "required": [kind for kind, _ in SUMMARY_KINDS],
}

SUMMARY_SYSTEM = """You write the executive summary of a shopping-app performance brief for {focus_app}'s team.
Use ONLY the numbered findings below. They were computed from the data, and every [lead], [behind] and [weak]
finding passed a significance test; [coverage] lines are context and are never cited. Every bullet:
- cites the finding ids it is based on in "findings" (at least one),
- repeats numbers exactly as the cited findings write them, never a new or rounded number,
- names only apps and segments that appear in the cited findings.
went_well cites only [lead] findings; stumbled only [weak] findings ({focus_app}'s own weak segments);
competitors_better only [behind] findings. Never cite one finding under two of these three lists. Leave a list
empty when no finding of its kind exists.
actions: as many as the brief asks for (3 when it does not say, at most 5): concrete, testable actions for the
next sale day, each citing the findings that motivate it (no numbers needed). [directional] findings are gaps
that may be chance and [context] findings are facts about visitors, switching or the data itself: an action
resting only on them must be worded as a test to run or a data fix, never as a proven gap.
One sentence per bullet, at most 35 words, in business language: never name tables, columns, SQL or the model.
Reply with JSON: {{"went_well": [{{"text": "...", "findings": ["F1"]}}], "stumbled": [...],
"competitors_better": [...], "actions": [...]}}"""


def _within(name: str, text: str) -> bool:
    """``name`` as whole words inside ``text`` ("male" is not inside "female")."""
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(name.lower())}(?![a-z0-9])", text.lower()))


def _mentions(text: str, names: set[str]) -> set[str]:
    return {n for n in names if len(n) >= 3 and _within(n, text)}


def check_bullet(text: str, cited: list[str], by_id: dict[str, Finding], apps: list[str]) -> list[str]:
    """Why a written bullet cannot be shown (empty = it only repeats what its cited findings say)."""
    problems = []
    sources = [by_id[c] for c in cited if c in by_id]
    if not sources:
        return ["it cites no finding"]
    evidence = [f.text for f in sources]
    bad = grounding.ungrounded_numbers(text, evidence)
    if bad:
        problems.append("numbers not in its findings: " + ", ".join(bad[:4]))
    allowed_apps = {a.lower() for f in sources for a in f.apps}
    stray = {a for a in _mentions(text, set(apps)) if a.lower() not in allowed_apps}
    # "Amazon Now" also contains "Amazon": only flag a name that is not part of an allowed longer name.
    stray = {a for a in stray if not any(_within(a, ok) and a.lower() != ok for ok in allowed_apps)}
    if stray:
        problems.append("apps not in its findings: " + ", ".join(sorted(stray)))
    segments = {f.segment for f in by_id.values() if f.segment and f.segment not in ("Unknown", "Other")
                and not re.fullmatch(r"[\d\s+<>-]+", f.segment)}
    cited_segments = {f.segment.lower() for f in sources}
    wrong = {s for s in _mentions(text, segments) if s.lower() not in cited_segments
             and not any(_within(s, c) for c in cited_segments)}
    if wrong:
        problems.append("segments not in its findings: " + ", ".join(sorted(wrong)))
    return problems


SECTION_KINDS = {"went_well": {"lead"}, "stumbled": {"weak"}, "competitors_better": {"behind"},
                 "actions": {"lead", "behind", "weak", "directional", "context", "coverage"}}


def validated_summary(reply: dict, by_id: dict[str, Finding], apps: list[str]) -> tuple[dict, list[str]]:
    """Keep the bullets that pass ``check_bullet``, sit under the heading their findings belong to, and do not
    repeat a finding already shown under another heading; report the ones dropped."""
    kept: dict[str, list[tuple[str, list[str]]]] = {}
    dropped = []
    shown: set[str] = set()
    for kind, title in SUMMARY_KINDS:
        for item in (reply.get(kind) or [])[:5 if kind == "actions" else 4]:
            if not isinstance(item, dict):
                continue
            text = plain(str(item.get("text") or "").strip())
            cited = [str(c).strip() for c in item.get("findings") or [] if str(c).strip()]
            if not text:
                continue
            problems = check_bullet(text, cited, by_id, apps)
            kinds = {by_id[c].kind for c in cited if c in by_id}
            if kind == "actions":
                if not kinds & SECTION_KINDS["actions"]:
                    problems.append("an action must rest on a finding")
            else:
                if kinds - SECTION_KINDS[kind]:
                    problems.append(f"cites {', '.join(sorted(kinds - SECTION_KINDS[kind]))} findings under "
                                    f"'{title}'")
                if shown & set(cited):
                    problems.append("repeats a finding shown under another heading")
            if problems:
                dropped.append(f"{text[:80]} ({'; '.join(problems)})")
                continue
            kept.setdefault(kind, []).append((text, cited))
            if kind != "actions":
                shown |= set(cited)
    return kept, dropped


def computed_summary(all_findings: list[Finding]) -> dict:
    """The summary without a model: the strongest findings of each kind, as computed. Whole breakdowns rank
    ahead of crossed ones (a bigger base), and no group appears under two headings."""
    pick = {"competitors_better": "behind", "went_well": "lead", "stumbled": "weak"}
    out: dict[str, list[tuple[str, list[str]]]] = {}
    shown: set[tuple[str, str]] = set()
    for kind, source in pick.items():
        ranked = sorted((f for f in all_findings if f.kind == source),
                        key=lambda f: -abs(f.score) * (0.5 if "+" in f.breakdown else 1.0))
        chosen = []
        for f in ranked:
            if (f.breakdown, f.segment) in shown:
                continue
            shown.add((f.breakdown, f.segment))
            chosen.append(f)
            if len(chosen) == TOP_FINDINGS:
                break
        if chosen:
            out[kind] = [(f.text, [f.id]) for f in chosen]
    return out


def render_summary(summary: dict, scope: Scope, proven: bool = True) -> str:
    """``proven`` = at least one gap passed the significance test; otherwise actions are tests to run."""
    blocks = []
    for kind, title in SUMMARY_KINDS:
        items = summary.get(kind) or []
        if not items:
            continue
        heading = title + (f" for {next_day(scope)}" if kind == "actions" and next_day(scope) else "")
        if kind == "actions" and not proven:
            heading += " (tests to run: no gap here is statistically reliable yet)"
        blocks.append(f"**{heading}**\n" + "\n".join(
            f"- {text} [{', '.join(cited)}]" for text, cited in items))
    return "\n\n".join(blocks)


def next_day(scope: Scope) -> str:
    """The day after a one-day brief ("yesterday" -> today), which is what its actions are for."""
    if scope.date_from and scope.date_from == scope.date_to:
        try:
            return (date.fromisoformat(scope.date_to) + timedelta(days=1)).isoformat()
        except ValueError:
            pass
    return ""


SCOPE_SCHEMA = {
    "type": "object",
    "properties": {
        "focus_app": {"type": "string"},
        "date_from": {"type": "string"},
        "date_to": {"type": "string"},
        "categories": {"type": "array", "items": {"type": "object", "properties": {
            "asked": {"type": "string"}, "matches": {"type": "array", "items": {"type": "string"}}},
            "required": ["asked", "matches"]}},
        "breakdowns": {"type": "array", "items": {"type": "string"}},
        "min_users": {"type": "integer"},
        "unavailable": {"type": "array", "items": {"type": "object", "properties": {
            "asked": {"type": "string"}, "reason": {"type": "string"}}}},
    },
    "required": ["focus_app", "categories", "breakdowns"],
}

SCOPE_SYSTEM = """You set the scope of an executive-summary brief about shopping-app data. Do not analyse or answer.
Reply with JSON:
- focus_app: the app the brief is about: the company the asker says they work for in this message, else the app
  the question asks about ("how did Amazon perform"), otherwise {home_app}. Exact name from: {apps}.
- date_from, date_to: ISO dates of the period asked about, inside {first_day} to {last_day}. "Yesterday" is the day
  before {last_day}; "today" is {last_day}. Leave both empty when no period is named.
- categories: every product category the asker names, as {{"asked": "<their words>", "matches": [names from the
  category list below that mean the same products]}}. Use [] when nothing in the list means the same; never stretch
  a name to a different product family.
- breakdowns: the splits the asker wants, from: {breakdowns}. Use "requested_category" to split by the asked
  categories. For "interlocking", "micro" or cross-cuts add pairs joined with "+" (e.g. "requested_category+gender").
- min_users: the smallest group size the asker allows (distinct users); 0 when not stated.
- unavailable: [{{"asked": "...", "reason": "..."}}] for requested splits the data does not hold. City tier is not
  recorded (user_master.tier is a loyalty tier).

Category list (master category: categories):
{categories}"""
