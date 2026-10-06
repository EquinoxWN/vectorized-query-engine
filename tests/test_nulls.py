"""SQL's three-valued logic around NULL matches DuckDB."""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pyarrow as pa
import pytest
from sqlcheck import check

from vectorized_query_engine import Catalog, Engine

T = pa.table(
    {
        "id": pa.array([1, 2, 3, 4, 5, 6], pa.int64()),
        "a": pa.array([1, None, 3, None, 5, 0], pa.int64()),
        "s": pa.array(["x", "y", None, "x", None, "z"], pa.string()),
        "v": pa.array([1.5, 2.5, None, 4.0, 0.0, 6.0], pa.float64()),
    }
)
U = pa.table(
    {"a": pa.array([1, None, 5, 5], pa.int64()), "w": pa.array(["one", "none", "five", "five2"])}
)

QUERIES = [
    ("SELECT id FROM t WHERE a IN (1, 3)", False),
    ("SELECT id FROM t WHERE a NOT IN (1, 3)", False),
    ("SELECT id FROM t WHERE a NOT IN (1, NULL)", False),
    ("SELECT id FROM t WHERE a IN (1, NULL)", False),
    ("SELECT id FROM t WHERE s NOT IN ('x')", False),
    ("SELECT id FROM t WHERE a IS NULL", False),
    ("SELECT id FROM t WHERE s IS NOT NULL AND v > 1", False),
    ("SELECT id FROM t WHERE NOT (a > 2)", False),
    ("SELECT id FROM t WHERE a > 2 OR s = 'x'", False),
    (
        "SELECT count(*) AS n, count(a) AS na, sum(a) AS sa, avg(v) AS av, min(s) AS ms FROM t",
        False,
    ),
    ("SELECT s, count(*) AS n, sum(v) AS sv FROM t GROUP BY s", False),
    ("SELECT id, v / a AS r FROM t ORDER BY id", True),
    ("SELECT id, a + 1 AS b FROM t ORDER BY id", True),
    ("SELECT t.id, u.w FROM t JOIN u ON t.a = u.a", False),
    ("SELECT count(*) AS n FROM t WHERE s LIKE 'x%'", False),
    ("SELECT id FROM t WHERE a BETWEEN 0 AND 3", False),
]


@pytest.fixture(scope="module")
def pair() -> Iterator[tuple[Engine, duckdb.DuckDBPyConnection]]:
    """The same two small tables with NULLs in the engine and in DuckDB."""
    cat = Catalog()
    cat.register("t", T)
    cat.register("u", U)
    con = duckdb.connect()
    con.register("t", T)
    con.register("u", U)
    yield Engine(cat), con
    con.close()


@pytest.mark.parametrize(("sql", "ordered"), QUERIES, ids=[q[0] for q in QUERIES])
def test_null_semantics_match_duckdb(
    pair: tuple[Engine, duckdb.DuckDBPyConnection], sql: str, ordered: bool
) -> None:
    engine, duck = pair
    check(engine, duck, sql, ordered, optimize=True)
    check(engine, duck, sql, ordered, optimize=False)
