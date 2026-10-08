"""The product master: 22,409 real products, each with a brand and a category.

This file was previously mistaken for the taxonomy, because it happens to carry
master_category / category / sub_category too. Ingesting it that way deduped
22,409 products down to ~296 triples and threw away every product name, brand
and uid - which is why "how many dairy" answered 1601 and nobody could tell
whether that meant products or categories.

Two things it unlocks:
  - product and brand questions      ("how many Amul products", "brands in Fresh")
  - a far better bridge to searches. A shopper types "nivea roll deo men";
    Nivea is a BRAND, not a category, so matching brands finds it where
    matching category labels never could.
"""

import config

# Placeholder values that are not real brands. Left in, "Not Available" becomes
# the biggest brand in the data and matches nothing useful.
JUNK_BRANDS = {"", "not available", "na", "n/a", "none", "null", "unknown",
               "unbranded", "no brand", "other", "others", "-", "--"}

REQUIRED = ("product_uid", "product_master", "brand")

COLUMNS = [
    ("product_uid", "TEXT PRIMARY KEY"),
    ("product_master", "TEXT"),
    ("brand", "TEXT"),
    ("brand_key", "TEXT"),          # lowercased, for joins and matching
    ("sub_category", "TEXT"),
    ("category", "TEXT"),
    ("master_category", "TEXT"),
]


def normalise(records: list[dict]) -> list[dict]:
    out, seen = [], set()
    for rec in records:
        uid = str(rec.get("product_uid") or "").strip()
        if not uid or uid in seen:
            continue           # product_uid is the key; duplicates are noise
        seen.add(uid)
        brand = str(rec.get("brand") or "").strip()
        if brand.lower() in JUNK_BRANDS:
            brand = ""
        out.append({
            "product_uid": uid,
            "product_master": str(rec.get("product_master") or "").strip(),
            "brand": brand,
            "brand_key": brand.lower(),
            "sub_category": str(rec.get("sub_category") or "").strip(),
            "category": str(rec.get("category") or "").strip(),
            "master_category": str(rec.get("master_category") or "").strip(),
        })
    return out


def brand_index(conn):
    """[(brand, brand_key, sub_category, category, master_category)] per brand.

    One row per brand, using its most common sub_category - a brand usually
    sells in one place, and picking the mode avoids a rare outlier product
    deciding where the whole brand belongs.
    """
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (config.PRODUCTS_TABLE,)).fetchone()
    if not exists:
        return []
    return conn.execute(
        f"""SELECT brand, brand_key, sub_category, category, master_category
            FROM (
              SELECT brand, brand_key, sub_category, category, master_category,
                     ROW_NUMBER() OVER (
                        PARTITION BY brand_key
                        ORDER BY COUNT(*) DESC, sub_category) AS rank
              FROM {config.PRODUCTS_TABLE}
              WHERE brand != '' AND LENGTH(brand_key) >= 3
              GROUP BY brand_key, sub_category, category, master_category
            ) WHERE rank = 1"""
    ).fetchall()
