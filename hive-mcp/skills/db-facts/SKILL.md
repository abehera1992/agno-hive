---
name: db-facts
description: When a value lives in a database table, treat the live table as authoritative over a file grep — load before answering any DB-backed fact question.
---
Database-backed facts (when db_query / db_schema are available): if a value is
stored in a database table (a count of rows, the current value of a column, "how
many X have status Y"), the LIVE TABLE is the source of truth — a file grep of a
seed/migration/code fallback can be stale or incomplete. Call db_schema(table) to
confirm the exact schema + column names, then db_query with an aggregate
(SELECT col, count(*) ... GROUP BY col) to get the authoritative number. Report the
DB result as the total; treat file greps as SUPPLEMENTARY subtotals (and note when
the DB and the code/seed disagree — they often do).
