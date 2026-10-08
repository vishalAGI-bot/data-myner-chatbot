"""Show what is actually in your taxonomy file and database.

Run:  python inspect_data.py [path/to/file.json]

Answers the questions you cannot see from row counts alone: how much of the
file is duplicated, which fields are being discarded, which sub_categories are
ambiguous, and which label pairs will confuse fuzzy search.
"""

import sys
from collections import Counter, defaultdict
from pathlib import Path

import config
import db
import ingest

REQUIRED = set(ingest.REQUIRED)


def section(title):
    print(f"\n{title}\n" + "-" * len(title))


def inspect_events(conn):
    t, lt = config.EVENTS_TABLE, config.LINK_TABLE
    if not db.has_table(conn, t):
        return

    section(f"EVENTS  {t}")
    total, n_j, n_s, n_u, n_q = conn.execute(
        f"""SELECT COUNT(*), COUNT(DISTINCT journey_assoc_id),
                   COUNT(DISTINCT session_assoc_id), COUNT(DISTINCT user_ref),
                   COUNT(DISTINCT normalized_query) FROM {t}"""
    ).fetchone()
    print(f"{total} searches | {n_j} journeys | {n_s} sessions | "
          f"{n_u} users | {n_q} distinct queries")

    print("\nsearches per app:")
    for name, app_id, n in conn.execute(
        f"SELECT app_name, app_assoc_id, COUNT(*) n FROM {t} "
        "GROUP BY 1, 2 ORDER BY n DESC LIMIT 25"):
        flag = "   <- no name in apps.json" if str(name).startswith("App ") else ""
        print(f"  {str(name):24} (id {app_id})  {n}{flag}")

    print("\ncross-app journeys (the point of this dataset):")
    cross_app = conn.execute(
        f"""SELECT COUNT(*) FROM (SELECT journey_assoc_id FROM {t}
            GROUP BY 1 HAVING COUNT(DISTINCT app_assoc_id) > 1)""").fetchone()[0]
    cross_brand = conn.execute(
        f"""SELECT COUNT(*) FROM (SELECT journey_assoc_id FROM {t}
            GROUP BY 1 HAVING COUNT(DISTINCT app_brand) > 1)""").fetchone()[0]
    if n_j:
        print(f"  {cross_app} of {n_j} journeys used more than one APP "
              f"({cross_app / n_j * 100:.1f}%)")
        print(f"  {cross_brand} of {n_j} journeys used more than one BRAND "
              f"({cross_brand / n_j * 100:.1f}%)")
        if cross_app > cross_brand:
            print(f"  -> {cross_app - cross_brand} journey(s) only moved between "
                  "sub-apps of the same brand (e.g. Amazon -> Amazon Now).")
            print("     Count DISTINCT app_brand for genuine cross-site behaviour.")
    for jid, apps, brands, names, bnames in conn.execute(
        f"""SELECT journey_assoc_id, COUNT(DISTINCT app_name) a,
                   COUNT(DISTINCT app_brand) b, GROUP_CONCAT(DISTINCT app_name),
                   GROUP_CONCAT(DISTINCT app_brand) FROM {t}
            GROUP BY 1 HAVING a > 1 ORDER BY b DESC, a DESC LIMIT 6"""):
        tag = "cross-brand" if brands > 1 else "same brand"
        print(f"    journey {jid}: {names}   [{tag}: {bnames}]")

    print("\ndata quality:")
    damaged = conn.execute(
        f"SELECT COUNT(*) FROM {t} WHERE query LIKE '%  %'").fetchone()[0]
    print(f"  {damaged} rows where `query` has a double space "
          "(dropped capitals from the accessibility capture)")
    differ = conn.execute(
        f"SELECT COUNT(*) FROM {t} WHERE query != normalized_query").fetchone()[0]
    print(f"  {differ} rows where query != normalized_query "
          "-> always match on normalized_query")
    bad_time = conn.execute(
        f"SELECT COUNT(*) FROM {t} WHERE search_utc IS NULL").fetchone()[0]
    print(f"  {bad_time} rows whose search_time could not be parsed")
    missing = conn.execute(
        f"SELECT COUNT(*) FROM {t} WHERE capture_app IS NULL").fetchone()[0]
    print(f"  {missing} rows whose page_id did not split into 4 parts")

    # page_id embeds a brand name; app_assoc_id resolves to one via the app
    # master. They should agree - if they stop agreeing, one source is stale.
    agree, disagree = conn.execute(
        f"""SELECT SUM(CASE WHEN capture_app = app_brand THEN 1 ELSE 0 END),
                   SUM(CASE WHEN capture_app != app_brand THEN 1 ELSE 0 END)
            FROM {t} WHERE capture_app IS NOT NULL
                       AND app_brand NOT LIKE 'App %'""").fetchone()
    agree, disagree = agree or 0, disagree or 0
    if agree + disagree:
        print(f"\n  page_id brand vs app_brand: {agree} agree, "
              f"{disagree} disagree")
        if disagree:
            print("  rows where they differ:")
            for cap, brand, name, n in conn.execute(
                f"""SELECT capture_app, app_brand, app_name, COUNT(*) n FROM {t}
                    WHERE capture_app != app_brand
                      AND app_brand NOT LIKE 'App %'
                    GROUP BY 1,2,3 ORDER BY n DESC LIMIT 8"""):
                print(f"    page_id says {cap}, app master says "
                      f"{brand} (app {name}): {n} rows")
        else:
            print("  -> consistent; app_assoc_id and page_id tell the same story")

    mismatch = conn.execute(
        f"""SELECT COUNT(*) FROM (SELECT session_assoc_id FROM {t}
            GROUP BY 1 HAVING COUNT(DISTINCT app_brand) > 1)""").fetchone()[0]
    if mismatch:
        print(f"\n  NOTE: {mismatch} session(s) span more than one BRAND. "
              "If a session is one website visit, these need explaining.")

    if db.has_table(conn, lt):
        n_link = conn.execute(f"SELECT COUNT(*) FROM {lt}").fetchone()[0]
        section("BRIDGE TO THE TAXONOMY")
        print(f"{n_link} of {n_q} distinct queries link to a sub_category "
              f"({n_link / n_q * 100:.1f}%)" if n_q else "no queries")
        print("unlinked queries are usually brand-only searches. Examples:")
        for (q,) in conn.execute(
            f"""SELECT DISTINCT normalized_query FROM {t}
                WHERE normalized_query NOT IN (SELECT normalized_query FROM {lt})
                AND normalized_query != '' LIMIT 8"""):
            print(f"  {q}")
        print("\nmost searched categories:")
        for cat, n in conn.execute(
            f"""SELECT l.category, COUNT(*) n FROM {t} e
                JOIN {lt} l ON e.normalized_query = l.normalized_query
                GROUP BY 1 ORDER BY n DESC LIMIT 10"""):
            print(f"  {cat:34} {n}")


def main(path=None):
    path = Path(path or config.JSON_PATH)

    # ---------- the file ----------
    section(f"FILE  {path}")
    raw = ingest.load_rows(path)
    print(f"records parsed: {len(raw)}")

    extra = Counter()
    import json
    text = path.read_text(encoding="utf-8").strip()
    for attempt in (text, f"[{text.rstrip(',')}]"):
        try:
            parsed = json.loads(attempt)
            break
        except json.JSONDecodeError:
            parsed = None
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                extra.update(set(item) - REQUIRED)
    if extra:
        print("\nFIELDS IN YOUR FILE THAT ingest.py IS THROWING AWAY:")
        for field, count in extra.most_common():
            print(f"  {field:30} present in {count} records")
        print("  -> if any of these are useful, they should become columns")
    else:
        print("no extra fields in the file - the three taxonomy columns are all there is")

    triples = Counter((r["master_category"], r["category"], r["sub_category"])
                      for r in raw)
    unique = len(triples)
    print(f"\nunique (master, category, sub) triples: {unique}")
    if len(raw) > unique:
        waste = len(raw) - unique
        print(f"duplicate rows: {waste}  ({waste / len(raw) * 100:.1f}% of the file)")
        print("top repeated triples:")
        for (m, c, s), n in triples.most_common(5):
            print(f"  {n:>6} x  {m} / {c} / {s}")

    # ---------- the database ----------
    try:
        conn = db.connect()
    except FileNotFoundError as exc:
        print(f"\n{exc}")
        return

    section(f"DATABASE  {config.DB_PATH.name}")
    total, n_m, n_c, n_s = conn.execute(
        """SELECT COUNT(*), COUNT(DISTINCT master_category),
                  COUNT(DISTINCT category), COUNT(DISTINCT sub_category)
           FROM taxonomy"""
    ).fetchone()
    print(f"rows {total} | master {n_m} | category {n_c} | sub_category {n_s}")

    print("\nsub_categories per master:")
    for m, c, s in conn.execute(
        """SELECT master_category, COUNT(DISTINCT category),
                  COUNT(DISTINCT sub_category)
           FROM taxonomy GROUP BY 1 ORDER BY 3 DESC"""
    ):
        print(f"  {m:<32} {c:>4} categories  {s:>5} sub_categories")

    # ---------- ambiguity ----------
    section("AMBIGUOUS LABELS")
    multi = conn.execute(
        """SELECT sub_category, COUNT(DISTINCT category) n
           FROM taxonomy GROUP BY 1 HAVING n > 1 ORDER BY n DESC LIMIT 15"""
    ).fetchall()
    if multi:
        print(f"{len(multi)} sub_category values sit under more than one category.")
        print("Lookup answers for these are inherently ambiguous:")
        for sub, n in multi:
            parents = [r[0] for r in conn.execute(
                "SELECT DISTINCT category FROM taxonomy WHERE sub_category = ?", (sub,))]
            print(f"  {sub:<38} -> {', '.join(parents[:4])}")
    else:
        print("none - every sub_category belongs to exactly one category")

    # ---------- fuzzy-search risk ----------
    section("LABELS THAT WILL CONFUSE FUZZY SEARCH")
    from search import normalise_label
    labels = [r[0] for r in conn.execute(
        "SELECT DISTINCT sub_category FROM taxonomy")]
    by_tokens = defaultdict(list)
    for lbl in labels:
        by_tokens[frozenset(normalise_label(lbl).split())].append(lbl)

    subsets = []
    token_sets = list(by_tokens)
    for i, a in enumerate(token_sets):
        for b in token_sets[i + 1:]:
            if a and b and (a < b or b < a):
                subsets.append((by_tokens[a][0], by_tokens[b][0]))
    if subsets:
        print(f"{len(subsets)} pairs where one label's words contain the other's.")
        print("A query matching the shorter one may rank the longer one first:")
        for a, b in subsets[:12]:
            print(f"  {a}   <->   {b}")
        print("\n  -> this is why scoring is two-stage; check these with /debug "
              "if a lookup looks wrong")
    else:
        print("none - no label is a word-subset of another")

    dupes = [v for v in by_tokens.values() if len(v) > 1]
    if dupes:
        print(f"\n{len(dupes)} groups of labels with IDENTICAL words:")
        for group in dupes[:8]:
            print(f"  {' | '.join(group)}")
        print("  -> fuzzy search cannot tell these apart; disambiguate by category")

    inspect_events(conn)

    # ---------- prompt cost ----------
    section("PROMPT COST")
    card = db.build_schema_card(conn)
    print(f"schema card: {len(card)} chars, roughly {len(card) // 4} tokens")
    print(f"context budget: {config.NUM_CTX} tokens")
    if len(card) // 4 > config.NUM_CTX * 0.5:
        print("  WARNING: the card eats over half the context. Raise "
              "OLLAMA_NUM_CTX or lower max_categories in db.build_schema_card.")
    else:
        print("  fine - plenty of room for the question and the answer")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
