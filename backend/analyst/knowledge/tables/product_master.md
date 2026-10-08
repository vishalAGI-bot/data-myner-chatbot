---
type: Table
title: Product catalogue
description: Product catalogue with brand and taxonomy.
resource: postgresql://datamyner_db/public/product_master
tags:
- product
- brand
- catalogue
- sku
status: stable
domain: analytics
grain: one row per product
primary_key: id
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Product catalogue

Product catalogue with brand and taxonomy.

## Grain

one row per product

## Joins

- `testing_accessibility_infos.product_assoc_id = product_master.id`

## Notes

product_master column is the product name.

## Example questions

- how many products by brand
- top brands in Dairy

# Schema

| column | type | meaning | policy |
|---|---|---|---|
| id | integer | Product id | ok |
| product_uid | text | External product uid | ok |
| product_master | text | Product name | ok |
| brand | text | Brand | ok |
| sub_category | text | Sub category | ok |
| category | text | Category | ok |
| master_category | text | Master category | ok |
