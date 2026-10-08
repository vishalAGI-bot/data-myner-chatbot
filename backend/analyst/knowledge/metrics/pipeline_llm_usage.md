---
type: Metric
title: Pipeline LLM usage
description: 'LLM calls made by the data pipeline: calls, tokens, average duration and errors by model and task.'
tags:
- llm
- tokens
- model
- pipeline
- llm calls
- cost
- latency
- categorization
tables:
- llm_call_log
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

# Pipeline LLM usage

LLM calls made by the data pipeline: calls, tokens, average duration and errors by model and task.

## Example questions

- how many tokens did the pipeline use
- llm calls by status
- llm tokens used by the pipeline

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
SELECT l.model_id, l.task_type,
       COUNT(*) AS calls,
       SUM(l.total_tokens) AS total_tokens,
       ROUND(CAST(AVG(l.duration_ms) AS numeric), 0) AS avg_duration_ms,
       COUNT(*) FILTER (WHERE l.status <> 'success') AS failed_calls
FROM llm_call_log l
WHERE 1 = 1
  [[ AND CAST(l.started_at AS date) >= CAST(:date_from AS date) ]]
  [[ AND CAST(l.started_at AS date) <= CAST(:date_to AS date) ]]
GROUP BY 1, 2
ORDER BY calls DESC, 1
```

