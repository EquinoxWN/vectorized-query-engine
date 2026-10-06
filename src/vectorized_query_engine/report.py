"""vqe-report [--scale 0.05] [--repeat 5]: the TPC-H-style queries with the optimizer off and on, and on DuckDB.

Prints a Markdown table: rows, whether the result equals DuckDB's, cells read by each plan, and the
best-of-N wall time of the optimized plan and of DuckDB on the same Arrow tables.
"""

from __future__ import annotations

import argparse
import functools
import gc
import math
import sys
import time
from collections.abc import Callable
from typing import Any

from vectorized_query_engine.catalog import Catalog
from vectorized_query_engine.compare import difference
from vectorized_query_engine.data import generate
from vectorized_query_engine.engine import Engine
from vectorized_query_engine.executor import ExecutionError, Executor
from vectorized_query_engine.queries import QUERIES


def best(fn: Callable[[], Any], repeat: int) -> tuple[float, Any]:
    """Fastest of `repeat` runs in milliseconds, and the last result."""
    times, out = [], None
    for _ in range(repeat):
        start = time.perf_counter()
        out = fn()
        times.append((time.perf_counter() - start) * 1000)
    return min(times), out


def print_table(header: list[str], rows: list[list[str]]) -> None:
    """A Markdown table padded to column widths, so it also reads well in a terminal."""
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    numeric = [bool(rows) and all(r[i][:1].isdigit() for r in rows) for i in range(len(header))]

    def line(cells: list[str]) -> str:
        padded = [
            c.rjust(w) if num else c.ljust(w)
            for c, w, num in zip(cells, widths, numeric, strict=True)
        ]
        return "| " + " | ".join(padded) + " |"

    print(line(header))
    print(
        "|"
        + "|".join(
            ("-" * (w + 1) + ":") if num else "-" * (w + 2)
            for w, num in zip(widths, numeric, strict=True)
        )
        + "|"
    )
    for r in rows:
        print(line(r))


def _duckdb_run(con: Any, sql: str) -> Any:
    """One DuckDB query, materialized as Arrow like ours."""
    return con.execute(sql).to_arrow_table()


def main(argv: list[str] | None = None) -> int:
    """Print the comparison table; exit 1 if any result differs from DuckDB."""
    p = argparse.ArgumentParser(prog="vqe-report", description=__doc__)
    p.add_argument("--scale", type=float, default=0.05)
    p.add_argument("--repeat", type=int, default=5)
    args = p.parse_args(argv)
    import duckdb

    tables = generate(args.scale)
    catalog = Catalog()
    con = duckdb.connect()
    for name, table in tables.items():
        catalog.register(name, table)
        con.register(name, table)
    engine = Engine(catalog)
    print(
        f"Scale {args.scale}: {tables['orders'].num_rows:,} orders, {tables['lineitem'].num_rows:,} line items. "
        f"Times are the best of {args.repeat} runs."
    )
    print()
    header = [
        "Query",
        "Rows",
        "Same as DuckDB",
        "Cells, no optimizer",
        "Cells, optimized",
        "Ours (ms)",
        "DuckDB (ms)",
    ]
    lines: list[list[str]] = []
    mismatches, refused = 0, 0
    ratios: list[float] = []
    naive_total = opt_total = 0
    for name, ordered, sql in QUERIES:
        # Time first, with a clean heap: the literal plans below allocate a lot and would slow
        # whatever ran next.
        gc.collect()
        ours_ms, ours = best(functools.partial(engine.sql, sql), args.repeat)
        opt_cells = engine.last_stats.cells_scanned
        gc.collect()
        duck_ms, theirs = best(functools.partial(_duckdb_run, con, sql), args.repeat)
        naive = Executor(catalog)
        try:
            naive.run(engine.plan(sql, optimize=False))
            naive_cells: str | None = f"{naive.stats.cells_scanned:,}"
        except ExecutionError:
            naive_cells = None
        if naive_cells is None:
            refused += 1
        else:
            naive_total += naive.stats.cells_scanned
            opt_total += opt_cells
        diff = difference(ours, theirs, ordered)
        mismatches += diff is not None
        ratios.append(ours_ms / duck_ms)
        lines.append(
            [
                name,
                f"{ours.num_rows:,}",
                "yes" if diff is None else f"NO: {diff}",
                naive_cells or "refused (too large)",
                f"{opt_cells:,}",
                f"{ours_ms:.1f}",
                f"{duck_ms:.1f}",
            ]
        )
    print_table(header, lines)
    geo = math.exp(sum(math.log(r) for r in ratios) / len(ratios))
    print()
    print(f"{len(QUERIES) - mismatches} of {len(QUERIES)} results equal DuckDB's.")
    print(
        f"Optimized plans read {opt_total / naive_total:.1%} of the cells the literal plans read "
        f"(over the {len(QUERIES) - refused} queries whose literal plan can run; {refused} cannot)."
    )
    print(f"Optimized time / DuckDB time, geometric mean over all queries: {geo:.2f}.")
    con.close()
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
