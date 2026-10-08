"""Data sources for the analyst.

* ``PostgresSource`` - the live ``datamyner_db`` database, optionally through an
  SSH tunnel (the same connection method as your ``fetch`` script). Every
  session is read-only (``default_transaction_read_only``), time-limited
  (``statement_timeout``) and pooled; a dropped tunnel or connection is rebuilt
  automatically. The statement timeout is per query: a caller with a thinking
  budget passes its remaining time, and ``statement_timeout_ms`` is the default
  used when there is none.
* ``SnapshotSource`` - a folder of JSON table exports (``--table x --output
  x.json``) loaded into an in-process DuckDB. Used for development, demos and
  tests when the live database is not reachable.

Both expose the same interface and accept PostgreSQL-dialect SQL; the snapshot
translates it to DuckDB with sqlglot.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from uuid import UUID

from .settings import Settings

log = logging.getLogger("analyst.sources")


# ``statement_timeout_ms`` is the DEFAULT for a query that carries no deadline of its
# own (schema probes, row counts, health pings). A caller that owns a thinking budget
# passes its remaining time as ``timeout_ms`` and may exceed that default, because the
# budget - not a fixed 15 s - is what the user actually agreed to wait. This is the hard
# ceiling for any single statement, matching the 20-minute thinking limit.
MAX_QUERY_TIMEOUT_MS = 1_200_000
# How long a query waits for a free pool slot. Bounded separately from the statement
# timeout: with a 20-minute budget, queueing silently for 20 minutes behind other
# queries is a worse failure than saying the database is busy.
SLOT_WAIT_S = 60


class SourceError(RuntimeError):
    """A query failed; the message is safe to show to the model and the user."""


class QueryTimeout(SourceError):
    pass


# Raw driver messages -> advice a person can act on (shown on the Settings page).
_EXPLAIN = [
    (r"password authentication failed", "Database login failed: check the database username and password."),
    (r"no pg_hba\.conf entry", "The database refused this user/host (pg_hba.conf). Ask the DBA to allow it, "
                               "or try SSL mode 'require'."),
    (r'database "[^"]+" does not exist', "That database name does not exist on the server."),
    (r'role "[^"]+" does not exist', "That database username does not exist."),
    (r"ssl.*(not support|unsupported)|server does not support ssl", "The server does not support SSL: "
                                                                     "set SSL mode to 'disable' or 'prefer'."),
    (r"connection refused", "Nothing is listening at the database host/port (connection refused). "
                            "With SSH, the host/port are as seen from the SSH server, usually 127.0.0.1:5432."),
    (r"timeout expired|timed out", "The database did not answer in time: check the host, port and firewall."),
    (r"could not translate host name|name or service not known|nodename nor servname",
     "The database host name could not be resolved."),
    (r"server closed the connection unexpectedly", "The database closed the connection: check the host/port "
                                                   "and SSL mode."),
]


def explain_error(exc: BaseException | str) -> str:
    text = str(exc).strip()
    for pattern, advice in _EXPLAIN:
        if re.search(pattern, text, re.IGNORECASE):
            first = text.splitlines()[0][:200] if text else ""
            return f"{advice} ({first})" if first and first not in advice else advice
    return text.splitlines()[0][:300] if text else "Unknown error"


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #

def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value:                                   # NaN
            return None
        return int(value) if value.is_integer() and abs(value) < 1e15 else round(value, 6)
    if isinstance(value, Decimal):
        f = float(value)
        return int(f) if f.is_integer() and abs(f) < 1e15 else round(f, 6)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, (date, dtime)):
        return value.isoformat()
    if isinstance(value, (UUID, bytes)):
        return str(value) if isinstance(value, UUID) else value.hex()
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    return str(value)


@dataclass
class Result:
    columns: list[str]
    rows: list[list[Any]]
    sql: str = ""
    truncated: bool = False
    elapsed_ms: int = 0

    def dicts(self) -> list[dict]:
        return [dict(zip(self.columns, r)) for r in self.rows]

    def scalar(self):
        return self.rows[0][0] if self.rows and self.rows[0] else None

    def to_text(self, limit: int = 40) -> str:
        """Pipe table for the language model, with an explicit completeness marker."""
        if not self.rows:
            return "(no rows)"
        lines = [" | ".join(self.columns)]
        for r in self.rows[:limit]:
            lines.append(" | ".join("" if v is None else str(v) for v in r))
        shown = min(limit, len(self.rows))
        if self.truncated or shown < len(self.rows):
            lines.append(f"[PARTIAL: {shown} of {len(self.rows)}{'+' if self.truncated else ''} rows shown]")
        else:
            lines.append(f"[COMPLETE: all {len(self.rows)} rows]")
        return "\n".join(lines)

    def as_table(self, limit: int = 500) -> dict:
        return {"columns": self.columns, "rows": self.rows[:limit],
                "total_rows": len(self.rows), "truncated": self.truncated or len(self.rows) > limit}


# --------------------------------------------------------------------------- #
# Type inference for loading JSON exports
# --------------------------------------------------------------------------- #

def _looks_ts(s: str) -> bool:
    return len(s) >= 19 and s[4] == "-" and s[7] == "-" and s[10] in "T " and s[13] == ":"


def infer_type(values: Iterable[Any]) -> str:
    """Postgres type for a JSON column: boolean, bigint, double precision, timestamptz, date, text, text[], jsonb..."""
    seen = [v for v in values if v is not None]
    if not seen:
        return "text"
    if all(isinstance(v, bool) for v in seen):
        return "boolean"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in seen):
        return "bigint"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in seen):
        return "double precision"
    if all(isinstance(v, list) for v in seen):
        items = [i for v in seen for i in v if i is not None]
        if not items or all(isinstance(i, str) for i in items):
            return "text[]"
        if all(isinstance(i, (int, float)) and not isinstance(i, bool) for i in items):
            return "double precision[]"
        return "jsonb"
    if all(isinstance(v, dict) for v in seen) or any(isinstance(v, (dict, list)) for v in seen):
        return "jsonb"
    if all(isinstance(v, str) for v in seen):
        if all(_looks_ts(v) for v in seen):
            return "timestamptz"
        if all(len(v) == 10 and v[4] == "-" and v[7] == "-" for v in seen):
            return "date"
    return "text"


_DUCK = {"boolean": "BOOLEAN", "bigint": "BIGINT", "double precision": "DOUBLE", "timestamptz": "TIMESTAMPTZ",
         "date": "DATE", "text": "VARCHAR", "text[]": "VARCHAR[]", "double precision[]": "DOUBLE[]", "jsonb": "JSON"}


def read_export(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("data") or data.get("rows") or []
    return data if isinstance(data, list) else []


def infer_schema(rows: list[dict], sample: int = 2000) -> list[tuple[str, str]]:
    columns: list[str] = []
    for r in rows[:sample]:
        for k in r:
            if k not in columns:
                columns.append(k)
    out = []
    for c in columns:
        values = [r.get(c) for r in rows]
        out.append((c, infer_type(values[:sample]) if any(v is not None for v in values) else type_from_name(c)))
    return out


def type_from_name(column: str) -> str:
    """Type for a column that is empty in the export, guessed from its name, so joins and sums still work
    (e.g. purchase_app_id must stay a number to join app_master.id)."""
    c = column.lower()
    if c.startswith(("is_", "has_")):
        return "boolean"
    if c == "id" or c.endswith("_id") and not c.endswith(("device_id", "order_id", "uid")):
        return "bigint"
    if c.endswith(("_time", "_at")) or c in ("timestamp", "created", "updated"):
        return "timestamptz"
    if c.endswith("_date") or c == "date":
        return "date"
    if re.search(r"(amount|price|value|hours|minutes|seconds|duration|rate|score|total|count)(_|$)|^no_of_", c):
        return "double precision"
    return "text"


# --------------------------------------------------------------------------- #
# Base
# --------------------------------------------------------------------------- #

def _query_timeout_ms(requested: int | None, default_ms: int) -> int:
    """Statement timeout for one query: the caller's deadline when it has one, else the
    configured default, always inside [1 s, MAX_QUERY_TIMEOUT_MS]."""
    return max(1000, min(int(requested or default_ms), MAX_QUERY_TIMEOUT_MS))


def _human_ms(milliseconds: int) -> str:
    seconds = milliseconds / 1000
    return f"{seconds:.0f}s" if seconds < 90 else f"{seconds / 60:.0f} min"


class Source:
    dialect = "postgres"
    kind = "unknown"

    def query(self, sql: str, params: dict | None = None, max_rows: int = 500,
              timeout_ms: int | None = None) -> Result:
        raise NotImplementedError

    def iter_dicts(self, sql: str, params: dict | None = None, batch_size: int = 5000,
                   timeout_ms: int | None = None):
        """Stream a consistent read-only result for internal bulk loaders."""
        raise NotImplementedError

    def introspect(self) -> dict[str, list[tuple[str, str]]]:
        """{table: [(column, type), ...]} for every table in the schema."""
        raise NotImplementedError

    def row_counts(self, tables: Iterable[str]) -> dict[str, int]:
        raise NotImplementedError

    def ping(self) -> int:
        raise NotImplementedError

    def server_info(self) -> dict:
        return {}

    def describe(self) -> dict:
        raise NotImplementedError

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# Live PostgreSQL (optionally through an SSH tunnel)
# --------------------------------------------------------------------------- #

class PostgresSource(Source):
    dialect = "postgres"
    kind = "postgres"

    def __init__(self, settings: Settings):
        import psycopg  # noqa: F401  (fail early with a clear error if missing)
        self.s = settings
        self._tunnel = None
        self._lock = threading.Lock()
        self._pool: queue.LifoQueue = queue.LifoQueue(maxsize=settings.pool_size)
        # A bounded queue only limits idle connections. A semaphore also limits
        # checked-out connections during bursts of simultaneous company chats.
        self._slots = threading.BoundedSemaphore(max(1, settings.pool_size))
        missing = [n for n, v in (("DB_USERNAME", settings.db_username), ("DB_PASSWORD", settings.db_password)) if not v]
        if settings.ssh_host and not (settings.ssh_password or settings.ssh_pkey):
            missing.append("SSH_PASSWORD or SSH_PKEY")
        if missing:
            raise SourceError("Missing connection settings: " + ", ".join(missing))

    # ---------------------------------------------------------------- tunnel
    def _endpoint(self) -> tuple[str, int]:
        if not self.s.ssh_host:
            return self.s.db_host, self.s.db_port
        with self._lock:
            if self._tunnel is None or not self._tunnel.is_active:
                from .ssh_tunnel import SSHTunnel, TunnelError
                if self._tunnel is not None:
                    self._tunnel.stop()
                    self._tunnel = None
                tunnel = SSHTunnel(self.s.ssh_host, self.s.ssh_port, self.s.ssh_username,
                                   password=self.s.ssh_password, pkey_path=os.path.expanduser(self.s.ssh_pkey)
                                   if self.s.ssh_pkey else "", remote=(self.s.db_host, self.s.db_port),
                                   expected_fingerprint=self.s.ssh_host_key_sha256)
                try:
                    tunnel.start()
                except TunnelError as exc:
                    raise SourceError(str(exc)) from exc
                self._tunnel = tunnel
            return "127.0.0.1", self._tunnel.local_port

    # ------------------------------------------------------------------ pool
    def _connect(self):
        import psycopg
        host, port = self._endpoint()
        try:
            return self._open_conn(psycopg, host, port)
        except psycopg.OperationalError as exc:
            if self._tunnel is not None and self._tunnel.last_error:
                raise SourceError(f"SSH login worked, but the SSH server could not reach the database at "
                                  f"{self.s.db_host}:{self.s.db_port} ({self._tunnel.last_error}). The database "
                                  "host/port must be as seen from the SSH server (often 127.0.0.1:5432).") from exc
            raise

    def _open_conn(self, psycopg, host, port):
        options = (f"-c default_transaction_read_only=on -c statement_timeout={self.s.statement_timeout_ms} "
                   f"-c idle_in_transaction_session_timeout=30000 -c search_path={self.s.db_schema} "
                   f"-c TimeZone={self.s.timezone}")
        # Keepalives matter for long queries: while PostgreSQL computes for minutes the
        # socket is idle, and an SSH tunnel or NAT in between may drop it silently.
        conn = psycopg.connect(host=host, port=port, dbname=self.s.db_name, user=self.s.db_username,
                               password=self.s.db_password, sslmode=self.s.db_sslmode, connect_timeout=10,
                               application_name="datamyner-analyst", options=options, autocommit=True,
                               keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5)
        return conn

    def _acquire(self):
        try:
            conn = self._pool.get_nowait()
            if not conn.closed and not conn.broken:
                return conn
        except queue.Empty:
            pass
        return self._connect()

    def _release(self, conn) -> None:
        if conn.closed or conn.broken:
            return
        try:
            self._pool.put_nowait(conn)
        except queue.Full:
            conn.close()

    # ----------------------------------------------------------------- query
    def query(self, sql: str, params: dict | None = None, max_rows: int = 500,
              timeout_ms: int | None = None) -> Result:
        import psycopg
        from psycopg import errors
        timeout = _query_timeout_ms(timeout_ms, self.s.statement_timeout_ms)
        if not self._slots.acquire(timeout=min(SLOT_WAIT_S, max(1.0, timeout / 1000))):
            raise QueryTimeout(f"The database is busy (all {self.s.pool_size} query slots are in use). Try again.")
        try:
            for attempt in (1, 2):
                conn = None
                started = time.perf_counter()
                try:
                    conn = self._acquire()
                    with conn.cursor() as cur:
                        cur.execute("SELECT set_config('statement_timeout', %s, false)", (str(max(timeout, 100)),))
                        cur.execute(sql, params or None)
                        fetched = cur.fetchmany(max_rows + 1) if cur.description else []
                        columns = [d.name for d in cur.description] if cur.description else []
                    truncated = len(fetched) > max_rows
                    rows = [[jsonable(v) for v in r] for r in fetched[:max_rows]]
                    self._release(conn)
                    return Result(columns, rows, sql=sql, truncated=truncated,
                                  elapsed_ms=int((time.perf_counter() - started) * 1000))
                except errors.QueryCanceled as exc:
                    self._release(conn) if conn else None
                    raise QueryTimeout(
                        f"The query took longer than {_human_ms(timeout)} and was stopped. Raise "
                        "\u201cMax thinking time\u201d in the sidebar, or add filters (dates, app) "
                        "and aggregate more.") from exc
                except (errors.InsufficientPrivilege, errors.ReadOnlySqlTransaction) as exc:
                    self._release(conn) if conn else None
                    raise SourceError(f"Not allowed: {exc.diag.message_primary or exc}") from exc
                except psycopg.OperationalError as exc:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
                    if attempt == 2:
                        raise SourceError(f"Database connection failed: {explain_error(exc)}") from exc
                    log.warning("connection lost, reconnecting: %s", exc)
                    with self._lock:
                        if self._tunnel is not None and not self._tunnel.is_active:
                            self._tunnel.stop()
                            self._tunnel = None
                except psycopg.Error as exc:
                    self._release(conn) if conn else None
                    msg = getattr(getattr(exc, "diag", None), "message_primary", None) or str(exc)
                    raise SourceError(f"SQL error: {msg}") from exc
            raise SourceError("Database connection failed.")
        finally:
            self._slots.release()

    def iter_dicts(self, sql: str, params: dict | None = None, batch_size: int = 5000,
                   timeout_ms: int | None = None):
        """Yield dictionaries from one repeatable-read, read-only transaction.

        This is intentionally separate from ``query``: bulk consumers such as
        the Journey agent must not silently stop at ``max_rows``, and all rows
        in one refresh must come from the same PostgreSQL snapshot.
        """
        import psycopg
        from psycopg import errors

        batch_size = max(100, min(int(batch_size), 20_000))
        # A full export may legitimately take longer than one interactive
        # query, while still having the same hard ceiling.
        timeout = _query_timeout_ms(timeout_ms, self.s.statement_timeout_ms)
        if not self._slots.acquire(timeout=min(SLOT_WAIT_S, max(1.0, timeout / 1000))):
            raise QueryTimeout(f"The database is busy (all {self.s.pool_size} query slots are in use). Try again.")
        conn = None
        try:
            conn = self._acquire()
            with conn.transaction():
                with conn.cursor() as setup:
                    setup.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    setup.execute("SELECT set_config('statement_timeout', %s, true)", (str(timeout),))
                cursor_name = f"datamyner_export_{threading.get_ident()}_{int(time.time() * 1000)}"
                with conn.cursor(name=cursor_name) as cur:
                    cur.itersize = batch_size
                    cur.execute(sql, params or None)
                    columns = [d.name for d in cur.description] if cur.description else []
                    while True:
                        rows = cur.fetchmany(batch_size)
                        if not rows:
                            break
                        for row in rows:
                            yield {name: jsonable(value) for name, value in zip(columns, row)}
            self._release(conn)
            conn = None
        except errors.QueryCanceled as exc:
            raise QueryTimeout(f"The live journey refresh took longer than {timeout / 1000:.0f}s and was stopped.") from exc
        except psycopg.OperationalError as exc:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
            raise SourceError(f"Database connection failed during live refresh: {explain_error(exc)}") from exc
        except psycopg.Error as exc:
            msg = getattr(getattr(exc, "diag", None), "message_primary", None) or str(exc)
            raise SourceError(f"SQL error during live refresh: {msg}") from exc
        finally:
            if conn is not None:
                try:
                    self._release(conn)
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
            self._slots.release()

    def introspect(self) -> dict[str, list[tuple[str, str]]]:
        r = self.query("""SELECT table_name, column_name,
                                 CASE WHEN data_type = 'ARRAY' THEN udt_name ELSE data_type END
                          FROM information_schema.columns WHERE table_schema = %(schema)s
                          ORDER BY table_name, ordinal_position""", {"schema": self.s.db_schema}, max_rows=100000)
        out: dict[str, list[tuple[str, str]]] = {}
        for table, column, typ in r.rows:
            typ = {"_text": "text[]", "_varchar": "text[]", "_float8": "double precision[]"}.get(typ, typ)
            out.setdefault(table, []).append((column, typ))
        return out

    def row_counts(self, tables: Iterable[str]) -> dict[str, int]:
        """Fast estimates from pg_class (exact counts on big tables would be slow)."""
        names = list(tables)
        if not names:
            return {}
        r = self.query("""SELECT c.relname, GREATEST(c.reltuples, 0)::bigint
                          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                          WHERE n.nspname = %(schema)s AND c.relname = ANY(%(names)s)""",
                       {"schema": self.s.db_schema, "names": names}, max_rows=10000)
        counts = {name: int(n) for name, n in r.rows}
        # Tables never analysed report 0/-1; count those exactly if they are small.
        for name in names:
            if counts.get(name, 0) <= 0:
                try:
                    counts[name] = int(self.query(f'SELECT COUNT(*) FROM "{name}"', max_rows=1,
                                                  timeout_ms=3000).scalar() or 0)
                except SourceError:
                    counts[name] = -1
        return counts

    def ping(self) -> int:
        """Round trip of SELECT 1 in ms (raises SourceError when the database is unreachable)."""
        return self.query("SELECT 1", max_rows=1, timeout_ms=5000).elapsed_ms

    def server_info(self) -> dict:
        r = self.query("""SELECT current_setting('server_version'), current_user,
                                 current_setting('default_transaction_read_only'),
                                 (SELECT rolsuper FROM pg_roles WHERE rolname = current_user),
                                 EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                                         WHERE n.nspname = %(schema)s AND c.relkind = 'r'
                                           AND has_table_privilege(c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE'))""",
                       {"schema": self.s.db_schema}, max_rows=1)
        version, user, read_only, superuser, can_write = r.rows[0]
        return {"server_version": version, "user": user, "read_only_session": read_only == "on",
                "superuser": bool(superuser), "can_write": bool(can_write)}

    def describe(self) -> dict:
        out = {"kind": "postgres", "database": self.s.db_name, "schema": self.s.db_schema,
               "via_ssh": bool(self.s.ssh_host), "read_only": True}
        if self._tunnel is not None and self._tunnel.host_fingerprint:
            out["ssh_host_key"] = self._tunnel.host_fingerprint
        return out

    def close(self) -> None:
        while True:
            try:
                self._pool.get_nowait().close()
            except queue.Empty:
                break
            except Exception:
                pass
        if self._tunnel is not None:
            try:
                self._tunnel.stop()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Offline snapshot (JSON exports -> DuckDB)
# --------------------------------------------------------------------------- #

class SnapshotSource(Source):
    dialect = "duckdb"
    kind = "snapshot"

    def __init__(self, folder: Path, skip_tables: Iterable[str] = (), statement_timeout_ms: int = 15000,
                 timezone: str = "Asia/Kolkata"):
        import duckdb
        self.folder = Path(folder)
        if not self.folder.is_dir():
            raise SourceError(f"Offline snapshot folder not found ({self.folder}). Connect your live database "
                              "in Connection settings, or unzip your JSON export into that folder.")
        self.timeout_ms = statement_timeout_ms
        self._con = duckdb.connect(":memory:")
        self._con.execute(f"SET TimeZone = '{timezone.replace(chr(39), '')}'")
        self._lock = threading.Lock()
        self.loaded: dict[str, int] = {}
        skip = {t.lower() for t in skip_tables}
        for path in sorted(self.folder.glob("*.json")):
            name = path.stem
            if name.lower() in skip:
                continue                      # secrets never enter the snapshot
            rows = read_export(path)
            if not rows:
                continue
            schema = infer_schema(rows)
            columns = {c: _DUCK[t] for c, t in schema}
            self._con.execute(
                f'CREATE TABLE "{name}" AS SELECT * FROM read_json(?, format = \'array\', columns = {columns!r})',
                [str(path)])
            self.loaded[name] = len(rows)
        log.info("snapshot loaded %d tables from %s", len(self.loaded), self.folder)

    def query(self, sql: str, params: dict | None = None, max_rows: int = 500,
              timeout_ms: int | None = None) -> Result:
        import duckdb
        timeout = _query_timeout_ms(timeout_ms, self.timeout_ms) / 1000
        started = time.perf_counter()
        with self._lock:
            timer = threading.Timer(timeout, self._con.interrupt)
            timer.start()
            try:
                cur = self._con.execute(sql, params or None)
                fetched = cur.fetchmany(max_rows + 1)
                columns = [d[0] for d in cur.description] if cur.description else []
            except duckdb.InterruptException as exc:
                raise QueryTimeout(f"The query took longer than {_human_ms(timeout * 1000)} "
                                   "and was stopped.") from exc
            except duckdb.Error as exc:
                raise SourceError(f"SQL error: {str(exc).splitlines()[0]}") from exc
            finally:
                timer.cancel()
        truncated = len(fetched) > max_rows
        rows = [[jsonable(v) for v in r] for r in fetched[:max_rows]]
        return Result(columns, rows, sql=sql, truncated=truncated,
                      elapsed_ms=int((time.perf_counter() - started) * 1000))

    def introspect(self) -> dict[str, list[tuple[str, str]]]:
        r = self.query("""SELECT table_name, column_name, data_type FROM information_schema.columns
                          WHERE table_schema = 'main' ORDER BY table_name, ordinal_position""", max_rows=100000)
        out: dict[str, list[tuple[str, str]]] = {}
        for table, column, typ in r.rows:
            out.setdefault(table, []).append((column, str(typ).lower()))
        return out

    def row_counts(self, tables: Iterable[str]) -> dict[str, int]:
        return {t: self.loaded.get(t, 0) for t in tables}

    def ping(self) -> int:
        return self.query("SELECT 1", max_rows=1).elapsed_ms

    def server_info(self) -> dict:
        return {"server_version": "DuckDB (offline snapshot)", "user": "", "read_only_session": True}

    def describe(self) -> dict:
        return {"kind": "snapshot", "database": self.folder.name, "schema": "main", "via_ssh": False, "read_only": True}

    def close(self) -> None:
        self._con.close()


def open_source(settings: Settings, skip_tables: Iterable[str] = ()) -> Source:
    if settings.source == "postgres":
        return PostgresSource(settings)
    return SnapshotSource(settings.snapshot_dir, skip_tables, settings.statement_timeout_ms, settings.timezone)
