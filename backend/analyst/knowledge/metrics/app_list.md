---
type: Metric
title: Apps in the data
description: All apps and sub-apps with their ids and parent app.
tags:
- list apps
- all apps
- which apps
- app names
- sub-apps
- app ids
tables:
- app_master
runtime: postgresql
parameters: []
status: draft
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Apps in the data

All apps and sub-apps with their ids and parent app.

## Example questions

- list all apps
- which apps are tracked
- what is app id 16

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
SELECT a.id AS app_id, a.app_name, p.app_name AS parent_app, a.is_active
FROM app_master a
LEFT JOIN app_master p ON p.id = a.parent_id
ORDER BY a.id
```

