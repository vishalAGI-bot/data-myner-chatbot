"""Catalog = OKF knowledge bundle + what the database actually contains.

The knowledge files (analyst/knowledge/*.md) carry meaning, joins and column
policies; introspection supplies the real columns and types. Only tables that
are documented AND present AND not blocked are visible to the chatbot.
"""

from __future__ import annotations

import logging
import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .sources import Source, SourceError

log = logging.getLogger("analyst.catalog")

POLICIES = ("ok", "aggregate_only", "heavy", "blocked")
_SENSITIVE = re.compile(r"(password|passwd|secret|token|api_?key|e_?mail|mobile|phone|address|first_name|"
                        r"last_name|username|device_id|fcm|hash|otp|aadhaar|pan_number|ip_address)", re.I)
_HEAVY = re.compile(r"(embedding|payload|raw_|_text$|content$|metadata|response|prompt)", re.I)
_AGG_ONLY = re.compile(r"(date_of_birth|dob|pincode|pin_code|zip)", re.I)


@dataclass
class Column:
    name: str
    type: str = "unknown"
    meaning: str = ""
    policy: str = "ok"


@dataclass
class TableDoc:
    name: str
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    status: str = "draft"
    domain: str = "analytics"
    grain: str = ""
    joins: list[str] = field(default_factory=list)
    notes: str = ""
    questions: list[str] = field(default_factory=list)
    columns: dict[str, Column] = field(default_factory=dict)
    available: bool = False
    row_count: int | None = None

    def visible_columns(self) -> list[Column]:
        return [c for c in self.columns.values() if c.policy != "blocked"]


@dataclass
class Param:
    name: str
    type: str                     # int | date | app | text | enum
    default: object = None
    values: list[str] = field(default_factory=list)


@dataclass
class MetricDoc:
    name: str
    title: str
    description: str
    tags: list[str]
    tables: list[str]
    params: list[Param]
    sql: str
    questions: list[str] = field(default_factory=list)
    caveats: str = ""
    dimensions: dict[str, str] = field(default_factory=dict)
    focus_parameter: str | None = None
    available: bool = True

    def param(self, name: str) -> Param | None:
        return next((p for p in self.params if p.name == name), None)


# --------------------------------------------------------------------------- #
# Markdown helpers
# --------------------------------------------------------------------------- #

def _split(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    _, head, body = text.split("---", 2)
    return yaml.safe_load(head) or {}, body


def _section(body: str, heading: str) -> str:
    m = re.search(rf"^#+\s+{re.escape(heading)}\s*$(.*?)(?=^#+\s|\Z)", body, re.M | re.S | re.I)
    return m.group(1).strip() if m else ""


def _bullets(text: str) -> list[str]:
    return [re.sub(r"^[-*]\s+", "", l).strip().strip("`") for l in text.splitlines() if re.match(r"^\s*[-*]\s+", l)]


def _schema_rows(text: str) -> list[Column]:
    cols = []
    for line in text.splitlines():
        if not line.startswith("|") or set(line) <= set("|-: "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4 or cells[0] == "column":
            continue
        policy = cells[3] if cells[3] in POLICIES else "ok"
        cols.append(Column(cells[0], cells[1], cells[2], policy))
    return cols


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #

class Catalog:
    def __init__(self, knowledge_dir: Path, home_app: str = ""):
        self.dir = Path(knowledge_dir)
        self.home_app = home_app
        self.tables: dict[str, TableDoc] = {}
        self.metrics: dict[str, MetricDoc] = {}
        self.blocked_tables: set[str] = set()
        self.glossary = ""
        self.apps: list[str] = []
        self.categories: list[str] = []
        self.date_range: tuple[str | None, str | None] = (None, None)
        # A table being present is not enough: upstream ETL can create rows
        # while leaving the outcome fields every analysis depends on empty.
        # These fields are populated from cheap aggregate-only probes.
        self.quality_issues: list[dict] = []
        self.metric_issues: dict[str, str] = {}
        # Caveats that do not block a metric but must travel with its answers (metric name -> note).
        self.metric_notes: dict[str, str] = {}
        # First day of the current search-category taxonomy; category comparisons start here.
        self.category_comparable_from: str | None = None
        self.profile: dict[str, int] = {}
        self.refreshed_at: float = 0.0
        self._load_docs()

    # ------------------------------------------------------------- knowledge
    def _load_docs(self) -> None:
        policy_meta, _ = _split((self.dir / "policy.md").read_text(encoding="utf-8"))
        self.blocked_tables = {t.lower() for t in policy_meta.get("blocked_tables", [])}
        glossary_path = self.dir / "glossary.md"
        if glossary_path.exists():
            self.glossary = _split(glossary_path.read_text(encoding="utf-8"))[1].strip()

        for path in sorted((self.dir / "tables").glob("*.md")):
            meta, body = _split(path.read_text(encoding="utf-8"))
            name = path.stem
            doc = TableDoc(
                name=name, title=meta.get("title", name), description=meta.get("description", ""),
                tags=[str(t).lower() for t in meta.get("tags", [])], status=meta.get("status", "draft"),
                domain=meta.get("domain", "analytics"), grain=meta.get("grain", ""),
                joins=_bullets(_section(body, "Joins")), notes=_section(body, "Notes"),
                questions=_bullets(_section(body, "Example questions")))
            for col in _schema_rows(_section(body, "Schema")):
                doc.columns[col.name] = col
            self.tables[name] = doc

        for path in sorted((self.dir / "metrics").glob("*.md")):
            meta, body = _split(path.read_text(encoding="utf-8"))
            sql = re.search(r"```sql\s*(.*?)```", _section(body, "Computation") or body, re.S)
            if not sql:
                log.warning("metric %s has no SQL; skipped", path.stem)
                continue
            params = [Param(p["name"], p.get("type", "text"), p.get("default"), p.get("values", []))
                      for p in meta.get("parameters", [])]
            self.metrics[path.stem] = MetricDoc(
                name=path.stem, title=meta.get("title", path.stem), description=meta.get("description", ""),
                tags=[str(t).lower() for t in meta.get("tags", [])], tables=meta.get("tables", []), params=params,
                sql=sql.group(1).strip(), questions=_bullets(_section(body, "Example questions")),
                caveats=_section(body, "Caveats"), dimensions=meta.get("dimensions") or {},
                focus_parameter=meta.get("focus_parameter"))

    # ------------------------------------------------------- live database
    def attach(self, source: Source) -> None:
        """Merge the real schema, mark availability, read app names and categories."""
        live = source.introspect()
        for name, doc in self.tables.items():
            cols = live.get(name)
            if not cols or name.lower() in self.blocked_tables:
                doc.available = False
                continue
            doc.available = True
            merged: dict[str, Column] = {}
            for col_name, typ in cols:
                known = doc.columns.get(col_name)
                if known:
                    merged[col_name] = Column(col_name, typ, known.meaning, known.policy)
                else:
                    merged[col_name] = Column(col_name, typ, "", self._guess_policy(col_name))
            doc.columns = merged
        undocumented = sorted(set(live) - set(self.tables) - self.blocked_tables)
        if undocumented:
            log.info("tables present but not documented (hidden from the chatbot): %s", ", ".join(undocumented))
        for metric in self.metrics.values():
            metric.available = all(self.tables.get(t) and self.tables[t].available for t in metric.tables)
        self.refresh_values(source)

    def refresh_values(self, source: Source) -> None:
        """Re-read what changes as new data arrives: row counts, apps, categories and the date range.

        Called once on connect and then periodically for a live database, so "today", new apps and new
        categories are recognised without a restart. Each list is replaced in one assignment (thread safe).
        """
        try:
            counts = source.row_counts([n for n, d in self.tables.items() if d.available])
            for name, n in counts.items():
                self.tables[name].row_count = n
        except SourceError as exc:
            log.warning("row counts unavailable: %s", exc)

        apps = self._values(source, "SELECT DISTINCT app_name FROM app_master WHERE app_name IS NOT NULL", "app_master")
        cats = []
        if self._has("category_master"):
            for col in ("master_category", "category", "sub_category"):
                cats += self._values(source, f"SELECT DISTINCT {col} FROM category_master WHERE {col} IS NOT NULL", "category_master")
        if self._has("journey_window", "search_category"):
            cats += self._values(source, "SELECT DISTINCT search_category FROM journey_window WHERE search_category IS NOT NULL", "journey_window")
        self.apps = apps
        self.categories = sorted({c for c in cats if isinstance(c, str) and c.strip()})
        if self._has("cross_app_journey", "first_search_time"):
            try:
                r = source.query("SELECT MIN(CAST(first_search_time AS date)), MAX(CAST(last_search_time AS date)) "
                                 "FROM cross_app_journey", max_rows=1)
                self.date_range = (r.rows[0][0], r.rows[0][1]) if r.rows else (None, None)
            except SourceError:
                pass
        self._refresh_quality(source)
        self.refreshed_at = time.time()

    def _refresh_quality(self, source: Source) -> None:
        """Detect missing derived facts without scanning or returning raw rows."""
        profile: dict[str, int] = {}
        issues: list[dict] = []
        metric_issues: dict[str, str] = {}
        metric_notes: dict[str, str] = {}
        comparable_from = None

        if self._has("journey_window"):
            try:
                result = source.query(
                    """SELECT COUNT(*) AS windows,
                              COUNT(*) FILTER (WHERE is_converted) AS converted,
                              COUNT(*) FILTER (WHERE purchase_app_id IS NOT NULL) AS purchase_apps,
                              COUNT(*) FILTER (WHERE hours_to_purchase IS NOT NULL) AS purchase_times,
                              COUNT(*) FILTER (WHERE search_category IS NOT NULL AND search_category <> '') AS categories,
                              COUNT(*) FILTER (WHERE window_end <= h.horizon) AS finished,
                              COUNT(*) FILTER (WHERE window_end <= h.horizon AND is_final) AS finished_flagged,
                              COUNT(DISTINCT journey_assoc_id) FILTER (WHERE is_converted) AS linked_journeys
                       FROM journey_window
                       CROSS JOIN (SELECT MAX(window_start) AS horizon FROM journey_window) h""", max_rows=1)
                if result.rows:
                    names = ("journey_windows", "converted_windows", "purchase_apps",
                             "purchase_times", "categorised_windows", "finished_windows",
                             "finished_windows_flagged_final", "linked_purchase_journeys")
                    profile.update({key: int(value or 0) for key, value in zip(names, result.rows[0])})
            except SourceError as exc:
                log.warning("journey-window quality probe failed: %s", exc)

        if self._has("journey_window", "search_category"):
            try:
                daily = source.query(
                    """SELECT CAST(window_start AS date) AS day, COUNT(DISTINCT search_category) AS categories
                       FROM journey_window
                       WHERE window_hours = 24 AND search_category IS NOT NULL AND search_category <> ''
                       GROUP BY 1 ORDER BY 1""", max_rows=1000).rows
                comparable_from, before, after = _category_regime([(str(d)[:10], int(n)) for d, n in daily])
                if comparable_from:
                    detail = (f"Search categories changed on {comparable_from}: about {before} broad categories per "
                              f"day before, about {after} detailed categories per day from then on. Category shares "
                              f"are only comparable from {comparable_from}.")
                    affected = sorted(m.name for m in self.metrics.values() if "search_category" in m.sql)
                    issues.append({"code": "category_taxonomy_change", "severity": "warning",
                                   "title": "Search categories changed", "detail": detail,
                                   "tables": ["journey_window"], "affected_metrics": affected})
                    metric_notes.update({name: detail for name in affected})
            except SourceError as exc:
                log.warning("category taxonomy probe failed: %s", exc)

        if self._has("cross_app_journey"):
            try:
                profile["purchased_journeys"] = int(source.query(
                    "SELECT COUNT(*) FROM cross_app_journey WHERE close_reason = 'purchased'",
                    max_rows=1).scalar() or 0)
            except SourceError as exc:
                log.warning("journey quality probe failed: %s", exc)

        if self._has("session_master"):
            try:
                profile["checkout_confirmations"] = int(source.query(
                    "SELECT COUNT(*) FROM session_master WHERE is_order_confirmation_page = true "
                    "AND COALESCE(insignificant_session, false) = false", max_rows=1).scalar() or 0)
            except SourceError as exc:
                log.warning("session quality probe failed: %s", exc)

        # A purchased outcome upstream plus zero linked outcomes in every
        # window is a pipeline defect, not a real 0% conversion rate.
        if (profile.get("journey_windows", 0) > 0 and profile.get("purchased_journeys", 0) > 0
                and profile.get("converted_windows", 0) == 0):
            affected = sorted(m.name for m in self.metrics.values() if re.search(
                r"\b(is_converted|is_retained|is_leaked|purchase_app_id|hours_to_purchase|order_amount)\b", m.sql))
            detail = (
                f"The outcome pipeline is incomplete: {profile['purchased_journeys']:,} journeys are marked "
                f"purchased, but 0 of {profile['journey_windows']:,} journey-window rows have linked purchase "
                "outcomes. Purchase, conversion, leakage, order-value and time-to-purchase figures from "
                "journey_window would be misleading until that pipeline is repaired."
            )
            if profile.get("checkout_confirmations", 0):
                detail += (f" There are {profile['checkout_confirmations']:,} checkout-confirmation sessions "
                           "available as a directional signal, not as verified orders.")
            issues.append({"code": "unlinked_purchase_outcomes", "severity": "error",
                           "title": "Purchase outcomes are not linked", "detail": detail,
                           "tables": ["cross_app_journey", "journey_window", "session_master"],
                           "affected_metrics": affected})
            metric_issues.update({name: detail for name in affected})
        elif 0 < profile.get("linked_purchase_journeys", 0) < 0.5 * profile.get("purchased_journeys", 0):
            # Partly linked: figures are usable but describe linked purchases only.
            issues.append({
                "code": "partly_linked_purchase_outcomes", "severity": "warning",
                "title": "Most purchased journeys have no linked purchase",
                "detail": (f"{profile['purchased_journeys']:,} journeys are closed as purchased, but only "
                           f"{profile['linked_purchase_journeys']:,} have a purchase linked in journey_window "
                           "(app, time, value). Conversion, leakage, purchase-share and time-to-purchase figures "
                           "describe linked purchases only and may understate purchasing."),
                "tables": ["cross_app_journey", "journey_window"], "affected_metrics": []})

        # The pipeline computes windows a few hours after they start and does not revisit them, so
        # is_final stays false for most finished windows. Rates use window_end instead (see glossary).
        finished = profile.get("finished_windows", 0)
        if finished and profile.get("finished_windows_flagged_final", 0) < 0.9 * finished:
            issues.append({
                "code": "stale_is_final", "severity": "warning", "title": "journey_window.is_final is stale",
                "detail": (f"Only {profile.get('finished_windows_flagged_final', 0):,} of {finished:,} finished "
                           "journey windows have is_final = true. Rates count finished windows by window_end "
                           "instead of this flag."),
                "tables": ["journey_window"], "affected_metrics": []})

        if profile.get("journey_windows", 0) and profile.get("categorised_windows", 0) == 0:
            affected = sorted(m.name for m in self.metrics.values() if "category" in m.name and (
                "search_category" in m.sql or "purchased_product_category" in m.sql))
            detail = (f"0 of {profile['journey_windows']:,} journey-window rows have a search category, so "
                      "category breakdowns would collapse into ‘Uncategorised’ and are not reliable.")
            issues.append({"code": "missing_journey_categories", "severity": "warning",
                           "title": "Journey categories are empty", "detail": detail,
                           "tables": ["journey_window"], "affected_metrics": affected})
            for name in affected:
                metric_issues.setdefault(name, detail)

        self.profile = profile
        self.metric_notes = metric_notes
        self.category_comparable_from = comparable_from
        self.quality_issues = issues
        self.metric_issues = metric_issues

    def _has(self, table: str, column: str | None = None) -> bool:
        doc = self.tables.get(table)
        return bool(doc and doc.available and (column is None or column in doc.columns))

    def _values(self, source: Source, sql: str, table: str) -> list:
        if not self._has(table):
            return []
        try:
            return [r[0] for r in source.query(sql, max_rows=5000).rows]
        except SourceError as exc:
            log.warning("could not read values from %s: %s", table, exc)
            return []

    @staticmethod
    def _guess_policy(column: str) -> str:
        if _SENSITIVE.search(column):
            return "blocked"
        if _AGG_ONLY.search(column):
            return "aggregate_only"
        if _HEAVY.search(column):
            return "heavy"
        return "ok"

    # ------------------------------------------------------------- queries
    @property
    def allowed_tables(self) -> set[str]:
        return {n for n, d in self.tables.items() if d.available and n.lower() not in self.blocked_tables}

    def policy(self, table: str, column: str) -> str:
        doc = self.tables.get(table)
        if not doc:
            return "blocked"
        col = doc.columns.get(column)
        return col.policy if col else self._guess_policy(column)

    def schema_card(self, tables: list[str], with_notes: bool = True) -> str:
        """Compact description for the model: only the chosen tables, only visible columns."""
        parts = []
        for name in tables:
            doc = self.tables.get(name)
            if not doc or not doc.available:
                continue
            quoted = f'"{name}"' if name in ("order", "user") else name
            lines = [f"TABLE {quoted} -- {doc.description} Grain: {doc.grain}."]
            if doc.row_count is not None and doc.row_count >= 0:
                lines[0] += f" ~{doc.row_count:,} rows."
            for col in doc.visible_columns():
                flag = {"aggregate_only": " [only inside aggregates/expressions]",
                        "heavy": " [do not select; filter only]"}.get(col.policy, "")
                meaning = f" - {col.meaning}" if col.meaning and col.meaning != "(hidden)" else ""
                lines.append(f"  {col.name} {col.type}{meaning}{flag}")
            if doc.joins:
                lines.append("  JOINS: " + "; ".join(doc.joins))
            if with_notes and doc.notes:
                lines.append("  NOTES: " + doc.notes)
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    def table_menu(self) -> str:
        return "\n".join(f"- {n}: {d.title}. {d.description}" for n, d in self.tables.items() if d.available)

    def metric_menu(self) -> str:
        lines = []
        for m in self.metrics.values():
            if not m.available:
                continue
            params = ", ".join(f"{p.name}{'=' + str(p.default) if p.default is not None else ''}"
                               + (f" ({'/'.join(p.values)})" if p.values else "") for p in m.params)
            lines.append(f"- {m.name}: {m.description} Params: {params or 'none'}")
        return "\n".join(lines)

    def summary(self) -> dict:
        metrics = [{"name": m.name, "title": m.title, "description": m.description,
                    "ready": m.name not in self.metric_issues,
                    "issue": self.metric_issues.get(m.name)}
                   for m in self.metrics.values() if m.available]
        row_counts = [d.row_count for d in self.tables.values()
                      if d.available and d.row_count is not None and d.row_count >= 0]
        return {
            "tables": [{"name": n, "title": d.title, "description": d.description, "rows": d.row_count,
                        "status": d.status, "domain": d.domain, "columns": len(d.visible_columns())}
                       for n, d in self.tables.items() if d.available],
            "unavailable_tables": [n for n, d in self.tables.items() if not d.available],
            "metrics": metrics,
            "metrics_ready": sum(bool(m["ready"]) for m in metrics),
            "blocked_tables": len(self.blocked_tables),
            "apps": self.apps,
            "date_range": list(self.date_range),
            "home_app": self.home_app,
            "quality_issues": self.quality_issues,
            "profile": self.profile,
            "total_rows": sum(row_counts),
        }


def _category_regime(daily: list[tuple[str, int]]) -> tuple[str | None, int, int]:
    """(first day of the current taxonomy, categories/day before, after) when the taxonomy changed, else None.

    The current taxonomy starts on the earliest day from which every day has at least half the recent typical
    category count; it is a change only when the earlier days used far fewer categories (broad groups).
    """
    if len(daily) < 4:
        return None, 0, 0
    recent = statistics.median(n for _, n in daily[-7:])
    start = len(daily)
    while start > 0 and daily[start - 1][1] >= recent / 2:
        start -= 1
    if start == 0 or start == len(daily):
        return None, 0, 0
    before = statistics.median(n for _, n in daily[:start])
    after = statistics.median(n for _, n in daily[start:])
    if before * 4 > after:
        return None, 0, 0
    return daily[start][0], round(before), round(after)

