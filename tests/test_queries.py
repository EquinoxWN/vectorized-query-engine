"""Twenty-one TPC-H-style queries give the same rows as DuckDB, with the optimizer on and off."""

from __future__ import annotations

import duckdb
import pytest
from sqlcheck import check

from vectorized_query_engine import Engine
from vectorized_query_engine.queries import QUERIES

# Comma joins of three or more tables: the literal plan is a cross product of millions of rows,
# which the executor refuses. Only the optimizer (join conditions taken from WHERE) can run them.
NAIVE_TOO_BIG = {"q3_shipping_priority", "q10_returned_items"}


@pytest.mark.parametrize("optimize", [True, False], ids=["optimized", "unoptimized"])
@pytest.mark.parametrize(("name", "ordered", "sql"), QUERIES, ids=[q[0] for q in QUERIES])
def test_same_rows_as_duckdb(
    engine: Engine,
    duck: duckdb.DuckDBPyConnection,
    name: str,
    ordered: bool,
    sql: str,
    optimize: bool,
) -> None:
    ran = check(engine, duck, sql, ordered, optimize)
    assert ran == (optimize or name not in NAIVE_TOO_BIG)


def test_queries_return_rows(engine: Engine) -> None:
    empty = [name for name, _, sql in QUERIES if engine.sql(sql).num_rows == 0]
    assert empty == [], "every query should exercise real data"
