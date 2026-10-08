"""Self-contained tests for the Journey agent's live PostgreSQL loader.

These tests intentionally do not use the private journey export expected by
``test_journey_agent.py``.  PostgreSQL is represented by a small streaming
source so the loader contract, cleanup, and safe public metadata are exercised
without a network connection.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest


BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from analyst.settings import Settings  # noqa: E402
from analyst.sources import Result, SourceError  # noqa: E402
from journey_agent import live  # noqa: E402
from journey_agent.store import JourneyStore  # noqa: E402


def _record(
    *,
    row_id: int,
    journey_id: str,
    user_id: int,
    apps_summary: str,
    total_apps: int,
    total_events: int,
    first: str,
    last: str,
    status: str,
    close_reason: str | None,
    first_app_id: int,
    start_app: str,
) -> dict:
    """One row in the shape produced by ``live._qualified_select``."""
    return {
        "id": row_id,
        "is_active": True,
        "is_deleted": False,
        # Deliberately misleading lifecycle dates: the displayed range must
        # come from first/last search time, not these fields or period_end.
        "created_at": "2030-01-01 00:00:00+00:00",
        "updated_at": "2030-01-02 00:00:00+00:00",
        "remarks": None,
        "journey_id": journey_id,
        "journey_hash": None,
        "query_cluster_rep": f"query for {journey_id}",
        "cluster_embedding": None,
        "total_apps": total_apps,
        "total_events": total_events,
        "apps_summary": apps_summary,
        "first_search_time": first,
        "last_search_time": last,
        "status": status,
        "close_reason": close_reason,
        "period_end": "2031-12-31 23:59:59+00:00",
        "first_search_app_id": first_app_id,
        "user_assoc_id": user_id,
        "start_app": start_app,
    }


def _rows() -> list[dict]:
    return [
        _record(
            row_id=1,
            journey_id="J000001",
            user_id=10,
            apps_summary="Amazon (2)",
            total_apps=1,
            total_events=2,
            first="2026-09-12 23:30:00+05:30",
            last="2026-09-13 00:05:00+05:30",
            status="closed",
            close_reason="purchased",
            first_app_id=101,
            start_app="Amazon",
        ),
        _record(
            row_id=7,  # IDs need not be contiguous.
            journey_id="J000007",
            user_id=20,
            apps_summary="Meesho (3), Flipkart (1)",
            total_apps=2,
            total_events=4,
            first="2026-09-17 09:00:00+05:30",
            last="2026-09-17 09:10:00+05:30",
            status="closed",
            close_reason="timeout",
            first_app_id=202,
            start_app="Meesho",
        ),
    ]


def _schema() -> dict[str, list[tuple[str, str]]]:
    return {
        table: [(column, "text") for column in sorted(columns)]
        for table, columns in live.REQUIRED_COLUMNS.items()
    }


class TrackingRows:
    """Closeable iterator like ``PostgresSource.iter_dicts``."""

    def __init__(self, rows: list[dict], *, fail_at: int | None = None):
        self.rows = rows
        self.fail_at = fail_at
        self.position = 0
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self.fail_at is not None and self.position == self.fail_at:
            raise SourceError("simulated database stream failure")
        if self.position >= len(self.rows):
            raise StopIteration
        row = self.rows[self.position]
        self.position += 1
        return dict(row)

    def close(self):
        self.closed = True


class FakeSource:
    kind = "postgres"
    dialect = "postgres"

    def __init__(self, records: TrackingRows, *, schema=None):
        self.records = records
        self.schema = _schema() if schema is None else schema
        self.closed = False
        self.iter_call: dict | None = None
        self.query_call: dict | None = None

    def ping(self) -> int:
        return 17

    def introspect(self):
        return self.schema

    def query(self, sql, params=None, max_rows=500, timeout_ms=None):
        self.query_call = {
            "sql": sql,
            "params": params,
            "max_rows": max_rows,
            "timeout_ms": timeout_ms,
        }
        return Result(
            ["row_count", "max_id", "max_updated_at", "first_search_time", "last_search_time"],
            [[len(self.records.rows), 7, "2026-09-17 09:11:00+05:30",
              "2026-09-12 23:30:00+05:30", "2026-09-17 09:10:00+05:30"]],
            sql=sql,
        )

    def iter_dicts(self, sql, params=None, batch_size=5000, timeout_ms=None):
        self.iter_call = {
            "sql": sql,
            "params": params,
            "batch_size": batch_size,
            "timeout_ms": timeout_ms,
        }
        return self.records

    def describe(self):
        # Only ``via_ssh`` is allowed through the loader's metadata whitelist.
        return {
            "kind": "postgres",
            "via_ssh": True,
            "password": "describe-secret",
            "db_host": "private-db.internal",
            "ssh_host_key": "sensitive-detail",
        }

    def close(self):
        self.closed = True


def _settings() -> Settings:
    return Settings(
        source="postgres",
        db_host="private-db.internal",
        db_port=5432,
        db_name="company_analytics",
        db_username="journey_reader",
        db_password="db-super-secret",
        db_schema="public",
        ssh_host="bastion.internal",
        ssh_username="analytics",
        ssh_password="ssh-super-secret",
        pool_size=8,
        statement_timeout_ms=15_000,
    )


def test_store_accepts_records_and_computes_summary_dates_and_start_app():
    store = JourneyStore(None, records=iter(_rows()), source_label="live test source")
    try:
        summary = store.summary()
        expected = {
            "journeys": 2,
            "users": 2,
            "events": 6,
            "cross_app": 1,
            "open_journeys": 0,
            "closed_journeys": 2,
            "purchased": 1,
            "timed_out": 1,
            "first_date": "2026-09-12",
            "last_date": "2026-09-17",
            "apps": ["Amazon", "Flipkart", "Meesho"],
            "source": "live test source",
        }
        assert {key: summary[key] for key in expected} == expected
        facts = store.query(
            """SELECT journey_id, start_app, first_search_utc, last_search_utc,
                      duration_seconds
               FROM journey ORDER BY id"""
        ).dicts()
        assert facts[0] == {
            "journey_id": "J000001",
            "start_app": "Amazon",
            "first_search_utc": "2026-09-12 18:00:00.000",
            "last_search_utc": "2026-09-12 18:35:00.000",
            "duration_seconds": 2100.0,
        }
        # A live app_master join gives an exact start app for multi-app rows;
        # the older file-only majority mapping must not erase it.
        assert facts[1]["start_app"] == "Meesho"
        assert "SOURCE: live test source (2 journeys)" in store.schema_card()
    finally:
        store.close()


def test_live_loader_streams_fixed_query_and_returns_only_safe_metadata(monkeypatch):
    records = TrackingRows(_rows())
    source = FakeSource(records)
    opened_with: list[Settings] = []

    def open_source(settings):
        opened_with.append(settings)
        return source

    monkeypatch.setattr(live, "open_source", open_source)
    # Large live datasets should not copy embedding arrays into SQLite.
    monkeypatch.setenv("JOURNEY_LIVE_EMBEDDING_MAX_ROWS", "1")
    loaded = live.load_live_journeys(_settings(), batch_size=2)
    temp_path = Path(loaded.store._tempdir.name)
    try:
        assert source.closed and records.closed
        assert opened_with[0].pool_size == 1
        assert loaded.db_ping_ms == 17
        assert re.fullmatch(r"[0-9a-f]{24}", loaded.fingerprint)
        assert loaded.refreshed_at.endswith("+00:00")
        assert loaded.source == {
            "kind": "postgres",
            "database": "company_analytics",
            "schema": "public",
            "table": "cross_app_journey",
            "via_ssh": True,
            "read_only": True,
            "filter_mode": "latest_month",
            "date_from": "2026-09-12",
            "date_to": "2026-09-17",
            "available_first_date": "2026-09-12",
            "available_last_date": "2026-09-17",
            "available_last_update": "2026-09-17 09:11:00",
        }
        assert loaded.store._tempdir is not None
        public_text = json.dumps({
            "source": loaded.source,
            "fingerprint": loaded.fingerprint,
            "summary": loaded.store.summary(),
        })
        for secret in (
            "db-super-secret",
            "ssh-super-secret",
            "describe-secret",
            "journey_reader",
            "private-db.internal",
            "bastion.internal",
            "sensitive-detail",
        ):
            assert secret not in public_text

        call = source.iter_call
        assert call is not None
        assert call["batch_size"] == 2
        assert call["timeout_ms"] == 60_000
        assert call["params"] == {"date_from": "2026-09-12", "date_to": "2026-09-17"}
        sql = call["sql"]
        assert 'FROM "public"."cross_app_journey" AS j' in sql
        assert 'LEFT JOIN "public"."app_master" AS a' in sql
        assert "j.is_active IS TRUE" in sql
        assert "COALESCE(j.is_deleted, FALSE) IS FALSE" in sql
        assert "j.first_search_time >= %(date_from)s::date" in sql
        assert "NULL::jsonb AS cluster_embedding" in sql
        assert sql.rstrip().endswith("ORDER BY j.id")
        stats_call = source.query_call
        assert stats_call is not None
        assert stats_call["max_rows"] == 1
        assert stats_call["timeout_ms"] == 15_000
        assert 'FROM "public"."cross_app_journey"' in stats_call["sql"]
        assert "MAX(updated_at)" in stats_call["sql"]
        assert loaded.store.summary()["first_date"] == "2026-09-12"
        assert loaded.store.summary()["last_date"] == "2026-09-17"
    finally:
        loaded.store.close()
    assert not temp_path.exists()


def test_live_loader_closes_iterator_and_source_when_stream_fails(monkeypatch):
    records = TrackingRows(_rows(), fail_at=1)
    source = FakeSource(records)
    monkeypatch.setattr(live, "open_source", lambda _settings: source)

    with pytest.raises(SourceError, match="simulated database stream failure"):
        live.load_live_journeys(_settings())

    assert records.closed
    assert source.closed


def test_live_loader_closes_source_when_schema_validation_fails(monkeypatch):
    records = TrackingRows(_rows())
    broken_schema = _schema()
    broken_schema[live.LIVE_TABLE] = [
        pair for pair in broken_schema[live.LIVE_TABLE] if pair[0] != "journey_id"
    ]
    source = FakeSource(records, schema=broken_schema)
    monkeypatch.setattr(live, "open_source", lambda _settings: source)

    with pytest.raises(SourceError, match=r"cross_app_journey.*journey_id"):
        live.load_live_journeys(_settings())

    assert source.closed
    assert source.iter_call is None
    assert not records.closed  # no iterator was opened, so there was nothing to close


def test_date_range_is_limited_to_31_calendar_days():
    available = {
        "first_search_time": "2026-01-01 00:00:00+05:30",
        "last_search_time": "2026-09-17 23:00:00+05:30",
    }
    assert live._normalise_filter("date_range", "2026-08-18", "2026-09-17", available) == (
        "date_range", "2026-08-18", "2026-09-17")
    with pytest.raises(SourceError, match="at most 31 calendar days"):
        live._normalise_filter("date_range", "2026-08-17", "2026-09-17", available)
