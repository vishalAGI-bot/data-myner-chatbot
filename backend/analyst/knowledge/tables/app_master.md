---
type: Table
title: Apps (shopping app master)
description: One row per shopping app or sub-app the panel tracks (Amazon, Flipkart, Meesho, Zepto, ...).
resource: postgresql://datamyner_db/public/app_master
tags:
- app
- apps
- platform
- sub-app
- amazon
- flipkart
- meesho
status: stable
domain: analytics
grain: one row per app
primary_key: id
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Apps (shopping app master)

One row per shopping app or sub-app the panel tracks (Amazon, Flipkart, Meesho, Zepto, ...).

## Grain

one row per app

## Joins

- `app_master.parent_id = app_master.id  (sub-app -> parent app, e.g. Amazon Now -> Amazon)`

## Notes

Apps have NO external code or site number. The only identifiers are `id` (1, 2, 3 ...), `app_name` and
`slug`. A question naming something like "site 0005" or "app 0005" therefore has no column to match:
`0005` is not a documented value anywhere. Read a bare number as `app_master.id` only when the question
makes that clear, state which app that id turned out to be, and otherwise ask which app is meant rather
than inventing a code mapping. Filter apps on `app_name` or `id`, never on an assumed code.

Every *_app_id / app_assoc_id column elsewhere points here. Join to show app names instead of ids. Sub-apps such as Amazon Now or Flipkart Minutes have parent_id set.

## Example questions

- list all apps
- which apps are sub-apps
- what is app id 16

# Schema

| column | type | meaning | policy |
|---|---|---|---|
| id | integer | App id (referenced as app_assoc_id, first_search_app_id, purchase_app_id) | ok |
| is_active | boolean | Record active flag | ok |
| is_deleted | boolean | Soft-delete flag | ok |
| created_at | timestamptz | Record created | ok |
| updated_at | timestamptz | Record updated | ok |
| remarks | unknown | Free text | ok |
| app_name | text | Display name | ok |
| slug | text | URL-safe name | ok |
| description | text | Free text | ok |
| metadata | json | JSON settings (policy: heavy) | heavy |
| parent_id | unknown | Parent app id for sub-apps, NULL for main apps | ok |
