---
type: Metric
title: Which competitor users switch to
description: For sessions on an app, which competitor app users switched to within 5 minutes.
tags:
- competitor
- switch to
- which competitor
- lose traffic to
tables:
- session_master
runtime: postgresql
parameters:
- name: app
  type: app
  default: HOME
status: draft
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Which competitor users switch to

For sessions on an app, which competitor app users switched to within 5 minutes.

## Example questions

- Which competitor do Meesho users switch to?

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
SELECT s.competitor_name AS switched_to, COUNT(*) AS sessions,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS share_pct
FROM session_master s
WHERE s.competition_switch_bfr_5min = true AND split_part(s.session_id, '_', 2) = :app
  AND s.competitor_name IS NOT NULL AND s.competitor_name <> 'not applicable'
GROUP BY 1
ORDER BY sessions DESC, 1
```

