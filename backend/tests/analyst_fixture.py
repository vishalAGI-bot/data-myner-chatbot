"""Build a test snapshot: the real sample export + larger real files + seeded
synthetic rows for tables the export only has 10 rows of.

Synthetic rows exist ONLY for tests (so every metric runs on enough data). They
are clearly separated from the real export and never shipped as data.
"""

from __future__ import annotations

import json
import random
import shutil
import zlib
from datetime import datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
EXPORT = BACKEND / "data" / "public"                       # your export (public.zip)
OLD = BACKEND / "data"                                      # earlier, larger real files


def build(target: Path) -> Path:
    rnd = random.Random(42)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for f in EXPORT.glob("*.json"):
        shutil.copy(f, target / f.name)

    def dump(name, rows):
        (target / f"{name}.json").write_text(json.dumps(rows), encoding="utf-8")

    # Real, larger tables from earlier uploads (same columns as the database tables).
    journeys = json.loads((OLD / "cross-app-journey" / "data-1790154732687.json").read_text())
    dump("cross_app_journey", journeys)
    dump("cross_app_journey_event", json.loads((OLD / "cross_app_journey_event.json").read_text()))
    products = json.loads((OLD / "master_product_uid.json").read_text())
    dump("product_master", [{"id": i + 1, **p} for i, p in enumerate(products)])
    cats = json.loads((OLD / "sub_category_category_master_mapping.json").read_text())
    dump("category_master", [{"id": i + 1, "micro_category": None, **c} for i, c in enumerate(cats)])

    apps = json.loads((EXPORT / "app_master.json").read_text())
    have = {a["id"] for a in apps}
    base = {k: None for k in apps[0]}
    for app_id, name, parent in ((2, "Amazon", None), (3, "Meesho", None), (14, "Flipkart Minutes", 1), (16, "Amazon Now", 2)):
        if app_id not in have:
            apps.append({**base, "id": app_id, "app_name": name, "slug": name.lower().replace(" ", "-"),
                         "description": f"{name} app", "metadata": {}, "parent_id": parent, "is_active": True,
                         "is_deleted": False})
    dump("app_master", apps)
    app_by_name = {a["app_name"]: a["id"] for a in apps}

    # ---- synthetic journey_window (3 windows per journey) -------------------
    sub_cats = [c["sub_category"] for c in cats]
    windows = []
    wid = 1
    for j in journeys:
        names = [p.split(" (")[0].strip() for p in (j["apps_summary"] or "").split("),") if p.strip()]
        names = [n.rstrip(")").strip() for n in names] or ["Meesho"]
        start = datetime.fromisoformat(j["first_search_time"].replace(" ", "T").replace("+02", "+02:00"))
        category = sub_cats[zlib.crc32((j["query_cluster_rep"] or "").encode()) % len(sub_cats)] if rnd.random() < 0.9 else None
        buys = j["close_reason"] == "purchased" or rnd.random() < 0.05
        hours = round(rnd.uniform(0.05, 70), 2) if buys else None
        purchase_app = (names[0] if len(names) == 1 or rnd.random() < 0.6 else rnd.choice(names[1:])) if buys else None
        if buys and rnd.random() < 0.15:
            purchase_app = rnd.choice(["Flipkart", "Meesho", "Amazon", "Myntra", "Zepto"])
        first_app = j["first_search_app_id"]
        for wh in (24, 48, 72):
            converted = bool(buys and hours <= wh)
            p_id = app_by_name.get(purchase_app) if converted else None
            windows.append({
                "id": wid, "window_hours": wh, "apps_count": len(names), "apps_searched": names,
                "total_searches": j["total_events"], "is_multi_app": len(names) > 1,
                "purchase_time": (start + timedelta(hours=hours)).isoformat() if converted else None,
                "hours_to_purchase": hours if converted else None,
                "order_amount": round(rnd.uniform(99, 4999), 2) if converted else None,
                "purchased_product_name": None, "purchased_product_category": category if converted else None,
                "purchased_product_names": [], "purchased_product_categories": [], "purchased_product_brands": [],
                "search_category": category, "is_converted": converted,
                "is_retained": converted and p_id == first_app, "is_leaked": converted and p_id != first_app,
                "window_start": start.isoformat(), "window_end": (start + timedelta(hours=wh)).isoformat(),
                "is_final": wh < 72 or rnd.random() < 0.8, "computed_at": "2026-09-18T00:00:00+02:00",
                "first_search_app_id": first_app, "journey_assoc_id": j["id"], "purchase_app_id": p_id,
                "purchase_order_id": None})
            wid += 1
    dump("journey_window", windows)

    # ---- synthetic users for every journey user (with fake PII to test blocking)
    real_users = json.loads((EXPORT / "user_master.json").read_text())
    template = {k: None for k in real_users[0]}
    cities = [("Mumbai", "Maharashtra", "West"), ("Delhi", "Delhi", "North"), ("Bangalore", "Karnataka", "South"),
              ("Kolkata", "West Bengal", "East"), ("Pune", "Maharashtra", "West"), ("Jaipur", "Rajasthan", "North")]
    users = []
    for uid in sorted({j["user_assoc_id"] for j in journeys}):
        city, state, zone = rnd.choice(cities)
        users.append({**template, "id": uid, "m_id": f"m{uid:06d}", "city": city, "state": state, "zone": zone,
                      "gender": rnd.choice(["Male", "Female"]), "tier": rnd.choice(["Gold", "Silver", "Bronze"]),
                      "device_model": rnd.choice(["Redmi Note 12", "Galaxy A14", "Vivo Y21", None]),
                      "date_of_birth": f"{rnd.randint(1965, 2006)}-0{rnd.randint(1, 9)}-1{rnd.randint(0, 9)}",
                      "email": f"user{uid}@example.com", "mobile_number": f"90000{uid:05d}",
                      "password": "pbkdf2_sha256$fake", "address": "fake address", "is_deleted": False,
                      "is_active": True, "metadata": {}})
    dump("user_master", users)

    # ---- synthetic sessions (enough for competitor metrics) -----------------
    real_sessions = json.loads((EXPORT / "session_master.json").read_text())
    stemplate = dict(real_sessions[0])
    sessions = list(real_sessions)
    for i in range(600):
        app = rnd.choice(["Amazon", "Meesho", "Flipkart", "Myntra", "Zepto"])
        switched = rnd.random() < 0.25
        sessions.append({**stemplate, "id": 100000 + i, "session_id": f"m{rnd.randint(1, 999):06d}_{app}_202609{rnd.randint(12, 17)}T1200{i:02d}",
                         "total_session_time_min": round(rnd.uniform(0.5, 25), 2), "number_of_screens": rnd.randint(3, 200),
                         "competition_switch_bfr_5min": switched,
                         "competitor_name": rnd.choice([a for a in ["Amazon", "Meesho", "Flipkart", "Myntra"] if a != app]) if switched else "not applicable",
                         "is_order_confirmation_page": rnd.random() < 0.1, "insignificant_session": False,
                         "no_of_searches": rnd.randint(0, 8), "created_at": f"2026-09-{rnd.randint(12, 17)}T12:00:00+02:00"})
    dump("session_master", sessions)
    return target


if __name__ == "__main__":
    import sys
    print(build(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/analyst_snapshot")))
