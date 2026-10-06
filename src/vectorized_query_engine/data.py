"""Deterministic TPC-H-shaped tables (region, nation, customer, orders, lineitem) for tests and demos."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pyarrow as pa

from vectorized_query_engine.catalog import Catalog

REGIONS = ["AFRICA", "AMERICA", "ASIA", "EUROPE", "MIDDLE EAST"]
SEGMENTS = ["AUTOMOBILE", "BUILDING", "FURNITURE", "HOUSEHOLD", "MACHINERY"]
PRIORITIES = ["1-URGENT", "2-HIGH", "3-MEDIUM", "4-NOT SPECIFIED", "5-LOW"]
START = dt.date(1992, 1, 1)


def generate(scale: float = 0.05, seed: int = 42) -> dict[str, pa.Table]:
    """Tables at a TPC-H-like scale: 15,000 customers, 150,000 orders and about 600,000 lines at scale 1."""
    rng = np.random.default_rng(seed)
    n_cust = max(10, int(15_000 * scale))
    n_orders = max(20, int(150_000 * scale))
    region = pa.table({"r_regionkey": pa.array(range(5), pa.int64()), "r_name": REGIONS})
    nation = pa.table(
        {
            "n_nationkey": pa.array(range(25), pa.int64()),
            "n_name": [f"NATION{i:02d}" for i in range(25)],
            "n_regionkey": pa.array([i % 5 for i in range(25)], pa.int64()),
        }
    )
    customer = pa.table(
        {
            "c_custkey": pa.array(np.arange(1, n_cust + 1), pa.int64()),
            "c_name": [f"Customer#{i:09d}" for i in range(1, n_cust + 1)],
            "c_nationkey": pa.array(rng.integers(0, 25, n_cust), pa.int64()),
            "c_mktsegment": np.array(SEGMENTS)[rng.integers(0, 5, n_cust)].tolist(),
            "c_acctbal": np.round(rng.uniform(-999.99, 9999.99, n_cust), 2),
        }
    )
    # As in TPC-H, a third of customers never order.
    buyers = np.arange(1, n_cust + 1)[np.arange(n_cust) % 3 != 2]
    order_days = rng.integers(0, 2405, n_orders)
    orders = pa.table(
        {
            "o_orderkey": pa.array(np.arange(1, n_orders + 1), pa.int64()),
            "o_custkey": pa.array(rng.choice(buyers, n_orders), pa.int64()),
            "o_orderstatus": np.array(["F", "O", "P"])[rng.integers(0, 3, n_orders)].tolist(),
            "o_totalprice": np.round(rng.uniform(850, 550_000, n_orders), 2),
            "o_orderdate": pa.array(
                [START + dt.timedelta(days=int(d)) for d in order_days], pa.date32()
            ),
            "o_orderpriority": np.array(PRIORITIES)[rng.integers(0, 5, n_orders)].tolist(),
        }
    )
    lines_per_order = rng.integers(1, 8, n_orders)
    n_lines = int(lines_per_order.sum())
    l_order = np.repeat(np.arange(1, n_orders + 1), lines_per_order)
    l_number = np.concatenate([np.arange(1, k + 1) for k in lines_per_order])
    quantity = rng.integers(1, 51, n_lines).astype(float)
    ship = np.repeat(order_days, lines_per_order) + rng.integers(1, 122, n_lines)
    lineitem = pa.table(
        {
            "l_orderkey": pa.array(l_order, pa.int64()),
            "l_linenumber": pa.array(l_number, pa.int64()),
            "l_quantity": quantity,
            "l_extendedprice": np.round(quantity * rng.uniform(900, 2000, n_lines), 2),
            "l_discount": np.round(rng.integers(0, 11, n_lines) / 100, 2),
            "l_tax": np.round(rng.integers(0, 9, n_lines) / 100, 2),
            "l_returnflag": np.array(["R", "A", "N"])[rng.integers(0, 3, n_lines)].tolist(),
            "l_linestatus": np.array(["O", "F"])[rng.integers(0, 2, n_lines)].tolist(),
            "l_shipdate": pa.array([START + dt.timedelta(days=int(d)) for d in ship], pa.date32()),
        }
    )
    return {
        "region": region,
        "nation": nation,
        "customer": customer,
        "orders": orders,
        "lineitem": lineitem,
    }


def catalog(scale: float = 0.05, seed: int = 42) -> Catalog:
    """A catalog with every generated table registered."""
    c = Catalog()
    for name, table in generate(scale, seed).items():
        c.register(name, table)
    return c
