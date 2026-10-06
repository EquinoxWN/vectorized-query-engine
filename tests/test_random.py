"""Hundreds of random queries (filters, joins, aggregates) agree with DuckDB, optimized or not."""

from __future__ import annotations

import random
from typing import Any

import duckdb
import pyarrow as pa
import pytest
from sqlcheck import check

from vectorized_query_engine import Engine

COLUMNS: dict[str, dict[str, list[str]]] = {
    "customer": {"num": ["c_acctbal", "c_custkey", "c_nationkey"], "cat": ["c_mktsegment"]},
    "orders": {
        "num": ["o_totalprice", "o_orderkey"],
        "cat": ["o_orderstatus", "o_orderpriority"],
        "date": ["o_orderdate"],
    },
    "lineitem": {
        "num": ["l_quantity", "l_extendedprice", "l_discount", "l_linenumber"],
        "cat": ["l_returnflag", "l_linestatus"],
        "date": ["l_shipdate"],
    },
    "nation": {"num": ["n_regionkey"], "cat": ["n_name"]},
}
SHAPES: list[tuple[list[str], list[str]]] = [
    (["orders"], []),
    (["lineitem"], []),
    (["customer", "orders"], ["c_custkey = o_custkey"]),
    (["orders", "lineitem"], ["o_orderkey = l_orderkey"]),
    (["customer", "orders", "lineitem"], ["c_custkey = o_custkey", "o_orderkey = l_orderkey"]),
    (["nation", "customer", "orders"], ["n_nationkey = c_nationkey", "c_custkey = o_custkey"]),
]
COUNT = 150


def literal(v: Any) -> str:
    """SQL text for a value taken from the data."""
    if isinstance(v, str):
        return f"'{v}'"
    if hasattr(v, "isoformat"):
        return f"DATE '{v.isoformat()}'"
    return repr(v)


def predicate(rng: random.Random, tables: dict[str, pa.Table], names: list[str]) -> str:
    """One random condition on a column of one of the joined tables."""
    table = rng.choice(names)
    kind = rng.choice([k for k in COLUMNS[table] if COLUMNS[table][k]])
    col = rng.choice(COLUMNS[table][kind])
    values = tables[table].column(col).to_pylist()
    v = rng.choice(values)
    if kind == "cat":
        choice = rng.randrange(4)
        if choice == 0:
            return f"{col} = {literal(v)}"
        if choice == 1:
            return f"{col} IN ({literal(v)}, {literal(rng.choice(values))})"
        if choice == 2:
            return f"{col} NOT IN ({literal(v)})"
        return f"{col} LIKE '{str(v)[:1]}%'"
    op = rng.choice(["<", "<=", ">", ">=", "=", "!="])
    text = f"{col} {op} {literal(v)}"
    if rng.random() < 0.2:
        other = rng.choice(values)
        text = f"({text} OR {col} = {literal(other)})"
    return text


def make_query(rng: random.Random, tables: dict[str, pa.Table]) -> str:
    """A random SELECT over one of the join shapes."""
    names, joins = rng.choice(SHAPES)
    preds = [predicate(rng, tables, names) for _ in range(rng.randrange(0, 4))]
    if joins and rng.random() < 0.5:
        frm = names[0] + "".join(f" JOIN {n} ON {c}" for n, c in zip(names[1:], joins, strict=True))
        where = preds
    else:
        frm = ", ".join(names)
        where = joins + preds
    cats = [c for n in names for c in COLUMNS[n]["cat"]]
    nums = [c for n in names for c in COLUMNS[n]["num"]]
    if rng.random() < 0.7:
        groups = rng.sample(cats, k=min(len(cats), rng.randrange(0, 3)))
        aggs = []
        for i in range(rng.randrange(1, 4)):
            func = rng.choice(["sum", "avg", "min", "max", "count", "count_distinct", "count_star"])
            col = rng.choice(nums)
            expr = {"count_star": "count(*)", "count_distinct": f"count(DISTINCT {col})"}.get(
                func, f"{func}({col})"
            )
            aggs.append(f"{expr} AS a{i}")
        select = ", ".join(groups + aggs)
        sql = f"SELECT {select} FROM {frm}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        if groups:
            sql += " GROUP BY " + ", ".join(groups)
            if rng.random() < 0.3:
                sql += f" HAVING count(*) > {rng.randrange(0, 50)}"
        return sql
    picked = rng.sample(cats + nums, k=3)
    sql = f"SELECT {', '.join(picked)} FROM {frm}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return sql


def queries() -> list[str]:
    """The same COUNT queries on every run (seeded)."""
    from conftest import SCALE

    from vectorized_query_engine.data import generate

    rng = random.Random(20261006)
    tables = generate(scale=SCALE, seed=7)
    return [make_query(rng, tables) for _ in range(COUNT)]


QUERIES = queries()


@pytest.mark.parametrize("sql", QUERIES, ids=[f"q{i:03d}" for i in range(COUNT)])
def test_random_query_matches_duckdb_optimized_and_not(
    engine: Engine, duck: duckdb.DuckDBPyConnection, sql: str
) -> None:
    check(engine, duck, sql, ordered=False, optimize=True)
    check(engine, duck, sql, ordered=False, optimize=False)
