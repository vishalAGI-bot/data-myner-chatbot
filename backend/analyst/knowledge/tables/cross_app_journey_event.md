---
type: Table
title: Journey search events (ordered)
description: Every search inside a journey, in order (step), with app, query and time since the first search.
resource: postgresql://datamyner_db/public/cross_app_journey_event
tags:
- search
- searches
- query
- queries
- step
- switch
- sequence
- before switching
status: stable
domain: analytics
grain: one row per search event
primary_key: id
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Journey search events (ordered)

Every search inside a journey, in order (step), with app, query and time since the first search.

## Grain

one row per search event

## Joins

- `cross_app_journey_event.journey_assoc_id = cross_app_journey.id`
- `cross_app_journey_event.app_assoc_id = app_master.id`
- `cross_app_journey_event.session_assoc_id = session_master.id`
- `cross_app_journey_event.source_accessibility_id = testing_accessibility_infos.id`

## Notes

Use step order (or search_time) with LAG() to find app switches, e.g. Amazon -> Meesho, and the query right before a switch.

## Example questions

- what do users search before switching from Amazon to Meesho
- searches per app
- top search queries

# Schema

| column | type | meaning | policy |
|---|---|---|---|
| id | integer | Event id | ok |
| step | integer | Order within the journey (1 = first) | ok |
| query | text | Search text as typed | ok |
| normalized_query | text | Cleaned search text | ok |
| search_time | timestamptz | Search time | ok |
| hours_since_first | numeric | Hours since the journey's first search | ok |
| page_id | text | Screen id | ok |
| app_assoc_id | integer | App id | ok |
| journey_assoc_id | integer | Journey id | ok |
| session_assoc_id | integer | Session id | ok |
| source_accessibility_id | integer | Raw event id | ok |
