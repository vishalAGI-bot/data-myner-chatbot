"""SQL guard: every statement is parsed (sqlglot, PostgreSQL dialect) and checked
structurally before it reaches the database - including SQL written by the model.

Rules
* exactly one read-only query (SELECT / WITH / UNION); no DDL, DML, SET, COPY, SELECT INTO, locks
* only documented, available, non-blocked tables in the configured schema
* no ``blocked`` column anywhere (passwords, phone, email, address, device ids, hashes ...)
* ``heavy`` and ``aggregate_only`` columns may be used in filters / expressions but not returned raw
* no ``SELECT *`` (it would leak blocked columns) and no admin functions (pg_*, dblink, lo_*, set_config ...)
* a row LIMIT is always present and capped

Then the statement is re-generated for the target engine (PostgreSQL or DuckDB).
The database session is read-only as well; this is the second, independent layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from .catalog import Catalog

_FORBIDDEN_NODES = tuple(t for t in (
    getattr(exp, n, None) for n in (
        "Insert", "Update", "Delete", "Create", "Drop", "Alter", "AlterTable", "Command", "Merge", "Copy",
        "Into", "Set", "Transaction", "Commit", "Rollback", "Lock", "Grant", "Revoke", "LoadData", "Pragma",
        "Use", "Describe", "Analyze", "Refresh", "Cache", "Uncache"))
    if isinstance(t, type))
_FORBIDDEN_FUNCS = re.compile(
    r"^(pg_|dblink|lo_|set_config|current_setting|query_to_xml|table_to_xml|cursor_to_xml|"
    r"read_csv|read_json|read_parquet|read_text|read_blob|glob|getenv|system|shell|copy|"
    r"txid_|inet_server|inet_client|has_.*privilege|version$)", re.I)


class GuardError(ValueError):
    """The SQL is not allowed. The message explains how to fix it (it is fed back to the model)."""


@dataclass
class Checked:
    sql: str                      # final SQL for the target engine
    tables: list[str] = field(default_factory=list)
    limited: bool = False


def _clean(sql: str) -> str:
    text = (sql or "").strip()
    fence = re.search(r"```(?:sql|postgresql|postgres)?\s*(.*?)```", text, re.S | re.I)
    if fence:
        text = fence.group(1)
    return text.strip().rstrip(";").strip()


def check(sql: str, catalog: Catalog, *, dialect: str = "postgres", max_rows: int = 500,
          schema: str = "public") -> Checked:
    text = _clean(sql)
    if not text:
        raise GuardError("The query is empty.")
    try:
        statements = [s for s in sqlglot.parse(text, read="postgres") if s is not None]
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError) as exc:
        raise GuardError(f"The SQL could not be parsed: {str(exc).splitlines()[0]}") from exc
    if len(statements) != 1:
        raise GuardError("Write exactly one SELECT statement.")
    root = statements[0]
    if not isinstance(root, exp.Query):
        raise GuardError("Only read-only SELECT queries are allowed.")
    for node in root.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise GuardError(f"{type(node).__name__.upper()} is not allowed; only SELECT queries can run.")

    # ---- tables ------------------------------------------------------------
    cte_names = {c.alias_or_name.lower() for c in root.find_all(exp.CTE)}
    allowed = catalog.allowed_tables
    alias_to_table: dict[str, str | None] = {}
    used: list[str] = []
    for table in root.find_all(exp.Table):
        name = table.name.lower()
        if not name:
            continue
        if table.args.get("db") is not None and table.text("db").lower() not in (schema.lower(), "main"):
            raise GuardError(f"Schema '{table.text('db')}' is not allowed. Use tables from {schema} only.")
        if name in cte_names and not table.args.get("db"):
            alias_to_table[table.alias_or_name.lower()] = None
            continue
        if name in catalog.blocked_tables:
            raise GuardError(f"Table '{name}' is not available to the chatbot (security policy).")
        if name not in allowed:
            raise GuardError(f"Unknown table '{name}'. Available tables: {', '.join(sorted(allowed))}.")
        alias_to_table[table.alias_or_name.lower()] = name
        if name not in used:
            used.append(name)
    for sub in root.find_all(exp.Subquery):
        if sub.alias:
            alias_to_table.setdefault(sub.alias.lower(), None)
    for cte in cte_names:
        alias_to_table.setdefault(cte, None)

    # ---- functions ---------------------------------------------------------
    for func in root.find_all(exp.Func):
        name = func.name if isinstance(func, exp.Anonymous) else func.sql_name()
        if name and _FORBIDDEN_FUNCS.match(str(name)):
            raise GuardError(f"Function {name}() is not allowed.")

    # ---- columns -----------------------------------------------------------
    def column_policy(col: exp.Column) -> str:
        qualifier = col.table.lower() if col.table else ""
        name = col.name.lower()
        if qualifier and alias_to_table.get(qualifier):
            return catalog.policy(alias_to_table[qualifier], name)
        candidates = [t for t in used] if not qualifier or qualifier in alias_to_table else used
        # Unresolved (CTE / subquery / unqualified): strictest policy among involved tables.
        order = {"blocked": 3, "aggregate_only": 2, "heavy": 1, "ok": 0}
        worst = "ok"
        for t in candidates:
            doc = catalog.tables.get(t)
            if doc and name in doc.columns and order[doc.columns[name].policy] > order[worst]:
                worst = doc.columns[name].policy
        return worst

    for col in root.find_all(exp.Column):
        if isinstance(col.this, exp.Star):
            raise GuardError("SELECT * is not allowed; name the columns you need.")
        if column_policy(col) == "blocked":
            raise GuardError(f"Column '{col.name}' contains personal or secret data and cannot be used.")

    selects = [root] if isinstance(root, exp.Select) else list(root.find_all(exp.Select))
    outer = [s for s in selects if s.parent_select is None] or selects[:1]
    for select in outer:
        for projection in select.expressions:
            inner = projection.this if isinstance(projection, exp.Alias) else projection
            if isinstance(inner, exp.Star):
                raise GuardError("SELECT * is not allowed; name the columns you need.")
            if isinstance(inner, exp.Column):
                policy = column_policy(inner)
                if policy == "heavy":
                    raise GuardError(f"Column '{inner.name}' is large raw data; filter on it but do not return it.")
                if policy == "aggregate_only":
                    raise GuardError(f"Column '{inner.name}' is personal data; use it only inside an aggregate or "
                                     "an expression (e.g. an age group), not as a raw value.")

    # ---- limit -------------------------------------------------------------
    limited = False
    cap = max_rows + 1
    limit = root.args.get("limit")
    if limit is None:
        root = root.limit(cap)
        limited = True
    else:
        value = limit.expression if isinstance(limit, exp.Limit) else None
        if isinstance(value, exp.Literal) and value.is_int and int(value.name) > cap:
            root = root.limit(cap)
            limited = True

    try:
        final = root.sql(dialect="duckdb" if dialect == "duckdb" else "postgres", pretty=True)
    except sqlglot.errors.UnsupportedError as exc:
        raise GuardError(f"This SQL cannot run on the current engine: {exc}") from exc
    return Checked(final, used, limited)
