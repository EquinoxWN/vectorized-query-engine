"""The binder resolves names and types, folds constants, and rejects bad queries with a clear reason."""

from __future__ import annotations

import datetime as dt
import math

import pytest

from vectorized_query_engine import BindError, Engine
from vectorized_query_engine.binder import MAX_SQL_LENGTH
from vectorized_query_engine.expr import Bin, Lit, conjuncts
from vectorized_query_engine.plan import Filter, Plan, Project, Scan, output_columns

ERRORS = [
    ("SELECT * FROM ordrs", r"unknown table ordrs \(did you mean orders\?\)"),
    (
        "SELECT o_totalprise FROM orders",
        r"unknown column o_totalprise \(did you mean o_totalprice\?\)",
    ),
    ("SELECT o.o_bogus FROM orders o", r"unknown column o\.o_bogus"),
    ("SELECT x.o_orderkey FROM orders o", r"unknown table or alias x"),
    ("SELECT l_orderkey FROM lineitem a, lineitem b", r"ambiguous"),
    ("SELECT * FROM orders o, customer o", r"duplicate table alias o"),
    ("SELECT o_custkey, count(*) FROM orders", r"must appear in GROUP BY"),
    ("SELECT sum(c_name) FROM customer", r"SUM needs a number"),
    ("SELECT * FROM orders WHERE o_totalprice", r"WHERE needs a boolean"),
    ("SELECT * FROM orders WHERE o_orderstatus = 1", r"cannot compare string with int64"),
    ("SELECT o_orderstatus + 1 FROM orders", r"cannot apply \+ to string and int64"),
    ("SELECT * FROM orders LEFT JOIN customer ON o_custkey = c_custkey", r"unsupported: LEFT JOIN"),
    ("SELECT * FROM (SELECT 1)", r"subqueries"),
    ("SELECT DISTINCT o_orderstatus FROM orders", r"unsupported: DISTINCT"),
    ("WITH x AS (SELECT 1) SELECT * FROM x", r"unsupported: WITH"),
    ("SELECT 1 FROM orders; SELECT 2 FROM orders", r"exactly one SELECT"),
    ("DELETE FROM orders", r"exactly one SELECT"),
    ("SELECT * FROM orders WHERE (", r"syntax error"),
    ("SELECT * FROM orders LIMIT -1", r"LIMIT must be a non-negative integer"),
    ("SELECT * FROM orders ORDER BY 9", r"ORDER BY position 9 is out of range"),
    (
        "SELECT o_orderkey AS k, o_custkey AS k FROM orders",
        r"duplicate output column names \['k'\]",
    ),
    ("SELECT * FROM orders WHERE o_orderdate > CAST('1995-13-01' AS DATE)", r"invalid date"),
    (
        "SELECT * FROM orders WHERE o_custkey IN (SELECT c_custkey FROM customer)",
        r"IN \(subquery\)",
    ),
    ("SELECT * FROM orders WHERE o_custkey IN (o_orderkey)", r"IN lists must contain constants"),
    ("SELECT sum(DISTINCT o_totalprice) FROM orders", r"SUM\(DISTINCT"),
    ("SELECT median(o_totalprice) FROM orders", r"unsupported aggregate"),
    ("SELECT 1", r"a FROM clause is required"),
    ("SELECT * FROM orders WHERE o_orderdate > INTERVAL 1 DAY", r"INTERVAL"),
    ("SELECT * FROM orders WHERE o_orderdate > o_orderdate + INTERVAL 1 DAY", r"DATE constant"),
]


@pytest.mark.parametrize(("sql", "message"), ERRORS, ids=[e[0][:60] for e in ERRORS])
def test_bad_queries_are_rejected_with_the_reason(engine: Engine, sql: str, message: str) -> None:
    with pytest.raises(BindError, match=message):
        engine.plan(sql)


def test_hostile_input_is_a_bind_error_never_a_crash(engine: Engine) -> None:
    with pytest.raises(BindError, match="longer than"):
        engine.plan("SELECT * FROM orders WHERE " + "1 = 1 AND " * (MAX_SQL_LENGTH // 10) + "TRUE")
    deep = "SELECT * FROM orders WHERE " + "(" * 3000 + "TRUE" + ")" * 3000
    with pytest.raises(BindError):
        engine.plan(deep)


def where_literals(plan: Plan) -> list[Lit]:
    """Constants on the right-hand side of the unoptimized WHERE."""
    while not isinstance(plan, Filter):
        plan = plan.child  # type: ignore[union-attr]
    return [
        p.right
        for p in conjuncts(plan.predicate)
        if isinstance(p, Bin) and isinstance(p.right, Lit)
    ]


@pytest.mark.parametrize(
    ("expr", "value"),
    [
        ("DATE '1994-01-01' + INTERVAL 1 YEAR", dt.date(1995, 1, 1)),
        ("DATE '1998-12-01' - INTERVAL 90 DAY", dt.date(1998, 9, 2)),
        ("DATE '1993-10-01' + INTERVAL 3 MONTH", dt.date(1994, 1, 1)),
        ("DATE '1995-01-31' + INTERVAL 1 MONTH", dt.date(1995, 2, 28)),
        ("DATE '1996-03-31' - INTERVAL 1 MONTH", dt.date(1996, 2, 29)),
    ],
)
def test_date_arithmetic_is_folded_at_bind_time(engine: Engine, expr: str, value: dt.date) -> None:
    plan = engine.plan(f"SELECT o_orderkey FROM orders WHERE o_orderdate < {expr}", optimize=False)
    assert where_literals(plan) == [Lit(value, "date32")]


def test_arithmetic_on_constants_is_folded(engine: Engine) -> None:
    plan = engine.plan(
        "SELECT o_orderkey FROM orders WHERE o_totalprice > 1000 * 100 + 1", optimize=False
    )
    assert where_literals(plan) == [Lit(100001, "int64")]
    plan = engine.plan("SELECT o_orderkey FROM orders WHERE o_totalprice > 1 / 0", optimize=False)
    assert where_literals(plan) == [Lit(math.inf, "double")], "IEEE division, as in DuckDB"


def test_between_becomes_two_comparisons(engine: Engine) -> None:
    plan = engine.plan(
        "SELECT o_orderkey FROM orders WHERE o_totalprice BETWEEN 10 AND 20", optimize=False
    )
    assert isinstance(plan, Project)
    assert isinstance(plan.child, Filter)
    assert [p.op for p in conjuncts(plan.child.predicate) if isinstance(p, Bin)] == [">=", "<="]


def test_output_names_and_star_expansion(engine: Engine) -> None:
    assert output_columns(engine.plan("SELECT o_orderkey, o_totalprice * 2 FROM orders")) == [
        "o_orderkey",
        "col1",
    ]
    assert output_columns(engine.plan("SELECT * FROM region")) == ["r_regionkey", "r_name"]
    assert output_columns(engine.plan("SELECT n.* FROM nation n, region r")) == [
        "n_nationkey",
        "n_name",
        "n_regionkey",
    ]


def test_unoptimized_plan_is_a_literal_translation(engine: Engine) -> None:
    plan = engine.plan(
        "SELECT c_name FROM customer, orders WHERE c_custkey = o_custkey", optimize=False
    )
    assert isinstance(plan, Project)
    assert isinstance(plan.child, Filter)
    scans = [plan.child.child.left, plan.child.child.right]  # type: ignore[union-attr]
    assert all(isinstance(s, Scan) and s.filter is None for s in scans)
    assert [len(s.columns) for s in scans if isinstance(s, Scan)] == [5, 6]
