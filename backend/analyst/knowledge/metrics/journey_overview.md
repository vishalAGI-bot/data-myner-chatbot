---
type: Metric
title: Journey overview
description: Total journeys, users, searches and outcome split.
tags:
- how many journeys
- overview
- summary
- total
- open
- closed
- purchased
- timeout
- status
tables:
- cross_app_journey
runtime: postgresql
parameters:
- name: date_from
  type: date
- name: date_to
  type: date
status: draft
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Journey overview

Total journeys, users, searches and outcome split.

## Example questions

- how many journeys are there
- overview of journeys

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
SELECT COUNT(*) AS journeys,
       COUNT(DISTINCT j.user_assoc_id) AS users,
       SUM(j.total_events) AS searches,
       COUNT(*) FILTER (WHERE j.total_apps > 1) AS multi_app_journeys,
       COUNT(*) FILTER (WHERE j.status = 'open') AS still_open,
       COUNT(*) FILTER (WHERE j.close_reason = 'purchased') AS purchased,
       COUNT(*) FILTER (WHERE j.close_reason = 'timeout') AS timed_out,
       MIN(CAST(j.first_search_time AS date)) AS first_day,
       MAX(CAST(j.last_search_time AS date)) AS last_day
FROM cross_app_journey j
WHERE 1 = 1
  [[ AND CAST(j.first_search_time AS date) >= CAST(:date_from AS date) ]]
  [[ AND CAST(j.first_search_time AS date) <= CAST(:date_to AS date) ]]
```

