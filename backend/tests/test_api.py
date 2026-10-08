"""API tests. No Ollama needed - the model is stubbed.

Run: python test_api.py
"""

import io
import json
import pathlib

import chatbot as chatbot_mod
import llm

ok = fail = 0


def check(label, condition, detail=""):
    global ok, fail
    if condition:
        ok += 1
        print(f"  PASS  {label}")
    else:
        fail += 1
        print(f"  FAIL  {label} {detail}")


def stub(system, user, model=None, max_tokens=1024, **kw):
    if "SQLite expert" in system:
        return "SELECT COUNT(*) AS n FROM taxonomy"
    return "stubbed answer"


llm.complete = chatbot_mod.llm.complete = stub

from fastapi.testclient import TestClient      # noqa: E402

import api                                      # noqa: E402

api.llm.health = lambda: (True, "stubbed ollama")
client = TestClient(api.app)
DATA = pathlib.Path("data")


def upload(path, session_id=None):
    url = "/api/sessions" if session_id is None else f"/api/sessions/{session_id}/datasets"
    with open(path, "rb") as fh:
        return client.post(url, files={"file": (pathlib.Path(path).name, fh,
                                                "application/json")})


print("\n1. Health")
r = client.get("/api/health")
check("health responds", r.status_code == 200, r.status_code)
check("reports ollama", "ollama" in r.json())

print("\n2. Rejects bad uploads")
r = client.post("/api/sessions",
                files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")})
check("rejects a non-json extension", r.status_code == 400, r.status_code)
r = client.post("/api/sessions",
                files={"file": ("x.json", io.BytesIO(b"{not json"), "application/json")})
check("rejects unparseable json", r.status_code == 400, r.status_code)
r = client.post("/api/sessions", files={"file": (
    "x.json", io.BytesIO(json.dumps([{"foo": 1}]).encode()), "application/json")})
check("rejects an unrecognised schema", r.status_code == 400, r.status_code)
check("explains which schemas are accepted",
      "taxonomy" in r.json().get("detail", "").lower(), r.json())

print("\n3. Each dataset is detected from its contents")
cases = [("sub_category_category_master_mapping.json", "taxonomy"),
         ("master_product_uid.json", "products"),
         ("app_master.json", "apps"),
         ("user_cross_app_journey_event_search.json", "events")]
for name, want in cases:
    path = DATA / name
    if not path.exists():
        check(f"{name} present", False, "missing")
        continue
    r = upload(path)
    if r.status_code != 200:
        check(f"{name} -> {want}", False, r.text[:120])
        continue
    body = r.json()
    check(f"{name} -> {want}", body["datasets"][0]["kind"] == want,
          body["datasets"][0]["kind"])
    client.delete(f"/api/sessions/{body['session_id']}")

print("\n4. Several datasets in one session")
r = upload(DATA / "sub_category_category_master_mapping.json")
check("first upload starts a session", r.status_code == 200, r.text[:120])
sid = r.json()["session_id"]
for name in ("master_product_uid.json", "app_master.json",
             "user_cross_app_journey_event_search.json"):
    r = upload(DATA / name, sid)
    check(f"added {name}", r.status_code == 200, r.text[:120])

body = r.json()
check("four datasets recorded", len(body["datasets"]) == 4, body["datasets"])
check("all five tables built",
      {"taxonomy", "product", "app", "search_event", "query_category"}
      <= set(body["tables"]), sorted(body["tables"]))
check("products kept, not deduped to triples",
      body["tables"]["product"] > 20000, body["tables"]["product"])
check("taxonomy is the mapping file, not the products",
      body["tables"]["taxonomy"] < 1000, body["tables"]["taxonomy"])
check("the bridge got built", body["tables"].get("query_category", 0) >= 1,
      body["tables"].get("query_category"))
check("reports what it can answer", len(body["ready_for"]) >= 3, body["ready_for"])

print("\n5. Session lifecycle")
r = client.get(f"/api/sessions/{sid}")
check("session can be read back", r.status_code == 200)
check("read-back lists the datasets", len(r.json()["datasets"]) == 4)
r = client.get("/api/sessions/does-not-exist")
check("unknown session is 404", r.status_code == 404, r.status_code)
r = upload(DATA / "app_master.json", "does-not-exist")
check("adding to an unknown session is 404", r.status_code == 404, r.status_code)

print("\n6. Chat")
r = client.post("/api/chat", json={"session_id": sid,
                                       "message": "how many products are there?"})
check("chat responds", r.status_code == 200, r.text[:160])
body = r.json()
check("global count answered without the model",
      body["path"] == "sql (direct)" and "22409" in body["answer"], body)
check("response names the agent", body["domain"] == "products", body["domain"])

r = client.post("/api/chat", json={"session_id": sid,
                                       "message": "how many categories are there?"})
check("category count uses the taxonomy table",
      "44" in r.json()["answer"], r.json()["answer"])
check("and the taxonomy agent", r.json()["domain"] == "taxonomy",
      r.json()["domain"])

r = client.post("/api/chat", json={"session_id": sid, "message": "   "})
check("blank message rejected", r.status_code == 422, r.status_code)
r = client.post("/api/chat", json={"session_id": "nope", "message": "hi"})
check("chat on a dead session is 404", r.status_code == 404, r.status_code)

r = client.post("/api/reset", json={"session_id": sid})
check("reset works", r.status_code == 200)

print("\n7. Cleanup and limits")
r = client.delete(f"/api/sessions/{sid}")
check("delete works", r.json()["status"] == "deleted", r.json())
r = client.delete(f"/api/sessions/{sid}")
check("double delete is safe", r.json()["status"] == "already_deleted")
r = client.post("/api/chat", json={"session_id": sid, "message": "hi"})
check("deleted session refuses chat", r.status_code == 404)

api.MAX_UPLOAD_BYTES = 100
big = json.dumps([{"master_category": "a", "category": "b",
                   "sub_category": "c"}] * 50).encode()
r = client.post("/api/sessions",
                files={"file": ("big.json", io.BytesIO(big), "application/json")})
check("oversized upload is rejected", r.status_code == 413, r.status_code)
api.MAX_UPLOAD_BYTES = 50 * 1024 * 1024

print("\n8. Expiry sweeper")
r = upload(DATA / "cat_sample.json")
sid2 = r.json()["session_id"]
api._sessions[sid2].last_used = 0          # pretend it is ancient
api.sweep_expired()
check("expired session is swept", sid2 not in api._sessions)
check("swept session is gone from the API",
      client.get(f"/api/sessions/{sid2}").status_code == 404)

print(f"\n{ok} passed, {fail} failed\n")
raise SystemExit(1 if fail else 0)
