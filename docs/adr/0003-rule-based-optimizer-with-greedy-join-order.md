# ADR 0003: Rule-based optimizer with a greedy join order and textbook estimates

- **Status:** Accepted

## Context

The literal translation of `FROM customer, orders, lineitem WHERE ...` is a cross product of all
three tables filtered afterwards: at TPC-H scale factor 1 that is about 1.35 x 10^15 rows, which no
engine can build. The optimizer has to place each predicate where it belongs, find join conditions
in the WHERE clause, choose a join order, and avoid reading columns nobody uses. Full cost-based
enumeration (System R dynamic programming, or a Cascades framework) is the industrial answer but is
large, and its value depends on cost models this engine does not have yet.

## Decision

Four rules, applied in order:

1. **Predicate pushdown:** split WHERE and ON conditions into conjuncts and place each as low as
   possible: into a scan if it uses one table, into one side of a join if it fits there, otherwise
   into the lowest join that has all its columns (which turns a cross product into a hash join).
   Conditions on aggregates (HAVING) stay above the aggregate.
2. **Transitive predicates:** when an inner equi join makes `a.k = b.k` and the scan of `a` keeps
   only rows where `a.k` compares with constants (`a.k <= 100`, `a.k IN (1, 2)`), add the same
   condition on `b.k` to the scan of `b`. Rows it removes could never have matched, and the join's
   input shrinks before it is built.
3. **Greedy join reordering:** flatten each tree of inner joins, start from the relation with the
   smallest estimated size, and repeatedly add the relation that is connected by a join condition
   and gives the smallest estimated result; unconnected relations come last.
4. **Column pruning:** walk down from the output and keep, at each scan, only the columns that
   some operator above it reads.

Estimates use table row counts and per-column distinct counts gathered at registration, with
textbook constants where statistics run out (a range keeps a third, `LIKE` a tenth).

## Consequences

- All 21 test queries run with the optimizer, including two whose literal plans are refused as too
  large, and every result equals DuckDB's with the optimizer on and off.
- Optimized plans read about 35% of the cells the literal plans read across the runnable queries.
- Transitive predicates took the self-join test query from 52.9 ms to 6.3 ms at scale 1, because
  the probe side shrinks from 600,669 rows to about 400 before the join.
- Greedy ordering can miss the best order for some shapes; the M3 benchmark will show whether
  dynamic programming over join subsets is worth adding, and the estimate function is already
  separate so a cost-based search can reuse it.
