"""Read-only data layer for the aggregated cross-app journey file.

Design goals
------------
* **Correct numbers first.** Every figure the chatbot reports comes from SQL
  run here, never from the language model's arithmetic.
* **Large, messy input.** JSON arrays, JSON Lines and CSV exports are accepted.
  Bad records are quarantined and reported instead of failing the whole upload
  (set ``JOURNEY_STRICT=1`` to fail fast instead).
* **Safe model-written SQL.** Untrusted SQL runs on a separate connection with
  a SQLite authorizer (only SELECT on the two journey tables), a wall-clock
  timeout and a row cap. Regex keyword filters are easy to bypass
  (``FROM journey, sqlite_master``); the authorizer is not.
* **No silent truncation.** Every result says whether rows were cut and how
  many rows exist in total, so the model can never present a partial list as
  "the complete list".
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

try:  # numpy makes similarity search O(n) vectorised instead of pure Python.
    import numpy as np
except ImportError:  # pragma: no cover - numpy is in requirements.txt
    np = None


DEFAULT_JSON_PATH = Path(os.getenv(
    "JOURNEY_JSON",
    Path(__file__).resolve().parents[1]
    / "data" / "cross-app-journey" / "data-1790154732687.json",
))

ALLOWED_TABLES = frozenset({"journey", "journey_app"})
HIDDEN_COLUMNS = frozenset({("journey", "journey_hash")})
MAX_SQL_ROWS = int(os.getenv("JOURNEY_MAX_SQL_ROWS", 500))
SQL_TIMEOUT_SECONDS = float(os.getenv("JOURNEY_SQL_TIMEOUT", 3))
STRICT_DEFAULT = os.getenv("JOURNEY_STRICT", "0") == "1"
MAX_ISSUES_KEPT = 50

_APP_ITEM = re.compile(r"(?:^|,\s*)(.+?)\s+\((\d+)\)(?=\s*,|\s*$)")
_BLOCKED_FUNCTIONS = frozenset({"load_extension", "readfile", "writefile", "edit",
                                "fts3_tokenizer", "randomblob", "zeroblob"})


class JourneyDataError(RuntimeError):
    """Raised when the journey file or a requested operation is invalid."""


# --------------------------------------------------------------------------- #
# Small value helpers
# --------------------------------------------------------------------------- #

def _nullable(value):
    if value is None:
        return None
    if isinstance(value, str) and value.strip() in ("", "NULL", "null", "None"):
        return None
    return value


def _as_bool_int(value) -> int:
    if isinstance(value, bool):
        return int(value)
    return int(str(value).strip().lower() in {"1", "true", "t", "yes", "y"})


def _as_int(value):
    value = _nullable(value)
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_timestamp(value) -> datetime | None:
    """Parse Postgres-style timestamps such as ``2026-09-12 18:15:06.677+02``."""
    value = _nullable(value)
    if value is None:
        return None
    text = str(value).strip().replace(" ", "T", 1)
    # Python < 3.11 cannot read "+02"; normalise it to "+02:00".
    text = re.sub(r"([+-]\d{2})$", r"\1:00", text)
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
    text = re.sub(r"Z$", "+00:00", text)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _utc_text(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def parse_apps_summary(summary: str) -> list[tuple[str, int]]:
    """``"Meesho (7), Flipkart (1)"`` -> ``[("Meesho", 7), ("Flipkart", 1)]``."""
    return [(m.group(1).strip(), int(m.group(2)))
            for m in _APP_ITEM.finditer(summary or "")]


def _parse_vector(value) -> tuple[float, ...]:
    value = _nullable(value)
    if value is None:
        return ()
    if isinstance(value, str):
        value = json.loads(value)          # CSV / Postgres exports store "[...]"
    if not isinstance(value, list):
        raise ValueError("cluster_embedding is not a list")
    return tuple(float(v) for v in value)


# --------------------------------------------------------------------------- #
# File readers: JSON array, JSON Lines, CSV
# --------------------------------------------------------------------------- #

def iter_records(path: Path) -> Iterator[dict]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        csv.field_size_limit(1 << 30)      # embeddings are ~8 KB per cell
        with path.open(newline="", encoding="utf-8-sig") as handle:
            yield from csv.DictReader(handle)
        return
    if suffix in (".jsonl", ".ndjson"):
        with path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if line.strip():
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise JourneyDataError(f"Line {number} is not valid JSON: {exc}") from exc
        return

    # JSON array. ijson streams it without holding two copies in memory; fall
    # back to the standard library when ijson is not installed.
    try:
        import ijson  # type: ignore
    except ImportError:
        ijson = None
    if ijson is not None:
        with path.open("rb") as handle:
            first = handle.read(1)
            while first and first.isspace():
                first = handle.read(1)
            handle.seek(0)
            prefix = "item" if first == b"[" else "data.item"
            try:
                yield from ijson.items(handle, prefix, use_float=True)
            except ijson.JSONError as exc:
                raise JourneyDataError(f"Invalid JSON: {exc}") from exc
        return
    with path.open(encoding="utf-8") as handle:
        try:
            data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise JourneyDataError(f"Invalid JSON: {exc}") from exc
    if isinstance(data, dict):
        data = data.get("data") or data.get("records") or data.get("items")
    if not isinstance(data, list):
        raise JourneyDataError("The journey JSON must be an array of journey objects.")
    yield from data


# --------------------------------------------------------------------------- #
# Result object
# --------------------------------------------------------------------------- #

@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    sql: str = ""
    params: tuple = ()
    truncated: bool = False
    total_rows: int | None = None

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def row_count(self) -> int:
        return self.total_rows if self.total_rows is not None else len(self.rows)

    def dicts(self) -> list[dict]:
        return [dict(zip(self.columns, row)) for row in self.rows]

    def scalar(self):
        return self.rows[0][0] if self.rows and self.rows[0] else None

    def to_text(self, limit: int = 60) -> str:
        """Pipe table for the model. Always states completeness explicitly."""
        if not self.rows:
            return "(no rows - the data has no matching records)"
        lines = [" | ".join(self.columns)]
        for row in self.rows[:limit]:
            lines.append(" | ".join(_fmt(v) for v in row))
        shown = min(limit, len(self.rows))
        total = self.row_count
        if shown < total or self.truncated:
            lines.append(f"[PARTIAL: showing {shown} of {total} rows. "
                         "Do not describe this as a complete list.]")
        else:
            lines.append(f"[COMPLETE: all {total} rows shown]")
        return "\n".join(lines)

    def as_table(self, limit: int = 200) -> dict:
        return {"columns": self.columns,
                "rows": [list(r) for r in self.rows[:limit]],
                "total_rows": self.row_count,
                "truncated": self.truncated or self.row_count > min(limit, len(self.rows))}


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".") if not value.is_integer() else str(int(value))
    return str(value)


@dataclass
class DataQuality:
    records_read: int = 0
    records_loaded: int = 0
    records_skipped: int = 0
    warnings: int = 0
    issues: list[str] = field(default_factory=list)

    def note(self, message: str, skipped: bool = False) -> None:
        if skipped:
            self.records_skipped += 1
        else:
            self.warnings += 1
        if len(self.issues) < MAX_ISSUES_KEPT:
            self.issues.append(message)

    def as_dict(self) -> dict:
        return {"records_read": self.records_read, "records_loaded": self.records_loaded,
                "records_skipped": self.records_skipped, "warnings": self.warnings,
                "issues": self.issues}


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #

SCHEMA_SQL = """
CREATE TABLE journey (
    id INTEGER,
    is_active INTEGER NOT NULL,
    is_deleted INTEGER NOT NULL,
    created_at TEXT,
    updated_at TEXT,
    remarks TEXT,
    journey_id TEXT PRIMARY KEY,
    journey_hash TEXT,
    query_cluster_rep TEXT,
    embedding_dimensions INTEGER,
    total_apps INTEGER NOT NULL,
    total_events INTEGER NOT NULL,
    apps_summary TEXT,
    first_search_time TEXT,
    last_search_time TEXT,
    first_search_utc TEXT,
    last_search_utc TEXT,
    search_date TEXT,
    search_hour INTEGER,
    duration_seconds REAL,
    status TEXT,
    close_reason TEXT,
    period_end TEXT,
    first_search_app_id INTEGER,
    user_assoc_id INTEGER,
    start_app TEXT                -- app of the first search, derived from first_search_app_id
);
CREATE TABLE journey_app (
    journey_id TEXT NOT NULL,
    app_name TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    PRIMARY KEY (journey_id, app_name)
);
CREATE INDEX idx_journey_user ON journey(user_assoc_id);
CREATE INDEX idx_journey_status ON journey(status, close_reason);
CREATE INDEX idx_journey_date ON journey(search_date);
CREATE INDEX idx_journey_start ON journey(start_app);
CREATE INDEX idx_journey_query ON journey(query_cluster_rep COLLATE NOCASE);
CREATE INDEX idx_journey_app_name ON journey_app(app_name, journey_id);
"""


class JourneyStore:
    """Private read-only query store built from a file or an iterable of rows."""

    def __init__(self, json_path: Path | str | None = DEFAULT_JSON_PATH, strict: bool | None = None,
                 *, records: Iterable[dict] | None = None, source_label: str | None = None,
                 disk_backed: bool = False):
        if records is None and json_path is None:
            raise JourneyDataError("A journey file or row source is required.")
        self.json_path = Path(json_path).resolve() if json_path is not None else None
        self.source_label = source_label or (str(self.json_path) if self.json_path else "live PostgreSQL")
        self.strict = STRICT_DEFAULT if strict is None else strict
        self.quality = DataQuality()
        self._lock = threading.RLock()
        # Live tables can be much larger than uploaded demos.  Keep those in a
        # private temporary SQLite file so the source rows and the query store
        # do not both have to fit in RAM.  Small file imports retain the faster
        # shared in-memory database.
        self._tempdir = tempfile.TemporaryDirectory(prefix="journey-store-") if disk_backed else None
        self._uri = (str(Path(self._tempdir.name) / "journeys.sqlite") if self._tempdir
                     else f"file:journey_{uuid.uuid4().hex}?mode=memory&cache=shared")
        self._uri_mode = not disk_backed
        self.conn = sqlite3.connect(self._uri, uri=self._uri_mode, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._ids: list[str] = []
        self._index: dict[str, int] = {}
        self._matrix = None
        self._vectors: dict[str, tuple[float, ...]] = {}
        self._load(records)
        self._sandbox = self._open_sandbox()
        self._summary = self._compute_summary()
        self._schema_card = self._build_schema_card()

    # ------------------------------------------------------------------ load
    def _load(self, records: Iterable[dict] | None = None) -> None:
        if records is None and (self.json_path is None or not self.json_path.is_file()):
            raise FileNotFoundError(f"Journey file not found: {self.json_path}")
        self.conn.executescript(SCHEMA_SQL)

        journey_rows, app_rows, vectors = [], [], []
        seen: set[str] = set()
        q = self.quality
        source_records = records if records is not None else iter_records(self.json_path)
        for position, record in enumerate(source_records, 1):
            q.records_read += 1
            if not isinstance(record, dict):
                q.note(f"Record {position} is not an object; skipped.", skipped=True)
                continue
            journey_id = str(record.get("journey_id") or "").strip().upper()
            if not journey_id:
                q.note(f"Record {position} has no journey_id; skipped.", skipped=True)
                continue
            if journey_id in seen:
                q.note(f"{journey_id} appears more than once; kept the first copy.", skipped=True)
                continue

            try:
                vector = _parse_vector(record.get("cluster_embedding"))
            except (TypeError, ValueError, json.JSONDecodeError):
                q.note(f"{journey_id} has an unreadable cluster_embedding; similarity disabled for it.")
                vector = ()

            apps = parse_apps_summary(str(record.get("apps_summary") or ""))
            total_events = _as_int(record.get("total_events"))
            total_apps = _as_int(record.get("total_apps"))
            app_sum = sum(c for _, c in apps)
            if apps and total_events is not None and app_sum != total_events:
                q.note(f"{journey_id}: apps_summary adds up to {app_sum} but "
                       f"total_events is {total_events}; per-app counts are used for app questions.")
            if apps and total_apps is not None and len(apps) != total_apps:
                q.note(f"{journey_id}: apps_summary lists {len(apps)} apps but total_apps is {total_apps}.")
            if total_events is None:
                total_events = app_sum
            if total_apps is None:
                total_apps = len(apps)
            merged: dict[str, int] = {}
            for name, count in apps:          # tolerate repeated app names
                merged[name] = merged.get(name, 0) + count

            first_dt = parse_timestamp(record.get("first_search_time"))
            last_dt = parse_timestamp(record.get("last_search_time"))
            if _nullable(record.get("first_search_time")) and first_dt is None:
                q.note(f"{journey_id}: first_search_time could not be read.")
            duration = (last_dt - first_dt).total_seconds() if first_dt and last_dt else None
            if duration is not None and duration < 0:
                q.note(f"{journey_id}: last_search_time is before first_search_time.")

            status = _nullable(record.get("status"))
            close_reason = _nullable(record.get("close_reason"))
            journey_rows.append((
                _as_int(record.get("id")) or position,
                _as_bool_int(record.get("is_active")),
                _as_bool_int(record.get("is_deleted")),
                _nullable(record.get("created_at")),
                _nullable(record.get("updated_at")),
                _nullable(record.get("remarks")),
                journey_id,
                _nullable(record.get("journey_hash")),
                _nullable(record.get("query_cluster_rep")),
                len(vector),
                total_apps,
                total_events,
                _nullable(record.get("apps_summary")),
                _nullable(record.get("first_search_time")),
                _nullable(record.get("last_search_time")),
                _utc_text(first_dt),
                _utc_text(last_dt),
                first_dt.strftime("%Y-%m-%d") if first_dt else None,
                first_dt.hour if first_dt else None,
                duration,
                str(status).strip().lower() if status else None,
                str(close_reason).strip().lower() if close_reason else None,
                _nullable(record.get("period_end")),
                _as_int(record.get("first_search_app_id")),
                _as_int(record.get("user_assoc_id")),
                _nullable(record.get("start_app")),      # exact in live PostgreSQL; derived for old files
            ))
            app_rows.extend((journey_id, name, count) for name, count in merged.items())
            if vector:
                vectors.append((journey_id, vector))
            seen.add(journey_id)

            if len(journey_rows) >= 5000:            # bounded memory for big files
                self._flush(journey_rows, app_rows)

        self._flush(journey_rows, app_rows)
        q.records_loaded = len(seen)
        if q.records_loaded == 0:
            raise JourneyDataError("The data source contains no usable journey records.")
        if self.strict and (q.records_skipped or q.warnings):
            raise JourneyDataError("Data-quality problems found: " + "; ".join(q.issues[:5]))
        self._derive_start_app()
        self.conn.execute("ANALYZE")
        self.conn.commit()
        self.conn.execute("PRAGMA query_only = ON")
        self._build_vectors(vectors)

    def _derive_start_app(self) -> None:
        """Map first_search_app_id -> app name.

        The file has ids, not names, for the first app. In single-app journeys
        the id can only mean that one app, so a majority vote over those gives
        the mapping. Ids without a clear winner are left unmapped and reported.
        """
        votes = self.conn.execute(
            """SELECT j.first_search_app_id, ja.app_name, COUNT(*) n
               FROM journey j JOIN journey_app ja ON ja.journey_id = j.journey_id
               WHERE j.total_apps = 1 AND j.first_search_app_id IS NOT NULL
               GROUP BY 1, 2 ORDER BY 1, n DESC""").fetchall()
        mapping: dict[int, str] = {}
        by_id: dict[int, list] = {}
        for app_id, name, n in votes:
            by_id.setdefault(app_id, []).append((name, n))
        for app_id, options in by_id.items():
            best, n = options[0]
            total = sum(c for _, c in options)
            if n / total >= 0.9:
                mapping[app_id] = best
            else:
                self.quality.note(f"first_search_app_id {app_id} maps to several apps "
                                  f"({', '.join(o[0] for o in options[:3])}); left unmapped.")
        self.conn.executemany("UPDATE journey SET start_app = ? WHERE first_search_app_id = ? AND start_app IS NULL",
                              [(name, app_id) for app_id, name in mapping.items()])
        # A journey whose mapped start app is not among its own apps is suspicious.
        bad = self.conn.execute(
            """SELECT COUNT(*) FROM journey j WHERE start_app IS NOT NULL AND NOT EXISTS
               (SELECT 1 FROM journey_app a WHERE a.journey_id = j.journey_id
                AND a.app_name = j.start_app)""").fetchone()[0]
        if bad:
            self.quality.note(f"{bad} journeys start on an app that is not in their apps_summary.")
        self.start_app_map = mapping

    def _flush(self, journey_rows: list, app_rows: list) -> None:
        if journey_rows:
            self.conn.executemany(
                f"INSERT INTO journey VALUES ({','.join('?' * 26)})", journey_rows)
        if app_rows:
            self.conn.executemany("INSERT INTO journey_app VALUES (?,?,?)", app_rows)
        journey_rows.clear()
        app_rows.clear()

    def _build_vectors(self, vectors: list[tuple[str, tuple]]) -> None:
        dims = {len(v) for _, v in vectors if v}
        if not dims:
            return
        dim = max(dims, key=lambda d: sum(1 for _, v in vectors if len(v) == d))
        usable = [(jid, v) for jid, v in vectors if len(v) == dim]
        self._ids = [jid for jid, _ in usable]
        self._index = {jid: i for i, jid in enumerate(self._ids)}
        if np is not None:
            matrix = np.asarray([v for _, v in usable], dtype=np.float32)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            self._matrix = matrix / np.clip(norms, 1e-9, None)
        else:
            self._vectors = dict(usable)

    # --------------------------------------------------------------- sandbox
    def _open_sandbox(self) -> sqlite3.Connection:
        sandbox = sqlite3.connect(self._uri, uri=self._uri_mode, check_same_thread=False)
        sandbox.execute("PRAGMA query_only = ON")

        def authorizer(action, arg1, arg2, _db, _trigger):
            if action in (sqlite3.SQLITE_SELECT, getattr(sqlite3, "SQLITE_RECURSIVE", -1)):
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ:
                table = (arg1 or "").lower()
                if table not in ALLOWED_TABLES:
                    return sqlite3.SQLITE_DENY
                if (table, (arg2 or "").lower()) in HIDDEN_COLUMNS:
                    return sqlite3.SQLITE_IGNORE        # reads as NULL
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION:
                return (sqlite3.SQLITE_DENY if (arg2 or "").lower() in _BLOCKED_FUNCTIONS
                        else sqlite3.SQLITE_OK)
            return sqlite3.SQLITE_DENY

        sandbox.set_authorizer(authorizer)
        return sandbox

    def _execute(self, conn: sqlite3.Connection, sql: str, params: Iterable = (),
                 max_rows: int = MAX_SQL_ROWS, count_total: bool = True) -> QueryResult:
        deadline = time.monotonic() + SQL_TIMEOUT_SECONDS
        params = tuple(params)
        sandboxed = conn is getattr(self, "_sandbox", None)
        with self._lock:
            if sandboxed:   # only model-written SQL is time-limited
                conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 20_000)
            try:
                cursor = conn.execute(sql, params)
                rows = cursor.fetchmany(max_rows + 1)
                columns = [c[0] for c in cursor.description or []]
                truncated = len(rows) > max_rows
                rows = [tuple(r) for r in rows[:max_rows]]
                total = None
                if truncated and count_total:
                    try:
                        total = conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
                    except sqlite3.Error:
                        total = None
            except sqlite3.OperationalError as exc:
                if "interrupted" in str(exc).lower():
                    raise JourneyDataError(
                        f"The query took longer than {SQL_TIMEOUT_SECONDS:g}s and was stopped. "
                        "Add filters or aggregate before joining.") from exc
                if "not authorized" in str(exc).lower() or "prohibited" in str(exc).lower():
                    raise JourneyDataError(
                        "Only SELECT queries on the journey and journey_app tables are allowed.") from exc
                raise JourneyDataError(f"SQL error: {exc}") from exc
            except sqlite3.ProgrammingError as exc:
                raise JourneyDataError(f"SQL error: {exc}") from exc
            except sqlite3.DatabaseError as exc:
                if "not authorized" in str(exc).lower():
                    raise JourneyDataError(
                        "Only SELECT queries on the journey and journey_app tables are allowed.") from exc
                raise JourneyDataError(f"SQL error: {exc}") from exc
            finally:
                if sandboxed:
                    conn.set_progress_handler(None, 0)
        return QueryResult(columns, rows, sql=sql, params=params,
                           truncated=truncated, total_rows=total)

    # ------------------------------------------------------------ query API
    def query(self, sql: str, params: Iterable = (), max_rows: int = MAX_SQL_ROWS) -> QueryResult:
        """Trusted, parameterised SQL written by this package (not by the model)."""
        return self._execute(self.conn, sql, params, max_rows)

    def run_untrusted_sql(self, sql: str, max_rows: int = MAX_SQL_ROWS) -> QueryResult:
        """SQL written by the language model: sandboxed, time-limited, row-capped."""
        cleaned = clean_sql(sql)
        return self._execute(self._sandbox, cleaned, (), max_rows)

    def run_sql(self, sql: str) -> str:
        """Backwards-compatible text interface used by the tool-calling agent."""
        return self.run_untrusted_sql(sql).to_text()

    # -------------------------------------------------------------- facts
    @property
    def record_count(self) -> int:
        return self._summary["journeys"]

    @property
    def apps(self) -> list[str]:
        return list(self._summary["apps"])

    def user_exists(self, user_id: int) -> bool:
        return self.query("SELECT 1 FROM journey WHERE user_assoc_id = ? LIMIT 1",
                          (user_id,)).rows != []

    def journey_exists(self, journey_id: str) -> bool:
        return self.query("SELECT 1 FROM journey WHERE journey_id = ? LIMIT 1",
                          (journey_id.upper(),)).rows != []

    def _compute_summary(self) -> dict:
        row = self.conn.execute(
            """SELECT COUNT(*) journeys,
                      COUNT(DISTINCT user_assoc_id) users,
                      COALESCE(SUM(total_events), 0) events,
                      SUM(CASE WHEN total_apps > 1 THEN 1 ELSE 0 END) cross_app,
                      SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) open_journeys,
                      SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) closed_journeys,
                      SUM(CASE WHEN close_reason = 'purchased' THEN 1 ELSE 0 END) purchased,
                      SUM(CASE WHEN close_reason = 'timeout' THEN 1 ELSE 0 END) timed_out,
                      MIN(search_date) first_date,
                      MAX(substr(last_search_time, 1, 10)) last_date
               FROM journey"""
        ).fetchone()
        result = dict(row)
        result["apps"] = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT app_name FROM journey_app ORDER BY app_name")]
        result["statuses"] = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT status FROM journey WHERE status IS NOT NULL ORDER BY 1")]
        result["close_reasons"] = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT close_reason FROM journey WHERE close_reason IS NOT NULL ORDER BY 1")]
        result["source"] = self.source_label
        result["data_quality"] = self.quality.as_dict()
        result["suggestion_groups"] = self._build_suggestion_groups(result)
        return result

    def _build_suggestion_groups(self, summary: dict) -> list[dict]:
        """Build the Journey chat menu from facts that actually exist in this store.

        The frontend deliberately does not own Journey example questions.  This
        keeps the menu in sync when a different live date window is loaded and
        avoids suggesting an app, date, journey or user that is not present.
        """
        profile = self.conn.execute(
            """SELECT
                   COUNT(NULLIF(TRIM(query_cluster_rep), '')) AS search_terms,
                   COUNT(first_search_time) AS start_times,
                   COUNT(last_search_time) AS end_times,
                   COUNT(duration_seconds) AS durations,
                   COUNT(user_assoc_id) AS user_ids
               FROM journey"""
        ).fetchone()
        top_app = self.conn.execute(
            """SELECT app_name FROM journey_app
               GROUP BY app_name ORDER BY SUM(event_count) DESC, app_name LIMIT 1"""
        ).fetchone()
        sample_journey = self.conn.execute(
            "SELECT journey_id FROM journey ORDER BY first_search_utc DESC, journey_id LIMIT 1"
        ).fetchone()
        sample_user = self.conn.execute(
            """SELECT user_assoc_id FROM journey WHERE user_assoc_id IS NOT NULL
               GROUP BY user_assoc_id ORDER BY COUNT(*) DESC, user_assoc_id LIMIT 1"""
        ).fetchone()

        groups: list[dict] = []

        def add(key: str, category: str, description: str, fields: list[str], *questions: str) -> None:
            usable = [q for q in questions if q]
            if usable:
                groups.append({"key": key, "category": category, "description": description,
                               "fields": fields, "questions": usable})

        if profile["search_terms"]:
            add("search_interests", "Search interests", "See what shoppers searched for.",
                ["query_cluster_rep", "journey_id"],
                "What are the top 10 search terms?",
                "Which queries have the most journeys?")

        if summary.get("first_date") or summary.get("last_date"):
            day = summary.get("last_date") or summary.get("first_date")
            add("journey_trends", "Journey volume and trends", "Count journeys and see how volume changes.",
                ["journey_id", "first_search_time"],
                f"How many journeys started on {day}?",
                "Show journey counts by day.")

        if summary.get("apps"):
            app = top_app[0] if top_app else summary["apps"][0]
            add("app_usage", "App usage", "Compare app activity and cross-app behavior.",
                ["total_apps", "apps_summary", "start_app"],
                "Which apps have the most search events?",
                f"How many journeys on {app}?")

        if summary.get("events") is not None:
            add("search_engagement", "Search engagement", "Measure searches within each journey.",
                ["total_events", "journey_id"],
                "What is the average number of searches per journey?",
                "Which 10 journeys have the most searches?")

        if profile["start_times"] or profile["durations"]:
            add("time_duration", "Time and duration", "Find busy hours and journey duration.",
                ["first_search_time", "last_search_time", "duration_seconds"],
                "Which hour has the most journeys?" if profile["start_times"] else "",
                "What is the average journey duration?" if profile["durations"] else "")

        if summary.get("statuses") or summary.get("close_reasons"):
            closure_question = ("How many timed out journeys?"
                                if "timeout" in summary.get("close_reasons", [])
                                else "Show journey counts by outcome.")
            add("status_closure", "Journey status and closure", "Check open, closed, and timed-out journeys.",
                ["status", "close_reason"],
                "How many open journeys?",
                closure_question)

        if "purchased" in summary.get("close_reasons", []):
            add("purchase_outcomes", "Purchase outcomes", "Analyze journeys recorded as ending in purchase.",
                ["close_reason", "query_cluster_rep", "journey_id"],
                "What percentage of journeys ended with a purchase?",
                "Which search terms have the most purchased journeys?")

        if profile["user_ids"]:
            user = sample_user[0] if sample_user else None
            add("user_behavior", "User behavior", "Review activity for anonymized users.",
                ["user_assoc_id", "journey_id", "query_cluster_rep"],
                "How many users?",
                f"Show the full journey profile for user {user}." if user is not None
                else "Which users have the most journeys?")

        if sample_journey:
            journey_id = sample_journey[0]
            add("record_details", "Journey details", "Inspect a real journey from the current data window.",
                ["journey_id", "status", "close_reason", "apps_summary"],
                f"Show journey {journey_id}.",
                (f"Find journeys similar to {journey_id}." if self._ids
                 else f"Show all journeys for user {sample_user[0]}." if sample_user else ""))

        add("data_quality", "Data quality", "Review records skipped or flagged while loading.",
            ["journey_id", "status", "first_search_time", "last_search_time"],
            "Show the data-quality report.",
            "Show the dataset summary.")
        return groups

    def summary(self) -> dict:
        return {k: (list(v) if isinstance(v, list) else v) for k, v in self._summary.items()}

    def _build_schema_card(self) -> str:
        s = self._summary
        return f"""SOURCE: {self.source_label} ({s['journeys']} journeys).

TABLE journey - one row per product-search journey (a grouped product hunt):
  journey_id TEXT (e.g. J000002), user_assoc_id INTEGER (internal user number)
  query_cluster_rep TEXT - representative search phrase (may contain damaged text)
  total_apps INTEGER, total_events INTEGER (= number of searches), apps_summary TEXT
  first_search_time TEXT, last_search_time TEXT (local time as stored)
  first_search_utc TEXT, last_search_utc TEXT (UTC, use for ordering/ranges)
  search_date TEXT 'YYYY-MM-DD', search_hour INTEGER 0-23 (of the first search)
  duration_seconds REAL (last search minus first search)
  status TEXT {s['statuses']}, close_reason TEXT {s['close_reasons']} or NULL
  first_search_app_id INTEGER, start_app TEXT (app of the first search), period_end TEXT (unreliable - avoid)

TABLE journey_app - one row per (journey, app):
  journey_id TEXT, app_name TEXT, event_count INTEGER (searches on that app)

Totals: {s['journeys']} journeys, {s['users']} users, {s['events']} searches,
{s['cross_app']} multi-app journeys, {s['purchased']} purchased, {s['timed_out']} timed out,
{s['open_journeys']} open. Dates {s['first_date']} to {s['last_date']}.
Apps (exact spelling): {', '.join(s['apps'])}.

Counting rules:
- journeys = COUNT(DISTINCT j.journey_id); searches = SUM(ja.event_count) when
  journey_app is joined, otherwise SUM(j.total_events). Never mix the two.
- After a join, SUM(j.total_events) double counts. Use ja.event_count.
- This journey dataset has NO price, order, product, payment, revenue, location or demographics.
"""

    def schema_card(self) -> str:
        return self._schema_card

    # ------------------------------------------------------------ entities
    def journey_detail(self, journey_id: str) -> dict:
        key = str(journey_id or "").strip().upper()
        row = self.conn.execute(
            """SELECT journey_id, user_assoc_id, query_cluster_rep, total_apps,
                      total_events, first_search_time, last_search_time,
                      duration_seconds, status, close_reason, apps_summary
               FROM journey WHERE journey_id = ?""", (key,)).fetchone()
        if row is None:
            raise JourneyDataError(f"There is no journey {key or '(blank)'} in this file.")
        detail = dict(row)
        detail["apps"] = self.query(
            "SELECT app_name, event_count FROM journey_app WHERE journey_id = ? "
            "ORDER BY event_count DESC, app_name", (key,)).dicts()
        return detail

    def show_journey(self, journey_id: str) -> str:
        detail = self.journey_detail(journey_id)
        detail.pop("apps", None)
        return "\n".join(f"{k}: {'' if v is None else v}" for k, v in detail.items())

    def user_profile(self, user_assoc_id, journey_limit: int = 500) -> dict:
        try:
            user_id = int(str(user_assoc_id).strip())
        except (TypeError, ValueError) as exc:
            raise JourneyDataError("Provide a numeric user id.") from exc
        stats = self.conn.execute(
            """SELECT COUNT(*) AS journeys,
                      COALESCE(SUM(total_events), 0) AS searches,
                      SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open_journeys,
                      SUM(CASE WHEN close_reason = 'purchased' THEN 1 ELSE 0 END) AS purchased,
                      SUM(CASE WHEN close_reason = 'timeout' THEN 1 ELSE 0 END) AS timed_out,
                      SUM(CASE WHEN total_apps > 1 THEN 1 ELSE 0 END) AS multi_app,
                      MIN(first_search_time) AS first_seen,
                      MAX(last_search_time) AS last_seen
               FROM journey WHERE user_assoc_id = ?""", (user_id,)).fetchone()
        if not stats or stats["journeys"] == 0:
            raise JourneyDataError(f"User {user_id} does not appear in this file.")
        apps = self.query(
            """SELECT ja.app_name, COUNT(DISTINCT j.journey_id) AS journeys,
                      SUM(ja.event_count) AS searches
               FROM journey j JOIN journey_app ja ON ja.journey_id = j.journey_id
               WHERE j.user_assoc_id = ?
               GROUP BY ja.app_name ORDER BY searches DESC, journeys DESC, ja.app_name""",
            (user_id,)).dicts()
        journey_list = self.query(
            """SELECT journey_id, query_cluster_rep AS query, apps_summary AS apps,
                      total_events AS searches, first_search_time AS first_search,
                      status, COALESCE(close_reason, '') AS outcome
               FROM journey WHERE user_assoc_id = ?
               ORDER BY first_search_utc, journey_id""", (user_id,), max_rows=journey_limit)
        return {"user_id": user_id, **dict(stats), "apps": apps, "journey_list": journey_list}

    def show_user(self, user_assoc_id) -> str:
        p = self.user_profile(user_assoc_id)
        apps = "; ".join(f"{a['app_name']}: {a['searches']} searches in {a['journeys']} journeys"
                         for a in p["apps"])
        return "\n".join([
            f"User {p['user_id']} summary: {p['journeys']} journeys, {p['searches']} searches, "
            f"{p['open_journeys']} open, {p['purchased']} purchased, {p['timed_out']} timed out.",
            f"Apps used ({len(p['apps'])}): {apps}.",
            f"All {p['journeys']} journeys:",
            p["journey_list"].to_text(limit=len(p["journey_list"])),
        ])

    def search_journeys(self, keyword: str, limit: int = 20) -> str:
        return self.find_journeys(keyword, limit).to_text(limit)

    def find_journeys(self, keyword: str, limit: int = 20, user_id: int | None = None) -> QueryResult:
        term = str(keyword or "").strip()
        if not term:
            raise JourneyDataError("Provide a search keyword.")
        limit = max(1, min(int(limit), 200))
        sql = """SELECT journey_id, query_cluster_rep AS query, apps_summary AS apps,
                        total_events AS searches, status, COALESCE(close_reason, '') AS outcome
                 FROM journey WHERE query_cluster_rep LIKE ? ESCAPE '\\' COLLATE NOCASE"""
        params: list = [f"%{escape_like(term)}%"]
        if user_id is not None:
            sql += " AND user_assoc_id = ?"
            params.append(user_id)
        sql += " ORDER BY total_events DESC, journey_id"
        return self.query(sql, params, max_rows=limit)

    def suggest_queries(self, keyword: str, limit: int = 5) -> list[str]:
        """Close spellings from the stored search phrases (typo tolerance)."""
        try:
            from rapidfuzz import fuzz, process
        except ImportError:
            return []
        choices = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT query_cluster_rep FROM journey WHERE query_cluster_rep IS NOT NULL")]
        hits = process.extract(keyword, choices, scorer=fuzz.WRatio, limit=limit, score_cutoff=80)
        return [h[0] for h in hits]

    def similar(self, journey_id: str, limit: int = 8) -> list[tuple[str, float]]:
        key = str(journey_id or "").strip().upper()
        if key not in self._index and key not in self._vectors:
            if not self.journey_exists(key):
                raise JourneyDataError(f"There is no journey {key} in this file.")
            raise JourneyDataError(f"Journey {key} has no usable embedding.")
        limit = max(1, min(int(limit), 50))
        if self._matrix is not None:
            scores = self._matrix @ self._matrix[self._index[key]]
            scores[self._index[key]] = -2.0
            top = np.argpartition(-scores, min(limit, len(scores) - 1))[:limit]
            top = top[np.argsort(-scores[top])]
            return [(self._ids[i], float(scores[i])) for i in top if scores[i] > -2.0]
        source = self._vectors[key]
        sn = math.sqrt(sum(v * v for v in source)) or 1.0
        ranked = []
        for other, vec in self._vectors.items():
            if other != key:
                n = math.sqrt(sum(v * v for v in vec)) or 1.0
                ranked.append((sum(a * b for a, b in zip(source, vec)) / (sn * n), other))
        ranked.sort(reverse=True)
        return [(jid, score) for score, jid in ranked[:limit]]

    def similar_journeys(self, journey_id: str, limit: int = 8) -> str:
        pairs = self.similar(journey_id, limit)
        if not pairs:
            return "(no similar journeys)"
        details = {r["journey_id"]: r for r in self.conn.execute(
            f"SELECT journey_id, query_cluster_rep, apps_summary, status FROM journey "
            f"WHERE journey_id IN ({','.join('?' * len(pairs))})", [p[0] for p in pairs])}
        lines = ["journey_id | similarity | query | apps | status"]
        for jid, score in pairs:
            row = details[jid]
            lines.append(f"{jid} | {score:.4f} | {row['query_cluster_rep']} | "
                         f"{row['apps_summary']} | {row['status']}")
        return "\n".join(lines)

    def close(self) -> None:
        for conn in (getattr(self, "_sandbox", None), self.conn):
            try:
                if conn is not None:
                    conn.close()
            except sqlite3.Error:
                pass
        if self._tempdir is not None:
            self._tempdir.cleanup()
            self._tempdir = None


# --------------------------------------------------------------------------- #
# SQL hygiene for model-written queries
# --------------------------------------------------------------------------- #

def escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def clean_sql(sql: str) -> str:
    cleaned = (sql or "").strip()
    fence = re.search(r"```(?:sql|sqlite)?\s*(.*?)```", cleaned, re.S | re.I)
    if fence:
        cleaned = fence.group(1)
    cleaned = cleaned.strip().rstrip(";").strip()
    if not cleaned:
        raise JourneyDataError("The query is empty.")
    if not re.match(r"^(select|with)\b", cleaned, re.I):
        raise JourneyDataError("Only a SELECT query is allowed.")
    if re.search(r"select\s+\*", cleaned, re.I):
        raise JourneyDataError("Name the columns you need instead of SELECT *.")
    return cleaned


# Backwards-compatible alias for older imports.
_clean_sql = clean_sql
