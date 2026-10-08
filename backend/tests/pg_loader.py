"""Load a folder of JSON table exports into a PostgreSQL database (used by tests)."""

from __future__ import annotations

import json
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from analyst.sources import infer_schema, read_export


def load_folder(folder: Path, dsn: str, skip: set[str] = frozenset()) -> dict[str, int]:
    loaded = {}
    with psycopg.connect(dsn, autocommit=True) as conn:
        for path in sorted(Path(folder).glob("*.json")):
            name = path.stem
            if name in skip:
                continue
            rows = read_export(path)
            if not rows:
                continue
            schema = infer_schema(rows)
            conn.execute(f'DROP TABLE IF EXISTS "{name}"')
            conn.execute(f'CREATE TABLE "{name}" (' + ", ".join(f'"{c}" {t}' for c, t in schema) + ")")
            cols = ", ".join(f'"{c}"' for c, _ in schema)
            marks = ", ".join(["%s"] * len(schema))

            def conv(v, t):
                if v is None:
                    return None
                if t == "jsonb":
                    return Jsonb(v)
                return v

            with conn.cursor() as cur:
                cur.executemany(f'INSERT INTO "{name}" ({cols}) VALUES ({marks})',
                                [[conv(r.get(c), t) for c, t in schema] for r in rows])
            conn.execute(f'ANALYZE "{name}"')
            loaded[name] = len(rows)
    return loaded
