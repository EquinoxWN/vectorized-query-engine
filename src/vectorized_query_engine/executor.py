"""Execute a plan on Arrow tables with vectorized Arrow compute kernels."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from vectorized_query_engine.catalog import Catalog
from vectorized_query_engine.expr import (
    Agg,
    Bin,
    Col,
    Expr,
    InList,
    IsNull,
    Like,
    Lit,
    Not,
    columns,
    conjoin,
    conjuncts,
)
from vectorized_query_engine.plan import (
    Aggregate,
    Filter,
    Join,
    Limit,
    Plan,
    Project,
    Scan,
    Sort,
    output_columns,
)

MAX_CROSS_ROWS = 20_000_000
MAX_JOIN_ROWS = 50_000_000
_ARROW_TYPES = {
    "int64": pa.int64(),
    "double": pa.float64(),
    "string": pa.string(),
    "bool": pa.bool_(),
    "date32": pa.date32(),
}
_CMP = {
    "=": pc.equal,
    "!=": pc.not_equal,
    "<": pc.less,
    "<=": pc.less_equal,
    ">": pc.greater,
    ">=": pc.greater_equal,
}
_AGG = {
    "sum": "sum",
    "avg": "mean",
    "min": "min",
    "max": "max",
    "count": "count",
    "count_distinct": "count_distinct",
}


class ExecutionError(RuntimeError):
    """The plan cannot be executed safely (for example an enormous cross join)."""


@dataclass
class ExecStats:
    """What execution read: cells (rows x columns) scanned per table, and rows produced."""

    cells_scanned: int = 0
    rows_scanned: int = 0
    scans: list[tuple[str, int, int]] = field(
        default_factory=list
    )  # (alias, rows read, columns read)


class Executor:
    """Runs a logical plan directly; each operator works on whole Arrow tables (vectors), never rows."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.stats = ExecStats()

    def run(self, plan: Plan) -> pa.Table:
        """Execute and return the result table."""
        match plan:
            case Scan():
                return self._scan(plan)
            case Filter(child=c, predicate=p):
                t = self.run(c)
                return t.filter(_mask(p, t))
            case Join():
                return self._join(plan)
            case Aggregate():
                return self._aggregate(plan)
            case Project(child=c, items=items):
                t = self.run(c)
                return pa.table(
                    {name: _as_array(evaluate(e, t), t.num_rows, e.type) for e, name in items}
                )
            case Sort(child=c, keys=keys):
                t = self.run(c)
                return (
                    t.sort_by([(k, "descending" if d else "ascending") for k, d in keys])
                    if t.num_rows
                    else t
                )
            case Limit(child=c, n=n):
                return self.run(c).slice(0, n)
        raise TypeError(f"not a plan: {plan!r}")

    def _scan(self, s: Scan) -> pa.Table:
        info = self.catalog.get(s.table)
        if info is None:
            raise ExecutionError(f"unknown table {s.table}")
        need = list(
            dict.fromkeys(
                [
                    *s.columns,
                    *(
                        c.split(".", 1)[1]
                        for c in (columns(s.filter) if s.filter is not None else set())
                    ),
                ]
            )
        )
        t = info.data.select(need)
        self.stats.cells_scanned += t.num_rows * len(need)
        self.stats.rows_scanned += t.num_rows
        self.stats.scans.append((s.alias, t.num_rows, len(need)))
        t = t.rename_columns([f"{s.alias}.{c}" for c in need])
        if s.filter is not None:
            t = t.filter(_mask(s.filter, t))
        return t.select([f"{s.alias}.{c}" for c in s.columns])

    def _join(self, j: Join) -> pa.Table:
        left, right = self.run(j.left), self.run(j.right)
        lcols, rcols = set(left.column_names), set(right.column_names)
        keys: list[tuple[str, str]] = []
        residual: list[Expr] = []
        for part in conjuncts(j.condition) if j.condition is not None else []:
            if (
                isinstance(part, Bin)
                and part.op == "="
                and isinstance(part.left, Col)
                and isinstance(part.right, Col)
            ):
                a, b = part.left.name, part.right.name
                if a in lcols and b in rcols:
                    keys.append((a, b))
                    continue
                if b in lcols and a in rcols:
                    keys.append((b, a))
                    continue
            residual.append(part)
        if keys:
            # Only joins that could exceed the limit pay for the exact size computation.
            could_explode = left.num_rows * right.num_rows > MAX_JOIN_ROWS
            size = (
                _equi_join_size(left, right, [k for k, _ in keys], [k for _, k in keys])
                if could_explode
                else 0
            )
            if size > MAX_JOIN_ROWS:
                raise ExecutionError(
                    f"join would produce {size:,} rows (limit {MAX_JOIN_ROWS:,}); add a more selective condition"
                )
            out = left.join(
                right,
                keys=[k for k, _ in keys],
                right_keys=[k for _, k in keys],
                join_type="inner",
                coalesce_keys=False,
            )
            out = out.select(output_columns(j))
        else:
            if left.num_rows * right.num_rows > MAX_CROSS_ROWS:
                raise ExecutionError(
                    f"cross join of {left.num_rows:,} x {right.num_rows:,} rows refused"
                )
            li = np.repeat(np.arange(left.num_rows), right.num_rows)
            ri = np.tile(np.arange(right.num_rows), left.num_rows)
            out = pa.Table.from_arrays(
                [*left.take(li).columns, *right.take(ri).columns],
                names=[*left.column_names, *right.column_names],
            )
        rest = conjoin(residual)
        return out.filter(_mask(rest, out)) if rest is not None else out

    def _aggregate(self, a: Aggregate) -> pa.Table:
        t = self.run(a.child)
        inputs: dict[str, Any] = {}
        for i, (e, _) in enumerate(a.groups):
            inputs[f"g{i}"] = _as_array(evaluate(e, t), t.num_rows, e.type)
        for i, (agg, _) in enumerate(a.aggs):
            if agg.arg is not None:
                inputs[f"a{i}"] = _as_array(evaluate(agg.arg, t), t.num_rows, agg.arg.type)
        work = pa.table(inputs) if inputs else pa.table({"_rows": pa.nulls(t.num_rows, pa.int8())})
        names = [n for _, n in a.groups] + [n for _, n in a.aggs]
        if not a.groups:
            return pa.table(
                {name: [_global(agg, work, i)] for i, (agg, name) in enumerate(a.aggs)}
            ).select(names)
        specs: list[Any] = []
        for i, (agg, _) in enumerate(a.aggs):
            specs.append(
                ([], "count_all") if agg.func == "count_all" else (f"a{i}", _AGG[agg.func])
            )
        grouped = work.group_by(
            [f"g{i}" for i in range(len(a.groups))], use_threads=False
        ).aggregate(specs)
        cols = {f"g{i}": name for i, (_, name) in enumerate(a.groups)}
        for i, (agg, name) in enumerate(a.aggs):
            cols["count_all" if agg.func == "count_all" else f"a{i}_{_AGG[agg.func]}"] = name
        return grouped.rename_columns([cols[c] for c in grouped.column_names]).select(names)


def _equi_join_size(left: pa.Table, right: pa.Table, lkeys: list[str], rkeys: list[str]) -> int:
    """Exact output rows of an inner equi join, from per-key counts on each side (no join materialized)."""
    lnames = [f"k{i}" for i in range(len(lkeys))]
    # Rows of the larger side whose first key never occurs on the smaller side cannot match:
    # drop them before counting, so a selective join costs little more than one hash lookup.
    if left.num_rows < right.num_rows:
        right = right.filter(
            pc.is_in(right.column(rkeys[0]), value_set=pc.unique(left.column(lkeys[0])))
        )
    else:
        left = left.filter(
            pc.is_in(left.column(lkeys[0]), value_set=pc.unique(right.column(rkeys[0])))
        )
    lc = (
        left.select(lkeys)
        .rename_columns(lnames)
        .group_by(lnames, use_threads=False)
        .aggregate([([], "count_all")])
    )
    rc = (
        right.select(rkeys)
        .rename_columns(lnames)
        .group_by(lnames, use_threads=False)
        .aggregate([([], "count_all")])
    )
    both = lc.rename_columns([*lnames, "lc"]).join(
        rc.rename_columns([*lnames, "rc"]), keys=lnames, join_type="inner"
    )
    total = pc.sum(pc.multiply(both.column("lc"), both.column("rc"))).as_py()
    return int(total or 0)


def _global(agg: Agg, work: pa.Table, i: int) -> Any:
    if agg.func == "count_all":
        return work.num_rows
    col = work.column(f"a{i}")
    match agg.func:
        case "count":
            return pc.count(col).as_py()
        case "count_distinct":
            return pc.count_distinct(col).as_py()
        case "sum":
            return pc.sum(col).as_py()
        case "avg":
            return pc.mean(col).as_py()
        case "min":
            return pc.min(col).as_py()
        case "max":
            return pc.max(col).as_py()
    raise ExecutionError(f"unknown aggregate {agg.func}")


def _as_array(v: Any, n: int, type_name: str) -> Any:
    if isinstance(v, pa.Scalar):
        target = _ARROW_TYPES.get(type_name, v.type)
        return pa.array(
            [v.as_py()] * n, type=target if v.is_valid else _ARROW_TYPES.get(type_name, pa.null())
        )
    return v


def _mask(p: Expr, t: pa.Table) -> Any:
    m = evaluate(p, t)
    if isinstance(m, pa.Scalar):
        return pa.array([bool(m.as_py())] * t.num_rows, type=pa.bool_())
    return m


def evaluate(e: Expr, t: pa.Table) -> Any:
    """Evaluate an expression over a whole table at once (an Arrow array or scalar)."""
    match e:
        case Col(name=n):
            return t.column(n)
        case Lit(value=v, type=ty):
            return (
                pa.scalar(v, type=_ARROW_TYPES.get(ty, pa.null()))
                if v is not None
                else pa.scalar(None, pa.null())
            )
        case Bin(op="AND", left=l, right=r):
            return pc.and_kleene(evaluate(l, t), evaluate(r, t))
        case Bin(op="OR", left=l, right=r):
            return pc.or_kleene(evaluate(l, t), evaluate(r, t))
        case Bin(op="/", left=l, right=r):
            # Always floating point, IEEE 754 on zero (x / 0 is +-inf, 0 / 0 is nan), as in DuckDB.
            num = pc.cast(evaluate(l, t), pa.float64())
            den = pc.cast(evaluate(r, t), pa.float64())
            return pc.divide(num, den)
        case Bin(op="+", left=l, right=r):
            return pc.add(evaluate(l, t), evaluate(r, t))
        case Bin(op="-", left=l, right=r):
            return pc.subtract(evaluate(l, t), evaluate(r, t))
        case Bin(op="*", left=l, right=r):
            return pc.multiply(evaluate(l, t), evaluate(r, t))
        case Bin(op=op, left=l, right=r) if op in _CMP:
            return _CMP[op](evaluate(l, t), evaluate(r, t))
        case Not(arg=a):
            return pc.invert(evaluate(a, t))
        case IsNull(arg=a, negated=neg):
            v = evaluate(a, t)
            return pc.is_valid(v) if neg else pc.is_null(v)
        case InList(arg=a, values=vs, negated=neg):
            v = evaluate(a, t)
            present: list[Any] = [x.value for x in vs if x.value is not None]
            try:
                value_set = pa.array(present, type=v.type)
            except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError):
                # An integer column against a fractional constant: compare as doubles.
                v, value_set = pc.cast(v, pa.float64()), pa.array(present, pa.float64())
            # SQL three-valued logic: a NULL input is never IN or NOT IN anything, and a NULL in the
            # list makes NOT IN unknown for every row that is not an explicit match.
            hit = pc.is_in(v, value_set=value_set, skip_nulls=True)
            if not neg:
                return hit
            if len(present) < len(vs):
                return pc.and_kleene(pc.invert(hit), pa.scalar(False))
            return pc.and_kleene(pc.invert(hit), pc.is_valid(v))
        case Like(arg=a, pattern=p, negated=neg):
            m = pc.match_like(evaluate(a, t), p)
            return pc.invert(m) if neg else m
    raise ExecutionError(f"cannot evaluate {e!r}")
