# Cross-App Journey Agent (v2)

A chatbot over one aggregated cross-app journey file. It is separate from the
taxonomy / product chatbot and never reads or changes their data.

## Why v2 exists

The first version sent almost every question to a 7B model that wrote SQL and
the final answer itself, and it resolved "this user" with keyword regexes.
That produced confidently wrong answers:

| Question | v1 answer | Truth (data-1790154732687.json) |
|---|---|---|
| ok User 5330, app name | "Meesho ... with 35 search events" | Meesho: **70 searches** in 35 journeys (35 was the journey count) |
| all app name of this user | all 12 apps in the file | **3 apps**: Meesho, Flipkart, Amazon |
| of particular user | "Insufficient evidence" | same 3 apps for user 5330 |

Root causes:

1. **Regex shortcut hijacked the question.** `_asks_for_complete_app_list`
   fired on "all" + "app" and ignored "this user".
2. **No real conversation memory.** "This user" / "particular user" were left to
   the model, which lost the thread after one wrong turn.
3. **The model did the arithmetic and labelling.** It confused journeys with
   searches. Nothing checked its numbers against the query results.
4. **Silent truncation.** Tool output was cut at 60 rows with no clear
   "partial" signal. History plus evidence could overflow Ollama's 8k context,
   which silently drops the start of the prompt (including the schema).
5. **API bugs.** An answer over 4,000 characters (any full user profile) made the
   *next* request fail with a 422. History over 12 turns was also rejected. One
   global lock serialised every chat, upload and health check.
6. **Weak SQL guard.** The table allow-list regex was bypassed by comma joins
   (`FROM journey, sqlite_master`), and there was no query timeout.

## Architecture

```
message ──► split "simple meaning" wrapper
        ──► context.py   resolve "this user", "the second user", "that app",
                          "what about user 4598?", "and purchased only"
        ──► planner.py   RuleParser: question -> QueryPlan (only if every word is understood)
                          LLMPlanner: model fills the same plan schema (validated) otherwise
        ──► planner.py   compile plan -> parameterised SQL (counting rules encoded once)
        ──► store.py     execute (trusted connection)
        ──► answer.py    deterministic wording; numbers come straight from SQL
        └─► agent.py     fallback: tool-using SQL agent on a sandboxed connection;
                          its final answer must pass a number-grounding check
```

* **Most questions never touch the model.** They are fast (milliseconds) and
  still work when Ollama is down.
* **The model's job is understanding, not arithmetic.** It picks plan fields.
  The SQL and the numbers come from code.
* **Grounding guard.** Every number in a model-written answer must appear in the
  tool results (after rounding, or converted from seconds). Otherwise the answer is
  rejected and the model retries. "Derived" percentages are not accepted, because
  with many numbers in play almost any figure can be derived. "Simple meaning" mode
  only lets the model add a plain-language intro with no numbers; the facts below
  it come from the query.
* **Clarify instead of guessing.** "Details of this user" right after a
  top-10 list asks which user you mean. "The second user" picks #2.
* **Transparent.** Answers say how references were resolved ("Using user 5330
  from the previous answer"). The API returns the resolved question, the SQL
  and the result table.

## Thinking depth (v2.3)

Every chat request takes `depth` and `max_seconds`:

| depth | what happens |
|---|---|
| `fast` | One exact query. Milliseconds when the keyword rules understand the question. |
| `deep` | The model splits the question into up to 4 data questions and flags parts the file cannot answer (e.g. "past 4 weeks", city, order value). Each part is answered by the exact query layer. A reviewer step checks every part was answered about the right thing and adds up to 2 follow-up queries if not. The model then writes the final answer, and every number and id in it must match the query results, or the draft is rejected and rewritten. |
| `auto` (default) | `fast` when the rules fully understand the question; `deep` when it has several asks, comparisons, trends, "why", or is long and unclear. |

**Hard limit: `max_seconds`, 60 by default and at most 1200 (20 min).** Every model call gets only the remaining
time, and runs in a worker thread that checks the deadline and the stop signal
every 0.25 s. When the limit is hit or the user presses Stop, the agent:

1. stops calling the model,
2. still answers any remaining parts the keyword rules understand (instant, no model),
3. returns the verified parts with a note saying what was not finished.

API additions:

* `POST /api/chat` with `{"depth": "auto|fast|deep", "max_seconds": 60, "request_id": "..."}`
  (`max_seconds` up to 1200).
  The response adds `depth`, `thinking` (steps with timings), `parts`, `verified`, `timed_out`.
* `POST /api/chat/stream`: server-sent events `start`, `thinking` (live steps), `final`.
  Closing the connection stops the work.
* `POST /api/chat/{request_id}/stop`: the Stop button. It returns the best verified answer.
* The React app in `../frontend` uses all of this: depth selector, 1-minute limit
  slider, live thinking bubble and Stop button. The Streamlit app has the same
  controls in its sidebar.

## Business questions added in v2.2–v2.3

* **Purchase share**: "What's Amazon's purchase share and who is ahead of us?" gives
  each app's share of *all* purchases, not its own conversion rate. "we / us / our" =
  `JOURNEY_HOME_APP` (default Amazon).
* **Start-app analysis**: "Of journeys that start on Amazon, how many buy on Amazon vs
  elsewhere?" The start app comes from `first_search_app_id`, mapped to app names
  using single-app journeys. The answer shows stayed, moved, bought and where they went.
  "purchase rate by start app" and "journeys by first app" also work.

## Data loading (large / messy files)

* Accepts `.json` (array, or `{"data": [...]}`), `.jsonl` and `.csv` exports.
* Streams with `ijson` when it is installed. Inserts in batches of 5,000.
* Bad records (missing id, duplicates, unreadable embeddings, app counts that
  do not add up) are **quarantined and reported** in `summary.data_quality`
  instead of rejecting the whole upload. `JOURNEY_STRICT=1` restores fail-fast.
* Timestamps like `2026-09-12 18:15:06.677+02` are normalised to UTC columns
  (`first_search_utc`) plus `search_date` / `search_hour` for grouping.
* Similarity search uses a normalised numpy matrix (vectorised cosine).
* Benchmark: a 543 MB, 60,000-journey JSONL file loads in about 12 s. Built-in
  questions answer in under 250 ms.

## SQL sandbox (model-written SQL only)

A separate read-only connection with a SQLite **authorizer**: only `SELECT` on
`journey` and `journey_app`, `journey_hash` reads as NULL, and
`load_extension` is blocked. It also has a wall-clock timeout
(`JOURNEY_SQL_TIMEOUT`, default 3 s) and a row cap. Results carry
`[COMPLETE]` / `[PARTIAL: showing N of M]` markers.

## Run

```bash
# API (used by the web UI)
uvicorn journey_agent.api:app --reload --port 8100
# terminal, with conversation memory; type "debug" to see route + SQL
python -m journey_agent.cli data/cross-app-journey/data-1790154732687.json
# Streamlit
streamlit run journey_agent/app.py
# tests (ground truth computed from the raw JSON, not via the store)
python -m pytest tests/test_journey_agent.py -q
```

### API

`POST /api/chat` is backwards compatible. `answer`, `steps` and
`stopped_early` are unchanged, and these fields are new:

| field | meaning |
|---|---|
| `route` | `rules`, `llm_plan`, `agent`, `clarify`, `unsupported`, `not_found`, ... |
| `resolved_question` | e.g. "searches by app for user 5330" |
| `sql` | the query that produced the numbers |
| `table` | `{columns, rows, total_rows, truncated}` for rendering a table |
| `notes`, `latency_ms`, `request_id` | diagnostics |

The front end **must send `history`** (previous user/assistant turns) for
follow-ups to work. The server keeps the last 60 turns and clips long ones;
it no longer rejects them.

## Extending

* New metric or dimension: add it to `METRICS` / `GROUPS` and `_GROUP_SQL` in
  `planner.py`, add words to the lexicon, add a renderer line in `answer.py`,
  and add a golden test.
* New app spellings: `APP_ALIASES` in `context.py`. App names themselves come
  from the loaded file.
* Every failed real conversation should become a test in
  `ScreenshotRegressionTests` or `GoldenQuestionTests`.
