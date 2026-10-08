---
type: Table
title: Sub-app sessions
description: Sessions inside a sub-app (e.g. Flipkart Minutes inside Flipkart, Amazon Now inside Amazon).
resource: postgresql://datamyner_db/public/sub_session_master
tags:
- sub session
- sub-app
- amazon now
- flipkart minutes
status: stable
domain: analytics
grain: one row per sub-app session
primary_key: id
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Sub-app sessions

Sessions inside a sub-app (e.g. Flipkart Minutes inside Flipkart, Amazon Now inside Amazon).

## Grain

one row per sub-app session

## Joins

- `sub_session_master.session_assoc_id = session_master.id`
- `testing_accessibility_infos.sub_session_assoc_id = sub_session_master.id`

## Notes

The sub-app name is the last part of sub_session_id.

## Example questions

- how many Flipkart Minutes sessions

# Schema

| column | type | meaning | policy |
|---|---|---|---|
| id | integer | Sub-session id | ok |
| sub_session_id | text | <session_id>_<SubApp> | ok |
| session_assoc_id | integer | Parent session id | ok |
