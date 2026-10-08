"""The tools the agent is allowed to use.

A tool is a normal Python function plus a description written FOR THE MODEL.
The description is the part that matters: the model picks a tool using only
these words, so vague wording produces wrong choices no matter how good the
model is.

Keep this list short. Three or four tools works on a 7B local model; ten does not.
"""

import config
import db

MAX_OBS_CHARS = 1500  # observations are re-sent every turn - keep them small


class ToolError(Exception):
    """Raised when a tool cannot run. The message goes back to the model so it
    can correct itself, instead of the whole loop crashing."""


# --------------------------------------------------------------------------
# tool implementations. Each takes (ctx, args) and returns a string.
# --------------------------------------------------------------------------

def _run_sql(ctx, args) -> str:
    sql = (args.get("sql") or "").strip()
    if not sql:
        raise ToolError("You must provide the 'sql' field when tool is run_sql.")
    allowed = ctx.get("tables")
    if allowed:
        try:
            db.check_tables(sql, allowed)
        except db.UnsafeSQL as exc:
            raise ToolError(str(exc)) from exc
    try:
        cols, rows = db.run_sql(ctx["conn"], sql)
    except db.UnsafeSQL as exc:
        raise ToolError(f"Query rejected: {exc}. Write a plain SELECT.") from exc
    except Exception as exc:
        raise ToolError(f"SQL error: {exc}. Check the column names and try again.") from exc
    return db.rows_to_text(cols, rows, limit=40)


def _search_taxonomy(ctx, args) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        raise ToolError("You must provide the 'query' field when tool is search_taxonomy.")
    hits = ctx["searcher"].search(query)
    if not hits:
        raise ToolError(
            f"No sub_category resembles '{query}'. Try fewer words, or use run_sql "
            "with LIKE to search for a single keyword."
        )
    return ctx["searcher"].to_text(hits)


def _show_journey(ctx, args) -> str:
    raw = str(args.get("journey_id") or "").strip()
    if not raw.lstrip("-").isdigit():
        raise ToolError("journey_id must be a number, e.g. 2.")
    rows = ctx["conn"].execute(
        f"""SELECT step, app_name, app_brand, normalized_query,
                   hours_since_first, search_time, session_assoc_id
            FROM {config.EVENTS_TABLE} WHERE journey_assoc_id = ?
            ORDER BY step, search_utc LIMIT 60""",
        (int(raw),),
    ).fetchall()
    if not rows:
        raise ToolError(f"No journey with id {raw}. Find real ids with run_sql.")
    lines = ["step | app | brand | query | hours_since_first | time | session"]
    for row in rows:
        lines.append(" | ".join("" if value is None else str(value) for value in row))
    return "\n".join(lines)


def _top_queries(ctx, args) -> str:
    raw = str(args.get("limit") or "15").strip()
    limit = int(raw) if raw.isdigit() and 0 < int(raw) <= 100 else 15
    rows = ctx["conn"].execute(
        f"""SELECT normalized_query, COUNT(*) searches,
                   COUNT(DISTINCT app_name) apps,
                   COUNT(DISTINCT journey_assoc_id) journeys
            FROM {config.EVENTS_TABLE}
            WHERE normalized_query IS NOT NULL AND normalized_query != ''
            GROUP BY 1 ORDER BY searches DESC, normalized_query LIMIT ?""",
        (limit,),
    ).fetchall()
    if not rows:
        raise ToolError("No search events loaded.")
    lines = ["normalized_query | searches | apps | journeys"]
    for row in rows:
        lines.append(" | ".join(str(value) for value in row))
    return "\n".join(lines)


def _list_values(ctx, args) -> str:
    column = (args.get("column") or "").strip()
    taxonomy_columns = ("master_category", "category", "sub_category")
    event_columns = ("app_name", "app_brand", "search_date", "search_dow", "step")
    if column in taxonomy_columns:
        table = config.TABLE
    elif column in event_columns:
        table = config.EVENTS_TABLE
    else:
        raise ToolError(
            "column must be one of: " + ", ".join(taxonomy_columns + event_columns)
            + ". Columns like normalized_query have too many values - use run_sql."
        )
    rows = ctx["conn"].execute(
        f"SELECT DISTINCT {column} FROM {table} "
        f"WHERE {column} IS NOT NULL ORDER BY {column} LIMIT 300"
    ).fetchall()
    values = [r[0] for r in rows]
    return f"{len(values)} distinct {column} values:\n" + "\n".join(values)


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

TOOLS = {
    "run_sql": {
        "fn": _run_sql,
        "args": ["sql"],
        "description": (
            "Run one read-only SQLite SELECT on taxonomy(master_category, "
            "category, sub_category). Use this for counting, listing, grouping, "
            "sorting and exact matching. Field: sql."
        ),
    },
    "search_taxonomy": {
        "fn": _search_taxonomy,
        "args": ["query"],
        "description": (
            "Find the closest matching sub_category when the user describes a "
            "product in their own words instead of the exact label, e.g. "
            "'infant vest'. Field: query."
        ),
    },
    "show_journey": {
        "fn": _show_journey,
        "args": ["journey_id"],
        "description": (
            "Show every search in one journey in order. Use when asked what a "
            "user did or for an example of cross-app behaviour. Field: journey_id."
        ),
    },
    "top_queries": {
        "fn": _top_queries,
        "args": ["limit"],
        "description": (
            "Show the most frequently searched queries with app and journey "
            "counts. Field: limit (a number, default 15)."
        ),
    },
    "list_values": {
        "fn": _list_values,
        "args": ["column"],
        "description": (
            "List every distinct value of one column, when you need to see what "
            "actually exists before writing a query. Field: column."
        ),
    },
    "final_answer": {
        "fn": None,  # terminal: handled by the loop
        "args": ["answer"],
        "description": (
            "Stop and give the user the answer. Use this as soon as you have "
            "enough information. Field: answer."
        ),
    },
}


def describe_tools(only=None) -> str:
    names = only or list(TOOLS)
    return "\n".join(
        f"- {name}: {TOOLS[name]['description']}"
        for name in names
        if name in TOOLS
    )


def call(name: str, ctx: dict, args: dict) -> str:
    available = ctx.get("tools") or list(TOOLS)
    spec = TOOLS.get(name)
    if spec is None or name not in available:
        raise ToolError(
            f"Unknown tool '{name}'. Choose one of: {', '.join(available)}."
        )
    result = spec["fn"](ctx, args)
    if len(result) > MAX_OBS_CHARS:
        result = result[:MAX_OBS_CHARS] + "\n... (truncated)"
    return result
