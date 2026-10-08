"""Turn raw search events into queryable columns.

Two fields arrive as packed strings and are useless until split:

  page_id     "67b58245..._Amazon_20260913T044920_12"
              -> user_ref, capture_app, session_started_at, page_seq

  search_time "2026-09-13 06:49:27.739+02"
              -> a sortable UTC string, plus local date / hour / weekday,
                 because SQLite has no date type and the raw string does not
                 sort correctly once timezone offsets differ.

app_assoc_id is the website the user searched on (2 = Flipkart, 16 = Myntra).
It is an opaque number, so data/apps.json maps it to a name.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import config

# "2026-09-13 06:49:27.739+02"  /  "...+02:00"  /  "...Z"
_TS = re.compile(
    r"^(?P<body>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)"
    r"(?P<tz>Z|[+-]\d{2}(?::?\d{2})?)?$"
)
_DOW = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

COLUMNS = [
    ("id", "INTEGER"), ("step", "INTEGER"),
    ("query", "TEXT"), ("normalized_query", "TEXT"),
    ("search_time", "TEXT"), ("search_utc", "TEXT"),
    ("search_date", "TEXT"), ("search_hour", "INTEGER"), ("search_dow", "TEXT"),
    ("hours_since_first", "REAL"),
    ("app_assoc_id", "INTEGER"), ("app_name", "TEXT"), ("app_brand", "TEXT"),
    ("journey_assoc_id", "INTEGER"), ("session_assoc_id", "INTEGER"),
    ("page_id", "TEXT"), ("user_ref", "TEXT"), ("capture_app", "TEXT"),
    ("session_started_at", "TEXT"), ("page_seq", "INTEGER"),
    ("source_accessibility_id", "INTEGER"),
]


def load_app_names(path: Path = None) -> dict:
    path = Path(path or config.APPS_MAP)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {str(k): str(v) for k, v in raw.items() if not str(k).startswith("_")}


def parse_time(value: str):
    """Return (utc_iso, local_date, local_hour, weekday). Never raises."""
    if not value:
        return None, None, None, None
    match = _TS.match(str(value).strip())
    if not match:
        return None, None, None, None

    body, tz = match.group("body").replace(" ", "T"), match.group("tz")
    if tz in (None, "Z"):
        iso = body + "+00:00"
    elif len(tz) == 3:                     # "+02" -> "+02:00"
        iso = f"{body}{tz}:00"
    elif len(tz) == 5 and ":" not in tz:   # "+0200"
        iso = f"{body}{tz[:3]}:{tz[3:]}"
    else:
        iso = body + tz

    try:
        local = datetime.fromisoformat(iso)
    except ValueError:
        return None, None, None, None

    utc = local.astimezone(timezone.utc)
    return (utc.strftime("%Y-%m-%dT%H:%M:%S"), local.strftime("%Y-%m-%d"),
            local.hour, _DOW[local.weekday()])


def split_page_id(page_id: str) -> tuple:
    """user_ref, capture_app, session_started_at, page_seq - blank if unusual."""
    parts = str(page_id or "").split("_")
    if len(parts) < 4:
        return page_id or None, None, None, None
    seq = parts[-1]
    return (parts[0], parts[1], parts[2],
            int(seq) if seq.isdigit() else None)


def normalise(records: list[dict], app_names: dict = None) -> list[dict]:
    """app_names maps id -> "Name", or id -> ("Name", "Brand") from the app master."""
    app_names = app_names if app_names is not None else load_app_names()

    def names_for(app_id):
        if app_id is None:
            return None, None
        entry = app_names.get(app_id, app_names.get(str(app_id)))
        if isinstance(entry, (tuple, list)):
            return entry[0], (entry[1] if len(entry) > 1 else entry[0])
        if entry:
            return entry, entry
        return f"App {app_id}", f"App {app_id}"

    out = []
    for rec in records:
        utc, date, hour, dow = parse_time(rec.get("search_time"))
        user_ref, capture_app, started, seq = split_page_id(rec.get("page_id"))
        app_id = rec.get("app_assoc_id")
        out.append({
            "id": rec.get("id"),
            "step": rec.get("step"),
            "query": rec.get("query"),
            "normalized_query": (rec.get("normalized_query") or "").strip(),
            "search_time": rec.get("search_time"),
            "search_utc": utc,
            "search_date": date,
            "search_hour": hour,
            "search_dow": dow,
            "hours_since_first": rec.get("hours_since_first"),
            "app_assoc_id": app_id,
            "app_name": names_for(app_id)[0],
            "app_brand": names_for(app_id)[1],
            "journey_assoc_id": rec.get("journey_assoc_id"),
            "session_assoc_id": rec.get("session_assoc_id"),
            "page_id": rec.get("page_id"),
            "user_ref": user_ref,
            "capture_app": capture_app,
            "session_started_at": started,
            "page_seq": seq,
            "source_accessibility_id": rec.get("source_accessibility_id"),
        })
    return out
