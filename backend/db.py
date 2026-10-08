"""Read-only database access, a SQL safety guard, and the 'schema card'.

The schema card is the single most valuable trick in this project: because the
master/category levels are tiny, we paste the entire upper taxonomy into the
prompt. The model then never has to guess a value name.
"""

import re
import sqlite3
from pathlib import Path

import config

# Anything that is not a plain read gets rejected before it reaches SQLite.
BANNED = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|"
    r"pragma|vacuum|reindex|truncate)\b",
    re.IGNORECASE,
)


class UnsafeSQL(Exception):
    pass


def connect(db_path: Path = config.DB_PATH) -> sqlite3.Connection:
    """Open the database read-only. Even a malicious query cannot write.

    check_same_thread=False because FastAPI runs sync endpoints in a
    threadpool: the connection is created on one worker thread and the next
    request usually arrives on another, which SQLite refuses by default.
    Safe here because the connection is read-only and every caller that can
    run concurrently (the API) serialises access behind a per-session lock.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"{db_path} not found - run `python ingest.py` first")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def guard(sql: str) -> str:
    """Validate a model-generated query. Raises UnsafeSQL if anything is off."""
    cleaned = sql.strip()
    cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)   # opening fence + language tag
    cleaned = re.sub(r"\s*```$", "", cleaned)            # closing fence
    cleaned = re.sub(r"^sql\s*\n", "", cleaned, flags=re.IGNORECASE)  # bare "sql" prefix
    cleaned = cleaned.strip().rstrip(";").strip()

    if not cleaned:
        raise UnsafeSQL("empty query")
    if ";" in cleaned:
        raise UnsafeSQL("multiple statements are not allowed")
    if not re.match(r"^\s*(select|with)\b", cleaned, re.IGNORECASE):
        raise UnsafeSQL("only SELECT / WITH queries are allowed")
    if BANNED.search(cleaned):
        raise UnsafeSQL("query contains a write or schema keyword")

    if not re.search(r"\blimit\b", cleaned, re.IGNORECASE):
        cleaned += f" LIMIT {config.MAX_SQL_ROWS}"
    return cleaned


def run_sql(conn: sqlite3.Connection, sql: str) -> tuple[list[str], list[tuple]]:
    """Guard then execute. Returns (column_names, rows)."""
    safe = guard(sql)
    cur = conn.execute(safe)
    cols = [d[0] for d in cur.description]
    return cols, cur.fetchall()


_TABLE_REF = re.compile(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


def tables_used(sql: str) -> set:
    """Every table named after FROM or JOIN. Aliases and subqueries are ignored."""
    return {m.lower() for m in _TABLE_REF.findall(sql)}


def check_tables(sql: str, allowed) -> None:
    """Refuse a query that reaches outside this agent's domain."""
    allowed_lower = {t.lower() for t in allowed}
    used = tables_used(sql)
    stray = used - allowed_lower
    if stray:
        raise UnsafeSQL(
            f"table(s) {', '.join(sorted(stray))} are not available here. "
            f"You may query: {', '.join(sorted(allowed))}."
        )


def card_for(conn: sqlite3.Connection, tables) -> str:
    """Only the schema sections this agent is allowed to see."""
    wanted = {t.lower() for t in tables}
    chunks = []
    if config.TABLE.lower() in wanted and has_table(conn, config.TABLE):
        chunks.append(build_taxonomy_card(conn))
    if config.PRODUCTS_TABLE.lower() in wanted:
        chunks.append(build_products_card(conn))
    if config.APPS_TABLE.lower() in wanted:
        chunks.append(build_apps_card(conn))
    if config.EVENTS_TABLE.lower() in wanted:
        chunks.append(build_events_card(conn, include_link=
                                        config.LINK_TABLE.lower() in wanted))
    text = "\n".join(c for c in chunks if c and c.strip())
    return text.strip() or "(no tables loaded yet - run `python ingest.py <file>`)"


def looks_empty(rows: list) -> bool:
    """True when a query found nothing - including the aggregate case.

    COUNT(*) on a non-matching filter returns ONE row containing 0, not zero
    rows, so `if not rows` alone misses the most common empty result there is.
    """
    if not rows:
        return True
    if len(rows) > 1:
        return False
    return all(v in (0, None, "", "0") for v in rows[0])


def rows_to_text(cols: list[str], rows: list, limit: int = 60) -> str:
    """Compact pipe-separated rendering, cheap on tokens."""
    if not rows:
        return "(no rows)"
    lines = [" | ".join(cols)]
    for r in rows[:limit]:
        lines.append(" | ".join("" if v is None else str(v) for v in r))
    if len(rows) > limit:
        lines.append(f"... and {len(rows) - limit} more rows")
    return "\n".join(lines)


def build_taxonomy_card(conn: sqlite3.Connection, max_categories: int = 250) -> str:
    """A compact description of the data, injected into every prompt."""
    t = config.TABLE
    if not has_table(conn, t):
        return ""
    total = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    n_master, n_cat, n_sub = conn.execute(
        f"""SELECT COUNT(DISTINCT master_category), COUNT(DISTINCT category),
                   COUNT(DISTINCT sub_category) FROM {t}"""
    ).fetchone()

    parts = [
        "=" * 60,
        f"TABLE {t}(master_category TEXT, category TEXT, sub_category TEXT)",
        "The product category tree: master_category > category > sub_category.",
        f"Rows: {total}. Distinct master_category: {n_master}, "
        f"category: {n_cat}, sub_category: {n_sub}.",
        "",
        "Breakdown by master_category (master | #categories | #sub_categories):",
    ]
    for row in conn.execute(
        f"""SELECT master_category, COUNT(DISTINCT category) c, COUNT(DISTINCT sub_category) s
            FROM {t} GROUP BY master_category ORDER BY s DESC"""
    ):
        parts.append(f"  {row[0]} | {row[1]} | {row[2]}")

    if n_cat <= max_categories:
        parts.append("")
        parts.append("All category values, grouped by master_category:")
        current = None
        for row in conn.execute(
            f"SELECT DISTINCT master_category, category FROM {t} "
            "ORDER BY master_category, category"
        ):
            if row[0] != current:
                current = row[0]
                parts.append(f"  {current}:")
            parts.append(f"    - {row[1]}")
    else:
        parts.append("")
        parts.append(f"(category has {n_cat} values - too many to list; query the table.)")

    if n_sub <= config.MAX_SUBS_IN_CARD:
        subs = [r[0] for r in conn.execute(
            f"SELECT DISTINCT sub_category FROM {t} ORDER BY sub_category")]
        parts.append("")
        parts.append(f"All {len(subs)} sub_category values (comma separated):")
        parts.append(", ".join(subs))
    else:
        samples = [r[0] for r in conn.execute(
            f"SELECT DISTINCT sub_category FROM {t} ORDER BY RANDOM() LIMIT 20")]
        parts.append("")
        parts.append(f"sub_category has {n_sub} values - too many to list. "
                     "Search it with LIKE, never with =. Examples: "
                     + ", ".join(samples))
    return "\n".join(parts)


def build_schema_card(conn: sqlite3.Connection, max_categories: int = 250) -> str:
    """Everything, for the cross agent and the plain SQL path."""
    rest = (build_products_card(conn) + build_apps_card(conn)
            + build_events_card(conn))
    if not has_table(conn, config.TABLE):
        return (rest.lstrip("\n") if rest.strip()
                else "(no tables yet - run `python ingest.py <file>`)")
    return build_taxonomy_card(conn, max_categories) + rest


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def build_apps_card(conn: sqlite3.Connection) -> str:
    """Small table, so every row goes in the prompt."""
    t = config.APPS_TABLE
    if not has_table(conn, t):
        return ""
    rows = conn.execute(
        f"""SELECT id, app_name, app_brand, is_sub_app, is_active
            FROM {t} ORDER BY app_brand, is_sub_app, app_name""").fetchall()
    parts = [
        "",
        "=" * 60,
        f"TABLE {t}(id, app_name, slug, description, parent_id, "
        "parent_app_name, app_brand, is_sub_app, is_active, is_deleted)",
        "The shopping apps. parent_id makes some of them sub-apps of a bigger "
        "brand. search_event.app_assoc_id joins to app.id.",
        "",
        "id | app_name | app_brand | sub-app? | active?",
    ]
    for app_id, name, brand, sub, active in rows:
        parts.append(f"  {app_id} | {name} | {brand} | "
                     f"{'sub' if sub else 'top'} | {'yes' if active else 'no'}")
    return "\n".join(parts)


def build_products_card(conn: sqlite3.Connection) -> str:
    """22,409 products and 3,361 brands: describe, never list."""
    t = config.PRODUCTS_TABLE
    if not has_table(conn, t):
        return ""
    total, n_brand, n_sub = conn.execute(
        f"""SELECT COUNT(*), COUNT(DISTINCT brand_key), COUNT(DISTINCT sub_category)
            FROM {t}"""
    ).fetchone()
    no_brand = conn.execute(
        f"SELECT COUNT(*) FROM {t} WHERE brand = ''").fetchone()[0]

    parts = [
        "",
        "=" * 60,
        f"TABLE {t}(product_uid, product_master, brand, brand_key, "
        "sub_category, category, master_category)",
        f"One row per real product. {total} products, {n_brand} brands, "
        f"spanning {n_sub} sub_categories.",
        "",
        "  product_master - the product name, long free text "
        "('amul masti pouch curd'). Search it with LIKE.",
        "  brand          - e.g. 'Amul'. brand_key is the same value "
        "lowercased; join and compare on brand_key.",
        f"  {no_brand} products have no brand (placeholders were blanked).",
        "",
        "COUNT(*) here means PRODUCTS. The taxonomy table counts CATEGORIES. "
        "Do not answer one with the other.",
        "product_master and brand have far too many values to list - always "
        "use LIKE, never = .",
        "",
    ]
    tops = conn.execute(
        f"SELECT brand, COUNT(*) n FROM {t} WHERE brand != '' "
        "GROUP BY brand_key ORDER BY n DESC LIMIT 15").fetchall()
    parts.append("Largest brands (brand | products): "
                 + ", ".join(f"{b} | {n}" for b, n in tops))
    samples = [r[0] for r in conn.execute(
        f"SELECT product_master FROM {t} ORDER BY RANDOM() LIMIT 5")]
    parts.append("Example product_master values: "
                 + ", ".join(repr(x) for x in samples))
    return "\n".join(parts)


def build_events_card(conn: sqlite3.Connection, include_link: bool = True) -> str:
    """Describe the events table.

    The taxonomy card LISTS its values because there are 273 of them. Events
    cannot work that way: normalized_query and user_ref have as many values as
    there are rows. Those columns get described - what they mean, an example,
    how many distinct values - and the model is told to search them with LIKE.
    Listing low-cardinality columns and describing high-cardinality ones is the
    whole trick for putting a log table in a prompt.
    """
    t, lt = config.EVENTS_TABLE, config.LINK_TABLE
    if not has_table(conn, t):
        return ""

    total = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    n_j, n_s, n_q, n_u = conn.execute(
        f"""SELECT COUNT(DISTINCT journey_assoc_id), COUNT(DISTINCT session_assoc_id),
                   COUNT(DISTINCT normalized_query), COUNT(DISTINCT user_ref)
            FROM {t}"""
    ).fetchone()
    lo, hi = conn.execute(
        f"SELECT MIN(search_date), MAX(search_date) FROM {t}").fetchone()

    parts = [
        "",
        "=" * 60,
        f"TABLE {t} - one row per product search a user made.",
        f"Rows: {total}. Distinct journeys: {n_j}, sessions: {n_s}, "
        f"users: {n_u}, distinct normalized_query: {n_q}.",
        f"Dates range {lo} to {hi}.",
        "",
        "Meaning of the key columns:",
        "  journey_assoc_id - ONE product hunt. The same journey id across rows "
        "means the user was looking for the SAME product, even on different "
        "websites. This is what makes the data 'cross app'.",
        "  session_assoc_id - one visit/sitting.",
        "  step             - position within the journey: 1 is the first search.",
        "  hours_since_first- hours between step 1 of the journey and this search. "
        "0.0 on the first search.",
        "  app_assoc_id / app_name - the exact app searched on, e.g. "
        "'Amazon Now'.",
        "  app_brand        - the parent brand of that app, e.g. 'Amazon'. "
        "Amazon Now, Amazon Fresh and Amazon Bazaar all have app_brand "
        "'Amazon'. Group by app_brand when the user names a company; group by "
        "app_name when they want the exact surface.",
        "  IMPORTANT: a journey moving between two app_name values of the SAME "
        "app_brand did NOT cross websites. For real cross-app behaviour count "
        "DISTINCT app_brand, not DISTINCT app_name.",
        "  normalized_query - the CLEANED search text. Always use this one.",
        "  query            - the raw captured text. It is often damaged "
        "(leading capitals missing). Never match on it; show it only if asked.",
        "  search_utc       - sortable timestamp. Use this for ordering and "
        "date maths, not search_time.",
        "  search_date, search_hour, search_dow - ready-made local date parts.",
        "",
        "Columns with too many values to list - search them with LIKE, never = :",
        f"  normalized_query ({n_q} distinct), query, page_id, user_ref "
        f"({n_u} distinct), source_accessibility_id",
        "",
    ]

    parts.append("All app_name values (app_name | id | brand | searches):")
    for name, app_id, brand, n in conn.execute(
        f"SELECT app_name, app_assoc_id, app_brand, COUNT(*) n FROM {t} "
        "GROUP BY 1, 2, 3 ORDER BY n DESC LIMIT 40"):
        parts.append(f"  {name} | {app_id} | {brand} | {n}")

    samples = [r[0] for r in conn.execute(
        f"SELECT DISTINCT normalized_query FROM {t} "
        "WHERE normalized_query != '' ORDER BY RANDOM() LIMIT 8")]
    parts.append("")
    parts.append("Example normalized_query values (these are product searches, "
                 "not category names): " + ", ".join(repr(x) for x in samples))

    if include_link and has_table(conn, lt):
        n_link = conn.execute(f"SELECT COUNT(*) FROM {lt}").fetchone()[0]
        parts += [
            "",
            f"TABLE {lt}(normalized_query, sub_category, category, "
            "master_category, match_score)",
            f"Links {n_link} distinct search queries to the taxonomy by fuzzy "
            "matching. JOIN through it to answer questions that span both "
            "datasets, e.g. which categories people search for:",
            f"  SELECT l.category, COUNT(*) n FROM {t} e",
            f"  JOIN {lt} l ON e.normalized_query = l.normalized_query",
            "  GROUP BY 1 ORDER BY n DESC",
            "Not every query links - brand-only searches match no category.",
        ]
    return "\n".join(parts)


def all_sub_categories(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT DISTINCT sub_category, category, master_category FROM {config.TABLE}"
    ).fetchall()
