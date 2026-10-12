# Data analyst: one chatbot over every table

Ask a business question in plain English. The analyst works out which table(s) hold the answer, runs a
verified query on your PostgreSQL database (read-only), and answers from the returned rows.

```
question ─► router ─► vetted metric? ─ yes ─► parameterised SQL ─┐
                │                                                 ├─► guard ─► PostgreSQL (read-only) ─► answer from rows
                └─ no ─► model picks metric or tables ─► model SQL ┘            (repair up to 2x on errors)
```

## 1. What the data means: `knowledge/` (OKF bundle)

The chatbot's understanding of your database lives in Markdown files, not in code, so your team can
review and correct it. Each file follows the Open Knowledge Format (YAML header + Markdown body).

| Folder / file | What it holds |
|---|---|
| `tables/<table>.md` | meaning, grain, joins, notes, example questions, and every column with a **policy** |
| `metrics/<name>.md` | a vetted analysis: description, tags, parameters, and the exact SQL (`# Computation`) |
| `glossary.md` | business terms: converted, retained, leaked, purchase share, window, "we/our", tier... |
| `policy.md` | tables that must never be queried (auth, tokens, sessions, API keys, admin logs) |

My reading of the tables (please confirm the items marked *inferred*):

| Table | One row per | Used for |
|---|---|---|
| `journey_window` | journey × window (24/48/72 h) | conversion, **retention** (bought on first app, *inferred*), **leakage** (bought elsewhere, *inferred*), purchase app, time to purchase, order value, category |
| `cross_app_journey` | product-hunt journey | journeys, users, multi-app share, outcome |
| `cross_app_journey_event` | search inside a journey (ordered) | app switches (Amazon → Meesho), query before a switch, top searches |
| `session_master` | app session | **active users per app** (who visited, who used only that app, last N hours or dates), session time, screens, order confirmation, competitor switch within 5 min |
| `sub_session_master` | sub-app session | Amazon Now / Flipkart Minutes sessions |
| `testing_accessibility_infos` | screen event (very large) | page types, add-to-cart, day of week |
| `user_master` | panel user | city, state, zone, gender, age group, device, **loyalty** tier (not city tier) |
| `app_master`, `category_master`, `product_master` | app / taxonomy node / product | names, taxonomy, brands |
| `llm_call_log`, `pipeline_*`, `import_job` | pipeline operations | tokens, calls, errors |
| `order`, `order_product`, `user_journey`, `accessibility_searches_done` | (empty in the export) | columns are read from the live database; documented as *draft* |

Column policies: `ok` · `aggregate_only` (e.g. date_of_birth: usable for age groups, never listed) ·
`heavy` (raw text/JSON/vectors: filter only) · `blocked` (password, name, phone, email, address, device ids, hashes).

### Add or change an analysis
1. Copy a file in `metrics/`, edit the header (`title`, `description`, `tags`, `tables`, `parameters`) and the SQL.
   Optional filters go inside `[[ ... :param ... ]]` and are dropped when the parameter is not given.
2. `POST /api/reload` (or restart). 3. Add a question to `tests/test_analyst.py`.

## 2. How a question is answered

**With OpenAI selected, OpenAI reads every question first, live, through a tool call** (`understanding.py`).
One forced call of the `understand_question` tool gets the latest message, the conversation and live facts from
the database (data coverage, apps and sub-apps, category list, vetted analyses, tables, business terms) and returns:
the question rephrased so it stands on its own (shown as "Understood as" and in the thinking steps), its kind
(analysis, several questions, brief, clarify, unsupported, out of scope), who "we" is, the period as dates, the
apps, categories, splits and minimum group size, the shape asked for (one row per day, per app, per user), and the
vetted analysis or tables that answer it. Everything below follows that reading; no keyword list or regular
expression interprets the question on this path. Code only checks the reading against the live data (names
must exist, dates must be real and inside the coverage) and still applies the out-of-scope safety filter. If
OpenAI cannot be reached, the question is not answered - there is no fallback to rules. Steps 1-5 below are the
path for the local model (Ollama).

**Many questions in one message** (`parts.py`):
- OpenAI lists every question in the message (up to 12; more are counted and reported, never dropped), each with
  a status - answer now, needs the user's input, not in the data, outside scope - its own vetted analysis or
  tables, and the earlier questions whose RESULT it needs.
- Answerable questions run in waves: independent ones side by side (at most `ANALYST_PART_CONCURRENCY`, default
  4, per request, so one long message cannot starve other users), a question that needs an earlier answer after
  it, rewritten with that answer's actual values first. A question whose prerequisite has no answer says so.
- The answer opens with "Your message has N questions: 3 answered, 1 needs your input, ..." and gives every
  question its own numbered section: a direct one-sentence answer checked against its rows, the figures, and
  OpenAI's check of both; or the reason it has no answer. A brief can be one of the questions.
- Messages up to 12,000 characters; a longer one gets a plain sentence back, not a schema error.

**Long requests, clarification and follow-ups** (same tool call, no extra request):
- The reading also breaks the request down - main goal, individual questions, constraints, expected output -
  shown as "Your request" under "How this was calculated". Every clear question gets its own numbered answer.
- A long message is never by itself a reason to ask. A part is marked unclear only when an ambiguity would
  change its answer and neither the conversation nor the defaults (whole coverage, 24-hour window, "we" = the
  asker's company) settle it. Each unclear part gets one focused question, why it matters, and 2-4 options from
  the user's words and the live data; the UI adds "Other - please specify". At most 3 questions.
- Nothing answerable without a reply: ask first, run nothing. Only some parts unclear: answer the clear parts,
  then "Still open" and the questions. A brief also asks about category names it had to leave out.
- The questions asked are kept in the conversation state, so the reply ("For "Which campaign?": Diwali week")
  is applied to the original request.
- After a complete answer, 2-3 optional follow-up questions specific to the question and the answer (one short
  OpenAI call); a suggestion with a figure the answer does not contain is dropped. None while a clarification is
  open. The UI shows "Clarification needed" (answer required) and "Suggested follow-up questions" (optional)
  as separate boxes.

1. **Router (no model).** Scores the question against every metric (tags, titles, example questions) and pulls
   out apps (longest name first: "Amazon Now" ≠ "Amazon"; "we/our" = `ANALYST_HOME_APP`), 24/48/72 h windows,
   dates ("this week", "last 7 days", "13 Sep"), recent periods ("last 24 hours", counted back from the newest
   data because the data can lag the calendar), categories, dimensions (city, device, time of day...), "top N",
   and "Amazon → Meesho" paths. Comparisons ("vs Meesho and Flipkart") show every app and highlight the named ones.
2. **Model (only when needed).** Picks a metric + parameters or the tables for a custom query; its choice is
   validated against the catalog. Custom SQL is written from the chosen tables' documentation only.
3. **Guard** (`guard.py`) parses every statement with sqlglot and allows only a single SELECT on documented tables,
   never a `blocked` column, never `SELECT *`, no admin functions, and always a LIMIT. The database session is
   read-only with a statement timeout as a second, independent layer.

   In Deep mode a model **reviewer** also reads the query before it runs. That verdict is an opinion, not a rule:
   it is kept separate from the guard (`_ReviewRejected`, not `GuardError`), because conflating them meant a
   debatable objection blocked as hard as a blocked column and the user got no answer at all after ~36 s. When no
   candidate satisfies the reviewer, the last query the guard approved is run anyway and its rows are returned
   under "**Check before relying on this**" with the objection quoted. The guard and the database stay absolute.
4. **Answer.** Every answer is written by the model from the rows the query just returned - vetted analyses
   included, which used to render from a fixed template and so read the same every time. Nothing is cached: each
   question re-runs its SQL against the live database, and the wording is generated again, so two identical
   questions give the same figures in different words. The model only words the result: every number in what it
   writes must appear in that run's rows or the text is dropped and the table stands alone. A comparison question
   gets a verdict written from computed comparison facts instead. "Simple meaning" adds a plain-language intro
   that may not contain numbers. With the model unreachable the rendered table is still returned, immediately.
5. **Follow-ups (one chat = one session).** Before anything else runs, a message that leans on earlier turns is
   rewritten into a question that stands on its own, and everything downstream sees only that. `conversation.py`
   keeps the last 3 turns in the `state` the UI echoes back - the question, the analysis used, its parameters,
   and the first rows of the result - so "what about just the last 24 hours?" keeps the analysis and changes the
   period, and "why is the second one lower?" resolves the ordinal against the rows of the *previous* answer.
   The step runs on **every** turn that has history behind it: trying to guess in advance which messages are
   follow-ups kept missing ones with no pronoun at all ("what is the result with 24 hr window?"), and a message
   that already stands alone comes back unchanged. A resolved question is shown as "Read as: ..." under the
   answer and as a thinking step, so a wrong reading is visible.

   A vetted analysis is never used when it cannot produce the shape asked for: a per-period or per-app
   breakdown it does not return, a list of individual users (no metric is per user except `top_users_by_visits`),
   or a request to **add a column** to the previous result - a vetted analysis has fixed columns, so extending one
   is always a custom query.

   A follow-up also keeps the **approach** of the turn it follows: when the previous answer came from a custom
   query, changing one setting re-runs that query with the change applied, instead of being keyword-matched to an
   unrelated vetted analysis. Resolving up front is what makes all of this work in Deep mode too, which
   previously received no conversation state at all.
6. **Deep mode accuracy**: a single simple question is answered straight from its query (no rewritten summary). Parts the
   model calls "not in the data" are re-checked against the vetted analyses before the chatbot says so, and the
   final summary is written from every result row, not a shortened list.
7. **Deep mode** (auto for multi-part questions): split into parts, answer and verify each, and list the parts' answers, all within
   the chosen thinking limit (60 s by default, up to 20 min); Stop returns the verified parts immediately.
   Deep planning uses OpenAI. With Ollama, **Auto** answers a multi-part question (lines, bullets or several "?")
   one part at a time, each as its own Fast question; choosing **Deep** explicitly still asks for OpenAI.
   Deep parts run side by side. When time runs out, finished parts are kept, and a query the guard approved but the
   reviewer questioned is shown flagged instead of being dropped. An OpenAI reply cut off at its output limit is
   asked for again with more room (it used to surface as "could not be parsed" or "no valid plan").
8. **Executive-summary briefs** (`report.py`, Auto or Deep): "I'm Flipkart's insights lead - how did we do yesterday
   in beauty and smartphones by category, age group and gender; what went well, where did we stumble, where did
   competitors do better; groups of at least 30 people". One model-written query for all of that was slow, got cut
   off and failed review. A brief is answered in four steps instead:
   - **Scope.** Who "we" is (the app the asker says they work for; kept for the rest of the chat, never changing
     `ANALYST_HOME_APP`), the period (router dates first), the minimum group size (default 30 distinct users), the
     breakdowns (crossed ones such as category × gender for "interlocking") and the categories. OpenAI maps the
     asker's category words to names in `category_master`; only names that exist are used, a name with no match is
     reported as left out (with the closest names), and city tier is reported as not recorded.
   - **Exact queries.** The vetted `segment_scorecard` metric runs once per breakdown, all at once: users, journeys,
     conversion, retention, leakage and purchase share per segment and app, groups under the minimum dropped.
   - **Computed findings.** Where we lead, where a competitor converts better (a sub-app such as Flipkart Minutes is
     never our competitor) and our own weak segments (vs the rest of our journeys), each numbered F1, F2 ... with its
     counts ("3 of 80 journeys"). Like for like: within a category every competitor is compared; across categories
     the benchmark is the biggest competitor, because a grocery app converts more often than a fashion app whatever
     either does well. A gap is reported only when Fisher's exact test says it is unlikely to be chance, with the
     false-discovery rate held at 5% across the whole brief (Benjamini-Hochberg); the rest are counted, not shown.
     Catch-all groups (Uncategorised, Unknown, Others) never become findings.
   - **Measures asked for.** The brief says what it measured and how: users and journeys, purchasers, conversion,
     retention, cross-app switching as purchases made on another app. Visitors and switching to a competitor app
     within 5 minutes come from sessions (dated by the session start in session_id, for all categories, since
     sessions carry no category) and are shown under "Visitors and app switching".
   - **Actions when nothing is significant.** The requested number of actions (default 3, at most 5) still comes,
     built on the largest gaps that did not pass the test (labelled "not statistically reliable") and on data
     caveats, under a heading that calls them tests to run, never proven findings.
   - **Categories as paths.** A name such as "Others" recurs under several master categories, so asked categories
     are matched as "Master > Category" paths, a shared name is tied to its sibling names' master (or left out), and
     a journey's sub-category with more than one parent is not attributed to either.
   - **Checked summary.** OpenAI writes went well / stumbled / competitors did better / actions. Every sentence must
     cite findings and repeat only their numbers, apps and segments, or it is dropped. Without a model, the strongest
     computed findings are the summary. Each breakdown's table and chart is a part under "How this was calculated".

## 3. Connect to your database

**Easiest: the Settings page.** In the UI click **Connect live database** (sidebar). Enter the SSH tunnel
(server, port, username, password or key file) and the database login, press **Test connection**, then
**Save & connect**. The running chatbot switches without a restart; questions already running finish on the
old connection. What happens underneath:

| Endpoint | What it does |
|---|---|
| `GET /api/settings` | current values; passwords are replaced by "saved: yes/no" |
| `POST /api/settings/test` | opens the SSH tunnel and a read-only session, reads the schema and reference values, and reports server version, login, read-only status, tables/analyses found, apps, date range, SSH host key and warnings. Nothing is saved. At most 60 s. |
| `PUT /api/settings` | the same test; if it passes, saves and switches (`force: true` saves even when the database is down) |
| `DELETE /api/settings` | forget the saved values and use `.env` again |
| `POST /api/llm/test` | checks an Ollama address and model |

Saved values go to `backend/config/analyst_settings.json` (mode 600, git-ignored); only values that differ from
`.env` are stored, and they override `.env`. An empty password field keeps the saved password.

**Who can change it:** only requests from the server itself (loopback address, local Host header, no remote
`X-Forwarded-For`, and no foreign `Origin`, which blocks other websites in your browser). Set
`ANALYST_ADMIN_TOKEN` to allow it from other computers with the token (sent as `X-Admin-Token`).

**Or `backend/.env`:**

```ini
ANALYST_SOURCE=postgres
SSH_HOST=<ssh server>          # leave empty to connect directly
SSH_USERNAME=<user>
SSH_PASSWORD=<password>        # or SSH_PKEY=/path/to/key (preferred)
SSH_HOST_KEY_SHA256=           # optional pin, shown after the first connection
DB_HOST=127.0.0.1              # as seen from the SSH server
DB_PORT=5432
DB_NAME=datamyner_db
DB_USERNAME=datamyner_chatbot  # a read-only role (see below)
DB_PASSWORD=<password>
ANALYST_HOME_APP=Amazon
ANALYST_TIMEZONE=Asia/Kolkata
ANALYST_REFRESH_SECONDS=300    # re-read apps, categories and the latest date (live data)
```

Then check it: `python -m analyst.tools.check` (lists the tables it can see, row counts, and runs two analyses;
uses the Settings page values too).

**Query time limits:** `ANALYST_STATEMENT_TIMEOUT_MS` (default 15 s) is the statement timeout for
queries with no deadline of their own - schema probes, row counts, health pings. A question from the
chat passes its remaining thinking budget instead, so "Max thinking time" in the sidebar is what
actually bounds a heavy analytical query (hard ceiling: 20 min, `sources.MAX_QUERY_TIMEOUT_MS`).

**Real time:** every question runs its query on the live database at that moment, so answers include rows
written a second ago. There is no result cache anywhere, and the API sends `Cache-Control: no-store` so a browser
cannot reuse an earlier response either. The one thing on a timer is the reference data - app names, categories,
row counts and the latest data date - because re-reading it costs several queries; `ANALYST_REFRESH_SECONDS`
(default 300) controls that, and lowering it makes "today" and new apps appear sooner at the cost of those
queries. It never affects the figures in an answer, only the lists used to interpret the question. Apps, categories, row counts and the latest date (used for "today", "this week") are
re-read in the background every `ANALYST_REFRESH_SECONDS`. `GET /api/health` pings the database
(`db_ping_ms`); the UI polls it every 15 s and shows "Live database · 12 ms". A dropped SSH tunnel or
connection is rebuilt on the next question.

The SSH tunnel (`ssh_tunnel.py`) is built on paramiko directly. The `sshtunnel` package is not used because
its last release breaks with paramiko 4+. Host keys in `~/.ssh/known_hosts` are enforced; set the pin to
enforce the key for other hosts too.

**Strongly recommended:** run `sql/readonly_role.sql` once as an admin and use that role. The database itself then
refuses writes and hides personal columns, whatever the application does. The connection test warns when the
login is a superuser or can write.

Offline mode (no database): `ANALYST_SOURCE=snapshot` and `ANALYST_SNAPSHOT_DIR=data/public` (your JSON export).
Secret tables in the export are skipped and never loaded.

## 4. Run and test

```bash
uvicorn analyst.api:app --port 8200
python -m pytest tests/test_analyst.py tests/test_settings.py -q   # 78 tests; real PostgreSQL via `pgserver`
                                                                  # and a real SSH server (paramiko) for the tunnel
```

The tests compute expected answers in plain Python from the fixture data, run every metric on PostgreSQL and
DuckDB and require identical results, and attack the guard with forbidden queries.
