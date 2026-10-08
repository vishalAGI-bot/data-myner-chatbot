"""Separate FastAPI service for the live cross-app Journey agent.

Run from the backend directory:
    uvicorn journey_agent.api:app --reload --port 8100

Contract:
    POST /api/source/live  refresh from the saved read-only PostgreSQL connection
    POST /api/dataset   multipart file (.json / .jsonl / .csv) -> dataset summary
    GET  /api/summary   -> dataset summary (+ data_quality)
    POST /api/chat      {"message": str, "history": [{"role","content"}]}
                        -> {"answer", "steps", "stopped_early", ...extra fields}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator, model_validator

import llm
from analyst import settings_store as shared_settings
from analyst.sources import SourceError

from .agent import JourneyAgent, JourneyAnswer
from .live import MAX_RANGE_DAYS, load_live_journeys
from .thinking import DEFAULT_THINK_SECONDS, MAX_THINK_SECONDS
from .store import DEFAULT_JSON_PATH, JourneyDataError, JourneyStore

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("journey_agent.api")

MAX_UPLOAD_BYTES = int(os.getenv("MAX_JOURNEY_UPLOAD_MB", "200")) * 1024 * 1024
MAX_TURN_CHARS = 6000
MAX_HISTORY_TURNS = 60
ALLOWED_SUFFIXES = {".json", ".jsonl", ".ndjson", ".csv"}
RETRY_AFTER_FAILURE_S = 20
RETIRE_AFTER_S = 75

_llm_settings_lock = threading.Lock()
_llm_settings_signature: tuple[int, int] | None | object = object()


def _refresh_llm_settings() -> None:
    """Apply provider/key changes saved by the shared Settings page."""
    global _llm_settings_signature
    try:
        stat = shared_settings.SETTINGS_FILE.stat()
        signature: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        signature = None
    if signature == _llm_settings_signature:
        return
    with _llm_settings_lock:
        if signature != _llm_settings_signature:
            shared_settings.apply_llm(shared_settings.load_overrides())
            _llm_settings_signature = signature


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = ""

    @field_validator("content", mode="before")
    @classmethod
    def clip(cls, value):
        # Long answers (a full user profile) used to fail validation with a 422
        # on the NEXT request. Clip instead of rejecting.
        text = "" if value is None else str(value)
        return text[:MAX_TURN_CHARS]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    history: list[ChatTurn] = Field(default_factory=list)
    # "auto": think deeper only for complex questions; "fast": one query;
    # "deep": always decompose + verify. max_seconds defaults to 60 s, capped at 20 min.
    depth: Literal["auto", "fast", "deep"] = "auto"
    max_seconds: float = Field(default=DEFAULT_THINK_SECONDS, ge=3, le=MAX_THINK_SECONDS)
    request_id: str | None = Field(default=None, max_length=64)   # lets the UI stop it

    @field_validator("history", mode="after")
    @classmethod
    def keep_recent(cls, turns):
        return [t for t in turns if t.content.strip()][-MAX_HISTORY_TURNS:]


class ChatResponse(BaseModel):
    answer: str
    steps: int
    stopped_early: bool
    route: str = ""
    resolved_question: str = ""
    sql: str | None = None
    table: dict | None = None
    notes: list[str] = Field(default_factory=list)
    latency_ms: int = 0
    request_id: str = ""
    depth: str = "fast"
    thinking: list[dict] = Field(default_factory=list)
    parts: list[dict] = Field(default_factory=list)
    verified: bool = True
    timed_out: bool = False
    suggested_questions: list[str] = Field(default_factory=list)


class LiveSourceRequest(BaseModel):
    mode: Literal["latest_month", "date_range", "last_24_hours"] = "latest_month"
    date_from: str | None = None
    date_to: str | None = None

    @model_validator(mode="after")
    def validate_range(self):
        if self.mode != "date_range":
            self.date_from = self.date_to = None
            return self
        from datetime import date
        try:
            start = date.fromisoformat(self.date_from or "")
            end = date.fromisoformat(self.date_to or "")
        except ValueError as exc:
            raise ValueError("Choose both Journey dates.") from exc
        if start > end:
            raise ValueError("Journey start date must be on or before the end date.")
        if (end - start).days >= MAX_RANGE_DAYS:
            raise ValueError(f"Journey date range can be at most {MAX_RANGE_DAYS} calendar days.")
        return self


def _response(result: JourneyAnswer, request_id: str) -> ChatResponse:
    return ChatResponse(
        answer=result.answer, steps=len(result.steps), stopped_early=result.stopped_early,
        route=result.route, resolved_question=result.resolved_question, sql=result.sql,
        table=result.table, notes=result.notes, latency_ms=result.latency_ms,
        request_id=request_id, depth=result.depth, thinking=result.thinking,
        parts=result.parts, verified=result.verified, timed_out=result.timed_out,
        suggested_questions=result.suggested_questions)


_running: dict[str, threading.Event] = {}      # request_id -> stop signal
_running_lock = threading.Lock()


def _register(request_id: str) -> threading.Event:
    event = threading.Event()
    with _running_lock:
        _running[request_id] = event
    return event


def _unregister(request_id: str) -> None:
    with _running_lock:
        _running.pop(request_id, None)


app = FastAPI(title="Cross-App Journey Agent API", version="2.0.0")
_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["http://localhost:3000", "http://localhost:5173"],
    allow_origin_regex=None if _origins else r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_agent: JourneyAgent | None = None
_agent_tempdir: tempfile.TemporaryDirectory | None = None
_filename: str | None = None
_mode = "live"
_source_info: dict = {"kind": "postgres", "table": "cross_app_journey", "read_only": True}
_fingerprint: str | None = None
_connected_since: float | None = None
_refreshed_at: float | None = None
_refresh_error: str | None = None
_failed_at = 0.0
_db_ping_ms: int | None = None
_live_filter: dict = {"filter_mode": "latest_month", "date_from": None, "date_to": None}
_swap_lock = threading.Lock()          # guards only references/metadata, never a chat
_refresh_lock = threading.Lock()       # only one database snapshot may be built at once
_refresher_started = threading.Event()


def _close_old(agent: JourneyAgent | None, tempdir: tempfile.TemporaryDirectory | None) -> None:
    if agent is not None:
        try:
            agent.store.close()
        except Exception:
            log.warning("could not close retired journey store", exc_info=True)
    if tempdir is not None:
        try:
            tempdir.cleanup()
        except Exception:
            log.warning("could not remove retired journey upload", exc_info=True)


def _retire(agent: JourneyAgent | None, tempdir: tempfile.TemporaryDirectory | None) -> None:
    """Keep an old snapshot alive longer than the maximum in-flight chat."""
    if agent is None and tempdir is None:
        return
    timer = threading.Timer(RETIRE_AFTER_S, _close_old, args=(agent, tempdir))
    timer.daemon = True
    timer.start()


def _safe_refresh_error(exc: BaseException) -> str:
    """Return actionable text without leaking saved hosts, users, paths or passwords."""
    text = str(exc).strip().splitlines()[0][:400] or "Live journey refresh failed."
    try:
        from analyst.settings_store import effective
        settings = effective()
        private = (
            settings.ssh_password, settings.db_password, settings.ssh_username,
            settings.db_username, settings.ssh_host, settings.db_host, settings.ssh_pkey,
        )
        for value in private:
            if value and len(str(value)) >= 3:
                text = text.replace(str(value), "[hidden]")
    except Exception:
        pass
    return text


def _refresh_interval() -> int:
    try:
        from analyst.settings_store import effective
        return max(0, int(effective().refresh_seconds))
    except Exception:
        return 300


def _refresh_live(force: bool = False, requested_filter: dict | None = None) -> bool:
    """Build a complete PostgreSQL snapshot and atomically activate it."""
    global _agent, _agent_tempdir, _filename, _mode, _source_info, _fingerprint
    global _connected_since, _refreshed_at, _refresh_error, _failed_at, _db_ping_ms, _live_filter

    now = time.time()
    with _swap_lock:
        active_filter = dict(requested_filter) if requested_filter is not None else dict(_live_filter)
        if _mode != "live" and not force:
            return _agent is not None
        if not force:
            if _agent is not None:
                interval = _refresh_interval()
                if interval <= 0 or (_refreshed_at and now - _refreshed_at < interval):
                    return True
            elif _refresh_error and now - _failed_at < RETRY_AFTER_FAILURE_S:
                return False

    # Do not hold the state lock while opening SSH or copying rows.
    if not _refresh_lock.acquire(blocking=force):
        with _swap_lock:
            return _agent is not None
    loaded = None
    try:
        # Re-check after waiting for a forced refresh lock.
        if not force:
            with _swap_lock:
                interval = _refresh_interval()
                if (_agent is not None and
                        (interval <= 0 or (_refreshed_at and time.time() - _refreshed_at < interval))):
                    return True
        loaded = load_live_journeys(**active_filter)
        new_agent = JourneyAgent(loaded.store)
        activated_at = time.time()
        with _swap_lock:
            old_agent, old_tempdir = _agent, _agent_tempdir
            previous_key = (_source_info.get("database"), _source_info.get("schema"),
                            _source_info.get("table"), _source_info.get("via_ssh"))
            new_key = (loaded.source.get("database"), loaded.source.get("schema"),
                       loaded.source.get("table"), loaded.source.get("via_ssh"))
            keep_since = _mode == "live" and _agent is not None and previous_key == new_key
            _agent, _agent_tempdir, _filename = new_agent, None, None
            _mode, _source_info, _fingerprint = "live", dict(loaded.source), loaded.fingerprint
            _live_filter = {
                "filter_mode": loaded.source.get("filter_mode", "latest_month"),
                "date_from": loaded.source.get("date_from"),
                "date_to": loaded.source.get("date_to"),
            }
            _connected_since = _connected_since if keep_since else activated_at
            _refreshed_at = activated_at
            _refresh_error, _failed_at, _db_ping_ms = None, 0.0, loaded.db_ping_ms
        _retire(old_agent, old_tempdir)
        _ensure_refresher()
        log.info("live journeys refreshed: %s rows, dates %s to %s",
                 new_agent.store.record_count, new_agent.store.summary().get("first_date"),
                 new_agent.store.summary().get("last_date"))
        return True
    except Exception as exc:
        if loaded is not None:
            try:
                loaded.store.close()
            except Exception:
                pass
        public_error = _safe_refresh_error(exc)
        with _swap_lock:
            _refresh_error, _failed_at = public_error, time.time()
        if isinstance(exc, (SourceError, JourneyDataError, FileNotFoundError, ValueError)):
            log.warning("live journey refresh failed: %s", public_error)
        else:
            log.exception("live journey refresh failed")
        return False
    finally:
        _refresh_lock.release()


def _refresher() -> None:
    while True:
        time.sleep(15)
        with _swap_lock:
            live = _mode == "live"
        if live:
            _refresh_live(force=False)


def _ensure_refresher() -> None:
    if not _refresher_started.is_set():
        _refresher_started.set()
        threading.Thread(target=_refresher, name="journey-live-refresh", daemon=True).start()


def _preload() -> None:
    """Optionally load a default dataset at start-up (JOURNEY_PRELOAD=1)."""
    global _agent, _filename, _mode, _source_info, _connected_since, _refreshed_at
    if os.getenv("JOURNEY_PRELOAD", "0") != "1":
        return
    path = Path(os.getenv("JOURNEY_JSON", str(DEFAULT_JSON_PATH)))
    try:
        _agent = JourneyAgent(JourneyStore(path, source_label=f"preloaded file {path.name}"))
        _filename = path.name
        _mode = "upload"
        _source_info = {"kind": "upload", "filename": path.name, "read_only": True}
        _connected_since = _refreshed_at = time.time()
        log.info("preloaded %s (%s journeys)", path.name, _agent.store.record_count)
    except Exception as exc:  # pragma: no cover - start-up convenience only
        log.error("preload failed: %s", exc)


_preload()


def get_agent() -> JourneyAgent:
    _refresh_llm_settings()
    if _mode == "live":
        _refresh_live(force=False)
    agent = _agent                      # read the reference once; swaps are atomic
    if agent is None:
        detail = _refresh_error or "The live cross_app_journey table is not ready. Check Connection settings."
        raise HTTPException(status_code=503, detail=f"Live journey data not ready: {detail}")
    return agent


@app.get("/api/health")
def health():
    _refresh_llm_settings()
    if _mode == "live":
        _refresh_live(force=False)
    healthy, message = llm.health()
    with _swap_lock:
        agent = _agent
        count = agent.store.record_count if agent else 0
        source = dict(_source_info)
        error = _refresh_error
        mode = _mode
        connected_since = _connected_since
        refreshed_at = _refreshed_at
        db_ping_ms = _db_ping_ms
    ready = count > 0 and (mode != "live" or source.get("kind") == "postgres")
    if error:
        source["error"] = error
    latest_date = agent.store.summary().get("last_date") if agent else None
    return {
        "status": "ok" if healthy and ready and not error else "degraded",
        "data_ready": ready,
        "data_loaded": ready,
        "data": f"{count} journeys loaded",
        "source": source,
        "db_ping_ms": db_ping_ms,
        "connected_since": connected_since,
        "refreshed_at": refreshed_at,
        "latest_date": latest_date,
        "stale": bool(error and count),
        "refresh_error": error,
        "llm": message,
        "ollama": message,  # compatibility with older frontends
        "llm_ok": healthy,
        "llm_provider": llm.config.LLM_PROVIDER,
        "llm_model": llm.config.SQL_MODEL,
        # Built-in questions work without the model; only unusual ones need it.
        "llm_required_for": "unusual questions only" if count else "n/a",
    }


@app.get("/api/summary")
def summary():
    get_agent()  # connect/refresh before taking one consistent state snapshot
    with _swap_lock:
        agent = _agent
        source = dict(_source_info)
        filename = _filename
    if agent is None:  # defensive: get_agent already raises in this state
        raise HTTPException(status_code=503, detail="Live journey data not ready.")
    result = agent.store.summary()
    result["source"] = source
    result["filename"] = filename
    return result


@app.post("/api/source/live")
def connect_live(request: LiveSourceRequest | None = None):
    """Use the analyst's saved read-only connection and refresh cross_app_journey now."""
    selected = None
    if request is not None:
        selected = {"filter_mode": request.mode,
                    "date_from": request.date_from, "date_to": request.date_to}
    if not _refresh_live(force=True, requested_filter=selected):
        raise HTTPException(status_code=503,
                            detail=f"Live journey data not ready: {_refresh_error or 'refresh failed'}")
    return summary()


@app.post("/api/dataset")
def upload_dataset(file: UploadFile = File(...)):
    """Legacy file fallback; the web app uses the live PostgreSQL source."""
    global _agent, _agent_tempdir, _filename, _mode, _source_info
    global _connected_since, _refreshed_at, _refresh_error, _failed_at, _db_ping_ms

    filename = Path(file.filename or "journeys.json").name
    if Path(filename).suffix.lower() not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="Upload a .json, .jsonl or .csv journey file.")

    new_tempdir = tempfile.TemporaryDirectory(prefix="journey-agent-")
    upload_path = Path(new_tempdir.name) / filename
    size = 0
    try:
        with upload_path.open("wb") as destination:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Journey file must be smaller than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
                destination.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="The uploaded file is empty.")
        new_agent = JourneyAgent(JourneyStore(upload_path, source_label=f"uploaded file {filename}"))
    except HTTPException:
        new_tempdir.cleanup()
        raise
    except (JourneyDataError, FileNotFoundError, TypeError, ValueError) as exc:
        new_tempdir.cleanup()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        new_tempdir.cleanup()
        raise
    finally:
        file.file.close()

    with _swap_lock:
        old_agent, old_tempdir = _agent, _agent_tempdir
        _agent, _agent_tempdir, _filename = new_agent, new_tempdir, filename
        _mode = "upload"
        _source_info = {"kind": "upload", "filename": filename, "read_only": True}
        _connected_since = _refreshed_at = time.time()
        _refresh_error, _failed_at, _db_ping_ms = None, 0.0, None
    _retire(old_agent, old_tempdir)

    result = new_agent.store.summary()
    result["source"] = dict(_source_info)
    log.info("dataset %s loaded: %s journeys, %s skipped", filename, result["journeys"],
             result["data_quality"]["records_skipped"])
    return {"filename": filename, **result}


@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest, http_request: Request) -> ChatResponse:
    """Blocking chat. Returns after at most ``max_seconds`` (20 min cap)."""
    request_id = request.request_id or http_request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    agent = get_agent()
    cancel = _register(request_id)
    try:
        result = agent.ask(request.message, [t.model_dump() for t in request.history],
                           depth=request.depth, max_seconds=request.max_seconds, cancel=cancel)
    except llm.OllamaError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("chat failed request_id=%s", request_id)
        raise HTTPException(status_code=500, detail=f"Internal error (request {request_id}).") from exc
    finally:
        _unregister(request_id)
    log.info("request_id=%s route=%s depth=%s latency_ms=%s", request_id, result.route,
             result.depth, result.latency_ms)
    return _response(result, request_id)


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest, http_request: Request):
    """Server-sent events: live 'thinking' steps, then the final answer.

    Events (each line ``data: {json}``):
      {"type": "start", "request_id", "max_seconds"}
      {"type": "thinking", "stage", "text", "elapsed_ms", "ok"}   (0..n)
      {"type": "final", ...same fields as /api/chat...}
      {"type": "error", "detail"}
    Closing the connection, or POST /api/chat/{request_id}/stop, stops the work.
    """
    request_id = request.request_id or uuid.uuid4().hex[:12]
    agent = get_agent()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    cancel = _register(request_id)

    def push(event: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def work() -> None:
        try:
            result = agent.ask(request.message, [t.model_dump() for t in request.history],
                               depth=request.depth, max_seconds=request.max_seconds,
                               on_event=push, cancel=cancel)
            push({"type": "final", **_response(result, request_id).model_dump()})
        except llm.OllamaError as exc:
            push({"type": "error", "detail": str(exc)})
        except Exception:
            log.exception("stream failed request_id=%s", request_id)
            push({"type": "error", "detail": f"Internal error (request {request_id})."})

    loop.run_in_executor(None, work)

    async def events():
        yield _sse({"type": "start", "request_id": request_id, "max_seconds": request.max_seconds})
        try:
            while True:
                if await http_request.is_disconnected():
                    cancel.set()
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(event)
                if event["type"] in ("final", "error"):
                    break
        finally:
            _unregister(request_id)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/chat/{request_id}/stop")
def stop_chat(request_id: str):
    """The UI's Stop button: finish now with whatever has been verified so far."""
    with _running_lock:
        event = _running.get(request_id)
    if event is None:
        return {"status": "not_running"}
    event.set()
    return {"status": "stopping"}


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"
