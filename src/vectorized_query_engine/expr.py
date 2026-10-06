"""Bound expressions: every column reference is resolved and every node is typed."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Union

Expr = Union["Col", "Lit", "Bin", "Not", "IsNull", "InList", "Like"]

COMPARISONS = {"=", "!=", "<", "<=", ">", ">="}
ARITHMETIC = {"+", "-", "*", "/"}
LOGICAL = {"AND", "OR"}


@dataclass(frozen=True)
class Col:
    """A column by its qualified name, such as "o.o_totalprice" or an output name."""

    name: str
    type: str


@dataclass(frozen=True)
class Lit:
    """A constant."""

    value: int | float | str | bool | dt.date | None
    type: str


@dataclass(frozen=True)
class Bin:
    """Arithmetic, comparison or AND / OR."""

    op: str
    left: Expr
    right: Expr
    type: str


@dataclass(frozen=True)
class Not:
    """Logical negation."""

    arg: Expr
    type: str = "bool"


@dataclass(frozen=True)
class IsNull:
    """IS NULL or IS NOT NULL."""

    arg: Expr
    negated: bool
    type: str = "bool"


@dataclass(frozen=True)
class InList:
    """x IN (constants) or NOT IN."""

    arg: Expr
    values: tuple[Lit, ...]
    negated: bool
    type: str = "bool"


@dataclass(frozen=True)
class Like:
    """x LIKE 'pattern' or NOT LIKE."""

    arg: Expr
    pattern: str
    negated: bool
    type: str = "bool"


@dataclass(frozen=True)
class Agg:
    """An aggregate call; only appears in an Aggregate node."""

    func: str  # count_all, count, count_distinct, sum, avg, min, max
    arg: Expr | None
    type: str


def columns(e: Expr) -> set[str]:
    """Every column name an expression reads."""
    match e:
        case Col(name=n):
            return {n}
        case Lit():
            return set()
        case Bin(left=l, right=r):
            return columns(l) | columns(r)
        case Not(arg=a) | IsNull(arg=a) | InList(arg=a) | Like(arg=a):
            return columns(a)
    raise TypeError(f"not an expression: {e!r}")


def conjuncts(e: Expr) -> list[Expr]:
    """Split a predicate on AND."""
    if isinstance(e, Bin) and e.op == "AND":
        return conjuncts(e.left) + conjuncts(e.right)
    return [e]


def conjoin(parts: list[Expr]) -> Expr | None:
    """AND the parts together (None for no parts)."""
    out: Expr | None = None
    for p in parts:
        out = p if out is None else Bin("AND", out, p, "bool")
    return out


def render(e: Expr | Agg) -> str:
    """Readable SQL-like text for EXPLAIN."""
    match e:
        case Col(name=n):
            return n
        case Lit(value=v, type="string"):
            return f"'{v}'"
        case Lit(value=v, type="date32"):
            return f"DATE '{v}'"
        case Lit(value=v):
            return "NULL" if v is None else str(v)
        case Bin(op=op, left=l, right=r):
            return f"({render(l)} {op} {render(r)})"
        case Not(arg=a):
            return f"NOT {render(a)}"
        case IsNull(arg=a, negated=n):
            return f"{render(a)} IS {'NOT ' if n else ''}NULL"
        case InList(arg=a, values=vs, negated=n):
            return f"{render(a)} {'NOT ' if n else ''}IN ({', '.join(render(v) for v in vs)})"
        case Like(arg=a, pattern=p, negated=n):
            return f"{render(a)} {'NOT ' if n else ''}LIKE '{p}'"
        case Agg(func=f, arg=a):
            return f"{f}({'*' if a is None else render(a)})"
    raise TypeError(f"not an expression: {e!r}")
