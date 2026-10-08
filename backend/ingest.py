"""Load the taxonomy JSON into SQLite.

The sample file is a *fragment*: no outer [ ], and a trailing comma at the end.
load_rows() repairs that automatically, and also accepts a proper JSON array,
a wrapped object, or JSONL.

Duplicate (master, category, sub_category) triples are collapsed by default:
with only these three columns stored, a repeated row carries no information,
and keeping 22,000 copies of 273 facts only makes COUNT(*) misleading. Pass
--keep-duplicates if you need the original row count.

Run:  python ingest.py [path/to/file.json] [--keep-duplicates]
"""

import json
import sqlite3
import sys
from pathlib import Path

import apps_master
import config
import products
import events

REQUIRED = ("master_category", "category", "sub_category")
EVENT_REQUIRED = ("query", "normalized_query", "search_time", "journey_assoc_id")


def load_json(path: Path) -> list[dict]:
    """Parse the file into raw records, repairing the common broken formats."""
    text = Path(path).read_text(encoding="utf-8").strip()

    attempts = [
        text,                                   # already valid
        f"[{text.rstrip(',')}]",                # bracket-less fragment (your file)
        f"[{text.rstrip(',').lstrip('[').rstrip(']')}]",  # double-bracket safety
    ]
    data = None
    for attempt in attempts:
        try:
            data = json.loads(attempt)
            break
        except json.JSONDecodeError:
            continue

    if data is None:  # last resort: JSONL, one object per line
        try:
            data = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise ValueError(f"Could not parse {path} as JSON, JSON fragment or JSONL") from exc

    if isinstance(data, dict):  # {"categories": [...]} style wrapper
        for value in data.values():
            if isinstance(value, list):
                data = value
                break

    if not isinstance(data, list):
        raise ValueError("Parsed JSON is not a list of records")
    return [d for d in data if isinstance(d, dict)]


def detect_kind(records: list[dict]) -> str:
    """Decide which dataset this file holds, so one command handles both."""
    if not records:
        raise ValueError("file has no records")
    keys = set(records[0])
    if set(apps_master.REQUIRED) <= keys:
        return "apps"
    # Checked BEFORE taxonomy: the product file also carries the three
    # taxonomy columns, and treating it as a taxonomy silently discards
    # 22,000 products.
    if set(products.REQUIRED) <= keys:
        return "products"
    if set(REQUIRED) <= keys:
        return "taxonomy"
    if set(EVENT_REQUIRED) <= keys:
        return "events"
    raise ValueError(
        "Unrecognised file. Expected one of:\n"
        f"  taxonomy: {', '.join(REQUIRED)}\n"
        f"  events:   {', '.join(EVENT_REQUIRED)}\n"
        f"  apps:     {', '.join(apps_master.REQUIRED)}\n"
        f"  products: {', '.join(products.REQUIRED)}\n"
        f"This file has: {', '.join(sorted(keys))}"
    )


def load_rows(path: Path) -> list[dict]:
    """Taxonomy rows only - kept for the existing taxonomy pipeline."""
    data = load_json(path)
    rows, skipped, extra = [], 0, set()
    for item in data:
        if not isinstance(item, dict) or not all(k in item for k in REQUIRED):
            skipped += 1
            continue
        extra |= set(item) - set(REQUIRED)
        rows.append({k: str(item[k]).strip() for k in REQUIRED})

    if skipped:
        print(f"  skipped {skipped} malformed record(s)")
    if extra:
        print(f"  NOTE: ignoring extra field(s) in this file: {', '.join(sorted(extra))}")
        print("        run `python inspect_data.py` to see whether you want them")
    return rows


def dedupe(rows: list[dict]) -> list[dict]:
    """Collapse repeated (master, category, sub_category) triples, order kept."""
    seen, out = set(), []
    for row in rows:
        key = (row["master_category"], row["category"], row["sub_category"])
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def build_db(rows: list[dict], db_path: Path = config.DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Drop the FTS index too. Dropping only the base table leaves the shadow
    # tables behind, and the next ingest fails with "table already exists".
    cur.execute(f"DROP TABLE IF EXISTS {config.TABLE}_fts")
    cur.execute(f"DROP TABLE IF EXISTS {config.TABLE}")
    cur.execute(
        f"""CREATE TABLE {config.TABLE} (
               id              INTEGER PRIMARY KEY,
               master_category TEXT NOT NULL,
               category        TEXT NOT NULL,
               sub_category    TEXT NOT NULL
           )"""
    )
    cur.executemany(
        f"INSERT INTO {config.TABLE} (master_category, category, sub_category) "
        "VALUES (:master_category, :category, :sub_category)",
        rows,
    )
    for col in REQUIRED:
        cur.execute(f"CREATE INDEX idx_{col} ON {config.TABLE}({col})")

    # Full-text index so SQL can do fast substring matching too.
    cur.execute(
        f"CREATE VIRTUAL TABLE {config.TABLE}_fts USING fts5("
        "master_category, category, sub_category, content='taxonomy', content_rowid='id')"
    )
    cur.execute(
        f"INSERT INTO {config.TABLE}_fts(rowid, master_category, category, sub_category) "
        f"SELECT id, master_category, category, sub_category FROM {config.TABLE}"
    )
    conn.commit()
    return conn


def build_products_db(rows: list[dict], db_path: Path = config.DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(db_path))
    cur = conn.cursor()
    t = config.PRODUCTS_TABLE
    cur.execute(f"DROP TABLE IF EXISTS {t}")
    cur.execute(f"CREATE TABLE {t} ({', '.join(f'{n} {k}' for n, k in products.COLUMNS)})")
    names = [n for n, _ in products.COLUMNS]
    cur.executemany(
        f"INSERT INTO {t} ({', '.join(names)}) "
        f"VALUES ({', '.join(':' + n for n in names)})", rows)
    for col in ("brand_key", "sub_category", "category", "master_category"):
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_prod_{col} ON {t}({col})")
    # Product names are long free text; FTS makes "which products mention
    # curd" fast instead of a full scan.
    cur.execute(f"DROP TABLE IF EXISTS {t}_fts")
    cur.execute(f"CREATE VIRTUAL TABLE {t}_fts USING fts5(product_master, brand, "
                f"content='{t}', content_rowid='rowid')")
    cur.execute(f"INSERT INTO {t}_fts(rowid, product_master, brand) "
                f"SELECT rowid, product_master, brand FROM {t}")
    conn.commit()
    return conn


def build_apps_db(rows: list[dict], db_path: Path = config.DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(db_path))
    cur = conn.cursor()
    t = config.APPS_TABLE
    cur.execute(f"DROP TABLE IF EXISTS {t}")
    cols = ", ".join(f"{n} {k}" for n, k in apps_master.COLUMNS)
    cur.execute(f"CREATE TABLE {t} ({cols})")
    names = [n for n, _ in apps_master.COLUMNS]
    cur.executemany(
        f"INSERT INTO {t} ({', '.join(names)}) "
        f"VALUES ({', '.join(':' + n for n in names)})", rows)
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_app_brand ON {t}(app_brand)")
    conn.commit()
    return conn


def backfill_event_apps(conn: sqlite3.Connection) -> int:
    """Re-stamp app_name / app_brand on events from the app table.

    Lets the three files be ingested in any order: whichever lands last fixes
    the names.
    """
    ev, ap = config.EVENTS_TABLE, config.APPS_TABLE
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if ev not in have or ap not in have:
        return 0
    conn.execute(
        f"""UPDATE {ev} SET
              app_name  = COALESCE((SELECT app_name FROM {ap}
                                    WHERE {ap}.id = {ev}.app_assoc_id), app_name),
              app_brand = COALESCE((SELECT app_brand FROM {ap}
                                    WHERE {ap}.id = {ev}.app_assoc_id), app_brand)""")
    conn.commit()
    named = conn.execute(
        f"SELECT COUNT(*) FROM {ev} WHERE app_name NOT LIKE 'App %'").fetchone()[0]
    total = conn.execute(f"SELECT COUNT(*) FROM {ev}").fetchone()[0]
    print(f"  named the app on {named} of {total} events")
    return named


def build_events_db(rows: list[dict], db_path: Path = config.DB_PATH) -> sqlite3.Connection:
    """Create the events table. Leaves the taxonomy table untouched."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    t = config.EVENTS_TABLE

    cur.execute(f"DROP TABLE IF EXISTS {t}")
    cols = ", ".join(f"{name} {kind}" for name, kind in events.COLUMNS)
    cur.execute(f"CREATE TABLE {t} ({cols})")
    names = [name for name, _ in events.COLUMNS]
    cur.executemany(
        f"INSERT INTO {t} ({', '.join(names)}) "
        f"VALUES ({', '.join(':' + n for n in names)})",
        rows,
    )
    # Index what gets filtered and grouped on. Events grow; the taxonomy does not.
    for col in ("normalized_query", "app_assoc_id", "app_name", "app_brand",
                "journey_assoc_id", "session_assoc_id", "search_date",
                "user_ref", "search_utc"):
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_ev_{col} ON {t}({col})")
    conn.commit()
    return conn


def build_link_table(conn: sqlite3.Connection) -> int:
    """Map every distinct normalized_query to its best sub_category.

    This is the bridge between the two datasets. Doing it once, into a real
    table, means questions like "how many searches were about Vitamins" are a
    plain SQL JOIN instead of something the model has to reason about.
    """
    from search import TaxonomySearch

    t, lt = config.TABLE, config.LINK_TABLE
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if config.EVENTS_TABLE not in have:
        print("  (no search events yet - the bridge is built once they arrive)")
        return 0
    if t not in have:
        print("  (no taxonomy table yet - run ingest on the taxonomy file to link)")
        return 0

    # Build plain dicts: this connection has no row_factory set.
    searcher = TaxonomySearch([
        {"sub_category": sub, "category": cat, "master_category": master}
        for sub, cat, master in conn.execute(
            f"SELECT DISTINCT sub_category, category, master_category FROM {t}")
    ])

    queries = [r[0] for r in conn.execute(
        f"SELECT DISTINCT normalized_query FROM {config.EVENTS_TABLE} "
        "WHERE normalized_query IS NOT NULL AND normalized_query != ''")]

    conn.execute(f"DROP TABLE IF EXISTS {lt}")
    conn.execute(
        f"""CREATE TABLE {lt} (
               normalized_query TEXT PRIMARY KEY, sub_category TEXT,
               category TEXT, master_category TEXT, match_score REAL)"""
    )

    # Brands beat category labels. A shopper types "nivea roll deo men";
    # Nivea is a brand, and no amount of fuzzy matching against the label
    # "Skincare" will ever find it. Brands are matched first, exactly, on a
    # word boundary; only unmatched queries fall through to fuzzy category
    # matching.
    brands = products.brand_index(conn)
    by_word = {}
    for brand, key, sub, cat, master in brands:
        if len(key) >= 3:
            by_word.setdefault(key, (brand, sub, cat, master))

    linked, via_brand = [], 0
    for query in queries:
        words = query.lower().split()
        hit_brand = None
        for size in (3, 2, 1):            # prefer the longest brand phrase
            for start in range(len(words) - size + 1):
                phrase = " ".join(words[start:start + size])
                if phrase in by_word:
                    hit_brand = by_word[phrase]
                    break
            if hit_brand:
                break
        if hit_brand:
            brand, sub, cat, master = hit_brand
            linked.append((query, sub, cat, master, 100.0))
            via_brand += 1
            continue

        hit = searcher.match_query_to_taxonomy(query)
        if hit:
            linked.append((query, hit["sub_category"], hit["category"],
                           hit["master_category"], hit["score"]))
    conn.executemany(f"INSERT INTO {lt} VALUES (?, ?, ?, ?, ?)", linked)
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_link_sub ON {lt}(sub_category)")
    conn.commit()
    print(f"  linked {len(linked)} of {len(queries)} distinct queries to a "
          f"sub_category ({via_brand} via brand name, "
          f"{len(linked) - via_brand} via category label)")
    return len(linked)


def main(path=None, keep_duplicates=False):
    path = Path(path or config.JSON_PATH)
    print(f"Reading {path}")
    records = load_json(path)
    kind = detect_kind(records)
    print(f"  detected: {kind}")

    if kind == "products":
        rows = products.normalise(records)
        print(f"  parsed {len(rows)} products")
        conn = build_products_db(rows)
        n_b, n_s = conn.execute(
            f"""SELECT COUNT(DISTINCT brand_key), COUNT(DISTINCT sub_category)
                FROM {config.PRODUCTS_TABLE}""").fetchone()
        print(f"  {n_b} brands across {n_s} sub-categories")
        for brand, n in conn.execute(
            f"SELECT brand, COUNT(*) n FROM {config.PRODUCTS_TABLE} "
            "GROUP BY brand_key ORDER BY n DESC LIMIT 5"):
            print(f"    {brand}: {n} products")
        build_link_table(conn)
        print(f"Wrote {config.DB_PATH}")
        conn.close()
        return

    if kind == "apps":
        rows = apps_master.resolve(records)
        print(f"  parsed {len(rows)} apps")
        conn = build_apps_db(rows)
        subs = [r for r in rows if r["is_sub_app"]]
        print(f"  {len(rows) - len(subs)} top-level apps, {len(subs)} sub-apps")
        for r in subs:
            print(f"    {r['app_name']} -> part of {r['app_brand']}")
        backfill_event_apps(conn)
        print(f"Wrote {config.DB_PATH}")
        conn.close()
        return

    if kind == "events":
        # Prefer the app master; fall back to apps.json only if it is absent.
        import sqlite3 as _sq
        probe = _sq.connect(config.DB_PATH) if Path(config.DB_PATH).exists() else None
        app_names = apps_master.lookup_from_db(probe) if probe else {}
        if probe:
            probe.close()
        if not app_names:
            app_names = events.load_app_names()
            if app_names:
                print(f"  using {config.APPS_MAP.name} (no app master ingested yet)")
        else:
            print(f"  using the {config.APPS_TABLE} table for app names")
        rows = events.normalise(records, app_names)
        print(f"  parsed {len(rows)} events")
        if not app_names:
            print("  NOTE: no app names yet - ingest app_master.json and the "
                  "names will be filled in automatically.")
        unknown = sorted({r["app_assoc_id"] for r in rows
                          if (r["app_name"] or "").startswith("App ")})
        if unknown and app_names:
            print(f"  app ids missing from the app master: {unknown}")

        conn = build_events_db(rows)
        n_j, n_s, n_a = conn.execute(
            f"""SELECT COUNT(DISTINCT journey_assoc_id),
                       COUNT(DISTINCT session_assoc_id),
                       COUNT(DISTINCT app_assoc_id) FROM {config.EVENTS_TABLE}"""
        ).fetchone()
        print(f"  {n_j} journeys, {n_s} sessions, {n_a} apps")
        build_link_table(conn)
        print(f"Wrote {config.DB_PATH}")
        conn.close()
        return

    rows = load_rows(path)
    print(f"  parsed {len(rows)} rows")

    if not keep_duplicates:
        unique = dedupe(rows)
        removed = len(rows) - len(unique)
        if removed:
            pct = removed / len(rows) * 100
            print(f"  collapsed {removed} duplicate rows ({pct:.1f}%) -> "
                  f"{len(unique)} unique triples")
            print("      (pass --keep-duplicates to keep every row)")
        rows = unique

    conn = build_db(rows)
    counts = conn.execute(
        f"""SELECT COUNT(DISTINCT master_category), COUNT(DISTINCT category),
                   COUNT(DISTINCT sub_category) FROM {config.TABLE}"""
    ).fetchone()
    print(f"  {counts[0]} master categories, {counts[1]} categories, {counts[2]} sub-categories")
    if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (config.EVENTS_TABLE,)).fetchone():
        build_link_table(conn)      # taxonomy changed, so relink the queries
    print(f"Wrote {config.DB_PATH}")
    conn.close()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(args[0] if args else None, keep_duplicates="--keep-duplicates" in sys.argv)
