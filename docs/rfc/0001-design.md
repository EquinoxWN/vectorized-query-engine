# RFC 0001: vectorized-query-engine design

- **Status:** Accepted (M1 implemented)
- **Author:** EquinoxWN
- **Created:** 2026

## Problem

Analytical databases such as DuckDB answer a five-table join over millions of rows in a fraction of
a second, and most engineers treat that as magic. Three ideas do most of the work: an optimizer that
rewrites the query before running it (filters as early as possible, only the needed columns, joins
in a sensible order), execution on whole columns at a time instead of one row at a time, and a
storage layer that skips data it can prove is irrelevant. This project builds those pieces in a
small, readable engine and checks every answer against DuckDB, so the claims are measured instead of
asserted.

## Goals

- **M1 (this RFC):** a SQL front end and a rule-based optimizer.
  - sqlglot parses SQL; a binder of our own resolves every table and column against a catalog,
    type-checks expressions, enforces GROUP BY rules and folds constants (including `DATE` +/-
    `INTERVAL`), and produces a typed logical plan.
  - The optimizer pushes each WHERE conjunct as low as it can go (into a scan, into one side of a
    join, or into the join itself, which turns a cross product into a hash join), copies constant
    filters across equi joins (transitive predicates), reorders inner joins greedily using row
    counts and distinct-value statistics, and prunes every scan to the columns the query reads.
  - EXPLAIN prints the plan with an estimated row count per node.
  - Every result is checked against DuckDB on the same Arrow tables: 21 TPC-H-style queries, 150
    seeded random queries and 16 NULL-semantics queries, each with the optimizer on and off.
- **M2:** physical operators of our own on Arrow record batches (hash join and hash aggregate
  written from scratch, pipelined batch by batch), with spill-to-disk as a stretch goal.
- **M3:** Parquet scans that skip row groups using min/max statistics, sqllogictest files, and a
  TPC-H scale-factor-1 benchmark against DuckDB with an honest analysis of where we are slower.

## Non-goals

- Being a production database: no transactions, no persistence, no concurrency control.
- Full SQL. M1 supports inner and cross joins, WHERE, GROUP BY, HAVING, ORDER BY, LIMIT, the five
  standard aggregates plus `count(DISTINCT ...)`, arithmetic, comparisons, `AND`/`OR`/`NOT`, `IN`
  lists, `LIKE`, `BETWEEN`, `IS [NOT] NULL` and date constants. Outer joins, subqueries, CTEs,
  window functions and `DISTINCT` are refused with a clear `BindError`, never silently mis-run.
- Cost-based join enumeration (dynamic programming over subsets): see ADR 0003.

## Proposed design

```
SQL text
  -> sqlglot AST (dialect: duckdb)
  -> Binder: catalog lookup, types, GROUP BY rules, constant folding  -> logical plan
  -> Optimizer: predicate pushdown -> transitive predicates -> greedy join reordering -> column pruning
  -> Executor: Arrow compute kernels over whole columns  -> pyarrow.Table
```

- **Catalog** (`catalog.py`): registered Arrow tables with row counts and the number of distinct
  values per column, computed once at registration.
- **Logical plan** (`plan.py`): `Scan` (with the columns to read and an optional pushed-down
  filter), `Filter`, `Join` (condition `None` is a cross product), `Aggregate`, `Project`, `Sort`,
  `Limit`. Every node is an immutable dataclass, so rewrites build new trees and tests compare plans
  structurally.
- **Expressions** (`expr.py`): bound and typed (`int64`, `double`, `string`, `bool`, `date32`,
  `null`); column references are qualified (`orders.o_orderdate`), so pushdown only has to compare
  sets of names.
- **Estimates** (`optimizer.py`): scans use table sizes; `col = constant` keeps `1 / ndv(col)`; an
  equi join divides the product of its inputs by the larger `ndv` of its keys; range predicates
  keep a third, `LIKE` a tenth, as in textbook optimizers. They only need to rank join orders, not
  to be exact.
- **Executor** (`executor.py`): each operator consumes and produces whole Arrow tables, so every
  comparison, arithmetic operation, filter, join and aggregation runs in Arrow's C++ kernels, not in
  a Python loop. In M1 the joins and aggregates are Arrow's own; writing them ourselves is M2.
- **Semantics checked against DuckDB:** three-valued logic (`NULL IN (...)`, `NOT IN` with a NULL in
  the list), IEEE 754 division (`x / 0` is infinity, `0 / 0` is NaN), `avg` as a double, and
  month arithmetic that clamps to the end of the month (`DATE '1995-01-31' + INTERVAL 1 MONTH` is
  February 28).

### Safety limits

User SQL is untrusted input, so the engine fails with a clear error rather than exhausting the
machine:

- queries longer than 100,000 characters, or nested deeply enough to exhaust Python's recursion
  limit, raise `BindError`;
- a cross product larger than 20 million rows is refused before it is built;
- an equi join's exact output size is computed from per-key counts on both sides before the join
  runs, and joins producing more than 50 million rows are refused.

## Alternatives considered

| Option | Why not (yet) |
|---|---|
| Write our own SQL parser | Parsing is a solved problem and not what makes analytics fast; sqlglot gives a well-tested AST in many dialects, and the binder is where the interesting checks live (ADR 0002). |
| Use sqlglot's optimizer | It rewrites SQL text-to-text and would hide exactly the steps this project exists to show. |
| Cost-based, dynamic-programming join enumeration (System R) | Exponential in the number of tables and needs good cost models; a greedy order already removes every cross product in the TPC-H shapes we test. Revisit if a benchmark query shows a bad order (ADR 0003). |
| Row-at-a-time (Volcano) execution in Python | Orders of magnitude slower in Python and the opposite of what the project explains. |
| Compare against SQLite | It is a row store; DuckDB is the vectorized engine this project is measured against. |

## Measurement plan

- **Correctness:** every query in the test suite gives the same rows as DuckDB (numbers compared by
  value with a relative tolerance of 1e-9; unordered results compared as multisets).
- **Optimizer effect:** cells read (rows times columns scanned) with the optimizer off and on, and
  which literal plans cannot run at all.
- **Speed:** `vqe-report` prints best-of-N wall time for each query against DuckDB on the same Arrow
  tables. M3 replaces this with TPC-H scale factor 1 from Parquet.

## Milestones

- **M1 (done):** binder, logical plan, rule-based optimizer, EXPLAIN, Arrow execution, DuckDB
  oracle tests, `vqe` and `vqe-report` commands.
- **M2:** our own hash join and hash aggregate over record batches; pipelining; spill to disk.
- **M3:** Parquet with row-group skipping; sqllogictest; TPC-H scale factor 1 against DuckDB.

## Risks and open questions

- Selectivity constants can be badly wrong for skewed data; histograms are a later improvement.
- A greedy join order can be poor for some shapes (for example star schemas with a large fact table
  in the middle); M3 measurements will show whether DP is worth adding.
- The engine and DuckDB call different kernels, so tiny floating-point differences in sums are
  expected; the tolerance is documented in the tests.
