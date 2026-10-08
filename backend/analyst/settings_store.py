"""Settings edited from the web Settings page.

Values saved from the UI are stored in ``backend/config/analyst_settings.json``
(file mode 600, git-ignored) and override ``backend/.env``. Passwords are never
sent back to the browser: the UI only learns whether one is set.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import fields, replace
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config as llm_config

from .settings import PACKAGE_DIR, Settings

SETTINGS_FILE = Path(os.getenv("ANALYST_SETTINGS_FILE", PACKAGE_DIR.parent / "config" / "analyst_settings.json"))

SECRETS = ("ssh_password", "db_password", "openai_api_key")
SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
# Editable fields and their types. llm_* fields are not part of Settings; they update config.py at runtime.
FIELDS: dict[str, type] = {
    "source": str, "snapshot_dir": str,
    "ssh_host": str, "ssh_port": int, "ssh_username": str, "ssh_password": str, "ssh_pkey": str,
    "ssh_host_key_sha256": str,
    "db_host": str, "db_port": int, "db_name": str, "db_username": str, "db_password": str,
    "db_sslmode": str, "db_schema": str,
    "statement_timeout_ms": int, "max_rows": int, "allow_free_sql": bool,
    "home_app": str, "default_window_hours": int, "timezone": str, "refresh_seconds": int,
    "llm_provider": str, "llm_host": str, "llm_model": str,
    "openai_api_key": str, "openai_model": str,
}
LLM_FIELDS = ("llm_provider", "llm_host", "llm_model", "openai_api_key", "openai_model")
# Model settings as loaded from .env, so removing an override restores them exactly.
_ORIGINAL_LLM = {attr: getattr(llm_config, attr) for attr in (
    "LLM_PROVIDER", "OLLAMA_HOST", "SQL_MODEL", "ANSWER_MODEL", "AGENT_MODEL", "ROUTER_MODEL",
    "OPENAI_API_KEY", "OPENAI_MODEL",
)}


class SettingsError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

def load_overrides() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if k in FIELDS}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_overrides(values: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=SETTINGS_FILE.parent, prefix=".settings-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({k: v for k, v in values.items() if k in FIELDS}, handle, indent=2)
        os.chmod(tmp, 0o600)                       # passwords inside: owner-only
        os.replace(tmp, SETTINGS_FILE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# --------------------------------------------------------------------------- #
# Effective settings
# --------------------------------------------------------------------------- #

def _llm_defaults() -> dict:
    return {
        "llm_provider": _ORIGINAL_LLM["LLM_PROVIDER"],
        "llm_host": _ORIGINAL_LLM["OLLAMA_HOST"],
        "llm_model": (_ORIGINAL_LLM["SQL_MODEL"] if _ORIGINAL_LLM["LLM_PROVIDER"] == "ollama"
                      else "qwen2.5-coder:7b"),
        "openai_api_key": _ORIGINAL_LLM["OPENAI_API_KEY"],
        "openai_model": _ORIGINAL_LLM["OPENAI_MODEL"],
    }


def env_values() -> dict:
    """What .env alone gives (the base the saved overrides are compared with)."""
    base = Settings()
    values = {k: (str(getattr(base, k)) if isinstance(getattr(base, k), Path) else getattr(base, k))
              for k in FIELDS if k not in LLM_FIELDS}
    values.update(_llm_defaults())
    return values


def diff_from_env(values: dict) -> dict:
    """Keep only the values that differ from .env, so later .env edits still apply to the rest."""
    base = env_values()
    return {k: v for k, v in values.items() if k in FIELDS and base.get(k) != v}


def effective(overrides: dict | None = None) -> Settings:
    """Settings from .env with saved (or given) overrides applied."""
    values = load_overrides() if overrides is None else overrides
    names = {f.name for f in fields(Settings)}
    changes = {}
    for key, value in values.items():
        if key not in names:
            continue
        if key == "snapshot_dir":
            value = Path(value)
        changes[key] = value
    return replace(Settings(), **changes)


def apply_llm(values: dict) -> None:
    """Point the language-model client at the saved host / model (takes effect immediately).

    The analyst applies changes immediately.  The separate journey process
    watches the same settings file and applies these fields before requests.
    """
    for attr, original in _ORIGINAL_LLM.items():
        setattr(llm_config, attr, original)
    provider = values.get("llm_provider")
    host = values.get("llm_host")
    model = values.get("llm_model")
    openai_key = values.get("openai_api_key")
    openai_model = values.get("openai_model")
    if provider:
        llm_config.LLM_PROVIDER = str(provider)
    if host:
        llm_config.OLLAMA_HOST = str(host).rstrip("/")
    if model and model != _ORIGINAL_LLM["SQL_MODEL"]:
        for attr in ("SQL_MODEL", "ANSWER_MODEL", "AGENT_MODEL", "ROUTER_MODEL"):
            setattr(llm_config, attr, str(model))
    if "openai_api_key" in values:
        llm_config.OPENAI_API_KEY = str(openai_key or "")
    if openai_model:
        llm_config.OPENAI_MODEL = str(openai_model)
    if llm_config.LLM_PROVIDER == "openai":
        for attr in ("SQL_MODEL", "ANSWER_MODEL", "AGENT_MODEL", "ROUTER_MODEL"):
            setattr(llm_config, attr, llm_config.OPENAI_MODEL)
    try:                                   # forget "model is down" from the previous host
        import llm
        llm._down_until = 0.0
    except Exception:  # pragma: no cover
        pass


def public_view(settings: Settings, overrides: dict) -> dict:
    """Everything the Settings page shows. Secrets are replaced by 'is set' flags."""
    view = {}
    for key in FIELDS:
        if key in LLM_FIELDS:
            view[key] = overrides[key] if key in overrides else _llm_defaults()[key]
            continue
        value = getattr(settings, key)
        view[key] = str(value) if isinstance(value, Path) else value
    for key in SECRETS:
        view[key] = ""
    secrets_set = {
        "ssh_password": bool(settings.ssh_password),
        "db_password": bool(settings.db_password),
        "openai_api_key": bool(overrides["openai_api_key"] if "openai_api_key" in overrides
                               else _llm_defaults()["openai_api_key"]),
    }
    return {"values": view, "secrets_set": secrets_set,
            "from_ui": sorted(k for k in overrides if k not in SECRETS),
            "settings_file": str(SETTINGS_FILE)}


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

_HOST = re.compile(r"^[A-Za-z0-9_.:/\-\[\]]{1,255}$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def merge(current: dict, incoming: dict) -> dict:
    """Apply a (partial) update. An empty password keeps the saved one; clear_* flags remove it."""
    merged = dict(current)
    for key, value in incoming.items():
        if key.startswith("clear_") and key[6:] in SECRETS and value:
            merged[key[6:]] = ""
            continue
        if key not in FIELDS:
            continue
        if key in SECRETS and (value is None or value == ""):
            continue
        merged[key] = value
    return merged


def validate(values: dict) -> dict:
    """Type-convert and check every field. Raises SettingsError with per-field messages."""
    clean: dict = {}
    errors: dict[str, str] = {}
    for key, value in values.items():
        if key not in FIELDS or value is None:
            continue
        kind = FIELDS[key]
        try:
            if kind is bool:
                value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
            elif kind is int:
                value = int(value)
            else:
                value = str(value).strip()
        except (TypeError, ValueError):
            errors[key] = "must be a number"
            continue
        clean[key] = value

    def check(key, ok, message):
        if key in clean and not ok(clean[key]):
            errors[key] = message

    check("source", lambda v: v in ("postgres", "snapshot"), "choose postgres or snapshot")
    for port in ("ssh_port", "db_port"):
        check(port, lambda v: 1 <= v <= 65535, "must be between 1 and 65535")
    for host in ("ssh_host", "db_host"):
        check(host, lambda v: v == "" or bool(_HOST.match(v)), "not a valid host name")
    check("db_host", lambda v: bool(v), "required")
    check("llm_host", lambda v: v == "" or bool(re.match(r"^https?://[A-Za-z0-9_.:\-\[\]]{1,255}/?$", v)),
          "use http://host:port, e.g. http://localhost:11434")
    check("llm_model", lambda v: v == "" or bool(re.match(r"^[A-Za-z0-9_.:/\-]{1,120}$", v)), "not a valid model name")
    check("llm_provider", lambda v: v in ("ollama", "openai"), "choose Ollama or OpenAI")
    check("openai_model", lambda v: v == "gpt-6-luna", "this integration uses gpt-6-luna")
    if clean.get("llm_provider") == "openai" and not clean.get("openai_api_key"):
        errors.setdefault("openai_api_key", "required when OpenAI is selected")
    check("ssh_username", lambda v: len(v) <= 64 and not re.search(r"\s", v), "no spaces")
    check("db_username", lambda v: len(v) <= 63, "too long")
    check("ssh_host_key_sha256", lambda v: v == "" or bool(re.match(r"^SHA256:[A-Za-z0-9+/]{43}$", v)),
          "looks like SHA256:… (43 characters after the colon)")
    check("refresh_seconds", lambda v: v == 0 or 30 <= v <= 86400, "0 (off) or 30 to 86400 seconds")
    check("db_name", lambda v: bool(v) and len(v) <= 63, "required")
    check("db_schema", lambda v: bool(_IDENT.match(v)), "letters, digits and _ only")
    check("db_sslmode", lambda v: v in SSL_MODES, "one of " + ", ".join(SSL_MODES))
    check("statement_timeout_ms", lambda v: 1000 <= v <= 120000, "between 1000 and 120000 ms")
    check("max_rows", lambda v: 10 <= v <= 5000, "between 10 and 5000")
    check("default_window_hours", lambda v: v in (24, 48, 72), "24, 48 or 72")
    check("home_app", lambda v: 0 < len(v) <= 60, "required")
    # Source-specific paths must not prevent switching to the other source.  In
    # particular, a fresh install has no data/public directory; that is valid
    # when the user is configuring PostgreSQL instead of an offline snapshot.
    if clean.get("source") == "postgres":
        check("ssh_pkey", lambda v: v == "" or Path(v).expanduser().is_file(), "file not found on the server")
    elif clean.get("source") == "snapshot":
        check("snapshot_dir", lambda v: v == "" or Path(v).is_dir(), "folder not found on the server")
    if "timezone" in clean:
        try:
            ZoneInfo(clean["timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            errors["timezone"] = "unknown time zone (e.g. Asia/Kolkata)"
    if clean.get("source") == "postgres":
        if not clean.get("db_username"):
            errors.setdefault("db_username", "required for a live connection")
        if not clean.get("db_password"):
            errors.setdefault("db_password", "required for a live connection")
        if clean.get("ssh_host") and not clean.get("ssh_username"):
            errors.setdefault("ssh_username", "required when an SSH host is set")
        if clean.get("ssh_host") and not (clean.get("ssh_password") or clean.get("ssh_pkey")):
            errors.setdefault("ssh_password", "SSH password or key file required")
    if errors:
        raise SettingsError(errors)
    return clean


def current_values() -> dict:
    """Effective values (env + saved), including secrets - server side only."""
    overrides = load_overrides()
    settings = effective(overrides)
    values = {k: (str(getattr(settings, k)) if isinstance(getattr(settings, k), Path) else getattr(settings, k))
              for k in FIELDS if k not in LLM_FIELDS}
    values.update({k: overrides[k] if k in overrides else _llm_defaults()[k] for k in LLM_FIELDS})
    return values
