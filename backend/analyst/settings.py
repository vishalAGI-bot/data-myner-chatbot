"""Settings for the Data analyst chatbot. Everything comes from environment variables (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

PACKAGE_DIR = Path(__file__).resolve().parent


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # "postgres" = live database (optionally through SSH); "snapshot" = JSON export loaded into DuckDB.
    source: str = field(default_factory=lambda: os.getenv("ANALYST_SOURCE", "snapshot"))
    snapshot_dir: Path = field(default_factory=lambda: Path(os.getenv(
        "ANALYST_SNAPSHOT_DIR", PACKAGE_DIR.parent / "data" / "public")))
    knowledge_dir: Path = field(default_factory=lambda: Path(os.getenv(
        "ANALYST_KNOWLEDGE_DIR", PACKAGE_DIR / "knowledge")))

    # SSH tunnel (leave SSH_HOST empty to connect to the database directly).
    ssh_host: str = field(default_factory=lambda: os.getenv("SSH_HOST", ""))
    ssh_port: int = field(default_factory=lambda: int(os.getenv("SSH_PORT", "22")))
    ssh_username: str = field(default_factory=lambda: os.getenv("SSH_USERNAME", ""))
    ssh_password: str = field(default_factory=lambda: os.getenv("SSH_PASSWORD", ""))
    ssh_pkey: str = field(default_factory=lambda: os.getenv("SSH_PKEY", ""))       # path to a private key (preferred)
    # Optional: pin the SSH server's host key (shown on the Settings page after the first connection).
    ssh_host_key_sha256: str = field(default_factory=lambda: os.getenv("SSH_HOST_KEY_SHA256", ""))

    # PostgreSQL (host/port are as seen from the SSH server when a tunnel is used).
    db_host: str = field(default_factory=lambda: os.getenv("DB_HOST", "127.0.0.1"))
    db_port: int = field(default_factory=lambda: int(os.getenv("DB_PORT", "5432")))
    db_name: str = field(default_factory=lambda: os.getenv("DB_NAME", "datamyner_db"))
    db_username: str = field(default_factory=lambda: os.getenv("DB_USERNAME", ""))
    db_password: str = field(default_factory=lambda: os.getenv("DB_PASSWORD", ""))
    db_sslmode: str = field(default_factory=lambda: os.getenv("DB_SSLMODE", "prefer"))
    db_schema: str = field(default_factory=lambda: os.getenv("DB_SCHEMA", "public"))
    pool_size: int = field(default_factory=lambda: int(os.getenv("ANALYST_POOL_SIZE", "4")))

    # Safety limits.
    statement_timeout_ms: int = field(default_factory=lambda: int(os.getenv("ANALYST_STATEMENT_TIMEOUT_MS", "15000")))
    max_rows: int = field(default_factory=lambda: int(os.getenv("ANALYST_MAX_ROWS", "500")))
    allow_free_sql: bool = field(default_factory=lambda: _bool("ANALYST_ALLOW_FREE_SQL", True))

    # Business context.
    home_app: str = field(default_factory=lambda: os.getenv("ANALYST_HOME_APP", "Amazon"))
    default_window_hours: int = field(default_factory=lambda: int(os.getenv("ANALYST_WINDOW_HOURS", "24")))
    # Day boundaries for "per day" questions (both engines use the same zone).
    timezone: str = field(default_factory=lambda: os.getenv("ANALYST_TIMEZONE", "Asia/Kolkata"))
    # Live database: re-read apps, categories, row counts and the latest date this often (0 = never).
    refresh_seconds: int = field(default_factory=lambda: int(os.getenv("ANALYST_REFRESH_SECONDS", "300")))


def get_settings() -> Settings:
    return Settings()
