---
type: Table
title: Journey outcome windows (conversion, retention, leakage)
description: 'Outcome of each journey within 24h / 48h / 72h windows: did it convert, on which app, how fast, for how much,
  and was the purchase kept on the first app (retained) or lost to another app (leaked).'
resource: postgresql://datamyner_db/public/journey_window
tags:
- purchase
- purchased
- buy
- conversion
- converted
- retention
- retained
- leakage
- leaked
- order value
- order amount
- time to purchase
- hours to purchase
- window
- 24h
- 48h
- 72h
- purchase share
- category
- brand
status: stable
domain: analytics
grain: one row per journey x window_hours (24, 48, 72)
primary_key: id
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Journey outcome windows (conversion, retention, leakage)

Outcome of each journey within 24h / 48h / 72h windows: did it convert, on which app, how fast, for how much, and was the purchase kept on the first app (retained) or lost to another app (leaked).

## Grain

one row per journey x window_hours (24, 48, 72)

## Joins

- `journey_window.journey_assoc_id = cross_app_journey.id`
- `journey_window.first_search_app_id = app_master.id`
- `journey_window.purchase_app_id = app_master.id`
- `journey_window.purchase_order_id = "order".id`

## Notes

ALWAYS filter window_hours (default 24) - otherwise every journey is counted three times. For rates count only finished windows: jw.window_end <= (SELECT MAX(lw.window_start) FROM journey_window lw), i.e. the window ended before the newest loaded journey. Do NOT use is_final: the pipeline computes rows a few hours after the window starts and never sets is_final once the window ends (most finished windows are false). is_retained = bought on the first-search app; is_leaked = bought on a different app (inferred from field names - confirm with the data team). apps_searched and purchased_product_* are arrays. search_category holds the sub-category (third level of category_master): to filter or group by a category or master category, map it through category_master.sub_category, never by matching the category name against search_category.

## Example questions

- purchase share by app
- conversion within 24h
- where does Amazon leak to
- time to purchase by app
- average order value by app
- leakage by category

# Schema

| column | type | meaning | policy |
|---|---|---|---|
| id | integer | Row id | ok |
| window_hours | integer | Window length: 24, 48 or 72 | ok |
| apps_count | integer | Apps searched in the window | ok |
| apps_searched | array | Array of app names | ok |
| total_searches | integer | Searches in the window | ok |
| is_multi_app | boolean | More than one app searched | ok |
| purchase_time | unknown | Purchase time | ok |
| hours_to_purchase | unknown | Hours from first search to purchase | ok |
| order_amount | unknown | Order value (INR) | ok |
| purchased_product_name | unknown | Main product bought | ok |
| purchased_product_category | unknown | Main product category | ok |
| purchased_product_names | array | Array of products | ok |
| purchased_product_categories | array | Array of categories | ok |
| purchased_product_brands | array | Array of brands | ok |
| search_category | unknown | Category of the searches; a category_master.sub_category value (third level) | ok |
| is_converted | boolean | Ended in a purchase within the window | ok |
| is_retained | boolean | Purchase on the first-search app | ok |
| is_leaked | boolean | Purchase on a different app | ok |
| window_start | timestamptz | Window start | ok |
| window_end | timestamptz | Window end | ok |
| is_final | boolean | Stale flag, not maintained by the pipeline; do not use (see Notes) | ok |
| computed_at | timestamptz | Computed at | ok |
| first_search_app_id | integer | App of the first search | ok |
| journey_assoc_id | integer | Journey id | ok |
| purchase_app_id | unknown | App where the purchase happened | ok |
| purchase_order_id | unknown | Order id | ok |
