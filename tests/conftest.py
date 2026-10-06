"""Shared fixtures: one small TPC-H-shaped dataset, loaded into the engine and into DuckDB."""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pyarrow as pa
import pytest

from vectorized_query_engine import Catalog, Engine
from vectorized_query_engine.data import generate

SCALE = 0.01


@pytest.fixture(scope="session")
def tables() -> dict[str, pa.Table]:
    """150 customers, 1,500 orders and about 6,000 line items."""
    return generate(scale=SCALE, seed=7)


@pytest.fixture(scope="session")
def catalog(tables: dict[str, pa.Table]) -> Catalog:
    """The tables registered with the engine."""
    c = Catalog()
    for name, table in tables.items():
        c.register(name, table)
    return c


@pytest.fixture(scope="session")
def engine(catalog: Catalog) -> Engine:
    """An engine with the optimizer on."""
    return Engine(catalog)


@pytest.fixture(scope="session")
def duck(tables: dict[str, pa.Table]) -> Iterator[duckdb.DuckDBPyConnection]:
    """DuckDB reading the very same Arrow tables."""
    con = duckdb.connect()
    for name, table in tables.items():
        con.register(name, table)
    yield con
    con.close()
