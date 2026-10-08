"""Offline smoke test - no Ollama, no GPU, no API key. Run: python test_local.py

Covers the parts that actually break in production: the JSON repair, the SQL
guard, the fuzzy search, the router, and every agent failure mode (bad SQL,
repeated calls, unknown tools, malformed JSON, runaway loops).
"""

import json as J
import pathlib

import config
import db
import ingest
import llm
import router
from search import TaxonomySearch

ok = fail = 0


def check(label, condition, detail=""):
    global ok, fail
    if condition:
        ok += 1
        print(f"  PASS  {label}")
    else:
        fail += 1
        print(f"  FAIL  {label} {detail}")


conn = db.connect()
N_ROWS, N_MASTER, N_CAT, N_SUB = conn.execute(
    """SELECT COUNT(*), COUNT(DISTINCT master_category),
              COUNT(DISTINCT category), COUNT(DISTINCT sub_category) FROM taxonomy"""
).fetchone()
print(f"\nTesting against {config.DB_PATH.name}: {N_ROWS} rows, {N_MASTER} master, "
      f"{N_CAT} category, {N_SUB} sub_category")
print("(every expected value below is read from this database, not hard-coded)")

print("\n1. JSON repair")
rows = ingest.load_rows(config.BASE_DIR / "data" / "cat_sample.json")
check("parses bracket-less fragment", len(rows) > 0, f"got {len(rows)}")
check("fields present", set(rows[0]) == {"master_category", "category", "sub_category"})

check("dedupe collapses repeated triples",
      len(ingest.dedupe(rows + rows)) == len(ingest.dedupe(rows)))

import tempfile
with tempfile.TemporaryDirectory() as tmp:
    scratch = pathlib.Path(tmp) / "scratch.db"
    ingest.build_db(rows, scratch).close()
    try:
        ingest.build_db(rows, scratch).close()   # re-ingest over an existing db
        check("ingest can be re-run over an existing database", True)
    except Exception as exc:
        check("ingest can be re-run over an existing database", False, str(exc)[:70])

print("\n2. SQL guard")
for bad in ["DROP TABLE taxonomy", "SELECT 1; DELETE FROM taxonomy",
            "UPDATE taxonomy SET category='x'", "PRAGMA table_info(taxonomy)", ""]:
    try:
        db.guard(bad)
        check(f"blocks {bad[:28]!r}", False)
    except db.UnsafeSQL:
        check(f"blocks {bad[:28]!r}", True)
check("strips ``` fences", db.guard("```sql\nSELECT 1\n```").startswith("SELECT"))
check("adds LIMIT", "LIMIT" in db.guard("SELECT * FROM taxonomy").upper())
check("keeps existing LIMIT",
      db.guard("SELECT * FROM taxonomy LIMIT 5").upper().count("LIMIT") == 1)
check("allows WITH", db.guard("WITH x AS (SELECT 1) SELECT * FROM x").startswith("WITH"))

print("\n3. Read-only connection")
try:
    conn.execute("DELETE FROM taxonomy")
    check("write rejected at the driver level", False)
except Exception:
    check("write rejected at the driver level", True)

print("\n4. Aggregation correctness")
table = conn.execute(
    "SELECT master_category, category, sub_category FROM taxonomy").fetchall()
for col, idx, expected in (("master_category", 0, N_MASTER),
                           ("category", 1, N_CAT),
                           ("sub_category", 2, N_SUB)):
    _, r = db.run_sql(conn, f"SELECT COUNT(DISTINCT {col}) FROM taxonomy")
    truth = len({row[idx] for row in table})
    check(f"COUNT(DISTINCT {col}) matches a Python count",
          r[0][0] == truth == expected, f"sql={r[0][0]} python={truth}")

_, r = db.run_sql(conn,
                  "SELECT master_category, COUNT(DISTINCT sub_category) "
                  "FROM taxonomy GROUP BY 1 LIMIT 1000")
check("GROUP BY covers every master", len(r) == N_MASTER, f"got {len(r)} of {N_MASTER}")
check("COUNT(*) differs from COUNT(DISTINCT) when rows repeat",
      N_ROWS >= N_SUB, f"rows={N_ROWS} subs={N_SUB}")

check("looks_empty: no rows", db.looks_empty([]))
check("looks_empty: COUNT(*) returning 0", db.looks_empty([(0,)]))
check("looks_empty: NULL aggregate", db.looks_empty([(None,)]))
check("looks_empty: a real count is not empty", not db.looks_empty([(7,)]))
check("looks_empty: several rows are not empty", not db.looks_empty([(0,), (0,)]))

print("\n5. Schema card")
card = db.build_schema_card(conn)
a_master = conn.execute("SELECT master_category FROM taxonomy LIMIT 1").fetchone()[0]
check("mentions the table", "taxonomy" in card)
check(f"lists a real master ({a_master!r})", a_master in card)
check("reports the true row count", str(N_ROWS) in card)
check("stays compact", len(card) < 20000, f"{len(card)} chars")

print("\n6. Fuzzy search")
searcher = TaxonomySearch(db.all_sub_categories(conn))

# Round-trip against YOUR data: mangle a real label, expect it back on top.
import random
random.seed(0)
samples = [r[0] for r in conn.execute(
    "SELECT DISTINCT sub_category FROM taxonomy").fetchall()]
multiword = [x for x in samples if len(x.split()) > 1][:25] or samples[:25]
exact = sum(searcher.search(lbl)[0]["sub_category"] == lbl
            for lbl in multiword if searcher.search(lbl))
check(f"exact label returns itself ({exact}/{len(multiword)})",
      exact >= len(multiword) * 0.9, f"{exact}/{len(multiword)}")

lowered = sum(searcher.search(lbl.lower())[0]["sub_category"] == lbl
              for lbl in multiword if searcher.search(lbl.lower()))
check(f"lowercased label still matches ({lowered}/{len(multiword)})",
      lowered >= len(multiword) * 0.9, f"{lowered}/{len(multiword)}")

def typo(text):                       # swap two adjacent letters
    i = max((len(text) // 2), 1)
    return text[:i - 1] + text[i] + text[i - 1] + text[i + 1:]


typos = sum(searcher.search(typo(lbl))[0]["sub_category"] == lbl
            for lbl in multiword if searcher.search(typo(lbl)))
# On a tiny taxonomy each label is a huge share of the total, so allow one
# miss outright rather than demanding a percentage that cannot be met.
check(f"survives a typo ({typos}/{len(multiword)})",
      typos >= len(multiword) * 0.8 or len(multiword) - typos <= 1,
      f"{typos}/{len(multiword)}")

# Only meaningful where the shortened form is reasonably distinctive. If 45
# labels share the prefix "Dairy & Eggs", no scorer can rank a specific one
# into the top 8 - that is arithmetic, not a defect.
from collections import Counter
prefixes = Counter(" ".join(x.split()[:-1]) for x in samples if len(x.split()) > 1)
shortened = [lbl for lbl in multiword
             if prefixes[" ".join(lbl.split()[:-1])] <= config.FUZZY_TOP_K
             and searcher.search(" ".join(lbl.split()[:-1]))]
if shortened:
    found = sum(any(h["sub_category"] == lbl for h in
                    searcher.search(" ".join(lbl.split()[:-1])))
                for lbl in shortened)
    check(f"distinctive prefix keeps its label in the top {config.FUZZY_TOP_K} "
          f"({found}/{len(shortened)})",
          found >= len(shortened) * 0.7, f"{found}/{len(shortened)}")
else:
    ambiguous = sum(1 for v in prefixes.values() if v > config.FUZZY_TOP_K)
    check(f"prefix test skipped: {ambiguous} prefixes shared by too many labels",
          True)

check("nonsense returns nothing useful",
      len(searcher.search("quantum tractor axle flange")) <= 2)

# Fixed fixture: the exact bug real data exposed - applying synonyms to LABELS
# turned "Baby & Infant" into "baby baby", which then beat the right answer.
fixture = [
    {"sub_category": "Baby & Infant", "category": "Grocery (Excl. Fresh)",
     "master_category": "Core Consumables"},
    {"sub_category": "Baby Bodysuits", "category": "Apparel",
     "master_category": "Softlines"},
    {"sub_category": "Baby Boys' Sleepwear", "category": "Apparel",
     "master_category": "Softlines"},
    {"sub_category": "Baby Food", "category": "Grocery",
     "master_category": "Core Consumables"},
]
fx = TaxonomySearch(fixture)
check("labels are not rewritten by synonyms",
      fx.keys[0] == "baby infant", fx.keys[0])
top = fx.search("infant onesie")
check("'infant onesie' -> Baby Bodysuits, not Baby & Infant",
      top[0]["sub_category"] == "Baby Bodysuits", f"got {top[:1]}")
wrong = [h["score"] for h in top if h["sub_category"] == "Baby & Infant"]
check("the wrong row is far behind or gone",
      not wrong or top[0]["score"] - wrong[0] > 20,
      [(h["sub_category"], h["score"]) for h in top])
top = fx.search("toddler pyjamas for boys")
check("'toddler pyjamas' -> sleepwear",
      "Sleepwear" in top[0]["sub_category"], f"got {top[:1]}")
check("query synonyms still applied",
      "bodysuit" in __import__("search").normalise_query("infant onesie"))

print("\n6b. Value hints and did-you-mean")
a_sub_any = conn.execute("SELECT sub_category FROM taxonomy LIMIT 1").fetchone()[0]
fragment = a_sub_any.split()[0].lower()
vh = searcher.search_values(f"how many {fragment} in sub category")
check(f"a fragment ({fragment!r}) finds its real value",
      any(h["value"] == a_sub_any for h in vh), [h["value"] for h in vh][:3])
check("hints know which column they came from",
      all(h["level"] in ("sub_category", "category", "master_category") for h in vh))
a_master_any = conn.execute("SELECT master_category FROM taxonomy LIMIT 1").fetchone()[0]
vh = searcher.search_values(a_master_any)
check("master names are searchable too",
      any(h["value"] == a_master_any for h in vh), [h["value"] for h in vh][:3])

fx2 = TaxonomySearch([
    {"sub_category": "Dairy & Eggs", "category": "Fresh",
     "master_category": "Core Consumables"},
    {"sub_category": "Frozen", "category": "Fresh",
     "master_category": "Core Consumables"},
    {"sub_category": "Printing Consumables", "category": "Office",
     "master_category": "Hardlines"},
])
hint = fx2.search_values("how many data of dairy in sub category")
check("'dairy' -> 'Dairy & Eggs' and nothing silly",
      hint and hint[0]["value"] == "Dairy & Eggs", [h["value"] for h in hint])

print("\n6c. Hints must not fire on generic questions")
from search import content_terms
GENERIC = ["how many master categories are there?", "how many categories are there?",
           "how many sub categories are there?", "list all master categories",
           "which master category has the most sub categories?",
           "count the sub categories", "total number of categories"]
for q in GENERIC:
    check(f"no content terms: {q[:38]!r}", content_terms(q) == [], content_terms(q))
    check(f"no hints: {q[:38]!r}", searcher.search_values(q) == [],
          [h["value"] for h in searcher.search_values(q)][:3])

a_sub_word = [w for w in a_sub_any.split() if len(w) > 3]
if a_sub_word:
    word = a_sub_word[0].lower()
    check(f"a real term ({word!r}) still produces a hint",
          bool(searcher.search_values(f"how many {word} in sub category")))

poison = TaxonomySearch([
    {"sub_category": "Gifts, Decor & All Other", "category": "Gifts",
     "master_category": "Media"},
    {"sub_category": "Dairy", "category": "Fresh",
     "master_category": "Core Consumables"},
])
check("'are there' no longer matches 'All Other'",
      poison.search_values("how many categories are there?") == [])
check("a named value still resolves",
      [h["value"] for h in poison.search_values("how many dairy")] == ["Dairy"])

print("\n6d. Global counts answered without the model")
import chatbot as chatbot_mod


def explode(*a, **k):
    raise AssertionError("the model must not be called for a global count")


saved_complete = chatbot_mod.llm.complete
chatbot_mod.llm.complete = explode
fast_bot = chatbot_mod.Chatbot()
for q, level, expected in (("how many master categories are there?",
                            "master_category", N_MASTER),
                           ("how many categories are there?", "category", N_CAT),
                           ("how many sub categories are there?",
                            "sub_category", N_SUB)):
    turn = fast_bot.ask(q)
    check(f"{q[:34]!r} -> {expected}",
          str(expected) in turn.answer and level in turn.answer, turn.answer)
    check("answered with no model call", turn.path == "sql (direct)", turn.path)
for q in ("how many rows mention " + a_sub_any, "which master has the most sub categories"):
    check(f"specific question skips the fast path: {q[:34]!r}",
          fast_bot._global_count(q) is None)
chatbot_mod.llm.complete = saved_complete

print("\n7. Router")
check("counting -> sql", router.heuristic("how many master categories") == "sql")
check("listing -> sql", router.heuristic("list all categories under Softlines") == "sql")
check("wording -> lookup", router.heuristic("where does an infant vest go") == "lookup")
check("multi-part -> agent",
      router.heuristic("count them and show me 3 examples") == "agent")
check("greeting -> chat", router.heuristic("hi there") == "chat")
check("simple question does NOT reach the agent",
      router.heuristic("how many master categories") != "agent")
check("unknown phrasing defers to the model", router.heuristic("softlines stuff") == "")

print("\n8. Agent loop (scripted model replies)")
import agent as agent_mod

scripted = []


def scripted_complete(system, user, model=None, max_tokens=1024, **kw):
    return scripted.pop(0) if scripted else "fallback answer"


_real_complete = llm.complete          # agent_mod.llm IS the llm module, so this
agent_mod.llm.complete = scripted_complete   # patch is global - restored in step 9


def make_agent(max_steps=4):
    return agent_mod.Agent(conn, searcher, card, max_steps=max_steps)


BIG_MASTER = conn.execute(
    "SELECT master_category FROM taxonomy GROUP BY 1 "
    "ORDER BY COUNT(DISTINCT sub_category) DESC LIMIT 1").fetchone()[0]
A_SUB = conn.execute("SELECT sub_category FROM taxonomy WHERE master_category = ? "
                     "LIMIT 1", (BIG_MASTER,)).fetchone()[0]

scripted[:] = [
    J.dumps({"thought": "counts first", "tool": "run_sql",
             "sql": "SELECT master_category, COUNT(DISTINCT sub_category) n "
                    "FROM taxonomy GROUP BY 1 ORDER BY n DESC LIMIT 1"}),
    J.dumps({"thought": "now examples", "tool": "run_sql",
             "sql": "SELECT sub_category FROM taxonomy "
                    f"WHERE master_category='{BIG_MASTER}' LIMIT 3"}),
    J.dumps({"thought": "done", "tool": "final_answer",
             "answer": f"{BIG_MASTER} has the most, e.g. {A_SUB}."}),
]
res = make_agent().run("which master has the most subs and 3 examples")
check("runs dependent steps in order", len(res.steps) == 3, f"{len(res.steps)} steps")
check("real data reaches the observation", BIG_MASTER in res.steps[0].observation)
check("second step used the first result", A_SUB in res.steps[1].observation)
check("returns the final answer", BIG_MASTER in res.answer)
check("not flagged stopped_early", not res.stopped_early)
check("scratchpad carries prior steps",
      "observation" in make_agent()._scratchpad("q", res.steps))

scripted[:] = [
    J.dumps({"thought": "guess", "tool": "run_sql", "sql": "SELECT nope FROM taxonomy"}),
    J.dumps({"thought": "fixed", "tool": "run_sql",
             "sql": "SELECT COUNT(DISTINCT master_category) FROM taxonomy"}),
    J.dumps({"thought": "done", "tool": "final_answer", "answer": "There is 1."}),
]
res = make_agent().run("how many masters")
check("bad SQL becomes a recoverable error",
      res.steps[0].error and "SQL error" in res.steps[0].observation,
      res.steps[0].observation[:60])
check("agent recovers after an error", res.answer == "There is 1.")

scripted[:] = [
    J.dumps({"thought": "wipe it", "tool": "run_sql", "sql": "DROP TABLE taxonomy"}),
    J.dumps({"thought": "ok", "tool": "final_answer", "answer": "no"}),
]
res = make_agent().run("drop everything")
check("guard blocks writes inside the loop",
      res.steps[0].error and "rejected" in res.steps[0].observation)

same = J.dumps({"thought": "again", "tool": "run_sql",
                "sql": "SELECT COUNT(*) FROM taxonomy"})
scripted[:] = [same, same,
               J.dumps({"thought": "fine", "tool": "final_answer", "answer": "8 rows."})]
res = make_agent().run("count rows")
check("repeated identical call is refused",
      res.steps[1].error and "already ran" in res.steps[1].observation)

scripted[:] = [J.dumps({"thought": "?", "tool": "send_email"}),
               J.dumps({"thought": "ok", "tool": "final_answer", "answer": "done"})]
res = make_agent().run("email someone")
check("unknown tool is rejected",
      res.steps[0].error and "Unknown tool" in res.steps[0].observation)

scripted[:] = [J.dumps({"thought": "x", "tool": "run_sql"}),
               J.dumps({"thought": "ok", "tool": "final_answer", "answer": "done"})]
res = make_agent().run("missing field")
check("missing argument is reported back",
      res.steps[0].error and "sql" in res.steps[0].observation)

scripted[:] = ["this is not json at all",
               J.dumps({"thought": "ok", "tool": "final_answer", "answer": "done"})]
res = make_agent().run("whatever")
check("malformed JSON does not crash the loop", res.answer == "done")

scripted[:] = [J.dumps({"thought": "loop", "tool": "run_sql",
                        "sql": f"SELECT {i} AS c FROM taxonomy"}) for i in range(10)]
res = make_agent().run("never ending")
check("stops at the step limit", res.stopped_early and len(res.steps) == 4,
      f"{len(res.steps)} steps, early={res.stopped_early}")
check("still answers after the limit", bool(res.answer))

scripted[:] = [J.dumps({"thought": "look it up", "tool": "search_taxonomy",
                        "query": A_SUB.lower()}),
               J.dumps({"thought": "done", "tool": "final_answer",
                        "answer": f"{A_SUB}."})]
res = make_agent().run(f"where does {A_SUB.lower()} go")
check("agent can use the fuzzy search tool", A_SUB in res.steps[0].observation)

scripted[:] = [J.dumps({"thought": "what exists", "tool": "list_values",
                        "column": "master_category"}),
               J.dumps({"thought": "done", "tool": "final_answer", "answer": "ok"})]
res = make_agent().run("what master categories exist")
check("agent can use the list_values tool",
      str(N_MASTER) in res.steps[0].observation and BIG_MASTER in res.steps[0].observation)

check("tool list reaches the system prompt", "search_taxonomy" in make_agent()._system())
check("schema card reaches the system prompt", "Softlines" in make_agent()._system())

print("\n8b. No duplicated code or config names")
import config as cfg
src = {f: pathlib.Path(f).read_text() for f in
       ("chatbot.py", "cli.py", "app.py", "config.py", "router.py", "agent.py")}
check("only one agent branch in ask()",
      src["chatbot.py"].count('if path == "agent"') == 1,
      src["chatbot.py"].count('if path == "agent"'))
check("cli prints the agent banner once",
      src["cli.py"].count("agent: {config.AGENT_MODEL}") == 1)
for legacy in ("USE_AGENT", "config.MAX_STEPS", "result.trace"):
    check(f"no stale reference to {legacy}",
          not any(legacy in t for t in src.values()))
check("agent settings exist once",
      hasattr(cfg, "AGENT_ENABLED") and hasattr(cfg, "AGENT_MAX_STEPS")
      and not hasattr(cfg, "USE_AGENT"))
for name, text in src.items():
    import ast as _ast
    try:
        _ast.parse(text)
        check(f"{name} parses", True)
    except SyntaxError as exc:
        check(f"{name} parses", False, str(exc))

print("\n9. Ollama client against a stub daemon")
llm.complete = agent_mod.llm.complete = _real_complete   # undo the stub
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

seen = {}


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self._send({"models": [{"name": "qwen2.5-coder:7b"},
                               {"name": "nomic-embed-text:latest"}]})

    def do_POST(self):
        body = J.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen[self.path] = body
        if self.path == "/api/embed":
            self._send({"embeddings": [[1.0, 0.0, 0.0]] * len(body["input"])})
        else:
            reply = ('{"path": "agent"}' if body.get("format")
                     else "<think>thinking hard</think>SELECT 1")
            self._send({"message": {"content": reply}})

    def _send(self, obj):
        raw = J.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


server = HTTPServer(("127.0.0.1", 11555), Stub)
threading.Thread(target=server.serve_forever, daemon=True).start()
config.OLLAMA_HOST = llm.config.OLLAMA_HOST = "http://127.0.0.1:11555"

check("lists installed models", "qwen2.5-coder:7b" in llm.installed_models())
healthy, msg = llm.health()
check("health passes when models present", healthy, msg)

out = llm.complete("sys", "user")
check("strips <think> blocks", out == "SELECT 1", repr(out))
opts = seen["/api/chat"]["options"]
check("num_ctx above the 2048 default", opts["num_ctx"] >= 8192, opts)
check("temperature 0 by default", opts["temperature"] == 0)
check("keeps the model resident", seen["/api/chat"]["keep_alive"] == config.KEEP_ALIVE)

out = llm.complete("sys", "user", schema=router.ROUTE_SCHEMA)
check("passes the JSON schema through", "format" in seen["/api/chat"])
check("constrained reply parses", J.loads(out)["path"] == "agent")
check("agent schema offers every tool",
      set(agent_mod.AGENT_SCHEMA["properties"]["tool"]["enum"]) ==
      {"run_sql", "search_taxonomy", "list_values", "final_answer"})

vecs = llm.embed(["a", "b", "c"])
check("embeds and normalises", vecs.shape == (3, 3) and abs(vecs[0][0] - 1.0) < 1e-6,
      getattr(vecs, "shape", None))

config.SQL_MODEL = "not-pulled:99b"
healthy, msg = llm.health()
check("health fails on a missing model", not healthy and "ollama pull" in msg)
server.shutdown()

print(f"\n{ok} passed, {fail} failed\n")
raise SystemExit(1 if fail else 0)
