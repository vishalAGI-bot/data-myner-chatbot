---
type: Table
title: Pipeline LLM calls (operations)
description: 'Every LLM call made by the data pipeline (e.g. query categorisation): model, tokens, duration, status.'
resource: postgresql://datamyner_db/public/llm_call_log
tags:
- llm
- model
- tokens
- pipeline
- categorization
- cost
- latency
status: stable
domain: operations
grain: one row per LLM call
primary_key: id
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Pipeline LLM calls (operations)

Every LLM call made by the data pipeline (e.g. query categorisation): model, tokens, duration, status.

## Grain

one row per LLM call

## Notes

Operations data, not shopper behaviour. Prompt and response text are blocked from output.

## Example questions

- how many tokens did the pipeline use
- llm calls by status

# Schema

| column | type | meaning | policy |
|---|---|---|---|
| id | integer | Call id | ok |
| model_id | text | Model | ok |
| provider_name | text | Provider | ok |
| region | text | Region | ok |
| task_type | text | Task, e.g. query_categorization | ok |
| prompt_text | text | (hidden) | heavy |
| response_text | text | (hidden) | heavy |
| parsed_response | json | (hidden) | heavy |
| started_at | timestamptz | Start | ok |
| ended_at | timestamptz | End | ok |
| duration_ms | integer | Duration ms | ok |
| prompt_tokens | integer | Prompt tokens | ok |
| completion_tokens | integer | Completion tokens | ok |
| total_tokens | integer | Total tokens | ok |
| status | text | success / error | ok |
| error_message | text | Error text | ok |
| source | text | Caller | ok |
| source_ref | text | Caller reference | ok |
| metadata | json | (hidden) | heavy |
| configuration_id | unknown | Configuration id | ok |
| prompt_template_id | unknown | Prompt template id | ok |
