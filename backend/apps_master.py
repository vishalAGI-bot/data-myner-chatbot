"""The app master: which websites exist, and which are sub-apps of which.

parent_id is the important column. app_assoc_id 16 is "Amazon Now", whose
parent is "Amazon" - so a journey moving from app 2 to app 16 never left the
Amazon brand. Without resolving the parent you would count that as cross-app
behaviour, which it is not.

Two names therefore end up on every event:
  app_name  - the exact surface searched on   ("Amazon Now")
  app_brand - the root of the hierarchy       ("Amazon")
"""

import config

REQUIRED = ("app_name", "slug", "parent_id")

COLUMNS = [
    ("id", "INTEGER PRIMARY KEY"), ("app_name", "TEXT"), ("slug", "TEXT"),
    ("description", "TEXT"), ("parent_id", "INTEGER"),
    ("parent_app_name", "TEXT"), ("app_brand", "TEXT"), ("is_sub_app", "INTEGER"),
    ("is_active", "INTEGER"), ("is_deleted", "INTEGER"),
    ("created_at", "TEXT"), ("remarks", "TEXT"),
]


def _as_int(value):
    return int(value) if isinstance(value, bool) else (
        int(value) if str(value).lstrip("-").isdigit() else None)


def resolve(records: list[dict]) -> list[dict]:
    """Flatten the parent chain onto every row."""
    by_id = {}
    for rec in records:
        app_id = _as_int(rec.get("id"))
        if app_id is not None:
            by_id[app_id] = rec

    def root(app_id, seen=None):
        """Walk up to the top parent, guarding against a cycle in the data."""
        seen = seen or set()
        rec = by_id.get(app_id)
        if not rec or app_id in seen:
            return rec
        parent = _as_int(rec.get("parent_id"))
        if parent is None or parent not in by_id:
            return rec
        return root(parent, seen | {app_id})

    out = []
    for rec in records:
        app_id = _as_int(rec.get("id"))
        parent_id = _as_int(rec.get("parent_id"))
        parent = by_id.get(parent_id) if parent_id is not None else None
        top = root(app_id) or rec
        out.append({
            "id": app_id,
            "app_name": rec.get("app_name"),
            "slug": rec.get("slug"),
            "description": rec.get("description"),
            "parent_id": parent_id,
            "parent_app_name": parent.get("app_name") if parent else None,
            "app_brand": top.get("app_name"),
            "is_sub_app": 1 if parent_id is not None else 0,
            "is_active": 1 if rec.get("is_active") else 0,
            "is_deleted": 1 if rec.get("is_deleted") else 0,
            "created_at": rec.get("created_at"),
            "remarks": rec.get("remarks"),
        })
    return out


def lookup_from_db(conn) -> dict:
    """{app_id: (app_name, app_brand)} from the app table, or {} if absent."""
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (config.APPS_TABLE,)).fetchone()
    if not exists:
        return {}
    return {row[0]: (row[1], row[2]) for row in conn.execute(
        f"SELECT id, app_name, app_brand FROM {config.APPS_TABLE}")}
