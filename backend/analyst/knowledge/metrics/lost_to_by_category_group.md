---
type: Metric
title: 'Who we lose to: fashion vs grocery'
description: Leaked purchases from an app, split by the master category of the search and the app that won the purchase.
tags:
- lose to
- fashion
- apparel
- grocery
- quick commerce
- category group
- competitor by category
tables:
- journey_window
- app_master
- category_master
runtime: postgresql
parameters:
- name: app
  type: app
  default: HOME
- name: window_hours
  type: int
  default: 24
- name: limit
  type: int
  default: 30
status: draft
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Who we lose to: fashion vs grocery

Leaked purchases from an app, split by the master category of the search and the app that won the purchase.

## Example questions

- Which app do we lose to most in fashion/apparel versus grocery/quick-commerce?

## Caveats

Each journey's category is matched to the category list; categories not in the list show as Uncategorised.

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
SELECT COALESCE(cm.master_category, 'Uncategorised') AS master_category,
       COALESCE(p.app_name, 'Unknown app') AS lost_to,
       COUNT(*) AS purchases
FROM journey_window jw
JOIN app_master f ON f.id = jw.first_search_app_id
LEFT JOIN app_master p ON p.id = jw.purchase_app_id
LEFT JOIN (SELECT DISTINCT sub_category, master_category FROM category_master) cm ON cm.sub_category = jw.search_category
WHERE jw.window_hours = :window_hours AND jw.is_leaked = true AND f.app_name = :app
GROUP BY 1, 2
ORDER BY master_category, purchases DESC, lost_to
LIMIT :limit
```

