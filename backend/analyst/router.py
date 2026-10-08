"""Decide what a question needs: a vetted metric (with parameters) or tables for custom SQL.

Deterministic first: questions that clearly match a metric are answered without
the language model (fast, exact, works offline). The model is asked only when
the match is weak; its choice is validated against the catalog.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

from .catalog import Catalog, MetricDoc

try:
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover
    fuzz = None

_STOP = set("""a an the of for in on at to from with and or is are was were be been do does did has have had
me my i we you your our us please can could would will what which who whose how many much show give tell list
get find by per each all any this that these those it its their there than more most less least top vs versus
right now currently overall ok okay""".split())

SYNONYMS = {
    "buy": "purchase", "buying": "purchase", "bought": "purchase", "purchases": "purchase", "purchased": "purchase",
    "orders": "order", "ordered": "order", "leak": "leakage", "leaks": "leakage", "leaked": "leakage", "lose": "leakage",
    "losing": "leakage", "lost": "leakage", "retain": "retention", "retained": "retention", "converted": "conversion",
    "convert": "conversion", "converts": "conversion", "switching": "switch", "switches": "switch", "switched": "switch",
    "apps": "app", "platforms": "app", "platform": "app", "journeys": "journey", "sessions": "session", "users": "user",
    "shoppers": "user", "customers": "user", "people": "user", "categories": "category", "searches": "search",
    "searched": "search", "queries": "query", "aov": "order value", "gmv": "order value", "revenue": "order value",
    "spend": "order value", "competitors": "competitor", "trend": "trend", "trends": "trend", "daily": "day",
    "days": "day", "weeks": "week", "hours": "hour", "hrs": "hour", "hr": "hour",
}

_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower())
    out = []
    for w in words:
        w = w.replace("'s", "")
        if w in _STOP or len(w) < 2:
            continue
        out.append(SYNONYMS.get(w, w))
    return out


@dataclass
class Extracted:
    apps: list[str] = field(default_factory=list)
    home_used: bool = False
    window_hours: int | None = None
    last_hours: int | None = None          # "in the last 24 hours" = a recent period, not a purchase window
    date_from: str | None = None
    date_to: str | None = None
    category: str | None = None
    dimension: str | None = None
    limit: int | None = None
    from_app: str | None = None
    to_app: str | None = None

    def any(self) -> bool:
        return bool(self.apps or self.window_hours or self.last_hours or self.date_from or self.date_to or self.category
                    or self.dimension or self.limit)


@dataclass
class Route:
    kind: str                      # metric | sql | unsupported | clarify | help
    metric: MetricDoc | None = None
    params: dict = field(default_factory=dict)
    tables: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""
    alternatives: list[str] = field(default_factory=list)
    source: str = "rules"
    focus: list[str] = field(default_factory=list)       # apps to highlight in a by-app result
    notes: list[str] = field(default_factory=list)       # caveats found while routing
    # The query that answered the previous turn, when this one only changes a setting on it.
    prior_sql: str = ""


UNSUPPORTED = {
    "city tier": "city tier is not recorded (user_master.tier is a loyalty tier: Gold/Silver/Bronze)",
    "shopsy": None,  # handled by app list: only unsupported when Shopsy is not an app in the data
}


class Router:
    def __init__(self, catalog: Catalog):
        self.cat = catalog
        self._idf: dict[str, float] = {}
        self._docs: dict[str, Counter] = {}
        self._build_index()

    # ------------------------------------------------------------- indexing
    def _build_index(self) -> None:
        docs: dict[str, list[str]] = {}
        for m in self.cat.metrics.values():
            text = " ".join([m.title, m.description, " ".join(m.tags) * 2, " ".join(m.questions)])
            docs[f"metric:{m.name}"] = tokens(text)
        for t in self.cat.tables.values():
            cols = " ".join(f"{c.name.replace('_', ' ')} {c.meaning}" for c in t.columns.values() if c.policy != "blocked")
            text = " ".join([t.name.replace("_", " "), t.title, t.description, " ".join(t.tags) * 2,
                             " ".join(t.questions), cols])
            docs[f"table:{t.name}"] = tokens(text)
        n = len(docs) or 1
        df = Counter(w for d in docs.values() for w in set(d))
        self._idf = {w: math.log(1 + n / c) for w, c in df.items()}
        self._docs = {k: Counter(v) for k, v in docs.items()}

    def _bm25(self, q: list[str], key: str) -> float:
        doc = self._docs[key]
        length = sum(doc.values()) or 1
        score = 0.0
        for w in set(q):
            f = doc.get(w, 0)
            if f:
                score += self._idf.get(w, 0) * (f * 2.2) / (f + 1.2 * (0.25 + 0.75 * length / 60))
        return score

    # ------------------------------------------------------------ extraction
    def extract(self, question: str) -> Extracted:
        ex = Extracted()
        low = question.lower()
        # Apps, longest names first so "Amazon Now" wins over "Amazon".
        found: list[tuple[int, str]] = []
        taken: list[tuple[int, int]] = []
        for app in sorted(self.cat.apps, key=len, reverse=True):
            for m in re.finditer(rf"\b{re.escape(app.lower())}\b", low):
                if any(a <= m.start() < b for a, b in taken):
                    continue
                taken.append((m.start(), m.end()))
                found.append((m.start(), app))
        if fuzz is not None and not found:
            for word in re.findall(r"[a-z]{5,}", low):
                for app in self.cat.apps:
                    if " " not in app and fuzz.ratio(word, app.lower()) >= 86:
                        found.append((low.find(word), app))
        found.sort()
        ex.apps = list(dict.fromkeys(a for _, a in found))
        home = self.cat.home_app
        if home and re.search(r"\b(we|us|our|ours)\b", low) and home not in ex.apps and home in self.cat.apps:
            pos = re.search(r"\b(we|us|our|ours)\b", low).start()
            found.append((pos, home))
            found.sort()
            ex.apps = list(dict.fromkeys(a for _, a in found))
            ex.home_used = True
        # "Amazon -> Meesho", "from Amazon to Meesho", "Amazon to Meesho"
        if len(ex.apps) >= 2 and re.search(r"(->|→|=>|\bto\b|\bthen\b|\bswitch)", low):
            ex.from_app, ex.to_app = ex.apps[0], ex.apps[1]

        m = re.search(r"\b(24|48|72)\s*-?\s*(h|hr|hrs|hour|hours)\b", low) or re.search(r"within (24|48|72)\b", low)
        if m:
            ex.window_hours = int(m.group(1))
        elif re.search(r"\b(one|1) day\b", low):
            ex.window_hours = 24
        elif re.search(r"\b(two|2) days\b", low):
            ex.window_hours = 48
        elif re.search(r"\b(three|3) days\b", low):
            ex.window_hours = 72

        m = re.search(r"\b(?:last|past|previous|recent|in the last|within the last)\s+(\d{1,4})\s*-?\s*(?:h|hr|hrs|hours?)\b", low)
        if m:
            ex.last_hours = int(m.group(1))
        elif re.search(r"\b(last|past) hour\b", low):
            ex.last_hours = 1

        m = re.search(r"\btop\s+(\d{1,3})\b", low)
        if m:
            ex.limit = int(m.group(1))

        dims = [("hour_of_day", r"time of day|hour of day|\bhour\b|\bhourly\b"), ("age_group", r"\bage\b|\bages\b|age group"),
                ("device_model", r"device|phone model|handset"), ("gender", r"gender|male|female"),
                ("zone", r"\bzones?\b"), ("state", r"\bstates?\b"), ("tier", r"loyalty|\btier\b"), ("city", r"\bcity\b|\bcities\b")]
        for dim, rx in dims:
            if re.search(rx, low):
                ex.dimension = dim
                break

        self._dates(low, ex)

        quoted = re.search(r"(?<![A-Za-z])[\"“']([^\"”']{3,60})[\"”'](?![A-Za-z])", question)
        if quoted:
            ex.category = quoted.group(1).strip()
        elif self.cat.categories:
            best, best_len = None, 0
            for c in self.cat.categories:
                cl = c.lower()
                if len(cl) > 3 and cl in low and len(cl) > best_len:
                    best, best_len = c, len(cl)
            if best is None and fuzz is not None:
                for c in self.cat.categories:
                    if len(c) > 6 and fuzz.partial_ratio(c.lower(), low) >= 92 and len(c) > best_len:
                        best, best_len = c, len(c)
            if best and best.lower() not in {a.lower() for a in self.cat.apps}:
                ex.category = best
        return ex

    def _dates(self, low: str, ex: Extracted) -> None:
        end = self._anchor()
        if re.search(r"\btoday\b", low):
            ex.date_from = ex.date_to = end.isoformat()
        elif re.search(r"\byesterday\b", low):
            d = end - timedelta(days=1)
            ex.date_from = ex.date_to = d.isoformat()
        elif m := re.search(r"\blast (\d{1,3}) days\b", low):
            ex.date_from, ex.date_to = (end - timedelta(days=int(m.group(1)) - 1)).isoformat(), end.isoformat()
        elif re.search(r"\bthis week\b", low):
            ex.date_from, ex.date_to = (end - timedelta(days=end.weekday())).isoformat(), end.isoformat()
        elif re.search(r"\blast week\b", low):
            start = end - timedelta(days=end.weekday() + 7)
            ex.date_from, ex.date_to = start.isoformat(), (start + timedelta(days=6)).isoformat()
        iso = re.findall(r"\b(20\d\d-\d\d-\d\d)\b", low)
        dm = [(int(d), _MONTHS[mo[:3]]) for d, mo in re.findall(
            r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*", low)]
        dates = iso + [date(end.year, mo, d).isoformat() for d, mo in dm if 1 <= d <= 31]
        if dates:
            ex.date_from, ex.date_to = min(dates), max(dates)
            if len(dates) == 1 and re.search(r"\b(since|after|from)\b", low):
                ex.date_to = None
            elif len(dates) == 1 and re.search(r"\b(before|until|till)\b", low):
                ex.date_from = None

    def _anchor(self) -> date:
        """'Today' = the last day with data (the data may lag the calendar)."""
        last = self.cat.date_range[1]
        try:
            return date.fromisoformat(str(last)) if last else date.today()
        except ValueError:
            return date.today()

    # --------------------------------------------------------------- routing
    def rank(self, question: str) -> tuple[list[tuple[float, MetricDoc]], list[tuple[float, str]]]:
        q = tokens(question)
        low = question.lower()
        metric_scores = []
        for m in self.cat.metrics.values():
            if not m.available:
                continue
            score = self._bm25(q, f"metric:{m.name}")
            for tag in m.tags:
                if " " in tag and tag in low:
                    score += 3.0                                # phrase tags are strong evidence
            if fuzz is not None and m.questions:
                score += max(fuzz.token_set_ratio(low, qq.lower()) for qq in m.questions) / 100 * 3
            metric_scores.append((score, m))
        metric_scores.sort(key=lambda x: -x[0])
        table_scores = sorted(((self._bm25(q, f"table:{t}"), t) for t in self.cat.allowed_tables),
                              key=lambda x: -x[0])
        return metric_scores, table_scores

    def route(self, question: str, state: dict | None = None) -> tuple[Route, Extracted]:
        ex = self.extract(question)
        low = question.lower()
        notes = []
        if "city tier" in low:
            notes.append("City tier is not recorded; user_master.tier is a loyalty tier (Gold/Silver/Bronze).")

        # "duration" also occurs in pipeline documentation, but when a user
        # asks which shopping app is top/longest they mean session duration.
        if (re.search(r"\b(app|session)s?\b", low)
                and re.search(r"\b(duration|session time|longest session|average minutes?)\b", low)):
            metric = self.cat.metrics.get("session_summary_by_app")
            if metric is not None and metric.available:
                params, focus = self._params_for(metric, ex, question)
                return Route("metric", metric, params, metric.tables, 1.0,
                             "explicit app-session duration intent", focus=focus, notes=notes), ex
        metrics, tables = self.rank(question)
        top_tables = [t for s, t in tables[:4] if s > 0]

        # Follow-up fragment ("what about Flipkart?", "and for 48h", "same for last week").
        fragment = bool(re.match(r"^\s*(and|also|what about|how about|same|now|only|for|but|then)\b", low)) \
            or (len(tokens(question)) <= 4 and ex.any())
        # Short references such as "which app is top in these durations?"
        # depend on the previous result even when they contain no new filter.
        if state and len(tokens(question)) <= 8 and re.search(
                r"\b(this|that|these|those|same|previous|above|result|results|duration|durations)\b", low):
            fragment = True
        if state and state.get("metric") and fragment and (not metrics or metrics[0][0] < 9):
            prev = self.cat.metrics.get(state["metric"])
            if prev is not None:
                params = dict(state.get("params") or {})
                new, focus = self._params_for(prev, ex, question)
                if focus:
                    params.pop("app", None)
                params.update(new)
                return Route("metric", prev, params, prev.tables, 0.9, "follow-up of the previous analysis",
                             focus=focus, notes=notes), ex

        if not metrics:
            return Route("sql", tables=top_tables, confidence=0.2), ex
        best_score, best = metrics[0]
        second = metrics[1][0] if len(metrics) > 1 else 0.0
        confidence = min(1.0, best_score / 12) * (0.6 + 0.4 * min(1.0, (best_score - second) / 3 + 0.3))
        params, focus = self._params_for(best, ex, question)
        alternatives = [m.name for s, m in metrics[1:4]]
        kind = "metric" if best_score >= 7.5 and best_score - second >= 0.8 else "sql"
        return Route(kind, best, params, best.tables if kind == "metric" else top_tables,
                     round(confidence, 2), f"best match {best.name} ({best_score:.1f} vs {second:.1f})",
                     alternatives, focus=focus, notes=notes), ex

    def _params_for(self, metric: MetricDoc, ex: Extracted, question: str = "") -> tuple[dict, list[str]]:
        """Parameters for ``metric`` plus apps to highlight (for by-app comparisons)."""
        params: dict = {}
        focus: list[str] = []
        names = {p.name for p in metric.params}
        comparing = len(ex.apps) > 1 or bool(re.search(
            r"\b(compar\w*|vs\.?|versus|than|ahead|behind|against|others|rank\w*|who is)\b", question.lower()))
        if "from_app" in names and ex.from_app:
            params["from_app"], params["to_app"] = ex.from_app, ex.to_app
        elif "from_app" in names and ex.apps:
            params["from_app"] = ex.apps[0]
            if len(ex.apps) > 1 and "to_app" in names:
                params["to_app"] = ex.apps[1]
        elif metric.focus_parameter == "app":
            # The result is grouped by app: compare by highlighting, filter only for a single app.
            if ex.apps and (comparing or "app" not in names):
                focus = list(ex.apps)
            elif ex.apps:
                params["app"] = ex.apps[0]
        elif "app" in names and ex.apps:
            params["app"] = ex.apps[0]
        # "Always this high, or a spike this week?" needs every day, not just this week.
        keep_dates = not ("trend" in metric.name and re.search(
            r"\b(always|spike|usual|normal|typical|over time|growing|increasing|grown|shrunk)\b", question.lower()))
        if "hours" in names and ex.last_hours:
            params["hours"] = ex.last_hours
        for key in ("window_hours", "date_from", "date_to", "category", "dimension", "limit"):
            if key in ("date_from", "date_to") and not keep_dates:
                continue
            value = getattr(ex, key)
            if value is not None and key in names:
                if key == "dimension":
                    p = metric.param("dimension")
                    if p and value not in p.values:
                        continue
                params[key] = value
        return params, focus
