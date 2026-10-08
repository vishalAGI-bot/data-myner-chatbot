"""Journey chatbot orchestrator.

Pipeline for every message::

    split UI style wrapper  ->  resolve references ("this user" -> user 5330)
      -> plan  (rules; LLM planner if the rules do not understand every word)
      -> follow-up merge ("of particular user" re-uses the previous question)
      -> execute the plan with parameterised SQL  -> deterministic answer
      -> (fallback) tool-using SQL agent, whose answer must pass a grounding check

The language model is used to *understand* unusual questions and to write SQL
for questions the plan cannot express. It is never trusted to do arithmetic or
to decide which user "this user" is.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Callable

import config
import llm
from output_format import OUT_OF_SCOPE_MESSAGE, is_explicitly_out_of_scope

from . import answer as render
from .context import (AppMatcher, Resolution, previous_user_turns, resolve_references,
                      split_style, substitute_references)
from .planner import (LLMPlanner, QueryPlan, RuleParser, compile_aggregate,
                      compile_journey_list, merge_follow_up)
from .store import JourneyDataError, JourneyStore, QueryResult
from . import thinking as think
from .context import HOME_APP
from .thinking import Budget, ThinkingStep, ThinkingTimeout

log = logging.getLogger("journey_agent")

TOOLS = ("run_sql", "show_journey", "show_user", "search_journeys", "similar_journeys",
         "final_answer")
DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "tool": {"type": "string", "enum": list(TOOLS)},
        "sql": {"type": "string"},
        "journey_id": {"type": "string"},
        "user_assoc_id": {"type": "string"},
        "keyword": {"type": "string"},
        "limit": {"type": "string"},
        "answer": {"type": "string"},
    },
    "required": ["thought", "tool"],
}

AGENT_SYSTEM = """You are the journey-data analyst. Your only source is the journey file below.
Never answer from general knowledge. Never do arithmetic in your head: compute every
number with SQL and copy numbers exactly from tool results.

{schema_card}

Tools (reply with ONE JSON object per turn):
- run_sql {{"sql": "..."}}: one read-only SELECT/WITH on journey / journey_app.
- show_journey {{"journey_id": "J000002"}}
- show_user {{"user_assoc_id": "5330"}}: a user's totals, apps and every journey.
- search_journeys {{"keyword": "...", "limit": "20"}}: journeys whose search phrase contains the words.
- similar_journeys {{"journey_id": "...", "limit": "8"}}
- final_answer {{"answer": "..."}}: plain-language answer built only from tool results.

Rules:
- Refuse requests for code, project/source files, creative content or unrelated general knowledge.
- Use the resolved entities given with the question. "this user" has already been resolved.
- journeys and searches are different things. Label every number with its unit.
- A result marked [PARTIAL] is not complete; say "top N" or query the full set.
- If the data cannot answer, say which information is missing. Do not guess.
- Do not reveal journey_hash. Do not repeat an identical tool call.
- You have at most {max_steps} tool steps.
"""

SIMPLE_SYSTEM = """You explain a data answer to a non-technical shop owner.
Write one to three short sentences saying what the answer below MEANS in real life
(a user is a shopper, a journey is one product hunt, searches are what they typed,
apps are the shopping apps, "open" means they have not finished, "timed out" means
they gave up).
Strict rules:
- Do NOT write any number, digit, count, date, user id or journey id. Not even as words.
  The exact figures are shown to the reader right below your text.
- Do not add facts that are not in the answer. No exclamation marks, no "wow".
Example: for "207 users are tied for the fewest journeys: 1 journey each" write
"Many shoppers came only once: each of them did a single product hunt and did not come back
for another one in this period."
"""

MAX_OBSERVATION_CHARS = 3500
MAX_HISTORY_TURNS = 8             # turns shown to the model
HISTORY_WINDOW = 60               # turns scanned for references (cheap, no model)
MAX_HISTORY_CHARS_PER_TURN = 400


@dataclass
class JourneyStep:
    thought: str
    tool: str
    arguments: dict = field(default_factory=dict)
    observation: str = ""
    error: bool = False


@dataclass
class JourneyAnswer:
    answer: str
    steps: list[JourneyStep] = field(default_factory=list)
    stopped_early: bool = False
    route: str = "rules"                    # rules | llm_plan | agent | clarify | unsupported | out_of_scope | ...
    resolved_question: str = ""
    plan: dict | None = None
    sql: str | None = None
    table: dict | None = None
    notes: list[str] = field(default_factory=list)
    grounded: bool = True
    latency_ms: int = 0
    depth: str = "fast"                     # fast | deep  (what actually ran)
    thinking: list[dict] = field(default_factory=list)   # visible reasoning trace
    parts: list[dict] = field(default_factory=list)      # deep mode: sub-questions + answers
    verified: bool = True                   # every number traced to query results
    timed_out: bool = False
    suggested_questions: list[str] = field(default_factory=list)


@dataclass
class _Run:
    """Per-request state (the agent object is shared across requests)."""
    budget: Budget
    trace: list[ThinkingStep] = field(default_factory=list)
    on_event: Callable[[dict], None] | None = None


_RUN: contextvars.ContextVar[_Run | None] = contextvars.ContextVar("journey_run", default=None)
# Model calls run here so a Stop press or the deadline can abandon a blocking call.
_LLM_POOL = ThreadPoolExecutor(max_workers=int(__import__("os").getenv("JOURNEY_LLM_WORKERS", 8)),
                               thread_name_prefix="journey-llm")


class JourneyAgent:
    """Natural-language agent scoped exclusively to one JourneyStore."""

    def __init__(self, store: JourneyStore | None = None, max_steps: int | None = None,
                 use_llm: bool = True):
        self.store = store or JourneyStore()
        self.max_steps = max_steps or config.AGENT_MAX_STEPS
        self.use_llm = use_llm
        summary = self.store.summary()
        self.apps = AppMatcher(summary["apps"])
        year = int(str(summary.get("first_date") or "2026")[:4] or 2026)
        self.rules = RuleParser(self.apps, data_year=year)
        self.llm_planner = LLMPlanner(lambda **kw: self._llm(**kw), config.AGENT_MODEL,
                                      summary, self.apps)
        self.summary = summary
        self._system = AGENT_SYSTEM.format(schema_card=self.store.schema_card(),
                                           max_steps=self.max_steps)

    # ================================================================ public
    def ask(self, question: str, history: list[dict[str, str]] | None = None,
            depth: str = "auto", max_seconds: float | None = None,
            on_event: Callable[[dict], None] | None = None,
            cancel: threading.Event | None = None) -> JourneyAnswer:
        """Answer one message.

        depth: "fast" = single query; "deep" = decompose, answer each part, verify,
        write a grounded answer; "auto" = deep only when the question looks complex.
        max_seconds: hard thinking budget, 60 s by default and capped at 20 min.
            ``cancel`` stops early.
        on_event: receives thinking steps as they happen (for a live UI).
        """
        started = time.perf_counter()
        run = _Run(Budget(max_seconds or think.DEFAULT_THINK_SECONDS, cancel), on_event=on_event)
        token = _RUN.set(run)
        try:
            result = self._ask(question, history, depth if depth in think.DEPTHS else "auto")
        finally:
            _RUN.reset(token)
        result.thinking = [step.as_dict() for step in run.trace]
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        log.info("route=%s depth=%s resolved=%r latency_ms=%s timed_out=%s", result.route,
                 result.depth, result.resolved_question, result.latency_ms, result.timed_out)
        return result

    def _ask(self, question: str, history, depth: str) -> JourneyAnswer:
        text, style = split_style(question)
        if not text:
            return JourneyAnswer("Please ask a question about the journey data.", route="empty")
        history = self._clean_history(history)
        if is_explicitly_out_of_scope(text):
            self._emit("plan", "The request is outside the data-analysis scope", ok=False)
            return JourneyAnswer(
                OUT_OF_SCOPE_MESSAGE, route="out_of_scope", resolved_question=text,
                verified=False, suggested_questions=self._available_questions())
        if think.requested_period_exceeds(
                text, self.summary.get("first_date"), self.summary.get("last_date")):
            self._emit("plan", "The requested period extends beyond the available data", ok=False)
            return JourneyAnswer(
                "The requested period is not fully available. The available data covers "
                f"{self.summary.get('first_date')} to {self.summary.get('last_date')}."
                "\n\nTry the same analysis within the available period, or choose a question below.",
                route="data_gap", resolved_question=text, verified=False,
                suggested_questions=self._available_questions())
        try:
            result: JourneyAnswer | None = None
            use_deep = depth == "deep"
            if depth in ("auto", "deep"):
                plan, res, _ = self.understand(text, history)
                confident = bool(plan and plan.intent and plan.confidence >= 1.0)
                if depth == "auto":
                    use_deep = think.looks_complex(text, confident) and not res.ambiguous
                elif confident and not res.ambiguous and not think.needs_decomposition(text):
                    # Deep mode should not split a question already covered by one vetted query.
                    # Decomposition can invent redundant date-range follow-ups and noisy warnings.
                    self._emit("plan", "One verified query fully covers this question")
                    result = self._answer(text, history)
                    result.depth = "deep"
                    self._emit("verify", "The result came from one verified read-only query")
                    use_deep = False
                else:
                    result = None
            if use_deep:
                result = self._deep(text, history)
            elif result is None:
                result = self._answer(text, history)
            if result.route == "out_of_scope":
                result.answer = OUT_OF_SCOPE_MESSAGE
                result.verified = False
                result.suggested_questions = result.suggested_questions or self._available_questions()
            elif result.route == "unsupported":
                result.answer = ("Not available from the current data. The dataset does not contain "
                                 "the fields needed for that analysis."
                                 "\n\nTry one of the available questions below.")
                result.route = "data_gap"
                result.verified = False
                result.suggested_questions = result.suggested_questions or self._available_questions()
            elif result.route == "data_gap":
                first, last = self.summary.get("first_date"), self.summary.get("last_date")
                coverage = f" The available data covers {first} to {last}." if first and last else ""
                result.answer = ("Not available from the current data." + coverage
                                 + "\n\nTry one of the available questions below.")
                result.verified = False
                result.suggested_questions = result.suggested_questions or self._available_questions()
            if style == "simple" and result.route not in (
                    "clarify", "smalltalk", "help", "data_gap", "unsupported", "out_of_scope"):
                result = self._simplify(result)
        except ThinkingTimeout as exc:
            self._emit("stop", f"Stopped: {exc}", ok=False)
            result = JourneyAnswer(
                f"I stopped because {exc} before I could verify an answer. Try the fast mode, "
                "or ask one part of the question at a time.", route="timeout", timed_out=True)
        except llm.OllamaError as exc:
            log.warning("LLM unavailable: %s", exc)
            result = JourneyAnswer(
                "I could not match that question to a built-in query, and the language model "
                f"is not reachable ({exc}). Try rephrasing, e.g. 'which app has the most searches'.",
                route="error", grounded=True)
        return result

    # ============================================================ budget/LLM
    def _emit(self, stage: str, text: str, ok: bool = True) -> None:
        run = _RUN.get()
        if run is None:
            return
        step = ThinkingStep(stage, text, int(run.budget.elapsed * 1000), ok)
        run.trace.append(step)
        if run.on_event:
            try:
                run.on_event({"type": "thinking", **step.as_dict()})
            except Exception:                       # a broken UI must not break the answer
                log.debug("on_event failed", exc_info=True)

    def _llm(self, reserve: float = 1.0, cap: float | None = None, **kwargs) -> str:
        """llm.complete limited by the request's remaining thinking budget."""
        run = _RUN.get()
        if run is None:
            return llm.complete(**kwargs)
        if run.budget.no_llm:
            raise ThinkingTimeout("the thinking limit was reached")
        run.budget.check(reserve)
        allowed = run.budget.remaining() - run.budget.scaled(reserve)
        if cap:
            allowed = min(allowed, cap)
        future = _LLM_POOL.submit(llm.complete, timeout=allowed, **kwargs)
        while True:
            try:
                return future.result(timeout=0.25)
            except FutureTimeout:
                if run.budget.cancelled or run.budget.remaining() <= 0:
                    future.cancel()                 # result, if it ever arrives, is ignored
                    run.budget.check(0)
                    raise ThinkingTimeout("the thinking limit was reached")
            except llm.OllamaError:
                run.budget.check(reserve)           # turn "ran out of time" into a timeout
                raise

    # ========================================================= understanding
    @staticmethod
    def _clean_history(history) -> list[dict]:
        cleaned = []
        for turn in (history or [])[-HISTORY_WINDOW:]:
            role = "user" if turn.get("role") == "user" else "assistant"
            content = str(turn.get("content") or "").strip()
            if role == "user":
                content = split_style(content)[0]
            if content:
                cleaned.append({"role": role, "content": content})
        return cleaned

    def understand(self, question: str, history: list[dict], depth: int = 0
                   ) -> tuple[QueryPlan | None, Resolution, str]:
        """Return (plan, resolution, standalone question)."""
        res = resolve_references(question, history, self.apps)
        standalone = substitute_references(question, res)
        plan = self.rules.parse(standalone, res)

        if plan.intent is None and depth < 3 and not plan.unknown_words:
            # Fragment: rebuild the previous question and apply this one on top.
            for index, previous in previous_user_turns(history):
                base, _, base_text = self.understand(previous, history[:index], depth + 1)
                if base is None or base.intent in (None, "smalltalk", "help"):
                    continue
                inherited = resolve_references(question, history, self.apps, inherit_all=True)
                if inherited.user_ids and not plan.user_ids and base.user_ids:
                    plan.user_ids = inherited.user_ids
                    res.notes.extend(n for n in inherited.notes if n not in res.notes)
                merged = merge_follow_up(base, plan)
                return merged, res, f"{base_text} ({question})"
            if plan.has_filters():
                plan.intent = ("user_detail" if plan.user_ids and len(plan.user_ids) == 1
                               and not (plan.apps or plan.outcome or plan.status)
                               else "list_journeys")
        return plan, res, standalone

    def _plan_with_llm(self, standalone: str, res: Resolution, hint: QueryPlan,
                       history: list[dict]) -> QueryPlan | None:
        if not self.use_llm:
            return None
        try:
            return self.llm_planner.plan(standalone, res, hint, self._history_text(history))
        except ThinkingTimeout:
            return None                                # fall back to the keyword plan
        except llm.OllamaError:
            raise
        except Exception as exc:                      # malformed output etc.
            log.warning("planner failed: %s", exc)
            return None

    # ============================================================= answering
    def _answer(self, question: str, history: list[dict]) -> JourneyAnswer:
        plan, res, standalone = self.understand(question, history)

        if res.ambiguous:
            return JourneyAnswer(res.ambiguous, route="clarify", resolved_question=standalone)

        route = "rules"
        if plan is None or plan.intent is None or plan.confidence < 1.0:
            llm_plan = None
            try:
                llm_plan = self._plan_with_llm(standalone, res, plan, history)
            except llm.OllamaError:
                if plan is None or plan.intent is None:
                    raise
            if llm_plan is not None:
                plan, route = llm_plan, "llm_plan"
            elif plan is None or plan.intent is None:
                return self._run_agent(standalone, res, history)

        if plan.intent == "agent":
            return self._run_agent(standalone, res, history)

        result = self._execute(plan)
        result.route = route if result.route == "rules" else result.route
        result.resolved_question = plan.describe() if plan.intent not in (
            "smalltalk", "help") else standalone
        result.plan = plan.to_dict()
        result.notes = res.notes + result.notes
        prefix = []
        if res.notes:
            prefix.append(f"Using {', '.join(res.notes)}.")
        if route == "rules" and plan.unknown_words and plan.intent not in ("smalltalk", "help"):
            prefix.append(f"I read this as: {plan.describe()}. If you meant something else, "
                          f"rephrase it (I did not understand: {', '.join(plan.unknown_words[:5])}).")
            result.notes.append("low-confidence keyword parse")
        if prefix:
            result.answer = "(" + " ".join(prefix) + ")\n" + result.answer
        return result

    def _execute(self, plan: QueryPlan) -> JourneyAnswer:
        s = self.store
        try:
            if plan.intent == "smalltalk":
                return JourneyAnswer("Hi! " + render.HELP_TEXT, route="smalltalk")
            if plan.intent == "help":
                return JourneyAnswer(render.HELP_TEXT, route="help")
            if plan.intent == "unsupported":
                return JourneyAnswer(render.render_unsupported(plan), route="unsupported")
            if plan.intent == "out_of_scope":
                return JourneyAnswer(OUT_OF_SCOPE_MESSAGE, route="out_of_scope", verified=False)
            if plan.intent == "summary":
                return JourneyAnswer(render.render_summary(s.summary()))
            if plan.intent == "quality":
                return JourneyAnswer(render.render_quality(s.summary()), route="quality")

            unknown_user = [u for u in plan.user_ids if not s.user_exists(u)]
            if unknown_user:
                return JourneyAnswer(
                    f"User {unknown_user[0]} does not appear in this file "
                    f"({s.summary()['users']:,} users are loaded). Check the number.",
                    route="not_found")
            unknown_journey = [j for j in plan.journey_ids if not s.journey_exists(j)]
            if unknown_journey:
                return JourneyAnswer(f"There is no journey {unknown_journey[0]} in this file.",
                                     route="not_found")

            if plan.intent == "journey_detail":
                detail = s.journey_detail(plan.journey_ids[0])
                return JourneyAnswer(render.render_journey(detail))
            if plan.intent == "similar":
                pairs = s.similar(plan.journey_ids[0], plan.limit or 8)
                ids = [p[0] for p in pairs]
                details = {r["journey_id"]: r for r in s.query(
                    f"SELECT journey_id, query_cluster_rep, apps_summary, status, close_reason "
                    f"FROM journey WHERE journey_id IN ({','.join('?' * len(ids))})", ids).dicts()
                } if ids else {}
                return JourneyAnswer(render.render_similar(plan.journey_ids[0], pairs, details))
            if plan.intent == "start_funnel":
                from .planner import compile_start_funnel
                queries = compile_start_funnel(plan)
                totals = s.query(*queries["totals"]).dicts()[0]
                dest = s.query(*queries["destinations"])
                return JourneyAnswer(render.render_start_funnel(plan, totals, dest.dicts()),
                                     sql=_inline(*queries["totals"]), table=dest.as_table())
            if plan.intent == "user_detail":
                profile = s.user_profile(plan.user_ids[0])
                return JourneyAnswer(render.render_user(profile),
                                     table=profile["journey_list"].as_table(),
                                     sql=profile["journey_list"].sql)
            if plan.intent == "list_journeys":
                sql, params, count_sql, count_params = compile_journey_list(plan)
                rows = s.query(sql, params)
                total = s.query(count_sql, count_params).scalar() or 0
                suggestions = (s.suggest_queries(plan.query_text)
                               if total == 0 and plan.query_text else None)
                return JourneyAnswer(render.render_journey_list(plan, rows, total, suggestions),
                                     sql=_inline(sql, params), table=rows.as_table())
            if plan.intent == "aggregate":
                if plan.metric == "apps" and not plan.group_by:
                    # "how many apps" -> list them too; the count is the row count.
                    plan.group_by, plan.metric, plan.singular = "app", "searches", False
                sql, params, _ = compile_aggregate(plan)
                rows = s.query(sql, params)
                if plan.group_by:
                    total_groups = self._group_count(plan)
                    if total_groups is not None:
                        rows.total_rows = total_groups
                ranked = plan.group_by and not (plan.group_by in ("date", "hour") and not plan.singular)
                page_tied = len(rows.rows) > 1 and rows.rows[0][1:] and \
                    rows.dicts()[0].get(_sort_col(plan)) == rows.dicts()[-1].get(_sort_col(plan))
                tie = (self._tie(plan, rows)
                       if ranked and not plan.share and
                       (plan.singular or (page_tied and not plan.limit)) else None)
                table = (tie.pop("table") if tie and tie.get("total", 0) > 1
                         else _focused_table(plan, rows))
                if plan.share:
                    scope = QueryPlan(**{**plan.__dict__, "group_by": None, "limit": None,
                                         "share": False})
                    tsql, tparams, _ = compile_aggregate(scope)
                    totals = s.query(tsql, tparams).dicts()[0]
                    multi = QueryPlan(**{**scope.__dict__, "multi_app": True})
                    msql, mparams, _ = compile_aggregate(multi)
                    totals["multi_app_journeys"] = s.query(msql, mparams).dicts()[0]["journeys"]
                    return JourneyAnswer(render.render_share(plan, rows, totals),
                                         sql=_inline(sql, params), table=rows.as_table())
                return JourneyAnswer(render.render_aggregate(plan, rows, tie),
                                     sql=_inline(sql, params), table=table)
        except JourneyDataError as exc:
            return JourneyAnswer(str(exc), route="not_found")
        return JourneyAnswer(render.HELP_TEXT, route="help")

    def _tie(self, plan: QueryPlan, rows: QueryResult) -> dict | None:
        """How many groups share the top value - not just the ones inside LIMIT."""
        from .planner import _GROUP_SQL, _METRIC_COLUMN
        if not rows.rows:
            return None
        sort_col = _METRIC_COLUMN.get(plan.metric or "journeys", "journeys")
        data = rows.dicts()
        if sort_col not in data[0]:
            return None
        best = data[0][sort_col]
        label = _GROUP_SQL[plan.group_by][1]
        probe = QueryPlan(**{**plan.__dict__, "limit": 100000, "singular": False})
        sql, params, _ = compile_aggregate(probe, row_cap=100000)
        where = f"WHERE {sort_col} = ?" if best is not None else f"WHERE {sort_col} IS NULL"
        args = list(params) + ([best] if best is not None else [])
        total = self.store.query(f"SELECT COUNT(*) FROM ({sql}) {where}", args).scalar() or 0
        tied = self.store.query(f"SELECT * FROM ({sql}) {where}", args, max_rows=1000)
        return {"total": total, "labels": [r[0] for r in tied.rows], "table": tied.as_table(1000)}

    def _group_count(self, plan: QueryPlan) -> int | None:
        """Number of groups before LIMIT, so the answer can say 'top 10 of 699'."""
        try:
            probe = QueryPlan(**{**plan.__dict__, "limit": 100000, "singular": False})
            sql, params, _ = compile_aggregate(probe, row_cap=100000)
            return self.store.query(f"SELECT COUNT(*) FROM ({sql})", params).scalar()
        except JourneyDataError:
            return None

    # ======================================================= "simple" style
    def _simplify(self, result: JourneyAnswer) -> JourneyAnswer:
        """'Simple meaning' mode.

        The model writes a short plain-language intro that explains what the
        result means in real life. It may not contain any number or id; the
        exact facts are always kept underneath, straight from the query. An
        earlier version let the model rewrite the whole answer, and it dropped
        user ids and invented figures ("10 searches", "two minutes").
        """
        if not self.use_llm or result.route in ("not_found", "unsupported", "error"):
            return result
        for attempt in range(2):
            try:
                text = self._llm(system=SIMPLE_SYSTEM, user=result.answer,
                                 model=config.ANSWER_MODEL, max_tokens=160).strip()
            except (llm.OllamaError, ThinkingTimeout):
                return result
            problems = render.plain_summary_problems(text)
            if text and not problems:
                result.answer = f"In simple words: {text}\n\n{result.answer}"
                return result
            result.notes.append(f"simple intro rejected ({', '.join(problems)})")
        return result

    # ========================================================== deep thinking
    def _deep(self, question: str, history: list[dict]) -> JourneyAnswer:
        """Decompose -> answer each part from data -> verify coverage -> grounded answer."""
        run = _RUN.get()
        budget = run.budget
        s = self.summary
        parts: list[str] = []
        cannot: list[dict] = []
        results: list[dict] = []
        timed_out = None

        try:
            # 1. Understand / decompose ---------------------------------------
            self._emit("understand", "Breaking the question into parts that the data can answer")
            if self.use_llm:
                try:
                    raw = self._llm(
                        system=think.DECOMPOSE_SYSTEM.format(
                            home_app=HOME_APP or "our app", first_date=s.get("first_date"),
                            last_date=s.get("last_date"), journeys=s.get("journeys")),
                        user=(f"Recent conversation:\n{self._history_text(history)}\n\n"
                              if history else "") + f"Question: {question}",
                        model=config.AGENT_MODEL, max_tokens=400,
                        schema=think.DECOMPOSE_SCHEMA, cap=15, reserve=8)
                    data = think.parse_json(raw)
                    parts = [str(p).strip() for p in data.get("parts") or [] if str(p).strip()][:4]
                    cannot = [c for c in data.get("cannot_answer") or []
                              if isinstance(c, dict) and c.get("part")][:4]
                except (llm.OllamaError, ValueError, json.JSONDecodeError) as exc:
                    self._emit("understand", f"Model could not split the question ({exc}); "
                                             "using a simple split", ok=False)
            if not parts:
                parts = think.split_parts_without_llm(question)
            self._emit("plan", "Plan: " + " | ".join(f"{i}. {p}" for i, p in enumerate(parts, 1)))
            for c in cannot:
                self._emit("plan", f"Cannot answer from this file: {c['part']} "
                                   f"(missing: {c.get('missing', 'data')})", ok=False)

            # Do not answer a different/shorter period and present it as a
            # partial substitute.  State the gap once, then offer questions
            # built from fields that are genuinely present in this store.
            if cannot:
                self._emit("write", "The requested data is unavailable; suggesting available questions", ok=False)
                return JourneyAnswer(
                    "Not available from the current data. The dataset does not contain the fields "
                    "needed for that analysis.\n\nTry one of the available questions below.",
                    route="unsupported", depth="deep", resolved_question=question,
                    verified=False, suggested_questions=self._available_questions())

            # 2. Answer each part from the data -------------------------------
            pending = list(parts)
            while pending:
                part = pending.pop(0)
                budget.check(reserve=6)
                self._emit("query", f"Part {len(results) + 1}: {part}")
                sub = self._answer(part, history)
                ok = (sub.route in ("rules", "llm_plan", "agent") and not sub.stopped_early
                      and "low-confidence keyword parse" not in sub.notes)
                results.append({"question": part, "understood_as": sub.resolved_question,
                                "answer": sub.answer, "route": sub.route, "sql": sub.sql,
                                "verified": ok})
                self._emit("query", f"-> {sub.resolved_question or sub.route}"
                                    + ("" if ok else " (not reliable, left out)"), ok=ok)

                # 3. Verify coverage once all planned parts are done -----------
                if not pending and self.use_llm and budget.remaining() > 15 and \
                        not any(r.get("followup") for r in results):
                    self._emit("verify", "Checking that every part of the question is answered")
                    try:
                        raw = self._llm(
                            system=think.CRITIC_SYSTEM,
                            user=f"Question: {question}\n\n" + "\n\n".join(
                                f"Part: {r['question']}\nUnderstood as: {r['understood_as']}\n"
                                f"Answer: {r['answer'][:700]}" for r in results),
                            model=config.AGENT_MODEL, max_tokens=250,
                            schema=think.CRITIC_SCHEMA, cap=12, reserve=6)
                        review = think.parse_json(raw)
                        for problem in (review.get("problems") or [])[:3]:
                            self._emit("verify", f"Issue found: {problem}", ok=False)
                        follow = [str(f) for f in review.get("follow_up") or [] if str(f).strip()][:2]
                        if not review.get("complete", True) and follow:
                            self._emit("verify", "Answering the missing parts: " + " | ".join(follow))
                            pending.extend(follow)
                            for r in results:
                                r["followup"] = True
                        elif review.get("complete", True):
                            self._emit("verify", "All parts are covered")
                    except (llm.OllamaError, ValueError, json.JSONDecodeError):
                        self._emit("verify", "Coverage check skipped (model unavailable)", ok=False)
        except ThinkingTimeout as exc:
            timed_out = str(exc)
            self._emit("stop", f"Stopped: {exc}. Using the parts finished so far.", ok=False)

        # After a timeout or Stop, still answer the parts the keyword rules
        # understand fully: they need no model and take milliseconds.
        if timed_out:
            budget.no_llm = True
            if not parts:
                parts = think.split_parts_without_llm(question)
            done = {r["question"] for r in results}
            for part in [p for p in parts if p not in done]:
                sub = self._answer(part, history)
                if sub.route == "rules" and "low-confidence keyword parse" not in sub.notes:
                    results.append({"question": part, "understood_as": sub.resolved_question,
                                    "answer": sub.answer, "route": sub.route, "sql": sub.sql,
                                    "verified": True})
                    self._emit("query", f"Answered without the model: {part}")

        # 4. Write the final answer from verified parts only -------------------
        good = [r for r in results if r["verified"]]
        text, grounded = None, True
        if good and self.use_llm and not budget.cancelled and budget.remaining() > 4:
            evidence = [f"Q: {r['question']}\nA: {r['answer']}" for r in good]
            gaps = [f"{c['part']} (missing: {c.get('missing', 'data')})" for c in cannot]
            self._emit("write", "Writing the final answer from the verified numbers")
            prompt = (f"Question: {question}\n\nVerified data answers:\n" + "\n\n".join(evidence)
                      + ("\n\nCannot be answered from this data:\n- " + "\n- ".join(gaps)
                         if gaps else ""))
            feedback = ""
            for attempt in range(2):
                try:
                    draft = self._llm(system=think.SYNTH_SYSTEM.format(home_app=HOME_APP or "our app"),
                                      user=prompt + feedback, model=config.ANSWER_MODEL,
                                      max_tokens=450, cap=20, reserve=1).strip()
                except (llm.OllamaError, ThinkingTimeout):
                    break
                bad = render.ungrounded_numbers(draft, evidence, question)
                bad_ids = _unknown_ids(draft, evidence + [question])
                if draft and not bad and not bad_ids:
                    text = draft
                    self._emit("verify", "Final answer checked: every number matches the data")
                    break
                problems = bad + bad_ids
                self._emit("verify", f"Draft rejected, not in the data: {', '.join(problems[:6])}",
                           ok=False)
                feedback = (f"\n\nYour previous draft used values that are not in the data "
                            f"answers: {', '.join(problems[:8])}. Rewrite using only numbers "
                            "that appear above.")

        if text is None:
            # Deterministic composition: always correct, less polished.
            if good:
                self._emit("write", "Showing the verified part answers directly")
            text = "\n\n".join(f"{i}. {r['question']}\n{r['answer']}"
                                for i, r in enumerate(good, 1)) or \
                "I could not answer any part of this question reliably from the data."
        extra = []
        bad_parts = [r["question"] for r in results if not r["verified"]]
        if cannot:
            extra.append("Some requested information is not available in the current data.")
        if timed_out:
            extra.append("The analysis stopped before every requested part was completed.")
        answer = text + ("\n\n" + "\n".join(extra) if extra else "")
        return JourneyAnswer(
            answer, route="deep", depth="deep", resolved_question=" | ".join(parts),
            parts=results, verified=grounded and bool(good), timed_out=bool(timed_out),
            sql=next((r["sql"] for r in good if r.get("sql")), None),
            notes=([f"cannot answer: {c['part']}" for c in cannot]
                   + [f"not verified: {part}" for part in bad_parts]
                   + (["analysis timed out before completion"] if timed_out else [])))

    def _available_questions(self, limit: int = 4) -> list[str]:
        """Questions generated from capabilities present in the loaded store."""
        questions: list[str] = []
        for group in self.summary.get("suggestion_groups") or []:
            for question in group.get("questions") or []:
                if question and question not in questions:
                    questions.append(str(question))
                if len(questions) >= limit:
                    return questions
        return questions

    # ============================================================ SQL agent
    def _history_text(self, history: list[dict]) -> str:
        lines = []
        for turn in history[-MAX_HISTORY_TURNS:]:
            content = turn["content"]
            if len(content) > MAX_HISTORY_CHARS_PER_TURN:
                content = content[:MAX_HISTORY_CHARS_PER_TURN] + " ..."
            lines.append(f"{'User' if turn['role'] == 'user' else 'Assistant'}: {content}")
        return "\n".join(lines)

    def _prompt(self, question: str, res: Resolution, steps: list[JourneyStep],
                history: list[dict]) -> str:
        parts = []
        if history:
            parts.append("Recent conversation (context only):\n" + self._history_text(history))
        parts.append(f"Question: {question}")
        ents = []
        if res.user_ids:
            ents.append(f"user_assoc_id = {', '.join(map(str, res.user_ids))}")
        if res.journey_ids:
            ents.append(f"journey_id = {', '.join(res.journey_ids)}")
        if res.apps:
            ents.append(f"app_name = {', '.join(res.apps)}")
        if ents:
            parts.append("Resolved entities (use these filters): " + "; ".join(ents))
        if steps:
            parts.append("\nEvidence so far:")
            for number, step in enumerate(steps, 1):
                parts.append(f"Step {number}: {step.tool} {json.dumps(step.arguments)}")
                parts.append(step.observation)
        parts.append(f"\nSteps remaining: {self.max_steps - len(steps)}. Reply with the next JSON.")
        return "\n".join(parts)

    @staticmethod
    def _limit(value, default) -> int:
        raw = str(value or "").strip()
        return int(raw) if raw.isdigit() else default

    def _call(self, tool: str, decision: dict) -> tuple[dict, str]:
        if tool == "run_sql":
            args = {"sql": decision.get("sql", "")}
            return args, self.store.run_untrusted_sql(args["sql"]).to_text()
        if tool == "show_journey":
            args = {"journey_id": decision.get("journey_id", "")}
            return args, self.store.show_journey(args["journey_id"])
        if tool == "show_user":
            args = {"user_assoc_id": decision.get("user_assoc_id", "")}
            return args, self.store.show_user(args["user_assoc_id"])
        if tool == "search_journeys":
            args = {"keyword": decision.get("keyword", ""),
                    "limit": self._limit(decision.get("limit"), 20)}
            return args, self.store.search_journeys(args["keyword"], args["limit"])
        if tool == "similar_journeys":
            args = {"journey_id": decision.get("journey_id", ""),
                    "limit": self._limit(decision.get("limit"), 8)}
            return args, self.store.similar_journeys(args["journey_id"], args["limit"])
        raise JourneyDataError(f"Unknown tool: {tool}")

    @staticmethod
    def _args_for(tool: str, decision: dict) -> dict:
        keys = {"run_sql": ("sql",), "show_journey": ("journey_id",),
                "show_user": ("user_assoc_id",), "search_journeys": ("keyword", "limit"),
                "similar_journeys": ("journey_id", "limit")}.get(tool, ())
        return {k: str(decision.get(k, "")).strip() for k in keys}

    def _run_agent(self, question: str, res: Resolution, history: list[dict]) -> JourneyAnswer:
        if not self.use_llm:
            return JourneyAnswer(render.HELP_TEXT, route="help", resolved_question=question)
        steps: list[JourneyStep] = []
        seen: set[tuple[str, str]] = set()
        last_sql = None
        for _ in range(self.max_steps):
            try:
                raw = self._llm(system=self._system,
                                user=self._prompt(question, res, steps, history),
                                model=config.AGENT_MODEL, max_tokens=700, schema=DECISION_SCHEMA)
            except ThinkingTimeout as exc:
                self._emit("stop", f"SQL agent stopped: {exc}", ok=False)
                break
            self._emit("query", f"SQL agent step {len(steps) + 1}")
            try:
                decision = json.loads(raw)
            except json.JSONDecodeError:
                steps.append(JourneyStep("", "invalid", {}, "Reply was not valid JSON.", True))
                continue
            thought = str(decision.get("thought") or "").strip()
            tool = str(decision.get("tool") or "").strip()

            if tool == "final_answer":
                text = str(decision.get("answer") or "").strip()
                if not text:
                    steps.append(JourneyStep(thought, tool, {}, "final_answer needs answer text.", True))
                    continue
                evidence = [st.observation for st in steps if not st.error]
                if not evidence:
                    steps.append(JourneyStep(thought, tool, {},
                                             "No evidence yet. Call a data tool before answering.", True))
                    continue
                bad = render.ungrounded_numbers(text, evidence, question)
                if bad:
                    steps.append(JourneyStep(thought, tool, {},
                                             f"Rejected: these numbers are not in the tool results: "
                                             f"{', '.join(bad[:8])}. Copy numbers exactly.", True))
                    continue
                steps.append(JourneyStep(thought, tool, {}, "(finished)"))
                return JourneyAnswer(text, steps, route="agent", resolved_question=question,
                                     sql=last_sql, notes=list(res.notes))

            args = self._args_for(tool, decision)
            fingerprint = (tool, json.dumps(args, sort_keys=True))
            if fingerprint in seen:
                steps.append(JourneyStep(thought, tool, args,
                                         "This exact call already ran. Use its result or answer.", True))
                continue
            seen.add(fingerprint)
            try:
                args, observation = self._call(tool, decision)
                if len(observation) > MAX_OBSERVATION_CHARS:
                    observation = (observation[:MAX_OBSERVATION_CHARS] +
                                   "\n[PARTIAL: output cut to fit the context window.]")
                steps.append(JourneyStep(thought, tool, args, observation))
                if tool == "run_sql":
                    last_sql = args["sql"]
            except JourneyDataError as exc:
                steps.append(JourneyStep(thought, tool, args, f"Error: {exc}", True))

        # Out of steps: never return an unverified model summary.
        good = [st for st in steps if not st.error and st.tool != "final_answer"]
        if not good:
            return JourneyAnswer(
                "I could not answer that from the journey data. Try asking it more directly, "
                "for example with a user id, a journey id, an app name or an outcome.",
                steps, stopped_early=True, route="agent", resolved_question=question,
                grounded=True)
        last = good[-1]
        return JourneyAnswer(
            "I could not finish a verified answer. This is the most relevant data I found:\n"
            + last.observation, steps, stopped_early=True, route="agent",
            resolved_question=question, sql=last_sql)


def _unknown_ids(text: str, sources: list[str]) -> list[str]:
    """User numbers / journey ids in ``text`` that never appear in ``sources``."""
    from .context import extract_journey_ids, extract_user_ids
    joined = "\n".join(sources)
    known_users = set(extract_user_ids(joined)) | {int(n) for n in re.findall(r"\d+", joined)}
    known_journeys = set(extract_journey_ids(joined))
    bad = [f"user {u}" for u in extract_user_ids(text) if u not in known_users]
    bad += [j for j in extract_journey_ids(text) if j not in known_journeys]
    return bad


def _sort_col(plan: QueryPlan) -> str:
    from .planner import _METRIC_COLUMN
    return _METRIC_COLUMN.get(plan.metric or "journeys", "journeys")


def _focused_table(plan: QueryPlan, result: QueryResult) -> dict:
    """Expose only the grouping and requested measure in the main result table."""
    if not plan.group_by or not result.columns:
        return result.as_table()
    wanted = [result.columns[0], _sort_col(plan)]
    indexes = [result.columns.index(name) for name in dict.fromkeys(wanted)
               if name in result.columns]
    if len(indexes) < 2:
        return result.as_table()
    return {
        "columns": [result.columns[index] for index in indexes],
        "rows": [[row[index] for index in indexes] for row in result.rows[:200]],
        "total_rows": result.row_count,
        "truncated": result.truncated or result.row_count > min(200, len(result.rows)),
    }


def _inline(sql: str, params) -> str:
    """SQL with parameters shown inline, for the UI's 'show query' panel only."""
    parts = sql.split("?")
    if len(parts) - 1 != len(params):
        return sql
    out = [parts[0]]
    for value, tail in zip(params, parts[1:]):
        out.append(repr(value) if isinstance(value, str) else str(value))
        out.append(tail)
    return re.sub(r"\s+", " ", "".join(out)).strip()
