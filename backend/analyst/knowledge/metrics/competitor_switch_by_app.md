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

The app is taken from session_id; dates use the record created time.

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
  [[ AND CAST(s.created_at AS date) >= CAST(:date_from AS date) ]]
  [[ AND CAST(s.created_at AS date) <= CAST(:date_to AS date) ]]
GROUP BY 1
ORDER BY sessions DESC, 1
```

