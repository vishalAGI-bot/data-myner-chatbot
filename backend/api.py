"""FastAPI backend: one private SQLite database per chat session.

A session accepts SEVERAL files - the taxonomy, the product master, the app
master and the search events all land in the same database, because the
interesting questions join them. The file type is detected from its contents,
so the caller does not have to say which is which.

Run with:
    uvicorn api:app --reload --port 8000
"""

import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import config
import db
import ingest
import llm
from chatbot import Chatbot


MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "50")) * 1024 * 1024
# Sessions hold a temp directory and an open SQLite handle. Without a sweeper
# an abandoned browser tab leaks both until the process restarts.
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_MINUTES", "120")) * 60
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "50"))
UPLOAD_CHUNK_BYTES = 1024 * 1024
ALLOWED_SUFFIXES = {".json", ".jsonl"}


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=4000)


class SessionRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)


class ChatResponse(BaseModel):
    answer: str
    path: str
    domain: str = ""            # which specialist agent answered
    sql: str | None = None
    evidence: str | None = None
    steps: int = 0
    stopped_early: bool = False


class DatasetSummary(BaseModel):
    filename: str
    kind: str                   # taxonomy | products | apps | events
    records: int
    stored_rows: int
    note: str = ""


class SessionResponse(BaseModel):
    session_id: str
    datasets: list[DatasetSummary]
    tables: dict[str, int]      # table name -> row count
    ready_for: list[str]        # which kinds of question can be answered now
    missing: list[str]          # what is still worth uploading


class HealthResponse(BaseModel):
    status: str
    ollama: str
    active_sessions: int


@dataclass
class SessionState:
    session_id: str
    temporary_directory: tempfile.TemporaryDirectory
    db_path: Path
    bot: Chatbot | None = None
    datasets: list = field(default_factory=list)
    last_used: float = field(default_factory=time.time)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def touch(self) -> None:
        self.last_used = time.time()

    def rebuild_bot(self) -> None:
        """Re-open the database and rebuild the agents after an ingest.

        The schema card, the fuzzy index and the per-domain agents are all
        built at construction time, so a new dataset means a new Chatbot.
        """
        if self.bot is not None:
            try:
                self.bot.conn.close()
            except Exception:
                pass
        self.bot = Chatbot(conn=db.connect(self.db_path))


app = FastAPI(title="DataMyner Intelligence API", version="2.0.0")

configured_origins = os.getenv("CORS_ORIGINS")
default_origins = "http://localhost:3000,http://localhost:5173"
origins = [
    origin.strip()
    for origin in (configured_origins or default_origins).split(",")
    if origin.strip()
]
# During local development Vinext/Vite selects the next available port when
# 3000 is occupied. Permit localhost on any port only when the user has not
# supplied an explicit production allow-list.
origin_regex = None
if configured_origins is None:
    origin_regex = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=origin_regex,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

_sessions: dict[str, SessionState] = {}
_sessions_lock = threading.RLock()


KIND_LABELS = {
    "taxonomy": "category tree",
    "products": "product catalogue",
    "apps": "shopping apps",
    "events": "search events",
}


def ingest_into(session: SessionState, upload_path: Path, filename: str
                ) -> DatasetSummary:
    """Detect the file type and load it into this session's database."""
    import apps_master
    import events as events_mod
    import products as products_mod

    records = ingest.load_json(upload_path)
    if not records:
        raise HTTPException(status_code=400, detail="The file has no records.")
    kind = ingest.detect_kind(records)          # raises ValueError if unknown

    note = ""
    if kind == "taxonomy":
        rows = ingest.load_rows(upload_path)
        unique = ingest.dedupe(rows)
        if len(unique) < len(rows):
            note = f"collapsed {len(rows) - len(unique)} duplicate rows"
        ingest.build_db(unique, session.db_path).close()
        stored = len(unique)
    elif kind == "products":
        rows = products_mod.normalise(records)
        ingest.build_products_db(rows, session.db_path).close()
        stored = len(rows)
        note = f"{len({r['brand_key'] for r in rows if r['brand']})} brands"
    elif kind == "apps":
        rows = apps_master.resolve(records)
        conn = ingest.build_apps_db(rows, session.db_path)
        ingest.backfill_event_apps(conn)
        conn.close()
        stored = len(rows)
        note = f"{sum(r['is_sub_app'] for r in rows)} sub-apps"
    else:                                        # events
        import sqlite3 as _sq
        probe = _sq.connect(session.db_path)
        app_names = apps_master.lookup_from_db(probe)
        probe.close()
        rows = events_mod.normalise(records, app_names)
        ingest.build_events_db(rows, session.db_path).close()
        stored = len(rows)
        if not app_names:
            note = "upload app_master.json to turn app ids into names"

    # The bridge depends on taxonomy + products + events, so rebuild it after
    # every upload rather than only when the events file lands.
    import sqlite3 as _sq
    conn = _sq.connect(session.db_path)
    try:
        ingest.build_link_table(conn)
    except Exception:
        pass
    conn.close()

    session.rebuild_bot()
    return DatasetSummary(filename=filename, kind=kind, records=len(records),
                          stored_rows=stored, note=note)


def describe_session(session: SessionState) -> SessionResponse:
    conn = session.bot.conn
    tables = {}
    for name in (config.TABLE, config.PRODUCTS_TABLE, config.APPS_TABLE,
                 config.EVENTS_TABLE, config.LINK_TABLE):
        if db.has_table(conn, name):
            tables[name] = conn.execute(
                f"SELECT COUNT(*) FROM {name}").fetchone()[0]

    ready, missing = [], []
    (ready if config.TABLE in tables else missing).append("category questions")
    (ready if config.PRODUCTS_TABLE in tables else missing).append(
        "product and brand questions")
    (ready if config.EVENTS_TABLE in tables else missing).append(
        "search and journey questions")
    if config.EVENTS_TABLE in tables and config.APPS_TABLE not in tables:
        missing.append("readable app names (upload the app master)")
    if tables.get(config.LINK_TABLE):
        ready.append("which categories people search for")

    return SessionResponse(
        session_id=session.session_id,
        datasets=session.datasets,
        tables=tables,
        ready_for=ready,
        missing=missing,
    )


def sweep_expired() -> None:
    """Drop sessions nobody has used recently."""
    cutoff = time.time() - SESSION_TTL_SECONDS
    with _sessions_lock:
        stale = [sid for sid, st in _sessions.items() if st.last_used < cutoff]
        victims = [_sessions.pop(sid) for sid in stale]
    for victim in victims:
        try:
            close_session(victim)
        except Exception:
            pass


def get_session(session_id: str) -> SessionState:
    with _sessions_lock:
        session = _sessions.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail="This chat session no longer exists. Upload the files again.",
        )
    session.touch()
    return session


def close_session(session: SessionState) -> None:
    with session.lock:
        if session.bot is not None:
            try:
                session.bot.conn.close()
            except Exception:
                pass
        session.temporary_directory.cleanup()


def save_upload(file: UploadFile, destination: Path) -> str:
    """Stream to disk with a hard size cap. Returns the safe filename."""
    filename = Path(file.filename or "dataset.json").name
    if Path(filename).suffix.lower() not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400,
                            detail="Upload a .json or .jsonl file.")
    total = 0
    with destination.open("wb") as out:
        while chunk := file.file.read(UPLOAD_CHUNK_BYTES):
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"File is larger than "
                           f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
            out.write(chunk)
    return filename


@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    sweep_expired()
    healthy, ollama_message = llm.health()
    with _sessions_lock:
        active_sessions = len(_sessions)
    return HealthResponse(
        status="ok" if healthy else "degraded",
        ollama=ollama_message,
        active_sessions=active_sessions,
    )


@app.post("/api/sessions", response_model=SessionResponse)
def create_session(file: UploadFile = File(...)) -> SessionResponse:
    """Start a session from the first file. Add more with /api/sessions/{id}/datasets."""
    sweep_expired()
    with _sessions_lock:
        if len(_sessions) >= MAX_SESSIONS:
            raise HTTPException(
                status_code=503,
                detail="Too many active sessions. Try again shortly.")

    temporary_directory = tempfile.TemporaryDirectory(prefix="datamyner-session-")
    root = Path(temporary_directory.name)
    session_id = uuid.uuid4().hex
    session = SessionState(session_id=session_id,
                           temporary_directory=temporary_directory,
                           db_path=root / "chat.db")
    try:
        filename = save_upload(file, root / "upload-0.json")
        summary = ingest_into(session, root / "upload-0.json", filename)
        session.datasets.append(summary)
        response = describe_session(session)
    except HTTPException:
        close_session(session)
        raise
    except ValueError as exc:
        close_session(session)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        close_session(session)
        raise HTTPException(status_code=500,
                            detail=f"Could not load dataset: {exc}") from exc

    with _sessions_lock:
        _sessions[session_id] = session
    return response


@app.post("/api/sessions/{session_id}/datasets", response_model=SessionResponse)
def add_dataset(session_id: str, file: UploadFile = File(...)) -> SessionResponse:
    """Add another file to an existing session, so the datasets can be joined."""
    session = get_session(session_id)
    root = Path(session.temporary_directory.name)
    target = root / f"upload-{len(session.datasets)}.json"
    with session.lock:
        try:
            filename = save_upload(file, target)
            summary = ingest_into(session, target, filename)
            session.datasets.append(summary)
            return describe_session(session)
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Could not load dataset: {exc}") from exc


@app.get("/api/sessions/{session_id}", response_model=SessionResponse)
def read_session(session_id: str) -> SessionResponse:
    """Check a session is still alive and see what is loaded."""
    session = get_session(session_id)
    with session.lock:
        return describe_session(session)


@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="message cannot be blank")

    session = get_session(request.session_id)
    try:
        with session.lock:
            turn = session.bot.ask(message)
    except llm.OllamaError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return ChatResponse(
        answer=turn.answer,
        path=turn.path,
        domain=getattr(turn, "domain", ""),
        sql=turn.sql or None,
        evidence=turn.evidence or None,
        steps=len(turn.steps),
        stopped_early=turn.stopped_early,
    )


@app.post("/api/reset")
def reset(request: SessionRequest) -> dict[str, str]:
    session = get_session(request.session_id)
    with session.lock:
        session.bot.reset()
    return {"status": "ok"}


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str) -> dict[str, str]:
    with _sessions_lock:
        session = _sessions.pop(session_id, None)
    if session is None:
        return {"status": "already_deleted"}
    close_session(session)
    return {"status": "deleted"}
