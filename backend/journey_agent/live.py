"""Load the Journey agent from the analyst's live PostgreSQL source.

The Journey agent deliberately keeps its existing private SQLite query store.
Only this module reads PostgreSQL, using one static application-owned query;
model-written SQL continues to run against :class:`JourneyStore`'s sandbox.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any

from analyst import settings_store
from analyst.settings import Settings
from analyst.sources import Source, SourceError, open_source

from .store import JourneyStore


LIVE_TABLE = "cross_app_journey"
APP_TABLE = "app_master"
LIVE_BATCH_SIZE = 5_000
MAX_RANGE_DAYS = 31
FILTER_MODES = frozenset({"latest_month", "date_range", "last_24_hours"})
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

# Validate the live schema before opening the bulk cursor.  Keeping this list
# beside the SELECT makes a database migration fail with a useful error rather
# than halfway through building a new in-memory store.
REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    LIVE_TABLE: frozenset({
        "id", "is_active", "is_deleted", "journey_id", "query_cluster_rep",
        "cluster_embedding", "total_apps", "total_events", "apps_summary",
        "first_search_time", "last_search_time", "status", "close_reason",
        "period_end", "first_search_app_id", "user_assoc_id", "updated_at",
    }),
    APP_TABLE: frozenset({"id", "app_name"}),
}


@dataclass(frozen=True)
class LiveJourneyLoad:
    """A completed, atomically swappable live Journey dataset.

    ``source`` contains display-safe metadata only.  It intentionally does not
    retain the ``PostgresSource`` object, its settings, or any credentials.
    """

    store: JourneyStore
    source: dict[str, Any]
    fingerprint: str
    refreshed_at: str
    db_ping_ms: int


def _qualified_select(schema: str, *, include_embeddings: bool, filter_sql: str) -> str:
    """Return the fixed live export query for a validated schema name."""
    if not _IDENTIFIER.fullmatch(schema):
        raise SourceError("The configured database schema name is not valid.")
    quoted = f'"{schema}"'
    embedding = "j.cluster_embedding" if include_embeddings else "NULL::jsonb"
    return f"""SELECT
    j.id,
    j.is_active,
    j.is_deleted,
    NULL::text AS created_at,
    NULL::text AS updated_at,
    NULL::text AS remarks,
    j.journey_id,
    NULL::text AS journey_hash,
    j.query_cluster_rep,
    {embedding} AS cluster_embedding,
    j.total_apps,
    j.total_events,
    j.apps_summary,
    j.first_search_time,
    j.last_search_time,
    j.status,
    j.close_reason,
    j.period_end,
    j.first_search_app_id,
    j.user_assoc_id,
    a.app_name AS start_app
FROM {quoted}."{LIVE_TABLE}" AS j
LEFT JOIN {quoted}."{APP_TABLE}" AS a ON a.id = j.first_search_app_id
WHERE j.is_active IS TRUE
  AND COALESCE(j.is_deleted, FALSE) IS FALSE
  {filter_sql}
ORDER BY j.id"""


def _stats_query(schema: str, filter_sql: str = "") -> str:
    if not _IDENTIFIER.fullmatch(schema):
        raise SourceError("The configured database schema name is not valid.")
    quoted = f'"{schema}"'
    return f"""SELECT COUNT(*)::bigint AS row_count,
    MAX(id)::bigint AS max_id,
    MAX(updated_at) AS max_updated_at,
    MIN(first_search_time) AS first_search_time,
    MAX(last_search_time) AS last_search_time
FROM {quoted}."{LIVE_TABLE}" AS j
WHERE j.is_active IS TRUE
  AND COALESCE(j.is_deleted, FALSE) IS FALSE
  {filter_sql}"""


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _normalise_filter(mode: str, date_from: str | None, date_to: str | None,
                      available: dict[str, Any]) -> tuple[str, str | None, str | None]:
    mode = (mode or "latest_month").strip().lower()
    if mode not in FILTER_MODES:
        raise SourceError("Journey date filter must be latest month, last 24 hours, or a date range.")
    if mode == "last_24_hours":
        return mode, None, None

    first_available = _as_date(available.get("first_search_time"))
    last_available = _as_date(available.get("last_search_time")) or first_available
    if mode == "latest_month":
        if last_available is None:
            return mode, None, None
        start = last_available - timedelta(days=MAX_RANGE_DAYS - 1)
        if first_available is not None:
            start = max(start, first_available)
        return mode, start.isoformat(), last_available.isoformat()

    try:
        start = date.fromisoformat(str(date_from or ""))
        end = date.fromisoformat(str(date_to or ""))
    except ValueError as exc:
        raise SourceError("Choose both Journey dates in YYYY-MM-DD format.") from exc
    if start > end:
        raise SourceError("Journey start date must be on or before the end date.")
    if (end - start).days >= MAX_RANGE_DAYS:
        raise SourceError(f"Journey date range can be at most {MAX_RANGE_DAYS} calendar days.")
    return mode, start.isoformat(), end.isoformat()


def _filter_clause(mode: str, date_from: str | None, date_to: str | None) -> tuple[str, dict | None]:
    if mode == "last_24_hours":
        return "AND j.first_search_time >= CURRENT_TIMESTAMP - INTERVAL '24 hours'", None
    if date_from and date_to:
        return ("AND j.first_search_time >= %(date_from)s::date "
                "AND j.first_search_time < (%(date_to)s::date + INTERVAL '1 day')",
                {"date_from": date_from, "date_to": date_to})
    return "", None


def _validate_schema(schema: dict[str, list[tuple[str, str]]], schema_name: str) -> None:
    problems: list[str] = []
    for table, required in REQUIRED_COLUMNS.items():
        actual = {column for column, _kind in schema.get(table, [])}
        if table not in schema:
            problems.append(f'{schema_name}."{table}" table is missing')
            continue
        missing = sorted(required - actual)
        if missing:
            problems.append(f'{schema_name}."{table}" is missing columns: {", ".join(missing)}')
    if problems:
        raise SourceError("Live journey data is not compatible: " + "; ".join(problems))


def _fingerprint(settings: Settings, stats: dict[str, Any]) -> str:
    """Identify this database snapshot without retaining or hashing secrets."""
    identity = {
        "kind": "postgres",
        "db_host": settings.db_host,
        "db_port": settings.db_port,
        "database": settings.db_name,
        "schema": settings.db_schema,
        "table": LIVE_TABLE,
        "ssh_host": settings.ssh_host,
        "ssh_port": settings.ssh_port if settings.ssh_host else None,
        "snapshot": stats,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _safe_source(source: Source, settings: Settings, *, filter_mode: str,
                 date_from: str | None, date_to: str | None,
                 available: dict[str, Any]) -> dict[str, Any]:
    """Whitelisted source details suitable for API responses and logs."""
    described = source.describe()
    return {
        "kind": "postgres",
        "database": settings.db_name,
        "schema": settings.db_schema,
        "table": LIVE_TABLE,
        "via_ssh": bool(described.get("via_ssh")),
        "read_only": True,
        "filter_mode": filter_mode,
        "date_from": date_from,
        "date_to": date_to,
        "available_first_date": str(available.get("first_search_time") or "")[:10] or None,
        "available_last_date": str(available.get("last_search_time") or "")[:10] or None,
        "available_last_update": str(available.get("max_updated_at") or "")[:19] or None,
    }


def load_live_journeys(settings: Settings | None = None, *,
                       batch_size: int = LIVE_BATCH_SIZE,
                       filter_mode: str = "latest_month",
                       date_from: str | None = None,
                       date_to: str | None = None) -> LiveJourneyLoad:
    """Build a JourneyStore from the configured live PostgreSQL database.

    The returned store is complete and ready to swap into the API.  PostgreSQL
    connections and the row iterator are always closed before this function
    returns, including when schema validation or local ingestion fails.
    """
    configured = settings or settings_store.effective()
    if configured.source != "postgres":
        raise SourceError("The Journey agent needs a live PostgreSQL source. "
                          "Choose Live PostgreSQL in Connection settings.")

    # A refresh is one bulk reader, so it needs only one database/SSH slot even
    # when the interactive analyst is configured with a larger pool.
    live_settings = replace(configured, pool_size=1)
    source: Source | None = None
    records = None
    try:
        source = open_source(live_settings)
        if source.kind != "postgres":
            raise SourceError("The Journey agent needs a live PostgreSQL source.")
        db_ping_ms = source.ping()
        _validate_schema(source.introspect(), live_settings.db_schema)
        stats_timeout = max(15_000, live_settings.statement_timeout_ms)
        available_result = source.query(_stats_query(live_settings.db_schema), max_rows=1,
                                        timeout_ms=stats_timeout)
        available = available_result.dicts()[0] if available_result.rows else {"row_count": 0}
        filter_mode, date_from, date_to = _normalise_filter(
            filter_mode, date_from, date_to, available)
        filter_sql, filter_params = _filter_clause(filter_mode, date_from, date_to)
        stats_result = source.query(_stats_query(live_settings.db_schema, filter_sql),
                                    filter_params, max_rows=1, timeout_ms=stats_timeout)
        stats = stats_result.dicts()[0] if stats_result.rows else {"row_count": 0}
        row_count = int(stats.get("row_count") or 0)
        if row_count <= 0:
            latest = str(available.get("last_search_time") or "")[:10] or "not available"
            selected = ("the last 24 hours" if filter_mode == "last_24_hours"
                        else f"{date_from} to {date_to}")
            raise SourceError(f"No live journeys were found for {selected}. "
                              f"The newest cross_app_journey data is {latest}.")
        # Embeddings are optional (only "similar journey" questions need
        # them) and dominate transfer/memory on company-sized tables.
        embedding_limit = max(0, int(os.getenv("JOURNEY_LIVE_EMBEDDING_MAX_ROWS", "10000")))
        include_embeddings = bool(embedding_limit and row_count <= embedding_limit)
        sql = _qualified_select(live_settings.db_schema, include_embeddings=include_embeddings,
                                filter_sql=filter_sql)
        records = source.iter_dicts(
            sql,
            filter_params,
            batch_size=batch_size,
            # Bulk refreshes get a larger floor than an interactive question;
            # sources.MAX_QUERY_TIMEOUT_MS is the hard ceiling above it.
            timeout_ms=max(60_000, live_settings.statement_timeout_ms),
        )
        label = (f"live PostgreSQL "
                 f"{live_settings.db_name}.{live_settings.db_schema}.{LIVE_TABLE}")
        store = JourneyStore(None, records=records, source_label=label, disk_backed=True)
        safe_source = _safe_source(source, live_settings, filter_mode=filter_mode,
                                   date_from=date_from, date_to=date_to, available=available)
        return LiveJourneyLoad(
            store=store,
            source=safe_source,
            fingerprint=_fingerprint(live_settings, {"available": available, "selected": stats,
                                                     "filter_mode": filter_mode,
                                                     "date_from": date_from, "date_to": date_to}),
            refreshed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            db_ping_ms=db_ping_ms,
        )
    finally:
        # Closing a partially-consumed generator runs iter_dicts' transaction
        # cleanup before the source closes its pool and optional SSH tunnel.
        try:
            close_records = getattr(records, "close", None)
            if callable(close_records):
                close_records()
        finally:
            if source is not None:
                source.close()


__all__ = [
    "APP_TABLE",
    "LIVE_BATCH_SIZE",
    "LIVE_TABLE",
    "MAX_RANGE_DAYS",
    "LiveJourneyLoad",
    "REQUIRED_COLUMNS",
    "load_live_journeys",
]
