---
type: Access Policy
title: What the chatbot may read
description: Tables and columns the chatbot must never query or show.
blocked_tables:
- auth_group
- auth_group_permissions
- auth_permission
- authtoken_token
- analytics_token
- django_admin_log
- django_content_type
- django_migrations
- django_session
- internal_user
- internal_user_groups
- internal_user_user_permissions
- user_master_groups
- user_master_user_permissions
- llm_api_key
- llm_provider
- llm_configuration
- llm_prompt_template
- audit_log
status: stable
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Access policy

The chatbot connects with a read-only session and additionally enforces this policy on every SQL statement
(including SQL written by the language model) before it runs.

## Blocked tables
Authentication, sessions, tokens, API keys, admin logs and Django internals (listed in `blocked_tables` above).
Any table not documented in `tables/` is also refused.

## Column policies (in each table's Schema)
- `ok` - can be selected, filtered and grouped.
- `aggregate_only` - personal data that may only be used inside expressions or aggregates (e.g. age groups from
  date_of_birth), never returned as a raw column.
- `heavy` - large raw text / JSON / vectors; may be filtered on but not returned.
- `blocked` - never referenced at all (passwords, names, phone, email, address, device ids, hashes).

## Recommended database role
Run `analyst/sql/readonly_role.sql` once so the database itself enforces the same rules.
