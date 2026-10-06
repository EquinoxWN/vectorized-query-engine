"""The generated data is deterministic and shaped like TPC-H."""

from __future__ import annotations

import pyarrow as pa
import pyarrow.compute as pc

from vectorized_query_engine.data import generate


def test_same_seed_same_tables_other_seed_other_tables() -> None:
    a, b, c = generate(0.01, seed=1), generate(0.01, seed=1), generate(0.01, seed=2)
    assert all(a[n].equals(b[n]) for n in a)
    assert not a["orders"].equals(c["orders"])


def test_sizes_and_keys_follow_tpch() -> None:
    t = generate(0.01, seed=1)
    assert t["customer"].num_rows == 150
    assert t["orders"].num_rows == 1500
    assert 1500 <= t["lineitem"].num_rows <= 1500 * 7
    assert t["region"].num_rows == 5
    assert t["nation"].num_rows == 25
    buyers = set(t["orders"].column("o_custkey").to_pylist())
    assert len(buyers) <= 100, "a third of customers never order"
    assert pc.all(pc.less(t["lineitem"].column("l_orderkey"), pa.scalar(1501))).as_py()
