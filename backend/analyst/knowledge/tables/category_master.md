---
type: Table
title: Category taxonomy
description: 'Product taxonomy: master_category > category > sub_category > micro_category.'
resource: postgresql://datamyner_db/public/category_master
tags:
- category
- sub category
- master category
- taxonomy
- micro category
status: stable
domain: analytics
grain: one row per taxonomy path
primary_key: id
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Category taxonomy

Product taxonomy: master_category > category > sub_category > micro_category.

## Grain

one row per taxonomy path

## Joins

- `testing_accessibility_infos.category_assoc_id = category_master.id`
- `journey_window.search_category` holds a category NAME from a MIXED level, because the taxonomy
  changed during the loaded period (see the data-quality warning "Search categories changed"). Measured
  on the live 24h windows (46,340 categorised rows): 75.1% of rows name a `sub_category`, 24.9% name a
  `master_category` (the older broad taxonomy: "Softlines", "Core Consumables", "OHL excl. H&B"), 5.8%
  name a `category`, and 0% match nothing. Joining on `sub_category` alone therefore returns NULL for a
  quarter of the rows.
  Join on every level and fall back to the value itself, always with a LEFT JOIN:
  ```sql
  LEFT JOIN category_master cm
         ON cm.sub_category = jw.search_category
         OR cm.category = jw.search_category
         OR cm.master_category = jw.search_category
  -- then: COALESCE(cm.master_category, jw.search_category) AS master_category,
  --       COALESCE(cm.category, jw.search_category)        AS category,
  --       jw.search_category                               AS sub_category
  ```
  A value that is already a master_category has no finer path above it; report it as its own top level
  rather than NULL. Aggregate or pick one row per category when a single path is required, since one
  name can sit on several paths.

## Notes

Text values are also denormalised into product_master and journey_window.search_category.

Mind the level names, which do not mean what they sound like. `master_category` is the TOP level
(e.g. Softlines), `category` is the second (e.g. Apparel) and `sub_category` is the third (e.g.
Women's Traditional Wear). So a request to "add the subcategory and master category" to a result that
already shows `search_category` is asking for `master_category` and `category`: the search category
IS the sub_category. `micro_category` is NULL in every row, so it carries no information.

One row per taxonomy path (430 rows, 401 distinct sub_category values), so a join on `sub_category`
can match more than one path; aggregate or pick one row if a single path per category is required.

## Example questions

- how many master categories
- sub categories under Apparel

# Schema

| column | type | meaning | policy |
|---|---|---|---|
| id | integer | Category id | ok |
| master_category | text | Top level, e.g. Core Consumables, Softlines | ok |
| category | text | Second level, e.g. Fresh, Apparel | ok |
| sub_category | text | Third level, e.g. Dairy | ok |
| micro_category | unknown | Fourth level (often NULL) | ok |
