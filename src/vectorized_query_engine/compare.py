"""Compare two result tables by value, the way a SQL test suite does (used by the tests and vqe-report)."""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal
from typing import Any

import pyarrow as pa


def rows(t: pa.Table) -> list[tuple[Any, ...]]:
    """Rows with every number as a float, so int64, double and DuckDB's HUGEINT compare by value."""
    cols = [t.column(i).to_pylist() for i in range(t.num_columns)]
    return [tuple(_norm(v) for v in r) for r in zip(*cols, strict=True)]


def _norm(v: Any) -> Any:
    if v is None or isinstance(v, bool | str | dt.date):
        return v
    if isinstance(v, int | float | Decimal):
        return float(v)
    return v


def _sort_key(r: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(
        (v is None, round(v, 6) if isinstance(v, float) else ("" if v is None else v)) for v in r
    )


def _close(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return (math.isnan(a) and math.isnan(b)) or math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6)
    return bool(a == b)


def difference(ours: pa.Table, theirs: pa.Table, ordered: bool) -> str | None:
    """None when both tables hold the same rows (in the same order if ordered), else what differs."""
    a, b = rows(ours), rows(theirs)
    if ours.num_columns != theirs.num_columns:
        return f"{ours.num_columns} columns vs {theirs.num_columns}"
    if len(a) != len(b):
        return f"{len(a)} rows vs {len(b)}"
    if not ordered:
        a, b = sorted(a, key=_sort_key), sorted(b, key=_sort_key)
    for i, (x, y) in enumerate(zip(a, b, strict=True)):
        if not all(_close(p, q) for p, q in zip(x, y, strict=True)):
            return f"row {i}: {x} vs {y}"
    return None
