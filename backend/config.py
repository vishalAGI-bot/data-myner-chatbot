"""Central configuration. Everything is overridable via environment variables."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# --- data ---
JSON_PATH = Path(os.getenv("TAXONOMY_JSON", BASE_DIR / "data" / "cat_sample.json"))
DB_PATH = Path(os.getenv("TAXONOMY_DB", BASE_DIR / "taxonomy.db"))
TABLE = "taxonomy"                 # the category tree
EVENTS_TABLE = "search_event"      # cross-app product search events
LINK_TABLE = "query_category"      # normalized_query -> best taxonomy match
APPS_TABLE = "app"                 # the app master, with the sub-app hierarchy
PRODUCTS_TABLE = "product"         # the product master: uid, name, brand, category

EVENTS_JSON = Path(os.getenv("EVENTS_JSON", BASE_DIR / "data" /
                             "user_cross_app_journey_event_search.json"))
# Fallback only. The real names come from the app master file (app_master.json),
# which also carries parent_id - the difference between "Amazon Now" and "Amazon".
APPS_MAP = Path(os.getenv("APPS_MAP", BASE_DIR / "data" / "apps.json"))
# Minimum fuzzy score for linking a search query to a sub_category.
LINK_THRESHOLD = int(os.getenv("LINK_THRESHOLD", 75))

# --- language model provider ---
# Keep local Ollama as the backward-compatible default.  Set this to ``openai``
# (or choose OpenAI on the Settings page) to use the Responses API instead.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna").strip() or "gpt-6-luna"
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "none").strip().lower()
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", 180))

# --- ollama ---
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", 180))
# Keep the model resident between questions. Without this Ollama unloads after
# 5 minutes and the next question pays the full 9 GB reload.
KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "30m")
# Ollama's default context is 2048 tokens, which silently truncates the schema
# card and makes the model invent category names. Never leave this at default.
NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", 8192))

# --- models, one per role ---
# Defaults use a single 7B model for all three roles so nothing swaps in VRAM.
# See the README for the higher-quality profile.
SQL_MODEL = os.getenv("SQL_MODEL", "qwen2.5-coder:7b")
ANSWER_MODEL = os.getenv("ANSWER_MODEL", "qwen2.5-coder:7b")
ROUTER_MODEL = os.getenv("ROUTER_MODEL", "qwen2.5-coder:7b")
# Multi-step tool choice is the hardest job here. A reasoning model beats a
# coder model at it; qwen3:14b is the upgrade if you have the VRAM.
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
# Multi-step tool choice is the hardest job for a small model. qwen3:14b is
# noticeably better at it than the coder models if you have the VRAM.
AGENT_MODEL = os.getenv("AGENT_MODEL", SQL_MODEL)

# OpenAI uses one configured model for all roles.  The app's own router, SQL
# guards, and agent loop remain unchanged; only completion generation moves.
if LLM_PROVIDER == "openai":
    SQL_MODEL = ANSWER_MODEL = ROUTER_MODEL = AGENT_MODEL = OPENAI_MODEL

# qwen3 emits <think>...</think>. Set to 1 to ask Ollama to disable thinking
# outright (needs a recent Ollama); otherwise the tags are stripped afterwards.
DISABLE_THINK = os.getenv("OLLAMA_DISABLE_THINK", "0") == "1"

# --- behaviour ---
MAX_SQL_ROWS = int(os.getenv("MAX_SQL_ROWS", 200))
# List every sub_category in the prompt when there are at most this many. The
# model cannot match a value it has never seen, so this is the difference
# between "= 'Dairy'" and "LIKE '%Dairy%'" finding "Dairy & Eggs".
MAX_SUBS_IN_CARD = int(os.getenv("MAX_SUBS_IN_CARD", 400))
# Hard ceiling on the agent loop. Without it a confused model runs forever, and
# past ~6 the scratchpad crowds out an 8k context anyway.
AGENT_MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", 5))
AGENT_ENABLED = os.getenv("AGENT_ENABLED", "1") == "1"
FUZZY_THRESHOLD = int(os.getenv("FUZZY_THRESHOLD", 60))
FUZZY_TOP_K = int(os.getenv("FUZZY_TOP_K", 8))
USE_EMBEDDINGS = os.getenv("USE_EMBEDDINGS", "0") == "1"
EMBED_CACHE = Path(os.getenv("EMBED_CACHE", BASE_DIR / "embeddings.npz"))

# Words users say vs words YOUR taxonomy uses. Applied to the query only.
#
# These defaults suit a baby-apparel taxonomy. If your taxonomy is general
# retail, most of them are wrong for you - empty this dict and rebuild it from
# your own query logs. A bad synonym is worse than no synonym.
SYNONYMS = {
    "kids": "baby", "kid": "baby", "child": "baby", "children": "baby",
    "infant": "baby", "toddler": "baby", "newborn": "baby",
    "onesie": "bodysuit", "vest": "bodysuit",
    "clothes": "apparel", "clothing": "apparel", "garment": "apparel",
    "pants": "bottoms", "trousers": "bottoms",
    "jacket": "outerwear", "coat": "outerwear",
    "pyjama": "sleepwear", "pajama": "sleepwear", "nightwear": "sleepwear",
    "underwear": "innerwear", "undergarment": "innerwear",
}
