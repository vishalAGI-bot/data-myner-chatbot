"""Two specialists, plus one for questions that genuinely span both.

Why split at all? A 7B model picks tools badly when there are many, and reads
a schema card badly when it is long. Scoping each agent to its own tables:

  - halves the prompt (each card is ~700 tokens instead of ~1400 together)
  - removes the tools it must never use
  - lets the examples be about the right subject
  - makes "how many searches" vs "how many categories" structurally hard to
    confuse, because the wrong table is not in the prompt at all

The cross agent exists because a real question does span both: "which
categories do people search for most" needs search_event JOINed to taxonomy.
"""

import config

TAXONOMY_TABLES = (config.TABLE,)
EVENT_TABLES = (config.EVENTS_TABLE, config.APPS_TABLE)
PRODUCT_TABLES = (config.PRODUCTS_TABLE, config.TABLE)
CROSS_TABLES = (config.TABLE, config.EVENTS_TABLE, config.APPS_TABLE,
                config.LINK_TABLE, config.PRODUCTS_TABLE)

SPECS = {
    "taxonomy": {
        "label": "taxonomy agent",
        "tables": TAXONOMY_TABLES,
        "tools": ("run_sql", "search_taxonomy", "list_values", "final_answer"),
        "subject": "a product category tree: master_category > category > "
                   "sub_category.",
        "examples": """Q: which master category has the most sub categories, and 3 examples
1 {"thought": "counts per master first", "tool": "run_sql", "sql": "SELECT master_category, COUNT(DISTINCT sub_category) n FROM taxonomy GROUP BY 1 ORDER BY n DESC LIMIT 1"}
2 {"thought": "now examples from that master", "tool": "run_sql", "sql": "SELECT DISTINCT sub_category FROM taxonomy WHERE master_category = '<the answer from step 1>' LIMIT 3"}
3 {"thought": "have both", "tool": "final_answer", "answer": "..."}""",
    },
    "events": {
        "label": "events agent",
        "tables": EVENT_TABLES,
        "tools": ("run_sql", "show_journey", "top_queries", "list_values",
                  "final_answer"),
        "subject": "a log of product searches people made on shopping apps, "
                   "grouped into journeys (one product hunt, possibly across "
                   "several apps).",
        "examples": """Q: which app had the most searches, and what did people search there
1 {"thought": "busiest app first", "tool": "run_sql", "sql": "SELECT app_name, COUNT(*) n FROM search_event GROUP BY 1 ORDER BY n DESC LIMIT 1"}
2 {"thought": "now its queries", "tool": "run_sql", "sql": "SELECT normalized_query, COUNT(*) n FROM search_event WHERE app_name = '<the answer from step 1>' GROUP BY 1 ORDER BY n DESC LIMIT 10"}
3 {"thought": "have both", "tool": "final_answer", "answer": "..."}

Remember: app_brand groups sub-apps ('Amazon Now' has app_brand 'Amazon').
Use app_brand for a company, app_name for the exact app. A journey moving
between two app_name values of the same app_brand did NOT change website.""",
    },
    "products": {
        "label": "product agent",
        "tables": PRODUCT_TABLES,
        "tools": ("run_sql", "search_taxonomy", "list_values", "final_answer"),
        "subject": "a catalogue of real products, each with a brand and a "
                   "place in the category tree.",
        "examples": """Q: which brand has the most products, and in which categories
1 {"thought": "biggest brand first", "tool": "run_sql", "sql": "SELECT brand, COUNT(*) n FROM product WHERE brand != '' GROUP BY brand_key ORDER BY n DESC LIMIT 1"}
2 {"thought": "now where its products sit", "tool": "run_sql", "sql": "SELECT category, COUNT(*) n FROM product WHERE brand_key = LOWER('<the brand from step 1>') GROUP BY 1 ORDER BY n DESC LIMIT 10"}
3 {"thought": "have both", "tool": "final_answer", "answer": "..."}

COUNT(*) on product counts PRODUCTS. If the user asks how many categories,
use COUNT(DISTINCT category). Match brand on brand_key (already lowercased)
and product_master with LIKE.""",
    },
    "cross": {
        "label": "cross-dataset agent",
        "tables": CROSS_TABLES,
        "tools": ("run_sql", "search_taxonomy", "show_journey", "top_queries",
                  "list_values", "final_answer"),
        "subject": "a category tree, a product catalogue, and a log of "
                   "searches people made on shopping apps, joined by the "
                   "query_category table.",
        "examples": """Q: which categories do people search for most, on which app
1 {"thought": "join the searches to their categories", "tool": "run_sql", "sql": "SELECT l.category, e.app_brand, COUNT(*) n FROM search_event e JOIN query_category l ON e.normalized_query = l.normalized_query GROUP BY 1,2 ORDER BY n DESC LIMIT 20"}
2 {"thought": "that answers it", "tool": "final_answer", "answer": "..."}

query_category only holds queries that matched a category. Brand-only searches
like 'logo' are absent from it by design, so a JOIN silently drops them - say
so if the user asks about coverage.""",
    },
}


def spec(name: str) -> dict:
    return SPECS.get(name, SPECS["cross"])
