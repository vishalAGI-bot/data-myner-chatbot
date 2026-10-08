# DataMyner Intelligence: chatbot (backend + frontend)

```
datamyner/
├── backend/            Python (FastAPI)
│   ├── analyst/        Data analyst: every database table      -> port 8200   (main chatbot)
│   │   └── knowledge/  what each table / metric means (OKF Markdown, editable)
│   ├── journey_agent/  Journey file agent: an uploaded JSON    -> port 8100
│   └── data/           your exports (not included in the zip; never commit)
├── frontend/           React + TypeScript (Vite) -> port 5173
├── start.sh            start everything (macOS / Linux)
└── start.ps1           start everything (Windows PowerShell)
```

## 1. Requirements

* Python 3.10+
* Node.js 18+ (20 or 22 recommended)
* Ollama with `qwen2.5-coder:7b`, or an OpenAI API key for `gpt-6-luna` (both optional). The 24 built-in analyses and the
  journey file questions work without it. It is needed for custom questions
  (model-written SQL), deep-mode planning and the written summaries.
* Access to `datamyner_db` (SSH + database credentials in `backend/.env`), or
  the JSON export for offline mode.

## 2. Install (once)

```bash
# backend
cd backend
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # optional settings

# frontend
cd ../frontend
npm install
```

Then:
1. Start the app (section 3), open http://localhost:5173 and click **Connect live database** in the
   sidebar. Fill in the SSH tunnel and database login, press **Test connection**, then **Save & connect**.
   The chatbot switches to your live PostgreSQL at once, no restart. (Or put the same values in
   `backend/.env`, see `backend/analyst/README.md`.)
2. Recommended: run `backend/analyst/sql/readonly_role.sql` once as a DB admin and
   use that read-only login.
3. Check the connection: `cd backend && python -m analyst.tools.check`
4. For offline mode / tests: unzip your export to `backend/data/public/` and copy
   the earlier `data/` files (cross-app-journey/, cross_app_journey_event.json, ...).

## 3. Run

One command:

```bash
./start.sh                         # Windows: powershell -ExecutionPolicy Bypass -File start.ps1
```

Or in three terminals:

```bash
cd backend && uvicorn analyst.api:app --port 8200           # data analyst (main)
cd backend && uvicorn journey_agent.api:app --port 8100     # journey file agent
cd frontend && npm run dev                                  # UI
```

Open http://localhost:5173. The **Data analyst** is connected to your database; the
**Journey file agent** answers questions about an uploaded journey JSON. To load a file automatically at start-up, set
`JOURNEY_PRELOAD=1` and `JOURNEY_JSON=path/to/file.json` for the journey backend.

## 4. Two pages

| URL | For | Shows |
|---|---|---|
| `/` | whoever runs it | everything below: both agents, the data source, connection settings, thinking depth, and the SQL behind every answer |
| `/ask` | everyone else | the question, the answer, the figures and a download - and, under "How this was calculated", what the question was understood as and which app/period it was applied to |

`/ask` opens with a 10-second **DataMyner Intelligence** screen. It is not only a delay: opening the
SSH tunnel, connecting to PostgreSQL and reading the catalogue genuinely take seconds, and the screen
reports each of those steps as it happens. It waits for whichever finishes last - the animation never
cuts off a connection still being made, and a connection that finishes early still gets its full
introduction - and gives up after 25 s so a database that cannot be reached falls through to the
chat's own message rather than spinning forever. Change `MIN_MS` in `src/components/SplashScreen.tsx`
to make it shorter. The working page at `/` has no splash and opens at once.

`/ask` never names the language model, the provider, the agent, the tables or the SQL, has no sidebar
and no settings, and always answers in plain language. Hand that link out; there is no control on the
page that leads back to `/`. Both pages are the same app and the same API, so nothing extra to deploy -
the SPA fallback (`vite dev`, `vite preview`, and the nginx `try_files $uri /index.html` line in
section 5) already serves it.

## 4a. What the UI does

* **Data analyst**: decides which table(s) answer each question (journeys,
  purchase windows, sessions, users, apps, categories, products, screen events)
  and shows which tables and which query it used.
* **Data source card**: live / snapshot, tables, analyses, apps, date range, and
  how many tables are blocked for security; "Show tables" lists row counts.
* **Journey file agent**: upload a journey JSON / CSV and ask about it.
* **Simple meaning**: a short plain-language intro above the exact figures.
  The intro may not contain numbers, so it cannot change them.
* **Rich answers**: headings, lists and emphasis render as formatted text, and Markdown data tables become
  responsive tables. The chatbot is restricted to questions about the available data and its analysis;
  unrelated content, code and project-source requests are refused.
* **Thinking depth**: Auto / Fast / Deep plus **Max thinking time** (10 s to 20 min,
  60 s by default, enforced by the server). The chosen limit is also the time the
  database query itself is given, so a heavy question can be allowed to finish instead
  of being stopped at the default query timeout. While thinking, a bubble shows each step live, with a
  timer and a progress bar. The send button turns into **Stop**, which returns
  the best verified answer at once.
* **How this was calculated** under every answer: what the question was
  understood as, the thinking steps, the SQL, and the result table.
* Follow-ups ("this user", "the second user", "what about Flipkart?", "why is it lower?") work because each chat
  is one session: the UI sends the history and the previous answers' results back, and a question that refers to
  an earlier turn is rewritten into a standalone one first. The rewritten question is shown as "Read as: ...".
  **New chat** clears that memory.
* **Settings page** (sidebar → Connect live database / Connection settings): choose live PostgreSQL or the
  offline snapshot; SSH tunnel (password or key file, optional host-key pinning); database login, schema
  and SSL mode; "we/our" app, purchase window, time zone, query time limit, row limit; language model
  provider, address/name or write-only OpenAI API key (with **Test model**). The OpenAI integration uses
  the Responses API with storage disabled. **Test connection** runs real queries and reports the server
  version, login, read-only status, tables and analyses found, apps and date range, plus warnings (e.g.
  the login can write). **Save & connect** switches the running chatbot; questions already running
  finish on the old connection. Passwords are never sent back to the browser.
* **Live status**: every 15 s the UI pings the database; the sidebar shows "Live database · 12 ms" and
  the Settings page shows since when it is connected and the latest data date. On a live database, new
  apps, categories and the latest date are re-read every 5 minutes, so "today" and new apps just work.
* Works on phones (the sidebar becomes a slide-out menu).

## 5. Production build

```bash
cd frontend && npm run build        # static files in frontend/dist
```

Serve `frontend/dist` from any web server and forward the two API paths:

```nginx
location /analyst-api/  { proxy_pass http://127.0.0.1:8200/; proxy_buffering off;    # buffering off: live thinking steps
                          proxy_set_header X-Forwarded-For $remote_addr; }        # needed by the Settings page check
location /journey-api/  { proxy_pass http://127.0.0.1:8100/; proxy_buffering off; }
location /              { root /srv/datamyner/frontend/dist; try_files $uri /index.html; }
```

If the APIs are on another domain instead, build with `VITE_ANALYST_API=...` and
`VITE_JOURNEY_API=...` and set `CORS_ORIGINS` on both backends.
`npm run preview` serves the build locally with the same proxy as `npm run dev`.

## 6. Tests

```bash
cd backend && pip install -r requirements-dev.txt        # once: pytest, test client, embedded PostgreSQL
python -m pytest tests/test_analyst.py tests/test_journey_agent.py tests/test_settings.py -q   # 116 tests
cd frontend && npm run typecheck
```

The backend tests check answers against numbers calculated separately from the
raw JSON. See `backend/journey_agent/README.md` for the design (query planner,
grounding checks, thinking budget, SQL sandbox).

## 7. Security checklist

* The chatbot only reads: read-only database session + statement timeout + a SQL guard that
  blocks secret tables (auth, tokens, API keys, sessions) and personal columns (name, phone,
  email, address, password, device ids). Use the read-only role in `analyst/sql/readonly_role.sql`.
* Keep passwords in `backend/.env` or the Settings page only (never in code). The Settings page stores them in
  `backend/config/analyst_settings.json` (file mode 600, git-ignored) and never sends them back to the browser.
  Prefer an SSH key over an SSH password, and pin the SSH host key (one click after the first connection).
* OpenAI API keys follow the same write-only handling. When OpenAI is selected, question context and relevant
  database metadata/results are sent to the OpenAI API; use Ollama when data must remain entirely local.
* The Settings page works only on the server itself (http://localhost:5173). To use it from another computer
  set `ANALYST_ADMIN_TOKEN` in `backend/.env` (e.g. `openssl rand -hex 24`) and enter it on the page; behind
  nginx keep the `X-Forwarded-For` line above so remote visitors are recognised as remote.
* The JSON export you shared contains an analytics API token, password hashes and users' personal
  data. Rotate that token, and keep exports out of git and chat tools (`backend/.gitignore` excludes `data/`).
* Do not expose ports 8100/8200 to the internet directly; put them behind your web server with login.

The old taxonomy "Data explorer" (`backend/api.py`, port 8000) is superseded by the Data analyst
and no longer shown in the UI; its code is kept unchanged.
