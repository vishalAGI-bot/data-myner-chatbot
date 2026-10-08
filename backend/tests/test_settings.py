"""Settings page: validation, secret handling, admin protection and live switching to PostgreSQL.

    python -m pytest tests/test_settings.py -q

The live-switch test starts a real PostgreSQL 16 (pgserver) and switches the running API from the offline
snapshot to it without a restart, then proves answers come from the live database (a row inserted after
the switch appears in the next answer).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import llm  # noqa: E402
from analyst import settings_store as store  # noqa: E402
from analyst.catalog import Catalog  # noqa: E402
from analyst_fixture import build  # noqa: E402

KNOWLEDGE = BACKEND / "analyst" / "knowledge"
LOCAL = ("127.0.0.1", 50000)


def offline(**_):
    raise llm.OllamaError("offline in tests")


@pytest.fixture(scope="module")
def snap(tmp_path_factory):
    return build(tmp_path_factory.mktemp("snap") / "public")


@pytest.fixture(scope="module")
def pg_uri(snap, tmp_path_factory):
    pgserver = pytest.importorskip("pgserver")
    from pg_loader import load_folder
    server = pgserver.get_server(str(tmp_path_factory.mktemp("pgs")), cleanup_mode="stop")
    load_folder(snap, server.get_uri(), Catalog(KNOWLEDGE, "Amazon").blocked_tables)
    yield server.get_uri()


def pg_values(uri: str) -> dict:
    import urllib.parse as up
    u = up.urlparse(uri)
    return {"source": "postgres", "ssh_host": "", "db_host": dict(up.parse_qsl(u.query)).get("host", "127.0.0.1"),
            "db_port": u.port or 5432, "db_name": u.path.lstrip("/") or "postgres",
            "db_username": u.username or "postgres", "db_password": "s3cret-pass"}


@pytest.fixture()
def api(snap, tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_SOURCE", "snapshot")
    monkeypatch.setenv("ANALYST_SNAPSHOT_DIR", str(snap))
    monkeypatch.delenv("ANALYST_ADMIN_TOKEN", raising=False)
    for key in ("SSH_HOST", "SSH_PASSWORD", "DB_PASSWORD", "DB_USERNAME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(store, "SETTINGS_FILE", tmp_path / "config" / "analyst_settings.json")
    from analyst import api as module
    monkeypatch.setattr(module, "RETIRE_AFTER_S", 0.05)
    module._activate(None)
    module._state.update(error=None, failed_at=0.0)
    yield module
    module._activate(None)
    module._state.update(error=None, failed_at=0.0)
    store.apply_llm({})


def client(module, host=LOCAL, **kw):
    from fastapi.testclient import TestClient
    kw.setdefault("base_url", "http://127.0.0.1:8200")
    return TestClient(module.app, client=host, **kw)


# --------------------------------------------------------------------------- store
def test_validate_reports_each_bad_field():
    with pytest.raises(store.SettingsError) as err:
        store.validate({"source": "mysql", "db_port": "abc", "ssh_port": 70000, "db_schema": "public; drop",
                        "timezone": "Mars/Base", "max_rows": 5, "default_window_hours": 12,
                        "llm_host": "localhost:11434", "db_sslmode": "maybe"})
    errors = err.value.errors
    for key in ("source", "db_port", "ssh_port", "db_schema", "timezone", "max_rows", "default_window_hours",
                "llm_host", "db_sslmode"):
        assert key in errors, key


def test_postgres_needs_login_and_ssh_secret():
    with pytest.raises(store.SettingsError) as err:
        store.validate({"source": "postgres", "db_host": "127.0.0.1", "db_name": "x", "ssh_host": "10.0.0.5",
                        "db_username": "", "db_password": ""})
    assert {"db_username", "db_password", "ssh_password", "ssh_username"} <= set(err.value.errors)


def test_postgres_does_not_validate_unused_snapshot_folder():
    values = store.validate({
        "source": "postgres",
        "snapshot_dir": "/no/such/offline-snapshot",
        "ssh_host": "",
        "db_host": "127.0.0.1",
        "db_port": 5432,
        "db_name": "datamyner_db",
        "db_username": "reader",
        "db_password": "secret",
    })
    assert values["source"] == "postgres"


def test_merge_keeps_saved_password_unless_cleared():
    current = {"db_password": "old", "ssh_password": "ssh-old", "db_host": "a"}
    assert store.merge(current, {"db_password": "", "db_host": "b"}) == {**current, "db_host": "b"}
    assert store.merge(current, {"db_password": "new"})["db_password"] == "new"
    assert store.merge(current, {"clear_ssh_password": True})["ssh_password"] == ""
    assert "evil" not in store.merge(current, {"evil": 1, "knowledge_dir": "/etc"})


def test_saved_file_is_private_and_only_holds_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "SETTINGS_FILE", tmp_path / "s.json")
    values = {**store.env_values(), "home_app": "Flipkart", "db_password": "pw"}
    store.save_overrides(store.diff_from_env(values))
    assert stat.S_IMODE(os.stat(tmp_path / "s.json").st_mode) == 0o600
    assert json.loads((tmp_path / "s.json").read_text()) == {"home_app": "Flipkart", "db_password": "pw"}
    assert store.effective().home_app == "Flipkart"


def test_llm_override_applies_and_resets():
    import config
    before = (config.LLM_PROVIDER, config.OLLAMA_HOST, config.SQL_MODEL, config.ANSWER_MODEL)
    store.apply_llm({"llm_host": "http://gpu-box:11434/", "llm_model": "qwen3:14b"})
    assert config.OLLAMA_HOST == "http://gpu-box:11434" and config.ANSWER_MODEL == "qwen3:14b"
    store.apply_llm({"llm_provider": "openai", "openai_api_key": "sk-test", "openai_model": "gpt-6-luna"})
    assert config.LLM_PROVIDER == "openai" and config.ANSWER_MODEL == "gpt-6-luna"
    assert config.OPENAI_API_KEY == "sk-test"
    store.apply_llm({})
    assert (config.LLM_PROVIDER, config.OLLAMA_HOST, config.SQL_MODEL, config.ANSWER_MODEL) == before


# --------------------------------------------------------------------------- admin protection
def test_settings_refused_from_other_computers(api):
    assert client(api, host=("203.0.113.9", 4000)).get("/api/settings").status_code == 403
    # a proxy on this machine forwarding a remote visitor
    assert client(api).get("/api/settings", headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 403
    # another website in the admin's own browser
    assert client(api).put("/api/settings", json={"values": {}},
                           headers={"Origin": "https://evil.example"}).status_code == 403
    # DNS rebinding: loopback connection but a foreign Host name
    assert client(api, base_url="http://evil.example:8200").get("/api/settings").status_code == 403
    assert client(api, host=("203.0.113.9", 4000)).post("/api/reload").status_code == 403
    assert client(api).get("/api/settings", headers={"Origin": "http://localhost:5173"}).status_code == 200


def test_admin_token(api, monkeypatch):
    monkeypatch.setenv("ANALYST_ADMIN_TOKEN", "t0ken-123")
    remote = client(api, host=("203.0.113.9", 4000))
    assert remote.get("/api/settings").status_code == 401
    assert remote.get("/api/settings", headers={"X-Admin-Token": "wrong"}).status_code == 401
    ok = remote.get("/api/settings", headers={"X-Admin-Token": "t0ken-123"})
    assert ok.status_code == 200 and ok.json()["admin"] == "token" and ok.json()["token_required"]
    assert client(api).get("/api/settings").status_code == 401        # token applies to local users too


def test_passwords_never_returned(api, monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "env-db-secret")
    store.save_overrides({"ssh_password": "ui-ssh-secret"})
    body = client(api).get("/api/settings")
    text = body.text
    assert "env-db-secret" not in text and "ui-ssh-secret" not in text
    view = body.json()
    assert view["secrets_set"] == {"ssh_password": True, "db_password": True, "openai_api_key": False}
    assert view["values"]["db_password"] == "" and view["values"]["ssh_password"] == ""


# --------------------------------------------------------------------------- test / save
def test_invalid_values_are_rejected_with_field_errors(api):
    r = client(api).post("/api/settings/test", json={"values": {"snapshot_dir": "/no/such/folder", "max_rows": 1}})
    assert r.status_code == 422
    assert {"snapshot_dir", "max_rows"} <= set(r.json()["detail"]["errors"])


def test_snapshot_connection_test(api):
    r = client(api).post("/api/settings/test", json={"values": {}}).json()
    assert r["ok"] and r["kind"] == "snapshot" and r["tables_found"] >= 5
    assert "Amazon" in r["apps"] and r["date_range"][0]


def test_unreachable_ssh_server_gives_clear_error(api):
    values = {"source": "postgres", "ssh_host": "127.0.0.1", "ssh_port": 1, "ssh_username": "u",
              "ssh_password": "p", "db_username": "u", "db_password": "p"}
    r = client(api).post("/api/settings/test", json={"values": values}).json()
    assert r["ok"] is False and "Cannot reach the SSH server 127.0.0.1:1" in r["error"]


def test_failed_save_keeps_the_old_connection(api):
    c = client(api)
    assert c.get("/api/health").json()["source"]["kind"] == "snapshot"
    bad = {"source": "postgres", "db_host": "127.0.0.1", "db_port": 1, "db_username": "u", "db_password": "p"}
    r = c.put("/api/settings", json={"values": bad}).json()
    assert r["saved"] is False and "connection refused" in r["error"].lower()
    assert not store.SETTINGS_FILE.exists()
    assert c.get("/api/health").json()["source"]["kind"] == "snapshot"


def test_live_switch_to_postgres_and_real_time_answers(api, pg_uri):
    import psycopg
    c = client(api)
    with patch("llm.complete", side_effect=offline):
        assert c.post("/api/chat", json={"message": "list all apps"}).json()["metric"] == "app_list"

        r = c.put("/api/settings", json={"values": pg_values(pg_uri)}).json()
        assert r["ok"] and r["saved"] and r["kind"] == "postgres", r
        assert r["read_only_session"] is True and r["server_version"].startswith("16")
        assert r["tables_found"] >= 5 and r["ping_ms"] >= 0

        health = c.get("/api/health").json()
        assert health["data_ready"] and health["source"]["kind"] == "postgres"
        assert health["db_ping_ms"] is not None and health["connected_since"]

        view = c.get("/api/settings").json()
        assert view["values"]["source"] == "postgres" and view["secrets_set"]["db_password"]
        assert "s3cret-pass" not in json.dumps(view)
        saved = json.loads(store.SETTINGS_FILE.read_text())
        assert saved["source"] == "postgres" and "snapshot_dir" not in saved      # only changes are stored

        # new data written to the live database shows up in the very next answer
        with psycopg.connect(pg_uri, autocommit=True) as conn:
            conn.execute("INSERT INTO app_master (id, app_name, is_active) VALUES (9901, 'LiveTestMart', true)")
        answer = c.post("/api/chat", json={"message": "list all apps"}).json()
        assert "LiveTestMart" in json.dumps(answer["table"])

        # reference values refresh (what the background refresher does) so new apps are recognised
        agent = api.get_agent()
        agent.cat.refresh_values(agent.source)
        assert "LiveTestMart" in agent.cat.apps

        # sessions are read-only even if the login could write
        from analyst.sources import SourceError
        with pytest.raises(SourceError):
            agent.source.query("CREATE TABLE should_fail (x int)")

        # back to .env (snapshot)
        r = c.delete("/api/settings").json()
        assert r["ok"] and r["kind"] == "snapshot" and not store.SETTINGS_FILE.exists()
        assert c.get("/api/health").json()["source"]["kind"] == "snapshot"


def test_force_save_when_database_is_down(api):
    c = client(api)
    bad = {"source": "postgres", "db_host": "127.0.0.1", "db_port": 1, "db_username": "u", "db_password": "p"}
    r = c.put("/api/settings", json={"values": bad, "force": True}).json()
    assert r["saved"] is True and r["ok"] is False
    health = c.get("/api/health").json()
    assert health["data_ready"] is False and "refused" in health["source"]["error"].lower()


def test_llm_test_endpoint(api):
    c = client(api)
    r = c.post("/api/llm/test", json={"llm_host": "http://127.0.0.1:1"}).json()
    assert r["ok"] is False and "Cannot reach the model server" in r["error"]
    assert c.post("/api/llm/test", json={"llm_host": "file:///etc/passwd"}).status_code == 422


def test_openai_key_is_write_only_and_test_uses_gpt_6_luna(api):
    c = client(api)
    store.save_overrides({"openai_api_key": "sk-saved-secret", "llm_provider": "openai",
                          "openai_model": "gpt-6-luna"})
    view = c.get("/api/settings")
    assert "sk-saved-secret" not in view.text
    assert view.json()["secrets_set"]["openai_api_key"] is True
    with patch("llm._openai_health", return_value=(True, "OpenAI ready (gpt-6-luna)")) as health:
        result = c.post("/api/llm/test", json={"llm_provider": "openai"}).json()
    assert result["ok"] is True
    health.assert_called_once_with("sk-saved-secret", "gpt-6-luna")


# --------------------------------------------------------------------------- SSH tunnel (real SSH protocol)
@pytest.fixture(scope="module")
def ssh(pg_uri):
    from ssh_test_server import SSHTestServer
    v = pg_values(pg_uri)
    server = SSHTestServer(f"{v['db_host']}/.s.PGSQL.{v['db_port']}")
    yield server
    server.close()


def ssh_values(pg_uri, ssh, **over):
    from ssh_test_server import FAKE_DB_PORT
    v = pg_values(pg_uri)
    return {**v, "ssh_host": "127.0.0.1", "ssh_port": ssh.port, "ssh_username": ssh.username,
            "ssh_password": ssh.password, "db_host": "127.0.0.1", "db_port": FAKE_DB_PORT, **over}


def test_ssh_wrong_password(api, pg_uri, ssh):
    r = client(api).post("/api/settings/test", json={"values": ssh_values(pg_uri, ssh, ssh_password="nope")}).json()
    assert r["ok"] is False and "SSH login to 127.0.0.1 failed for user 'tester'" in r["error"]
    assert "nope" not in r["error"]


def test_ssh_ok_but_database_unreachable(api, pg_uri, ssh):
    r = client(api).post("/api/settings/test", json={"values": ssh_values(pg_uri, ssh, db_port=5999)}).json()
    assert r["ok"] is False and "SSH login worked, but the SSH server could not reach the database" in r["error"]


def test_ssh_host_key_pinning(api, pg_uri, ssh):
    wrong = "SHA256:" + "A" * 43
    r = client(api).post("/api/settings/test",
                         json={"values": ssh_values(pg_uri, ssh, ssh_host_key_sha256=wrong)}).json()
    assert r["ok"] is False and "does not match" in r["error"]


def test_live_switch_through_ssh_tunnel(api, pg_uri, ssh):
    c = client(api)
    with patch("llm.complete", side_effect=offline):
        r = c.put("/api/settings", json={"values": ssh_values(pg_uri, ssh)}).json()
        assert r["ok"] and r["via_ssh"] and r["ssh_host_key"].startswith("SHA256:"), r
        health = c.get("/api/health").json()
        assert health["data_ready"] and health["source"]["via_ssh"] and health["db_ping_ms"] is not None
        body = c.post("/api/chat", json={"message": "purchase share by app"}).json()
        assert body["metric"] == "purchase_share_by_app" and body["table"]["rows"]
        # the tunnel drops (server restart, network blip): the next question reconnects by itself
        agent = api.get_agent()
        agent.source._tunnel.stop()
        body = c.post("/api/chat", json={"message": "users by city"}).json()
        assert body["metric"] == "users_by_attribute" and body["table"]["rows"], body["answer"]
        pinned = c.put("/api/settings", json={"values": {"ssh_host_key_sha256": r["ssh_host_key"]}}).json()
        assert pinned["ok"], pinned
