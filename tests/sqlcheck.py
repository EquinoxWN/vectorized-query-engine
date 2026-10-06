"""Run one query on the engine and on DuckDB and fail with the first difference."""

from __future__ import annotations

import duckdb

from vectorized_query_engine import Engine
from vectorized_query_engine.compare import difference
from vectorized_query_engine.executor import ExecutionError


def check(
    engine: Engine, duck: duckdb.DuckDBPyConnection, sql: str, ordered: bool, optimize: bool
) -> bool:
    """Run sql on both engines and fail with the first difference.

    Returns False when the unoptimized plan was refused as a too-large cross product: that literal
    translation of a multi-table WHERE is exactly what the optimizer exists to avoid.
    """
    try:
        ours = engine.sql(sql, optimize=optimize)
    except ExecutionError as e:
        if optimize or "cross join" not in str(e):
            raise
        return False
    theirs = duck.execute(sql).to_arrow_table()
    diff = difference(ours, theirs, ordered)
    assert diff is None, f"{diff}\n{sql}"
    return True
