---
type: Metric
title: Competitor switching within 5 minutes
description: Share of app sessions where the user switched to a competitor app within 5 minutes, by app.
tags:
- competitor
- switch
- switching
- 5 min
- 5 minutes
- sessions
- losing traffic
- traffic
tables:
- session_master
runtime: postgresql
parameters:
- name: app
  type: app
- name: date_from
  type: date
- name: date_to
  type: date
status: draft
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
focus_parameter: app
---

# Competitor switching within 5 minutes

Share of app sessions where the user switched to a competitor app within 5 minutes, by app.

## Example questions

- Are we losing search traffic to competitors?
- competitor switch rate by app

## Caveats

The app and the visit day are taken from session_id (its start time); created_at is only the record load time and
is never used as the visit date.

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
SELECT split_part(s.session_id, '_', 2) AS app,
       COUNT(*) AS sessions,
       COUNT(*) FILTER (WHERE s.competition_switch_bfr_5min) AS switched_within_5min,
       ROUND(100.0 * COUNT(*) FILTER (WHERE s.competition_switch_bfr_5min) / NULLIF(COUNT(*), 0), 1) AS switch_pct
FROM session_master s
WHERE COALESCE(s.insignificant_session, false) = false
  [[ AND split_part(s.session_id, '_', 2) = :app ]]
  [[ AND split_part(s.session_id, '_', 3) ~ '^20[0-9]{6}T[0-9]{6}'
     AND CAST(substr(split_part(s.session_id, '_', 3), 1, 4) || '-' || substr(split_part(s.session_id, '_', 3), 5, 2)
              || '-' || substr(split_part(s.session_id, '_', 3), 7, 2) AS date) >= CAST(:date_from AS date) ]]
  [[ AND split_part(s.session_id, '_', 3) ~ '^20[0-9]{6}T[0-9]{6}'
     AND CAST(substr(split_part(s.session_id, '_', 3), 1, 4) || '-' || substr(split_part(s.session_id, '_', 3), 5, 2)
              || '-' || substr(split_part(s.session_id, '_', 3), 7, 2) AS date) <= CAST(:date_to AS date) ]]
GROUP BY 1
ORDER BY sessions DESC, 1
```

