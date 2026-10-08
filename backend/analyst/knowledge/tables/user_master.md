---
type: Table
title: Panel users
description: People on the research panel whose phone activity is tracked, with demographics.
resource: postgresql://datamyner_db/public/user_master
tags:
- user
- users
- panelist
- city
- state
- zone
- gender
- age
- device
- tier
- demographic
status: stable
domain: analytics
grain: one row per panel user
primary_key: id
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Panel users

People on the research panel whose phone activity is tracked, with demographics.

## Grain

one row per panel user

## Joins

- `cross_app_journey.user_assoc_id = user_master.id`
- `testing_accessibility_infos.user_assoc_id = user_master.id`
- `session_master.session_id starts with user_master.m_id (split_part(session_id,'_',1) = m_id)`

## Notes

PERSONAL DATA. Names, phone, email, address, device ids and passwords are blocked. date_of_birth and pincode may only be used inside aggregates or to build age groups, never listed. tier holds loyalty values (Gold / Silver / Bronze) - it is NOT a city tier.

## Example questions

- users by city
- gender split of users
- users in West zone
- age groups of users

# Schema

| column | type | meaning | policy |
|---|---|---|---|
| password | text | (hidden) | blocked |
| last_login | timestamptz | (hidden) | blocked |
| is_superuser | boolean | (hidden) | blocked |
| username | text | (hidden) | blocked |
| first_name | text | (hidden) | blocked |
| last_name | text | (hidden) | blocked |
| email | text | (hidden) | blocked |
| is_staff | boolean | (hidden) | blocked |
| is_active | boolean | Active flag | ok |
| date_joined | timestamptz | Joined panel | ok |
| id | integer | User id (referenced as user_assoc_id) | ok |
| is_deleted | boolean | Soft-delete flag | ok |
| created_at | timestamptz | Record created | ok |
| updated_at | timestamptz | Record updated | ok |
| remarks | text | Free text | heavy |
| m_id | text | Device-side user key; prefix of session_id | ok |
| mobile_number | text | (hidden) | blocked |
| version | integer | Record version | ok |
| name | text | (hidden) | blocked |
| city | text | City | ok |
| android_version | text | Android version | ok |
| app_version | text | Tracker app version | ok |
| current_status | text | Panel status | ok |
| device_model | text | Phone model | ok |
| device_id | text | (hidden) | blocked |
| fcm_device_id | unknown | (hidden) | blocked |
| address | text | (hidden) | blocked |
| date_of_birth | date | Date of birth (aggregate only: use for age groups) | aggregate_only |
| gender | text | Gender | ok |
| marital_status | text | Marital status | ok |
| pincode | text | Postal code (aggregate only) | aggregate_only |
| population | integer | Population class of the user's town | ok |
| state | text | State | ok |
| tier | text | Loyalty tier (Gold/Silver/Bronze), not city tier | ok |
| zone | text | Zone (North/South/East/West) | ok |
| total_points | integer | Reward points | ok |
| new_total_points | integer | Reward points (new scheme) | ok |
| metadata | json | JSON (heavy) | heavy |
