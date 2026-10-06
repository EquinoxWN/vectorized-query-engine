"""Runaway joins are refused with a clear error before memory is spent; normal joins are exact."""

from __future__ import annotations

import duckdb
import pytest

from vectorized_query_engine import Engine, executor
from vectorized_query_engine.executor import ExecutionError, _equi_join_size


def test_a_huge_cross_join_is_refused(engine: Engine) -> None:
    with pytest.raises(ExecutionError, match="cross join of"):
        engine.sql("SELECT count(*) AS n FROM lineitem a CROSS JOIN lineitem b")


def test_a_huge_equi_join_is_refused_before_it_runs(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor, "MAX_JOIN_ROWS", 1_000_000)
    with pytest.raises(ExecutionError, match="join would produce"):
        engine.sql(
            "SELECT count(*) AS n FROM lineitem a JOIN lineitem b ON a.l_returnflag = b.l_returnflag"
        )


def test_join_size_is_computed_exactly(engine: Engine, duck: duckdb.DuckDBPyConnection) -> None:
    lineitem = engine.catalog.get("lineitem")
    assert lineitem is not None
    t = lineitem.data
    for keys in (["l_returnflag"], ["l_returnflag", "l_linestatus"], ["l_orderkey"]):
        on = " AND ".join(f"a.{k} = b.{k}" for k in keys)
        row = duck.execute(f"SELECT count(*) FROM lineitem a JOIN lineitem b ON {on}").fetchone()
        assert row is not None
        assert _equi_join_size(t, t, keys, keys) == row[0], keys
