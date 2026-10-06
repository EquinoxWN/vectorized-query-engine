# ADR 0002: Parse with sqlglot, bind and type-check with our own binder

- **Status:** Accepted

## Context

The engine needs a SQL front end. Writing a parser by hand would cost weeks and teach little about
why analytical databases are fast. sqlglot parses many dialects into a uniform AST and has its own
optimizer, but using that optimizer would hide the rewrites this project is meant to show, and its
AST is untyped: it does not know that `o_orderstatus = 1` compares text with a number.

## Decision

Use sqlglot only as a parser (DuckDB dialect, so the same SQL runs on our engine and on DuckDB). A
binder of our own walks the AST and produces a typed logical plan: every column is resolved to one
table (ambiguous names are errors, misspelled ones get a "did you mean" hint), every expression has a
type, GROUP BY rules are enforced, and constant expressions, including `DATE` +/- `INTERVAL`, are
folded at bind time. Anything the executor cannot run (outer joins, subqueries, CTEs, `DISTINCT`,
window functions) is refused at bind time with a `BindError` that names the feature.

## Consequences

- Errors are found before execution and read like a database's errors, which the binder tests pin
  down (29 bad queries, each with its expected message).
- The optimizer works on a small set of typed node classes instead of sqlglot's large AST.
- sqlglot changes its AST between major versions (for example `from` became `from_` and `with`
  became `with_`); the dependency is pinned to one major version and the binder tests catch such
  changes.
