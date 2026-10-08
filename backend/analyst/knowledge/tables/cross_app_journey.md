---
type: Table
title: Product-search journeys
description: 'One row per product-hunt journey: grouped searches for the same need across apps, with first/last search and
  outcome.'
resource: postgresql://datamyner_db/public/cross_app_journey
tags:
- journey
- journeys
- search
- cross app
- multi app
- open
- closed
- timeout
- purchased
status: stable
domain: analytics
grain: one row per journey
primary_key: id
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Product-search journeys

One row per product-hunt journey: grouped searches for the same need across apps, with first/last search and outcome.

## Grain

one row per journey

## Joins

- `cross_app_journey.user_assoc_id = user_master.id`
- `cross_app_journey.first_search_app_id = app_master.id`
- `journey_window.journey_assoc_id = cross_app_journey.id`
- `cross_app_journey_event.journey_assoc_id = cross_app_journey.id`

## Notes

apps_summary is text like 'Meesho (7), Flipkart (1)'; per-app counts are easier from cross_app_journey_event. status open/closed; close_reason purchased/timeout. For purchase app, time and value use journey_window. period_end is unreliable.

## Example questions

- how many journeys
- multi-app journeys per day
- journeys of user 5330

# Schema

| column | type | meaning | policy |
|---|---|---|---|
| id | integer | Journey id (referenced as journey_assoc_id) | ok |
| is_active | boolean | Active flag | ok |
| is_deleted | boolean | Soft-delete flag | ok |
| created_at | timestamptz | Record created | ok |
| updated_at | timestamptz | Record updated | ok |
| remarks | unknown | Free text | heavy |
| journey_id | text | Readable id, e.g. J000002 | ok |
| journey_hash | text | (hidden) | blocked |
| query_cluster_rep | text | Representative search phrase | ok |
| cluster_embedding | array | (hidden) | heavy |
| total_apps | integer | Apps used | ok |
| total_events | integer | Searches in the journey | ok |
| apps_summary | text | Apps with search counts (text) | ok |
| first_search_time | timestamptz | First search | ok |
| last_search_time | timestamptz | Last search | ok |
| status | text | open / closed | ok |
| close_reason | text | purchased / timeout / NULL | ok |
| period_end | timestamptz | Unreliable, avoid | ok |
| first_search_app_id | integer | App of the first search | ok |
| user_assoc_id | integer | User id | ok |
