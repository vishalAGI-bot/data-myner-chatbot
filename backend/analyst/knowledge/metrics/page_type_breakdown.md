---
type: Metric
title: Screens by page type
description: Screen events by page type for an app (large table - use a date range).
tags:
- page type
- pages
- screens
- product page
- cart
- home
- search results
- add to cart
- funnel
tables:
- testing_accessibility_infos
runtime: postgresql
parameters:
- name: app
  type: app
- name: date_from
  type: date
- name: date_to
  type: date
- name: limit
  type: int
  default: 20
status: draft
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Screens by page type

Screen events by page type for an app (large table - use a date range).

## Example questions

- page views by page type on Meesho
- how many cart screens

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
SELECT COALESCE(t.page_type, 'Unclassified') AS page_type,
       COUNT(*) AS screens,
       COUNT(DISTINCT t.session_assoc_id) AS sessions,
       SUM(COALESCE(t.add_to_cart_count, 0)) AS add_to_cart
FROM testing_accessibility_infos t
WHERE 1 = 1
  [[ AND t.final_appname = :app ]]
  [[ AND CAST(t.date AS date) >= CAST(:date_from AS date) ]]
  [[ AND CAST(t.date AS date) <= CAST(:date_to AS date) ]]
GROUP BY 1
ORDER BY screens DESC, 1
LIMIT :limit
```

