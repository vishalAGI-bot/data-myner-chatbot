"""Decide which path answers the question.

sql     -> one query answers it            ("how many master categories")
lookup  -> one fuzzy match answers it       ("where does an infant vest go")
agent   -> needs several steps, where each depends on the last
           ("which master has the most sub-categories, and show me 3 of them")
chat    -> greetings, meta questions, off-topic

Small local models drift off-format constantly, so the reply is constrained with
a JSON schema. Ollama enforces it during sampling - the model physically cannot
emit anything but one of the three valid words.
"""

import json

import config
import llm

VALID = {"sql", "lookup", "agent", "chat"}
DOMAINS = ("taxonomy", "products", "events", "cross")

# Words that only make sense for one dataset. Deliberately keyword-based: this
# runs on every question and must not cost a model call.
_EVENT_WORDS = (
    "search", "searches", "searched", "query", "queries", "journey", "journeys",
    "session", "sessions", "app", "apps", "website", "websites", "user", "users",
    "step", "steps", "click", "visit", "visited", "typed", "hours", "time",
    "when", "date", "day", "hour", "recent", "cross-app", "cross app",
)
_PRODUCT_WORDS = (
    "product", "products", "brand", "brands", "sku", "item", "items",
    "catalogue", "catalog", "sells", "sell", "sold", "stock",
)
_TAXONOMY_WORDS = (
    "category", "categories", "sub category", "sub-category", "subcategory",
    "master", "taxonomy", "tree", "classif", "belongs", "belong",
)
# Phrases that need BOTH datasets joined, not one or the other.
_CROSS_PHRASES = (
    "categor" + suffix + verb
    for suffix in ("y ", "ies ")
    for verb in ("people search", "users search", "do people", "are searched",
                 "get searched", "searched for", "searched on")
)


def pick_domain(question: str, app_names=(), names_category=False) -> str:
    """Which specialist should answer. Keyword-based, so it is free.

    names_category: the caller has already fuzzy-matched the question against
    real taxonomy values. "how many searches were about vitamins" contains no
    taxonomy KEYWORD, but Vitamins is a real sub_category, so it needs the JOIN.
    """
    q = question.lower()

    if any(phrase in q for phrase in _CROSS_PHRASES):
        return "cross"

    events = any(w in q for w in _EVENT_WORDS) or any(
        str(a).lower() in q for a in app_names if a)
    taxonomy = any(w in q for w in _TAXONOMY_WORDS) or names_category
    product = any(w in q for w in _PRODUCT_WORDS)

    if product and events:
        return "cross"
    if product:
        return "products"      # the product agent also sees the taxonomy

    # "how many searches" says events and nothing else; "which categories do
    # people search for" says both, and both means a JOIN.
    if events and taxonomy:
        return "cross"
    if events:
        return "events"
    if taxonomy:
        return "taxonomy"
    return "cross"          # unknown subject: the agent that can see everything

ROUTE_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string",
                           "enum": ["sql", "lookup", "agent", "chat"]}},
    "required": ["path"],
}

ROUTER_SYSTEM = """You classify questions about a product-category taxonomy
with two datasets: a category tree (master_category > category > sub_category)
and a log of product searches users made across shopping websites
(journeys, sessions, apps, queries, timestamps).

Choose one path:
sql    - ONE query answers it: counting, listing, grouping, filtering.
lookup - ONE fuzzy match answers it: the user describes a product in their own
         words and wants to know where it belongs.
agent  - it needs SEVERAL steps, because a later step depends on the result of
         an earlier one, or the question has two different parts.
chat   - greeting, thanks, a question about you, or anything unrelated.

Examples:
"how many master categories are there"        -> sql
"list all categories under Softlines"         -> sql
"which master has the most sub categories"    -> sql
"does Baby Bodysuits exist"                   -> sql
"where does an infant vest go"                -> lookup
"what is similar to sleepwear"                -> lookup
"classify: toddler jacket"                    -> lookup
"hi, what can you do"                         -> chat

Reply with JSON only: {"path": "sql"}"""

_SQL_HINTS = ("how many", "count", "list", "all ", "which", "total", "number of",
              "most", "least", "show me all", "group", "average", "exist", "kitne",
              "search", "searches", "journey", "session", "app ", "website",
              "user", "query", "queries", "when ", "top ")
_LOOKUP_HINTS = ("where does", "where do", "belong", "classify", "similar",
                 "closest", "which category is", "map ")
_CHAT_HINTS = ("hello", "hi ", "hey", "thanks", "thank you", "who are you",
               "what can you do", "help me", "namaste")
# Strong multi-step signals only. Anything weaker is left to the model, because
# sending a one-step question down the agent path makes it 4x slower for nothing.
# Signals that one query will not be enough: two requests joined together, a
# comparison whose second half depends on the first, or a question with a
# purpose-built tool that the plain SQL path would answer worse.
_AGENT_HINTS = (" and then", " then ", " also ", "for each", "after that",
                "compare", " and give", " and show", " and tell", " and list",
                ", and give", ", and show", ", and tell", ", and list",
                "and how many", "and what are", "and give me", "and show me",
                # journeys have a dedicated tool that returns 6 tidy columns;
                # left to plain SQL the model writes SELECT * and dumps 20
                "show me journey", "show journey", "journey ")


def heuristic(question: str) -> str:
    """Zero-cost fallback. Also catches obvious cases before the model is loaded."""
    q = question.lower().strip()
    if len(q) < 3:
        return "chat"
    if q in ("hi", "hey", "yo") or any(h in q for h in _CHAT_HINTS):
        return "chat"
    if config.AGENT_ENABLED and any(h in q for h in _AGENT_HINTS):
        return "agent"
    if any(h in q for h in _LOOKUP_HINTS):
        return "lookup"
    if any(h in q for h in _SQL_HINTS):
        return "sql"
    return ""  # undecided - ask the model


def route(question: str, use_llm: bool = True) -> str:
    """Heuristic first; the model is only called when the heuristic is unsure.
    On a local setup this skips a model call on most questions."""
    quick = heuristic(question)
    if quick:
        return quick
    if not use_llm:
        return "sql"

    try:
        raw = llm.complete(
            system=ROUTER_SYSTEM,
            user=question,
            model=config.ROUTER_MODEL,
            max_tokens=32,
            schema=ROUTE_SCHEMA,
        )
        path = json.loads(raw).get("path", "").lower().strip()
        if path == "agent" and not config.AGENT_ENABLED:
            return "sql"
        return path if path in VALID else "sql"
    except Exception:
        return "sql"  # data questions are the common case; SQL degrades gracefully
