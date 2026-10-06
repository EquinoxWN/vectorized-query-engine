"""vqe "SELECT ..." [--explain] [--no-optimize] [--scale 0.05] [--duckdb]: query generated TPC-H-shaped data."""

from __future__ import annotations

import argparse
import sys
import time

from vectorized_query_engine.binder import BindError
from vectorized_query_engine.data import catalog, generate
from vectorized_query_engine.engine import Engine
from vectorized_query_engine.executor import ExecutionError
from vectorized_query_engine.report import print_table


def _cell(v: object) -> str:
    """Display text for one value (floats to two decimals, NULL spelled out)."""
    if v is None:
        return "NULL"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


def main(argv: list[str] | None = None) -> int:
    """Run one query; 0 on success, 2 on an invalid query."""
    p = argparse.ArgumentParser(prog="vqe", description=__doc__)
    p.add_argument("sql")
    p.add_argument("--explain", action="store_true", help="print the plan instead of running it")
    p.add_argument("--no-optimize", action="store_true")
    p.add_argument("--scale", type=float, default=0.05)
    p.add_argument(
        "--duckdb", action="store_true", help="also run the query on DuckDB and compare times"
    )
    args = p.parse_args(argv)
    engine = Engine(catalog(args.scale), optimize=not args.no_optimize)
    try:
        if args.explain:
            print(engine.explain(args.sql))
            return 0
        start = time.perf_counter()
        result = engine.sql(args.sql)
        took = time.perf_counter() - start
    except (BindError, ExecutionError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    shown = result.slice(0, 20)
    cells = [[_cell(v) for v in row.values()] for row in shown.to_pylist()]
    print_table(shown.column_names, cells)
    if result.num_rows > shown.num_rows:
        print(f"... {result.num_rows - shown.num_rows:,} more rows")
    print(
        f"{result.num_rows} rows in {took * 1000:.1f} ms; scanned {engine.last_stats.cells_scanned:,} cells"
    )
    if args.duckdb:
        import duckdb

        con = duckdb.connect()
        for name, table in generate(args.scale).items():
            con.register(name, table)
        start = time.perf_counter()
        con.execute(args.sql).to_arrow_table()
        print(f"DuckDB: {(time.perf_counter() - start) * 1000:.1f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
