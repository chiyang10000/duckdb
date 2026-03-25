# QueryRelation rowid notes

This tree currently supports hidden/orderable `rowid` on pandas- and arrow-backed replacement scans.

The remaining gap is the lazy `duckdb.sql(...)` / `QueryRelation` path. `QueryRelation` rewrites Python replacement
scans into internal CTEs of the form:

```sql
WITH df AS (SELECT * FROM <replacement scan>)
SELECT ...
```

That transformation drops hidden virtual-column semantics, because the CTE/subquery binding only exposes regular output
columns.

## Earlier local approach

The earlier local CTE-oriented idea had two independent hacks:

- extend `SELECT *` expansion to include `rowid` whenever a binding had extra names
- mutate `BindCTE` so that if the child query referenced `rowid`, the CTE query also projected `rowid`

That made lazy rowid references work more often, but it also leaked `rowid` into `SELECT *`.

## Current experiment

This follow-up narrows the idea:

- only internal `QueryRelation` CTEs created for pandas/arrow replacement scans project an extra private column
  `__query_relation_rowid`
- when binding those subqueries, the last projected column is hidden from `SELECT *`
- explicit `rowid` references bind to that hidden last column instead

This aims to preserve:

- `duckdb.sql('select rowid from df')`
- `duckdb.sql('select * from df order by rowid desc')`
- `duckdb.sql('select rowid, * from df order by rowid')`

without reintroducing unconditional `rowid` leakage in `SELECT *`.

## Regression coverage

The pandas regression tests now cover both the successful lazy cases and the currently unsupported chaining case:

- positive:
  - `duckdb.sql('select * from df order by rowid desc')`
  - `duckdb.sql('select rowid, * from df order by rowid')`
  - explicit CTE usage such as `with t as (select rowid, * from df) ...`
- negative:
  - `duckdb.sql('from df').project('rowid, *')`

The chaining case is intentionally kept as a negative test for now so future changes do not accidentally blur the
current contract.

## Known limitation

This still does **not** automatically make chained lazy relations carry hidden rowid forward, e.g.
`duckdb.sql('from df').project('rowid, *')`. That requires the hidden column to survive relation-to-subquery conversion,
which is a broader `QueryRelation` problem.
