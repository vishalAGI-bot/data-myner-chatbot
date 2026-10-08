"""FastAPI service for the Data analyst agent (all database tables).

    uvicorn analyst.api:app --port 8200

Endpoints
    GET  /api/health                  source + model status
    GET  /api/catalog                 tables, metrics, apps, date range (what the chatbot knows)
    POST /api/chat                    {message, history, state, depth, max_seconds, request_id}
    POST /api/chat/stream             same, as server-sent events (live thinking steps)
    POST /api/chat/{request_id}/stop  Stop button: finish now with verified results
    POST /api/reload                  re-read knowledge files and the database schema      (admin)

Settings page (admin: same computer only, or the X-Admin-Token header when ANALYST_ADMIN_TOKEN is set)
    GET    /api/settings              current connection settings (passwords are never returned)
    POST   /api/settings/test         try settings without saving: connects, reads the schema, reports
    PUT    /api/settings              test, save to config/analyst_settings.json and switch live
    DELETE /api/settings              forget the saved settings and go back to backend/.env
    POST   /api/llm/test              check a language-model host and model
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Literal

import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

import llm
from journey_agent.thinking import DEFAULT_THINK_SECONDS, MAX_THINK_SECONDS

from . import settings_store as store
from .agent import AnalystAgent, AnalystAnswer
from .catalog import Catalog
from .settings import Settings
from .sources import SourceError, explain_error, open_source

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("analyst.api")


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = ""

    @field_validator("content", mode="before")
    @classmethod
    def clip(cls, v):
        return ("" if v is None else str(v))[:6000]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    history: list[Turn] = Field(default_factory=list)
    state: dict = Field(default_factory=dict)          # returned by the previous answer (follow-ups)
    depth: Literal["auto", "fast", "deep"] = "auto"
    max_seconds: float = Field(default=DEFAULT_THINK_SECONDS, ge=3, le=MAX_THINK_SECONDS)
    request_id: str | None = Field(default=None, max_length=64)

    @field_validator("history", mode="after")
    @classmethod
    def recent(cls, turns):
        return [t for t in turns if t.content.strip()][-40:]


class ChatResponse(BaseModel):
    answer: str
    route: str
    source: str
    resolved: str = ""
    metric: str | None = None
    params: dict = Field(default_factory=dict)
    tables: list[str] = Field(default_factory=list)
    sql: str | None = None
    table: dict | None = None
    notes: list[str] = Field(default_factory=list)
    thinking: list[dict] = Field(default_factory=list)
    parts: list[dict] = Field(default_factory=list)
    verified: bool = True
    timed_out: bool = False
    depth: str = "fast"
    latency_ms: int = 0
    state: dict = Field(default_factory=dict)
    suggested_questions: list[str] = Field(default_factory=list)
    request_id: str = ""


def _response(r: AnalystAnswer, request_id: str) -> ChatResponse:
    return ChatResponse(request_id=request_id, **{k: getattr(r, k) for k in ChatResponse.model_fields
                                                 if k != "request_id"})


app = FastAPI(title="DataMyner Data Analyst API", version="1.1.0")
_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
_LOCAL_ORIGIN = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")
app.add_middleware(CORSMiddleware, allow_origins=_origins or ["http://localhost:5173", "http://localhost:4173"],
                   allow_origin_regex=None if _origins else _LOCAL_ORIGIN.pattern,
                   allow_credentials=True, allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["*"])

@app.middleware("http")
async def no_store(request: Request, call_next):
    """Every answer is computed from a query run at that moment, so nothing here may be cached.

    FastAPI sets no cache headers of its own, which leaves a browser free to reuse a GET
    heuristically - the catalog, the row counts and the latest data date could then be shown long
    after they changed. Chat is POST and was never cacheable; this makes the rule explicit for all.
    """
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-store, no-cache, must-revalidate")
    response.headers.setdefault("Pragma", "no-cache")
    return response


RETRY_AFTER_FAILURE_S = 20      # do not retry a failed connection on every status poll
PROBE_TIMEOUT_S = 60            # a connection test never takes longer than this
# The old connection stays open this long after a settings switch so questions already
# running finish on it - it must outlast the longest thinking budget a question can hold.
RETIRE_AFTER_S = MAX_THINK_SECONDS + 60

_state: dict = {"agent": None, "error": None, "failed_at": 0.0, "since": None}
_state_lock = threading.Lock()
_running: dict[str, threading.Event] = {}
_running_lock = threading.Lock()
_settings_lock = asyncio.Lock()

store.apply_llm(store.load_overrides())          # model host/name saved from the Settings page


# --------------------------------------------------------------------------- #
# Building / switching the connection
# --------------------------------------------------------------------------- #

def _open(settings: Settings, abandoned: threading.Event | None = None) -> tuple[AnalystAgent, dict]:
    """Connect, read the schema and the reference values, and describe what was found."""
    started = time.perf_counter()
    catalog = Catalog(settings.knowledge_dir, settings.home_app)
    source = open_source(settings, catalog.blocked_tables)
    try:
        catalog.attach(source)
        if not catalog.allowed_tables:
            raise SourceError(f"Connected, but none of the chatbot's tables exist in schema "
                              f"'{settings.db_schema}'. Check the database name and schema.")
        ping = source.ping()
        info = source.server_info()
    except BaseException:
        source.close()
        raise
    if abandoned is not None and abandoned.is_set():       # the caller gave up waiting
        source.close()
        raise SourceError("Connection test timed out.")
    warnings = []
    if info.get("superuser"):
        warnings.append("This login is a superuser. Use a read-only role (backend/analyst/sql/readonly_role.sql).")
    elif info.get("can_write"):
        warnings.append("This login can change data. Sessions are opened read-only, but a read-only role "
                        "(backend/analyst/sql/readonly_role.sql) is safer.")
    warnings.extend(issue["detail"] for issue in catalog.quality_issues)
    missing = sorted(n for n, d in catalog.tables.items() if not d.available)
    key = {n: catalog.tables[n].row_count for n in ("journey_window", "cross_app_journey", "session_master",
                                                    "user_master") if n in catalog.allowed_tables}
    report = {
        "ok": True, **source.describe(), **{k: v for k, v in info.items() if k not in ("can_write", "superuser")},
        "connect_ms": int((time.perf_counter() - started) * 1000), "ping_ms": ping,
        "tables_found": len(catalog.allowed_tables), "tables_missing": missing,
        "metrics_ready": sum(m.available and name not in catalog.metric_issues
                             for name, m in catalog.metrics.items()),
        "metrics_total": sum(m.available for m in catalog.metrics.values()),
        "apps": catalog.apps[:20], "apps_total": len(catalog.apps), "date_range": list(catalog.date_range),
        "row_counts": key, "warnings": warnings, "quality_issues": catalog.quality_issues,
    }
    log.info("analyst connected: source=%s tables=%d metrics=%d", source.kind, len(catalog.allowed_tables),
             report["metrics_ready"])
    return AnalystAgent(source, catalog, settings), report


def _build() -> AnalystAgent:
    return _open(store.effective())[0]


def _retire(agent: AnalystAgent | None) -> None:
    if agent is not None:
        timer = threading.Timer(RETIRE_AFTER_S, agent.source.close)
        timer.daemon = True
        timer.start()


def _activate(agent: AnalystAgent | None, error: str | None = None) -> None:
    if agent is not None:
        _ensure_refresher()
    with _state_lock:
        old = _state["agent"]
        _state.update(agent=agent, error=error, failed_at=0.0 if agent else time.time(),
                      since=time.time() if agent else None)
    _retire(old)


def get_agent() -> AnalystAgent:
    with _state_lock:
        if _state["agent"] is None:
            if _state["error"] and time.time() - _state["failed_at"] < RETRY_AFTER_FAILURE_S:
                raise HTTPException(status_code=503, detail=f"Data source not ready: {_state['error']}")
            try:
                _state["agent"] = _build()
                _state.update(error=None, failed_at=0.0, since=time.time())
                _ensure_refresher()
            except (SourceError, FileNotFoundError, OSError) as exc:
                _state.update(error=explain_error(exc), failed_at=time.time())
                log.error("analyst not ready: %s", exc)
                raise HTTPException(status_code=503, detail=f"Data source not ready: {_state['error']}") from exc
        return _state["agent"]


def _refresher() -> None:
    """Keep apps, categories, row counts and the latest date current for a live database."""
    while True:
        time.sleep(15)
        agent = _state.get("agent")
        if agent is None or agent.source.kind != "postgres" or agent.s.refresh_seconds <= 0:
            continue
        if time.time() - agent.cat.refreshed_at < agent.s.refresh_seconds:
            continue
        try:
            agent.cat.refresh_values(agent.source)
            log.info("reference values refreshed (latest date %s)", agent.cat.date_range[1])
        except Exception as exc:          # never kill the thread
            agent.cat.refreshed_at = time.time()
            log.warning("refresh failed: %s", exc)


_refresher_started = threading.Event()


def _ensure_refresher() -> None:
    if not _refresher_started.is_set():
        _refresher_started.set()
        threading.Thread(target=_refresher, name="analyst-refresh", daemon=True).start()


# --------------------------------------------------------------------------- #
# Admin check for the Settings page
# --------------------------------------------------------------------------- #

def _loopback(host: str | None) -> bool:
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host.strip().strip("[]").split("%")[0])
        mapped = getattr(ip, "ipv4_mapped", None)          # ::ffff:127.0.0.1 from dual-stack proxies
        return (mapped or ip).is_loopback
    except ValueError:
        return host.strip().lower() == "localhost"


def require_admin(request: Request, x_admin_token: str | None = Header(default=None)) -> str:
    """Settings reveal hosts and change where data comes from, so only an admin may use them.

    With ANALYST_ADMIN_TOKEN set, the X-Admin-Token header must match it. Without a token, only requests
    from this computer are accepted (the dev server proxy and nginx must pass X-Forwarded-For).
    """
    token = os.getenv("ANALYST_ADMIN_TOKEN", "")
    if token:      # a custom header cannot be sent cross-site without CORS approval, so no origin check needed
        if x_admin_token and hmac.compare_digest(x_admin_token.encode(), token.encode()):
            return "token"
        raise HTTPException(status_code=401, detail="Admin token required. Enter the ANALYST_ADMIN_TOKEN "
                                                    "value from backend/.env.")
    origin = request.headers.get("origin")
    if origin and not (_LOCAL_ORIGIN.match(origin) or origin in _origins):
        raise HTTPException(status_code=403, detail="Settings cannot be changed from another website.")
    forwarded = [h for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    real_ip = request.headers.get("x-real-ip")
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    if (_loopback(request.client.host if request.client else None)
            and all(_loopback(h) for h in forwarded + ([real_ip] if real_ip else []))
            and (not host or _loopback(host))):
        return "local"
    raise HTTPException(status_code=403, detail="Settings can only be changed on the server itself. To allow it "
                                                "from other computers, set ANALYST_ADMIN_TOKEN in backend/.env.")


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #

@app.get("/api/health")
def health():
    healthy, message = llm.health()
    ping_ms, error = None, None
    try:
        agent = get_agent()
        source = agent.source.describe()
        try:
            ping_ms = agent.source.ping()
            ready = True
        except SourceError as exc:
            error = explain_error(exc)
            source, ready = {**source, "error": f"Connection lost: {error}"}, False
    except HTTPException as exc:
        agent, source, ready = None, {"error": exc.detail}, False
    live = {}
    if agent is not None:
        live = {"connected_since": _state.get("since"), "refreshed_at": agent.cat.refreshed_at or None,
                "latest_date": agent.cat.date_range[1]}
    return {"status": "ok" if (ready and healthy) else "degraded", "data_ready": ready, "source": source,
            "db_ping_ms": ping_ms, **live, "llm": message, "llm_ok": healthy,
            "llm_provider": llm.config.LLM_PROVIDER, "llm_model": llm.config.SQL_MODEL}


@app.get("/api/catalog")
def catalog():
    agent = get_agent()
    return {**agent.cat.summary(), "source": agent.source.describe()}


@app.post("/api/reload", dependencies=[Depends(require_admin)])
def reload():
    with _state_lock:
        old = _state["agent"]
        _state.update(agent=None, error=None, failed_at=0.0)
    _retire(old)
    return catalog()


# --------------------------------------------------------------------------- #
# Settings page
# --------------------------------------------------------------------------- #

class SettingsBody(BaseModel):
    values: dict = Field(default_factory=dict)     # only the fields that changed; empty password = keep
    force: bool = False                            # save even if the connection test fails


def _candidate(body: SettingsBody) -> dict:
    if len(json.dumps(body.values, default=str)) > 20000:
        raise HTTPException(status_code=413, detail="Too much data.")
    merged = store.merge(store.current_values(), body.values)
    try:
        return store.validate(merged)
    except store.SettingsError as exc:
        raise HTTPException(status_code=422, detail={"message": "Please fix the highlighted fields.",
                                                     "errors": exc.errors}) from exc


async def _probe(values: dict) -> tuple[AnalystAgent | None, dict]:
    """Try the settings in a worker thread, bounded by PROBE_TIMEOUT_S."""
    abandoned = threading.Event()
    loop = asyncio.get_running_loop()
    started = time.perf_counter()
    try:
        return await asyncio.wait_for(loop.run_in_executor(None, _open, store.effective(values), abandoned),
                                      timeout=PROBE_TIMEOUT_S)
    except asyncio.TimeoutError:
        abandoned.set()
        return None, {"ok": False, "error": f"No answer within {PROBE_TIMEOUT_S}s. Check the hosts, ports and "
                                            "firewall/VPN.", "connect_ms": PROBE_TIMEOUT_S * 1000}
    except (SourceError, FileNotFoundError, OSError, ImportError) as exc:
        return None, {"ok": False, "error": explain_error(exc),
                      "connect_ms": int((time.perf_counter() - started) * 1000)}


def _view(admin: str) -> dict:
    overrides = store.load_overrides()
    view = store.public_view(store.effective(overrides), overrides)
    with _state_lock:
        agent, error = _state["agent"], _state["error"]
    view.update(admin=admin, token_required=bool(os.getenv("ANALYST_ADMIN_TOKEN")), saved=store.SETTINGS_FILE.exists(),
                active=agent.source.describe() if agent else None, error=error)
    return view


@app.get("/api/settings")
def get_settings_view(admin: str = Depends(require_admin)):
    return _view(admin)


@app.post("/api/settings/test")
async def test_settings(body: SettingsBody, admin: str = Depends(require_admin)):
    values = _candidate(body)
    async with _settings_lock:
        agent, report = await _probe(values)
    if agent is not None:
        agent.source.close()
    return report


@app.put("/api/settings")
async def save_settings(body: SettingsBody, admin: str = Depends(require_admin)):
    values = _candidate(body)
    async with _settings_lock:
        agent, report = await _probe(values)
        if agent is None and not body.force:
            return {**report, "saved": False}
        store.save_overrides(store.diff_from_env(values))
        store.apply_llm(store.load_overrides())
        _activate(agent, None if agent else report.get("error"))
    log.info("settings saved from the Settings page (source=%s, connected=%s)", values.get("source"), bool(agent))
    return {**report, "saved": True, "settings": _view(admin)}


@app.delete("/api/settings")
async def reset_settings(admin: str = Depends(require_admin)):
    async with _settings_lock:
        store.SETTINGS_FILE.unlink(missing_ok=True)
        store.apply_llm({})
        agent, report = await _probe(store.current_values())
        _activate(agent, None if agent else report.get("error"))
    return {**report, "saved": True, "settings": _view(admin)}


class LlmBody(BaseModel):
    llm_provider: Literal["ollama", "openai"] = "ollama"
    llm_host: str = ""
    llm_model: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-6-luna"


@app.post("/api/llm/test")
def test_llm(body: LlmBody, admin: str = Depends(require_admin)):
    current = store.current_values()
    values = {
        "llm_provider": body.llm_provider,
        "llm_host": body.llm_host or current["llm_host"],
        "llm_model": body.llm_model or current["llm_model"],
        "openai_api_key": body.openai_api_key or current["openai_api_key"],
        "openai_model": body.openai_model or current["openai_model"],
    }
    try:
        store.validate(values)
    except store.SettingsError as exc:
        raise HTTPException(status_code=422, detail={"message": "Please fix the highlighted fields.",
                                                     "errors": exc.errors}) from exc
    started = time.perf_counter()
    if values["llm_provider"] == "openai":
        ok, message = llm._openai_health(values["openai_api_key"], values["openai_model"])
        return {"ok": ok, "latency_ms": int((time.perf_counter() - started) * 1000),
                "models": [values["openai_model"]] if ok else [], "error": None if ok else message}

    host = values["llm_host"].rstrip("/")
    try:
        resp = requests.get(f"{host}/api/tags", timeout=5)
        resp.raise_for_status()
        models = [m.get("name", "") for m in resp.json().get("models", [])]
    except (requests.RequestException, ValueError) as exc:
        return {"ok": False, "error": f"Cannot reach the model server at {host}: {type(exc).__name__}. "
                                      "Is `ollama serve` running there?"}
    model = values["llm_model"]
    found = model in models or f"{model}:latest" in models or any(m.split(":")[0] == model for m in models)
    return {"ok": found, "latency_ms": int((time.perf_counter() - started) * 1000), "models": models[:50],
            "error": None if found else f"The server is up but model '{model}' is not installed. "
                                        f"Run: ollama pull {model}"}


def _register(request_id: str) -> threading.Event:
    event = threading.Event()
    with _running_lock:
        _running[request_id] = event
    return event


def _unregister(request_id: str) -> None:
    with _running_lock:
        _running.pop(request_id, None)


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    request_id = req.request_id or uuid.uuid4().hex[:12]
    agent = get_agent()
    cancel = _register(request_id)
    try:
        result = agent.ask(req.message, [t.model_dump() for t in req.history], req.state, req.depth,
                           req.max_seconds, cancel=cancel)
    except SourceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("chat failed %s", request_id)
        raise HTTPException(status_code=500, detail=f"Internal error (request {request_id}).") from exc
    finally:
        _unregister(request_id)
    return _response(result, request_id)


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest, http: Request):
    request_id = req.request_id or uuid.uuid4().hex[:12]
    agent = get_agent()
    loop = asyncio.get_running_loop()
    events: asyncio.Queue = asyncio.Queue()
    cancel = _register(request_id)

    def push(event: dict) -> None:
        loop.call_soon_threadsafe(events.put_nowait, event)

    def work() -> None:
        try:
            result = agent.ask(req.message, [t.model_dump() for t in req.history], req.state, req.depth,
                               req.max_seconds, on_event=push, cancel=cancel)
            push({"type": "final", **_response(result, request_id).model_dump()})
        except SourceError as exc:
            push({"type": "error", "detail": str(exc)})
        except Exception:
            log.exception("stream failed %s", request_id)
            push({"type": "error", "detail": f"Internal error (request {request_id})."})

    loop.run_in_executor(None, work)

    async def stream():
        yield _sse({"type": "start", "request_id": request_id})
        try:
            while True:
                if await http.is_disconnected():
                    cancel.set()
                    break
                try:
                    event = await asyncio.wait_for(events.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(event)
                if event["type"] in ("final", "error"):
                    break
        finally:
            _unregister(request_id)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/chat/{request_id}/stop")
def stop(request_id: str):
    with _running_lock:
        event = _running.get(request_id)
    if event is None:
        return {"status": "not_running"}
    event.set()
    return {"status": "stopping"}


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"
