"""The chatbot itself: routes a question, gathers evidence, phrases an answer."""

from dataclasses import dataclass, field

import re

import config
import db
import llm
import router
import agents as agent_specs
from agent import Agent
from search import TaxonomySearch, content_terms

SQL_SYSTEM = """You are a SQLite expert. Write ONE SELECT query. Nothing else.

{schema_card}

Rules:
- Output raw SQL only. No markdown fences, no ```sql, no explanation, no
  comments, no trailing semicolon. Your entire reply must be the query.
- SELECT or WITH only. Never INSERT, UPDATE, DELETE, DROP, CREATE or PRAGMA.
- "how many X" about a level means COUNT(DISTINCT x_category), because the same
  master_category appears on thousands of rows. Plain COUNT(*) is wrong there.
- USE LIKE, NOT =. The user types fragments: they say "dairy" when the value
  is "Dairy & Eggs", "baby" when it is "Baby & Infant". Write
  LOWER(col) LIKE LOWER('%dairy%'). Only use = when you are copying a value
  exactly as it appears in the list above.
- Search the level the user names, but if their word is not in that level's
  values, search all three columns with OR rather than returning nothing.
- Pick the right table. taxonomy = the category tree, one row per triple.
  app = the shopping apps; sub-apps like 'Amazon Now' carry app_brand 'Amazon'.
  search_event = what users actually searched for, one row per search.
  query_category = the bridge; JOIN it when a question mixes searches with
  categories ("which categories do people search for most").
- "how many searches" means COUNT(*) on search_event. "how many categories"
  means COUNT(DISTINCT ...) on taxonomy. Do not mix them up.
- NEVER write SELECT *. search_event has 20 columns and dumping them all is
  unreadable. Name the few columns the question needs. For a journey that is
  step, app_name, normalized_query, hours_since_first - nothing else.
- On search_event always match normalized_query, never query, and always with
  LIKE. Order and compare dates with search_utc, or use search_date /
  search_hour / search_dow which are already split out.
- Add a LIMIT when listing rows (default 200).
- If a previous attempt failed, fix it using the error message given.

Examples. <TERM> stands for whatever the user typed - never output the word
<TERM> itself, and never reuse a value from these examples in an answer:

Q: how many master categories are there
A: SELECT COUNT(DISTINCT master_category) AS n FROM taxonomy

Q: how many <TERM> in sub category
A: SELECT sub_category, COUNT(DISTINCT sub_category) AS n FROM taxonomy WHERE LOWER(sub_category) LIKE LOWER('%<TERM>%') GROUP BY 1

Q: what is under <TERM>
A: SELECT DISTINCT sub_category FROM taxonomy WHERE LOWER(category) LIKE LOWER('%<TERM>%') LIMIT 200

Q: how many searches on <APP>
A: SELECT COUNT(*) AS n FROM search_event WHERE LOWER(app_name) LIKE LOWER('%<APP>%')

Q: which categories do people search for most
A: SELECT l.category, COUNT(*) AS n FROM search_event e JOIN query_category l ON e.normalized_query = l.normalized_query GROUP BY 1 ORDER BY n DESC LIMIT 20

Q: which journeys crossed more than one website
A: SELECT journey_assoc_id, COUNT(DISTINCT app_brand) AS brands FROM search_event GROUP BY 1 HAVING brands > 1 ORDER BY brands DESC LIMIT 50

Q: how many searches on <BRAND>
A: SELECT COUNT(*) AS n FROM search_event WHERE LOWER(app_brand) LIKE LOWER('%<BRAND>%')

A question with no search term - "how many categories are there", "list all
master categories" - takes NO WHERE clause at all. Never invent a filter."""

ANSWER_SYSTEM = """You answer questions about a product-category taxonomy and about cross-app
product search events.

{schema_card}

You are given the evidence retrieved for this question. Rules:
- Answer only from the evidence. Never invent a category name or a number.
- Every value name you write must appear character-for-character in the
  evidence. If the evidence says one thing and you remember another, the
  evidence wins. Do not add words to a label to make it read better.
- If the evidence lists "Closest values that DO exist", the user's wording did
  not match a label exactly. Name the real value from that list and answer for
  it. Never suggest anything that is not in the evidence.
- Be short and direct. Use a compact list when there are several values.
- Give the exact number when the user asked "how many".
- Do not show the SQL, do not explain your reasoning, do not add a preamble
  such as "Based on the evidence". Just state the answer.
- NEVER paste the evidence table back to the user. The evidence is raw data
  for you to read; the answer is a sentence, or a short list of the values
  that matter. Column headers, ids, timestamps and internal fields
  (page_id, user_ref, source_accessibility_id, search_utc) must not appear in
  your answer unless the user asked for them by name.
- For a journey, describe it as a story: how many steps, which apps in order,
  what was searched, and how long it took. For example: Journey 2 had 3
  searches over about 50 seconds - centrum men on Amazon, the same query
  again on Amazon Now, then centrum vitamin."""

CHAT_SYSTEM = """You are a friendly assistant for two datasets: a product-category taxonomy,
and a log of product searches users made across shopping websites. Answer
briefly. If the user seems to want data, tell them what kinds of questions you
can answer, using this summary:

{schema_card}"""


@dataclass
class Turn:
    question: str
    answer: str
    path: str = ""
    sql: str = ""
    evidence: str = ""
    steps: list = field(default_factory=list)   # agent path only
    domain: str = ""                            # which agent answered
    stopped_early: bool = False


@dataclass
class Chatbot:
    conn: object = None
    schema_card: str = ""
    searcher: TaxonomySearch = None
    history: list = field(default_factory=list)
    agents: dict = field(default_factory=dict)   # domain -> Agent
    cards: dict = field(default_factory=dict)    # domain -> schema card
    app_names: tuple = ()

    def __post_init__(self):
        self.conn = self.conn or db.connect()
        self.schema_card = self.schema_card or db.build_schema_card(self.conn)
        if self.searcher is None:
            rows = (db.all_sub_categories(self.conn)
                    if db.has_table(self.conn, config.TABLE) else [])
            self.searcher = TaxonomySearch(rows)
        # One agent per domain, each seeing only its own tables and tools.
        for name, spec in agent_specs.SPECS.items():
            self.cards[name] = db.card_for(self.conn, spec["tables"])
            self.agents[name] = Agent(self.conn, self.searcher,
                                      self.cards[name], spec=spec)

        if db.has_table(self.conn, config.APPS_TABLE):
            self.app_names = tuple(r[0] for r in self.conn.execute(
                f"SELECT DISTINCT app_name FROM {config.APPS_TABLE}"
            )) + tuple(r[0] for r in self.conn.execute(
                f"SELECT DISTINCT app_brand FROM {config.APPS_TABLE}"))

    def domain_for(self, question: str) -> str:
        """Which specialist should handle this question."""
        names_category = bool(self.searcher.rows
                              and self.searcher.search_values(question))
        return router.pick_domain(question, self.app_names, names_category)

    # ---------- paths ----------

    def _value_hint(self, question: str) -> str:
        """Tell the model which real values resemble the question.

        This is the single biggest accuracy win on the SQL path: the model
        cannot match a value it has never seen, and a fragment like "dairy"
        looks nothing like "Dairy & Eggs" to a WHERE clause.
        """
        hits = self.searcher.search_values(question)
        if not hits:
            return ""
        lines = "\n".join(f"  {h['value']}  (in column {h['level']})" for h in hits)
        return ("\n\nFYI, these values exist in the data and look related:\n" + lines +
                "\nUse them only if the user is actually asking about one of them. "
                "If the question is a general count or list, ignore this and write "
                "no WHERE clause.")

    def _did_you_mean(self, question: str) -> str:
        """Evidence of last resort: real near matches, not random samples."""
        hits = self.searcher.search_values(question, k=6, threshold=45)
        if not hits:
            return "(no rows, and nothing in the data resembles the question)"
        lines = "\n".join(f"{h['value']} | {h['level']}" for h in hits)
        return ("(the query returned no rows)\n\n"
                "Closest values that DO exist:\nvalue | column\n" + lines)

    def _sql_path(self, question: str, domain: str = "cross") -> tuple[str, str]:
        """Generate SQL and run it.

        Three things go wrong and each gets a retry: the query errors, the
        query is rejected by the guard, or - the quiet one - the query runs
        fine and returns zero rows. That last case used to be reported to the
        user as "not found" even when the data was right there.
        """
        system = SQL_SYSTEM.format(
            schema_card=self.cards.get(domain, self.schema_card))
        base = question + self._value_hint(question)
        prompt, sql, last_error = base, "", None

        for attempt in range(3):
            sql = llm.complete(
                system=system, user=prompt,
                model=config.SQL_MODEL, max_tokens=400,
            )
            try:
                cols, rows = db.run_sql(self.conn, sql)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                prompt = (f"{base}\n\nYour previous query failed.\n"
                          f"Query: {sql}\nError: {last_error}\n"
                          "Write a corrected query.")
                continue

            if not db.looks_empty(rows):
                return sql, db.rows_to_text(cols, rows)

            # Ran cleanly, found nothing. Almost always an over-strict WHERE.
            # db.looks_empty also catches COUNT(*) = 0, which returns a row.
            if attempt < 2:
                prompt = (
                    f"{base}\n\nYour previous query was valid but returned ZERO rows:\n"
                    f"{sql}\n\nThe data does contain relevant values, so the filter is "
                    "too strict. Rewrite it more loosely: replace = with LIKE, use a "
                    "shorter fragment of the search word, drop accents and '&', and "
                    "search master_category, category and sub_category with OR."
                )

        if last_error:
            return sql, f"(query failed: {last_error})"
        return sql, self._did_you_mean(question)

    def _lookup_path(self, question: str) -> tuple[str, str]:
        if not self.searcher.rows:
            return "", ("(no taxonomy loaded - run `python ingest.py` on the "
                        "category file to answer this)")
        hits = self.searcher.search(question)
        evidence = TaxonomySearch.to_text(hits)

        # Widen: also pull every sibling under the best-matching category.
        if hits:
            best = hits[0]
            siblings = self.conn.execute(
                f"SELECT sub_category FROM {config.TABLE} WHERE category = ? "
                "ORDER BY sub_category LIMIT 40",
                (best["category"],),
            ).fetchall()
            evidence += (
                f"\n\nOther sub_categories under '{best['category']}': "
                + ", ".join(r[0] for r in siblings)
            )
        return "", evidence

    # ---------- deterministic fast path ----------

    _COUNT_VERB = re.compile(r"how many|number of|count of|total number|how much")

    def _global_count(self, question: str):
        """Answer "how many X are there" from the database, with no model call.

        These are the most common questions asked of a taxonomy and the ones a
        7B model most often ruins by inventing a WHERE clause. There is nothing
        to reason about: it is one COUNT(DISTINCT). Returns None when the
        question names a specific value, in which case it is a real query.
        """
        q = question.lower()
        if not self._COUNT_VERB.search(q):
            return None
        if content_terms(question):        # names something specific -> real query
            return None

        ev, pr = config.EVENTS_TABLE, config.PRODUCTS_TABLE

        if db.has_table(self.conn, pr):
            if re.search(r"\bbrands?\b", q):
                sql = (f"SELECT COUNT(DISTINCT brand_key) AS n FROM {pr} "
                       "WHERE brand != ''")
                return "brands", self.conn.execute(sql).fetchone()[0], sql
            if re.search(r"\bproducts?\b|\bskus?\b|\bitems?\b", q):
                sql = f"SELECT COUNT(*) AS n FROM {pr}"
                return "products", self.conn.execute(sql).fetchone()[0], sql

        has_events = db.has_table(self.conn, ev)

        # events first: "how many searches" must not be read as a category count
        if has_events:
            if re.search(r"\bsearch(es)?\b|\bquer(y|ies)\b|\bevents?\b", q):
                if re.search(r"distinct|unique|different", q):
                    sql = f"SELECT COUNT(DISTINCT normalized_query) AS n FROM {ev}"
                    label = "distinct search queries"
                else:
                    sql = f"SELECT COUNT(*) AS n FROM {ev}"
                    label = "searches"
                return label, self.conn.execute(sql).fetchone()[0], sql
            for word, col, label in (("journey", "journey_assoc_id", "journeys"),
                                     ("session", "session_assoc_id", "sessions"),
                                     ("user", "user_ref", "users"),
                                     ("app", "app_assoc_id", "apps"),
                                     ("website", "app_assoc_id", "apps")):
                if word in q:
                    sql = f"SELECT COUNT(DISTINCT {col}) AS n FROM {ev}"
                    return label, self.conn.execute(sql).fetchone()[0], sql

        if re.search(r"sub[\s_-]?categor", q):
            level = "sub_category"
        elif "master" in q:
            level = "master_category"
        elif "categor" in q:
            level = "category"
        else:
            return None

        sql = f"SELECT COUNT(DISTINCT {level}) AS n FROM {config.TABLE}"
        n = self.conn.execute(sql).fetchone()[0]
        return level, n, sql

    # ---------- public ----------

    def ask(self, question: str) -> Turn:
        direct = self._global_count(question)
        if direct:
            level, n, sql = direct
            plural = level if level.endswith("s") else f"distinct {level} values"
            turn = Turn(question, f"{n} {plural}.", "sql (direct)", sql,
                        f"n\n{n}", domain=self.domain_for(question))
            self.history.append(turn)
            return turn

        path = router.route(question)
        domain = self.domain_for(question)

        if path == "chat":
            answer = llm.complete(
                system=CHAT_SYSTEM.format(schema_card=self.schema_card),
                user=question,
                model=config.ANSWER_MODEL,
                max_tokens=400,
            )
            turn = Turn(question, answer, path, domain=domain)
            self.history.append(turn)
            return turn

        if path == "agent":
            # The agent writes its own final answer, so it skips the shared
            # answer step below. Its trace becomes the evidence for /debug.
            agent = self.agents[domain]
            result = agent.run(question)
            turn = Turn(question, result.answer, path,
                        evidence=result.transcript, steps=result.steps,
                        domain=agent.label)
            self.history.append(turn)
            return turn

        if path == "lookup":
            sql, evidence = self._lookup_path(question)
        else:
            sql, evidence = self._sql_path(question, domain)

        context = ""
        if self.history:
            prev = self.history[-1]
            context = f"Previous question: {prev.question}\nPrevious answer: {prev.answer}\n\n"

        answer = llm.complete(
            system=ANSWER_SYSTEM.format(schema_card=self.schema_card),
            user=f"{context}Question: {question}\n\nEvidence:\n{evidence}",
            model=config.ANSWER_MODEL,
            max_tokens=900,
        )
        turn = Turn(question, answer, path, sql, evidence, domain=domain)
        self.history.append(turn)
        return turn

    def reset(self):
        self.history.clear()
