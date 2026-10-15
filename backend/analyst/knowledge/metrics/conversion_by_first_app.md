---
type: Metric
title: Conversion, retention and leakage by first-search app
description: 'For journeys that START on each app: share that converted, share bought on that same app (retained) and share
  bought elsewhere (leaked), as a share of all journeys and as a split of the purchases (what share of buyers stayed vs left).'
tags:
- conversion
- conversion rate
- retention
- retained
- leakage
- leaked
- start on
- first app
- within 24h
- buy elsewhere
- lose
tables:
- journey_window
- app_master
runtime: postgresql
parameters:
- name: window_hours
  type: int
  default: 24
- name: app
  type: app
- name: date_from
  type: date
- name: date_to
  type: date
- name: category
  type: text
status: draft
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
focus_parameter: app
---

# Conversion, retention and leakage by first-search app

For journeys that START on each app: share that converted, share bought on that same app (retained) and share bought elsewhere (leaked). The *_of_purchases_pct columns split the converted journeys only: of the journeys that bought, the share that bought on the first app vs on another app (they add up to 100%).

## Example questions

- Of journeys that start on Amazon, what share buy on Amazon vs somewhere else?
- Of the people who start on Amazon and buy, how many buy somewhere else?
- conversion rate by app within 24h
- retention rate by app
- which app leaks the most

## Caveats

Only journeys whose purchase window has ended are counted, so rates are not diluted by journeys still in progress.

# Computation

`category` matches a sub-category name (search_category, partly) or every sub-category of a category or master category of that name (any case): `:category_subs` is filled from category_master. A sub-category name shared by several parents is not attributed to either.

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
SELECT COALESCE(a.app_name, 'Unknown app') AS first_search_app,
       COUNT(*) AS journeys,
       COUNT(*) FILTER (WHERE jw.is_converted) AS converted,
       ROUND(100.0 * COUNT(*) FILTER (WHERE jw.is_converted) / NULLIF(COUNT(*), 0), 1) AS conversion_pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE jw.is_retained) / NULLIF(COUNT(*), 0), 1) AS retained_pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE jw.is_leaked) / NULLIF(COUNT(*), 0), 1) AS leaked_pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE jw.is_retained) / NULLIF(COUNT(*) FILTER (WHERE jw.is_converted), 0), 1)
         AS retained_of_purchases_pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE jw.is_leaked) / NULLIF(COUNT(*) FILTER (WHERE jw.is_converted), 0), 1)
         AS leaked_of_purchases_pct
FROM journey_window jw
LEFT JOIN app_master a ON a.id = jw.first_search_app_id
WHERE jw.window_hours = :window_hours AND jw.window_end <= (SELECT MAX(lw.window_start) FROM journey_window lw)
  [[ AND a.app_name = :app ]]
  [[ AND CAST(jw.window_start AS date) >= CAST(:date_from AS date) ]]
  [[ AND CAST(jw.window_start AS date) <= CAST(:date_to AS date) ]]
  [[ AND (jw.search_category ILIKE '%' || :category || '%' OR jw.search_category IN :category_subs) ]]
GROUP BY 1
ORDER BY journeys DESC, 1
```

