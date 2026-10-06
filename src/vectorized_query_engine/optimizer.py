"""
Rule-based optimizer: push predicates down to the scans and into joins, reorder inner joins
greedily by estimated size, and read only the columns the query needs.
"""

from __future__ import annotations

from vectorized_query_engine.catalog import Catalog
from vectorized_query_engine.expr import (
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


class Optimizer:
    """Rewrites a bound plan into an equivalent, cheaper one."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    def optimize(self, plan: Plan) -> Plan:
        """Apply every rule; the result produces the same rows."""
        self._aliases = _aliases(plan)
        plan = self._push(plan)
        plan = _transitive(plan)
        plan = self._reorder(plan)
        return self._prune(plan, set(output_columns(plan)))

    # ------------------------------------------------------------ estimates
    def estimate(self, plan: Plan) -> float:
        """Estimated output rows of a node, from table sizes and distinct counts."""
        # Resolve aliases from this plan every time: a cached map from another query would
        # silently fall back to default distinct counts.
        self._aliases = _aliases(plan)
        return self._est(plan)

    def _est(self, plan: Plan) -> float:
        match plan:
            case Scan(table=t, filter=f):
                info = self.catalog.get(t)
                rows = float(info.rows) if info else 1.0
                return rows * (self.selectivity(f) if f is not None else 1.0)
            case Filter(child=c, predicate=p):
                return self._est(c) * self.selectivity(p)
            case Join(left=l, right=r, condition=cond):
                rows = self._est(l) * self._est(r)
                if cond is None:
                    return rows
                for part in conjuncts(cond):
                    if isinstance(part, Bin) and _is_equi(part):
                        rows /= max(self._ndv(part.left), self._ndv(part.right))
                    else:
                        rows *= self.selectivity(part)
                return max(rows, 1.0)
            case Aggregate(child=c, groups=g):
                if not g:
                    return 1.0
                product = 1.0
                for e, _ in g:
                    product *= self._ndv(e)
                return min(self._est(c), product)
            case Project(child=c) | Sort(child=c):
                return self._est(c)
            case Limit(child=c, n=n):
                return min(float(n), self._est(c))
        raise TypeError(f"not a plan: {plan!r}")

    def _ndv(self, e: Expr) -> float:
        if isinstance(e, Col) and "." in e.name:
            alias, col = e.name.split(".", 1)
            table = self._aliases.get(alias)
            info = self.catalog.get(table) if table else None
            if info and col in info.ndv:
                return float(info.ndv[col])
        return 10.0

    def selectivity(self, p: Expr) -> float:
        """Fraction of rows a predicate keeps (textbook constants where statistics run out)."""
        match p:
            case Bin(op="AND", left=l, right=r):
                return self.selectivity(l) * self.selectivity(r)
            case Bin(op="OR", left=l, right=r):
                a, b = self.selectivity(l), self.selectivity(r)
                return a + b - a * b
            case (
                Bin(op="=", left=Col() as c, right=Lit())
                | Bin(op="=", left=Lit(), right=Col() as c)
            ):
                return 1.0 / self._ndv(c)
            case (
                Bin(op="!=", left=Col() as c, right=Lit())
                | Bin(op="!=", left=Lit(), right=Col() as c)
            ):
                return 1.0 - 1.0 / self._ndv(c)
            case Bin(op="=", left=Col() as a, right=Col() as b):
                return 1.0 / max(self._ndv(a), self._ndv(b))
            case Bin(op="<" | "<=" | ">" | ">="):
                return 1.0 / 3.0
            case InList(arg=a, values=vs, negated=neg):
                s = min(1.0, len(vs) / self._ndv(a))
                return 1.0 - s if neg else s
            case Like(negated=neg):
                return 0.9 if neg else 0.1
            case IsNull(negated=neg):
                return 0.95 if neg else 0.05
            case Not(arg=a):
                return 1.0 - self.selectivity(a)
            case Lit(value=v):
                return 1.0 if v else 0.0
        return 0.5

    # ------------------------------------------------------------ predicate pushdown
    def _push(self, p: Plan) -> Plan:
        match p:
            case Filter(child=c, predicate=pred):
                return _place_all(self._push(c), conjuncts(pred))
            case Join(left=l, right=r, condition=cond):
                return _place_all(
                    Join(self._push(l), self._push(r), None), conjuncts(cond) if cond else []
                )
            case Scan():
                return p
            case Aggregate(child=c, groups=g, aggs=a):
                return Aggregate(self._push(c), g, a)
            case Project(child=c, items=i):
                return Project(self._push(c), i)
            case Sort(child=c, keys=k):
                return Sort(self._push(c), k)
            case Limit(child=c, n=n):
                return Limit(self._push(c), n)
        raise TypeError(f"not a plan: {p!r}")

    # ------------------------------------------------------------ join reordering
    def _reorder(self, p: Plan) -> Plan:
        match p:
            case Join():
                relations: list[Plan] = []
                conditions: list[Expr] = []
                _flatten(p, relations, conditions)
                relations = [self._reorder(r) for r in relations]
                return self._greedy(relations, conditions)
            case Filter(child=c, predicate=pred):
                return Filter(self._reorder(c), pred)
            case Aggregate(child=c, groups=g, aggs=a):
                return Aggregate(self._reorder(c), g, a)
            case Project(child=c, items=i):
                return Project(self._reorder(c), i)
            case Sort(child=c, keys=k):
                return Sort(self._reorder(c), k)
            case Limit(child=c, n=n):
                return Limit(self._reorder(c), n)
        return p

    def _greedy(self, relations: list[Plan], conditions: list[Expr]) -> Plan:
        """Start from the smallest relation; repeatedly add the connected relation giving the smallest result."""
        remaining = list(relations)
        current = min(remaining, key=self.estimate)
        remaining.remove(current)
        pending = list(conditions)
        while remaining:
            have = set(output_columns(current))
            options: list[tuple[int, float, int, list[Expr]]] = []
            for i, r in enumerate(remaining):
                right_cols = set(output_columns(r))
                usable = [c for c in pending if columns(c) <= have | right_cols]
                connected = any(columns(c) & right_cols and columns(c) & have for c in usable)
                # Prefer relations connected by a condition (no cross product), then the smallest result.
                options.append(
                    (
                        0 if connected else 1,
                        self.estimate(Join(current, r, conjoin(usable))),
                        i,
                        usable,
                    )
                )
            _, _, index, usable = min(options, key=lambda o: o[:3])
            r = remaining[index]
            current = Join(current, r, conjoin(usable))
            pending = [c for c in pending if c not in usable]
            remaining.remove(r)
        return _place_all(current, pending)

    # ------------------------------------------------------------ column pruning
    def _prune(self, p: Plan, required: set[str]) -> Plan:
        match p:
            case Scan(alias=a, columns=cols, filter=f):
                keep = tuple(c for c in cols if f"{a}.{c}" in required)
                return Scan(p.table, a, keep or cols[:1], f)
            case Filter(child=c, predicate=pred):
                return Filter(self._prune(c, required | columns(pred)), pred)
            case Join(left=l, right=r, condition=cond):
                need = required | (columns(cond) if cond is not None else set())
                return Join(self._prune(l, need), self._prune(r, need), cond)
            case Aggregate(child=c, groups=g, aggs=a):
                inputs: set[str] = set()
                for e, _ in g:
                    inputs |= columns(e)
                for agg, _ in a:
                    if agg.arg is not None:
                        inputs |= columns(agg.arg)
                return Aggregate(self._prune(c, inputs), g, a)
            case Project(child=c, items=items):
                need = set()
                for e, _ in items:
                    need |= columns(e)
                return Project(self._prune(c, need), items)
            case Sort(child=c, keys=keys):
                return Sort(self._prune(c, required | {k for k, _ in keys}), keys)
            case Limit(child=c, n=n):
                return Limit(self._prune(c, required), n)
        raise TypeError(f"not a plan: {p!r}")


# ------------------------------------------------------------ transitive predicates
_COMPARE = {"=", "!=", "<", "<=", ">", ">="}


def _transitive(plan: Plan) -> Plan:
    """If a.k = b.k joins two scans and a's scan keeps a.k < 100, b's scan can keep b.k < 100 too.

    Only for inner equi joins (the only joins M1 has) and predicates comparing one column with
    constants, so the copy filters out only rows the join would have dropped anyway.
    """
    classes = _equivalences(plan)
    if not classes:
        return plan
    single: dict[str, list[Expr]] = {}
    for scan in _scans(plan):
        for part in conjuncts(scan.filter) if scan.filter is not None else []:
            col = _constant_predicate_column(part)
            if col is not None:
                single.setdefault(col.name, []).append(part)

    def extend(node: Plan) -> Plan:
        match node:
            case Scan(table=t, alias=a, columns=cols, filter=f):
                have = conjuncts(f) if f is not None else []
                added: list[Expr] = []
                for c in cols:
                    me = f"{a}.{c}"
                    for other in classes.get(me, set()) - {me}:
                        for pred in single.get(other, []):
                            src = _constant_predicate_column(pred)
                            if src is None:
                                continue
                            copy = _substitute(pred, other, Col(me, src.type))
                            if copy not in have and copy not in added:
                                added.append(copy)
                return Scan(t, a, cols, conjoin(have + added)) if added else node
            case Join(left=l, right=r, condition=cond):
                return Join(extend(l), extend(r), cond)
            case Filter(child=c, predicate=pr):
                return Filter(extend(c), pr)
            case Aggregate(child=c, groups=g, aggs=ag):
                return Aggregate(extend(c), g, ag)
            case Project(child=c, items=i):
                return Project(extend(c), i)
            case Sort(child=c, keys=k):
                return Sort(extend(c), k)
            case Limit(child=c, n=n):
                return Limit(extend(c), n)
        raise TypeError(f"not a plan: {node!r}")

    return extend(plan)


def _equivalences(plan: Plan) -> dict[str, set[str]]:
    """Columns made equal by equi-join conditions, as column -> its whole class."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            x = parent[x]
        return x

    def walk(p: Plan) -> None:
        if isinstance(p, Join):
            for part in conjuncts(p.condition) if p.condition is not None else []:
                if (
                    isinstance(part, Bin)
                    and _is_equi(part)
                    and isinstance(part.left, Col)
                    and isinstance(part.right, Col)
                    and part.left.type == part.right.type
                ):
                    parent[find(part.left.name)] = find(part.right.name)
            walk(p.left)
            walk(p.right)
        elif not isinstance(p, Scan):
            walk(p.child)

    walk(plan)
    groups: dict[str, set[str]] = {}
    for col in parent:
        groups.setdefault(find(col), set()).add(col)
    return {col: groups[find(col)] for col in parent}


def _scans(p: Plan) -> list[Scan]:
    if isinstance(p, Scan):
        return [p]
    if isinstance(p, Join):
        return _scans(p.left) + _scans(p.right)
    return _scans(p.child)


def _constant_predicate_column(e: Expr) -> Col | None:
    """The column of `col <op> constant`, `constant <op> col` or `col [NOT] IN (constants)`, else None."""
    match e:
        case (
            Bin(op=op, left=Col() as c, right=Lit()) | Bin(op=op, left=Lit(), right=Col() as c)
        ) if op in _COMPARE:
            return c
        case InList(arg=Col() as c):
            return c
    return None


def _substitute(e: Expr, name: str, to: Col) -> Expr:
    """The expression with column `name` replaced by `to`."""
    match e:
        case Col(name=n) if n == name:
            return to
        case Bin(op=op, left=l, right=r, type=t):
            return Bin(op, _substitute(l, name, to), _substitute(r, name, to), t)
        case InList(arg=a, values=v, negated=neg):
            return InList(_substitute(a, name, to), v, neg)
    return e


def _aliases(p: Plan) -> dict[str, str]:
    match p:
        case Scan(table=t, alias=a):
            return {a: t}
        case Join(left=l, right=r):
            return _aliases(l) | _aliases(r)
        case (
            Filter(child=c) | Aggregate(child=c) | Project(child=c) | Sort(child=c) | Limit(child=c)
        ):
            return _aliases(c)
    return {}


def _is_equi(e: Expr) -> bool:
    return (
        isinstance(e, Bin) and e.op == "=" and isinstance(e.left, Col) and isinstance(e.right, Col)
    )


def _flatten(p: Plan, relations: list[Plan], conditions: list[Expr]) -> None:
    if isinstance(p, Join):
        _flatten(p.left, relations, conditions)
        _flatten(p.right, relations, conditions)
        if p.condition is not None:
            conditions.extend(conjuncts(p.condition))
    else:
        relations.append(p)


def _place_all(node: Plan, preds: list[Expr]) -> Plan:
    remaining: list[Expr] = []
    for pr in preds:
        node, placed = _place(node, pr)
        if not placed:
            remaining.append(pr)
    rest = conjoin(remaining)
    return Filter(node, rest) if rest is not None else node


def _place(node: Plan, pr: Expr) -> tuple[Plan, bool]:
    """Put one conjunct as low as possible: into a scan, into one side of a join, or into the join."""
    cols = columns(pr)
    if not cols <= set(output_columns(node)):
        return node, False
    match node:
        case Scan(filter=f):
            return Scan(
                node.table, node.alias, node.columns, conjoin([*([f] if f is not None else []), pr])
            ), True
        case Join(left=l, right=r, condition=cond):
            if cols and cols <= set(output_columns(l)):
                return Join(_place(l, pr)[0], r, cond), True
            if cols and cols <= set(output_columns(r)):
                return Join(l, _place(r, pr)[0], cond), True
            return Join(l, r, conjoin([*conjuncts(cond), pr] if cond is not None else [pr])), True
        case Filter(child=c, predicate=existing):
            inner, placed = _place(c, pr)
            return (
                (Filter(inner, existing), True)
                if placed
                else (Filter(c, conjoin([existing, pr]) or existing), True)
            )
    return node, False
