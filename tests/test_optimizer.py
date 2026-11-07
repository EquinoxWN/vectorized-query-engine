"""The optimizer moves filters into scans and joins, reorders joins and reads fewer columns."""

from __future__ import annotations

import contextlib

import pytest

from vectorized_query_engine import Engine
from vectorized_query_engine.executor import ExecutionError, Executor
from vectorized_query_engine.expr import Bin, Col, Lit, columns
from vectorized_query_engine.optimizer import Optimizer, _equivalences
from vectorized_query_engine.plan import Aggregate, Filter, Join, Plan, Scan, output_columns
from vectorized_query_engine.queries import QUERIES

Q3 = next(sql for name, _, sql in QUERIES if name == "q3_shipping_priority")
Q5 = next(sql for name, _, sql in QUERIES if name == "q5_volume_by_nation")
JOINING = [
    (n, s) for n, _, s in QUERIES if "JOIN" in s or "," in s.split("FROM")[1].split("WHERE")[0]
]


def nodes(p: Plan) -> list[Plan]:
    """Every node, parents first."""
    out = [p]
    if isinstance(p, Join):
        return out + nodes(p.left) + nodes(p.right)
    child = getattr(p, "child", None)
    return out + (nodes(child) if child is not None else [])


def scans(p: Plan) -> dict[str, Scan]:
    """Scans by alias."""
    return {n.alias: n for n in nodes(p) if isinstance(n, Scan)}


def leftmost(p: Plan) -> Scan:
    """The first relation a left-deep join tree reads."""
    while not isinstance(p, Scan):
        p = p.left if isinstance(p, Join) else p.child
    return p


def test_filters_move_into_scans_and_join_conditions_come_from_where(engine: Engine) -> None:
    plan = engine.plan(Q3)
    s = scans(plan)
    filters = {alias: columns(scan.filter) for alias, scan in s.items() if scan.filter is not None}
    assert filters == {
        "customer": {"customer.c_mktsegment"},
        "orders": {"orders.o_orderdate"},
        "lineitem": {"lineitem.l_shipdate"},
    }
    joins = [n for n in nodes(plan) if isinstance(n, Join)]
    assert len(joins) == 2
    assert all(j.condition is not None for j in joins), "no cross products left"
    assert not any(isinstance(n, Filter) for n in nodes(plan)), "every WHERE conjunct was placed"


def test_unoptimized_q3_is_a_filter_over_cross_products(engine: Engine) -> None:
    plan = engine.plan(Q3, optimize=False)
    joins = [n for n in nodes(plan) if isinstance(n, Join)]
    assert all(j.condition is None for j in joins)
    assert any(isinstance(n, Filter) for n in nodes(plan))


def test_only_needed_columns_are_read(engine: Engine) -> None:
    s = scans(engine.plan(Q3))
    assert s["customer"].columns == ("c_custkey",)
    assert set(s["orders"].columns) == {"o_orderkey", "o_custkey", "o_orderdate"}
    assert set(s["lineitem"].columns) == {"l_orderkey", "l_extendedprice", "l_discount"}


def test_joins_start_from_the_smallest_filtered_relation(engine: Engine) -> None:
    assert leftmost(engine.plan(Q5)).table == "region"
    assert leftmost(engine.plan(Q5, optimize=False)).table == "customer"


def test_having_stays_above_the_aggregate(engine: Engine) -> None:
    plan = engine.plan(
        "SELECT o_custkey, count(*) AS n FROM orders GROUP BY o_custkey HAVING count(*) > 3"
    )
    filters = [n for n in nodes(plan) if isinstance(n, Filter)]
    assert len(filters) == 1
    assert isinstance(filters[0].child, Aggregate)


def test_optimized_plans_keep_the_output_columns(engine: Engine) -> None:
    for _, _, sql in QUERIES:
        assert output_columns(engine.plan(sql)) == output_columns(
            engine.plan(sql, optimize=False)
        ), sql


def test_estimates_use_table_sizes_and_distinct_counts(engine: Engine) -> None:
    opt = Optimizer(engine.catalog)
    orders = engine.catalog.get("orders")
    assert orders is not None
    plain = engine.plan("SELECT o_orderkey FROM orders")
    filtered = engine.plan("SELECT o_orderkey FROM orders WHERE o_orderstatus = 'F'")
    assert opt.estimate(plain) == orders.rows
    assert opt.estimate(filtered) == pytest.approx(orders.rows / orders.ndv["o_orderstatus"])
    joined = engine.plan("SELECT o_orderkey FROM customer JOIN orders ON c_custkey = o_custkey")
    assert opt.estimate(joined) == pytest.approx(orders.rows, rel=0.01)


def cells(engine: Engine, sql: str, optimize: bool) -> int:
    """Cells (rows x columns) the plan's scans read, even if a later join is refused."""
    ex = Executor(engine.catalog)
    with contextlib.suppress(ExecutionError):
        ex.run(engine.plan(sql, optimize=optimize))
    return ex.stats.cells_scanned


@pytest.mark.parametrize(("name", "sql"), JOINING, ids=[n for n, _ in JOINING])
def test_optimizer_never_reads_more_cells(engine: Engine, name: str, sql: str) -> None:
    assert cells(engine, sql, optimize=True) <= cells(engine, sql, optimize=False), name


def test_q3_reads_far_fewer_cells_when_optimized(engine: Engine) -> None:
    assert cells(engine, Q3, optimize=True) < cells(engine, Q3, optimize=False) / 2


def test_a_constant_filter_crosses_an_equi_join_to_the_other_side(engine: Engine) -> None:
    s = scans(engine.plan(next(sql for name, _, sql in QUERIES if name == "self_join")))
    assert s["b"].filter is not None
    assert columns(s["b"].filter) == {"b.l_orderkey"}, "a.l_orderkey <= 100 and a = b give b <= 100"
    s = scans(
        engine.plan(
            "SELECT o_orderkey FROM customer JOIN orders ON c_custkey = o_custkey WHERE c_custkey IN (1, 2)"
        )
    )
    assert s["orders"].filter is not None
    assert columns(s["orders"].filter) == {"orders.o_custkey"}


def test_filters_that_are_not_column_versus_constant_do_not_cross(engine: Engine) -> None:
    s = scans(
        engine.plan(
            "SELECT o_orderkey FROM customer JOIN orders ON c_custkey = o_custkey "
            "WHERE c_acctbal < 0 AND c_name LIKE 'Customer#00000001%'"
        )
    )
    assert s["orders"].filter is None, "LIKE and filters on non-key columns stay where they are"


def test_a_connected_relation_is_joined_before_a_cheaper_cross_product(engine: Engine) -> None:
    # region (one name) is the smallest start; customer with c_custkey = 1 is estimated at 1 row,
    # so crossing it is "cheaper" than joining nation; the connected nation must still come first.
    plan = engine.plan(
        "SELECT r_name, n_name, c_name FROM region, nation, customer "
        "WHERE r_regionkey = n_regionkey AND r_name = 'ASIA' AND c_custkey = 1"
    )
    joins = [n for n in nodes(plan) if isinstance(n, Join)]
    assert joins, plan
    top, *below = joins
    assert top.condition is None, "the unavoidable cross product comes last"
    assert all(j.condition is not None for j in below), "no cross product below the top join"


def test_or_selectivity_is_inclusion_exclusion(engine: Engine) -> None:
    opt = Optimizer(engine.catalog)
    a = Bin("<", Col("orders.o_totalprice", "double"), Lit(1.0, "double"), "bool")
    b = Bin(">", Col("orders.o_totalprice", "double"), Lit(2.0, "double"), "bool")
    assert opt.selectivity(Bin("OR", a, b, "bool")) == pytest.approx(1 / 3 + 1 / 3 - 1 / 9)
    assert opt.selectivity(Bin("AND", a, b, "bool")) == pytest.approx(1 / 9)


def test_columns_of_different_types_are_never_made_equivalent() -> None:
    cond = Bin("=", Col("a.k", "int64"), Col("b.k", "double"), "bool")
    plan = Join(Scan("t", "a", ("k",), None), Scan("u", "b", ("k",), None), cond)
    assert _equivalences(plan) == {}
    same = Bin("=", Col("a.k", "int64"), Col("b.k", "int64"), "bool")
    plan = Join(Scan("t", "a", ("k",), None), Scan("u", "b", ("k",), None), same)
    assert _equivalences(plan)["a.k"] == {"a.k", "b.k"}


def test_an_equi_join_written_right_to_left_is_a_hash_join_not_a_cross_product(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vectorized_query_engine import executor

    # Customer is the left input and the condition names the right side first.
    monkeypatch.setattr(executor, "MAX_CROSS_ROWS", 1000)
    sql = "SELECT count(*) AS n FROM customer JOIN orders ON o_custkey = c_custkey"
    result = engine.sql(sql, optimize=False)
    assert result.column("n").to_pylist() == [engine.catalog.get("orders").data.num_rows]  # type: ignore[union-attr]


def test_pruning_keeps_the_columns_a_filter_reads(engine: Engine) -> None:
    pred = Bin(">", Col("orders.o_totalprice", "double"), Lit(1.0, "double"), "bool")
    scan = Scan("orders", "orders", ("o_orderkey", "o_totalprice"), None)
    pruned = Optimizer(engine.catalog)._prune(Filter(scan, pred), {"orders.o_orderkey"})
    assert isinstance(pruned, Filter)
    assert isinstance(pruned.child, Scan)
    assert pruned.child.columns == ("o_orderkey", "o_totalprice")
