"""Logical plan nodes. Scan outputs columns named "alias.column"."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from vectorized_query_engine.expr import Agg, Expr, render

Plan = Union["Scan", "Filter", "Join", "Aggregate", "Project", "Sort", "Limit"]


@dataclass(frozen=True)
class Scan:
    """Read a table; columns lists what to read, filter is evaluated while scanning."""

    table: str
    alias: str
    columns: tuple[str, ...]
    filter: Expr | None = None


@dataclass(frozen=True)
class Filter:
    """Keep rows where the predicate is true."""

    child: Plan
    predicate: Expr


@dataclass(frozen=True)
class Join:
    """Inner join; condition None is a cross product (until the optimizer finds equi keys)."""

    left: Plan
    right: Plan
    condition: Expr | None = None


@dataclass(frozen=True)
class Aggregate:
    """Group by expressions and compute aggregates; outputs the group names then the aggregate names."""

    child: Plan
    groups: tuple[tuple[Expr, str], ...]
    aggs: tuple[tuple[Agg, str], ...]


@dataclass(frozen=True)
class Project:
    """Compute the output columns."""

    child: Plan
    items: tuple[tuple[Expr, str], ...]


@dataclass(frozen=True)
class Sort:
    """Order by output columns."""

    child: Plan
    keys: tuple[tuple[str, bool], ...]  # (column, descending)


@dataclass(frozen=True)
class Limit:
    """First n rows."""

    child: Plan
    n: int


def output_columns(p: Plan) -> list[str]:
    """Names a node produces, in order."""
    match p:
        case Scan(alias=a, columns=cols):
            return [f"{a}.{c}" for c in cols]
        case Filter(child=c) | Sort(child=c) | Limit(child=c):
            return output_columns(c)
        case Join(left=l, right=r):
            return output_columns(l) + output_columns(r)
        case Aggregate(groups=g, aggs=a):
            return [n for _, n in g] + [n for _, n in a]
        case Project(items=items):
            return [n for _, n in items]
    raise TypeError(f"not a plan: {p!r}")


def explain(p: Plan, estimate: object = None, indent: int = 0) -> str:
    """Indented plan tree; estimate(p) may add an estimated row count per node."""
    pad = "  " * indent
    est = f"  (~{estimate(p):,.0f} rows)" if callable(estimate) else ""
    match p:
        case Scan(table=t, alias=a, columns=cols, filter=f):
            where = f" WHERE {render(f)}" if f is not None else ""
            return f"{pad}Scan {t} AS {a} [{', '.join(cols)}]{where}{est}"
        case Filter(child=c, predicate=pr):
            line = f"{pad}Filter {render(pr)}{est}"
        case Join(left=l, right=r, condition=cond):
            kind = "HashJoin" if cond is not None else "CrossJoin"
            on = f" ON {render(cond)}" if cond is not None else ""
            return f"{pad}{kind}{on}{est}\n{explain(l, estimate, indent + 1)}\n{explain(r, estimate, indent + 1)}"
        case Aggregate(child=c, groups=g, aggs=a):
            line = f"{pad}Aggregate by [{', '.join(render(e) for e, _ in g)}] compute [{', '.join(f'{render(x)} AS {n}' for x, n in a)}]{est}"
        case Project(child=c, items=items):
            line = f"{pad}Project [{', '.join(f'{render(e)} AS {n}' for e, n in items)}]{est}"
        case Sort(child=c, keys=keys):
            order = ", ".join(f"{k} {'DESC' if d else 'ASC'}" for k, d in keys)
            line = f"{pad}Sort [{order}]{est}"
        case Limit(child=c, n=n):
            line = f"{pad}Limit {n}{est}"
        case _:
            raise TypeError(f"not a plan: {p!r}")
    return f"{line}\n{explain(c, estimate, indent + 1)}"
