"""
Turn sqlglot's syntax tree into a typed logical plan: resolve tables and columns against the
catalog, check types and GROUP BY rules, and fold constant expressions (including date arithmetic).
"""

from __future__ import annotations

import calendar
import datetime as dt
import difflib
import math
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

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
)
from vectorized_query_engine.plan import Aggregate, Filter, Join, Limit, Plan, Project, Scan, Sort

MAX_SQL_LENGTH = 100_000


class BindError(ValueError):
    """The query is invalid or uses a feature the engine does not support."""


@dataclass
class _Source:
    alias: str
    table: str
    types: dict[str, str]


_CMP: dict[type[exp.Expression], str] = {
    exp.EQ: "=",
    exp.NEQ: "!=",
    exp.LT: "<",
    exp.LTE: "<=",
    exp.GT: ">",
    exp.GTE: ">=",
}
_ARITH: dict[type[exp.Expression], str] = {exp.Add: "+", exp.Sub: "-", exp.Mul: "*", exp.Div: "/"}
_AGGS = {exp.Sum: "sum", exp.Avg: "avg", exp.Min: "min", exp.Max: "max"}


def _numeric(t: str) -> bool:
    return t in {"int64", "double"}


class Binder:
    """Binds one SELECT statement."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.sources: list[_Source] = []
        self.aggs: list[tuple[Agg, str]] = []
        self.in_aggregate_context = False

    # ---------------------------------------------------------------- entry
    def bind(self, sql: str) -> Plan:
        """Parse and bind a single SELECT."""
        if len(sql) > MAX_SQL_LENGTH:
            raise BindError(f"query is longer than {MAX_SQL_LENGTH:,} characters")
        try:
            statements = sqlglot.parse(sql, read="duckdb")
            if len(statements) != 1 or not isinstance(statements[0], exp.Select):
                raise BindError("expected exactly one SELECT statement")
            return self._select(statements[0])
        except ParseError as e:
            raise BindError(f"syntax error: {str(e).splitlines()[0]}") from e
        except RecursionError as e:
            # Deep nesting must be a clear error, never a crash of the host process.
            raise BindError("query is nested too deeply") from e

    def _select(self, q: exp.Select) -> Plan:
        # sqlglot names some keys with a trailing underscore ("with_", "from_").
        for key in ("with_", "with", "distinct", "qualify", "windows"):
            if q.args.get(key):
                raise BindError(f"unsupported: {key.rstrip('_').upper()}")
        plan = self._from(q)
        where = q.args.get("where")
        if where is not None:
            pred = self._expr(where.this)
            self._require_bool(pred, "WHERE")
            plan = Filter(plan, pred)
        group = q.args.get("group")
        group_exprs = [self._expr(g) for g in group.expressions] if group else []
        select_items = self._select_items(q)
        having_node = q.args.get("having")
        is_agg = bool(group_exprs) or any(e.find(exp.AggFunc) for e in q.expressions)
        is_agg = is_agg or (having_node is not None)
        if is_agg:
            self.in_aggregate_context = True
            group_named = tuple((g, f"group{i}") for i, g in enumerate(group_exprs))
            items = [(self._rewrite_agg(sel, group_named), name) for sel, name in select_items]
            having = (
                self._rewrite_agg(having_node.this, group_named)
                if having_node is not None
                else None
            )
            plan = Aggregate(plan, group_named, tuple(self.aggs))
            if having is not None:
                self._require_bool(having, "HAVING")
                plan = Filter(plan, having)
            project_items = tuple(items)
        else:
            project_items = tuple((self._expr(sel), name) for sel, name in select_items)
        plan = Project(plan, project_items)
        order = q.args.get("order")
        if order is not None:
            plan = Sort(plan, tuple(self._order_key(o, project_items) for o in order.expressions))
        limit = q.args.get("limit")
        if limit is not None:
            value = limit.expression
            if not isinstance(value, exp.Literal) or value.is_string or not value.this.isdigit():
                raise BindError("LIMIT must be a non-negative integer")
            plan = Limit(plan, int(value.this))
        return plan

    # ---------------------------------------------------------------- FROM
    def _from(self, q: exp.Select) -> Plan:
        frm = q.args.get("from_") or q.args.get("from")
        if frm is None:
            raise BindError("a FROM clause is required")
        plan = self._table(frm.this)
        for j in q.args.get("joins") or []:
            kind = (j.args.get("side") or "").upper() or (j.args.get("kind") or "").upper()
            if kind not in {"", "INNER", "CROSS"}:
                raise BindError(f"unsupported: {kind} JOIN (inner joins only in M1)")
            right = self._table(j.this)
            on = j.args.get("on")
            cond = self._expr(on) if on is not None else None
            if cond is not None:
                self._require_bool(cond, "JOIN ON")
            plan = Join(plan, right, cond)
        return plan

    def _table(self, t: exp.Expression) -> Plan:
        if not isinstance(t, exp.Table):
            raise BindError("unsupported: subqueries and table functions in FROM")
        info = self.catalog.get(t.name)
        if info is None:
            near = difflib.get_close_matches(t.name.lower(), self.catalog.names(), n=1)
            hint = f" (did you mean {near[0]}?)" if near else ""
            raise BindError(f"unknown table {t.name}{hint}")
        alias = (t.alias or t.name).lower()
        if any(s.alias == alias for s in self.sources):
            raise BindError(f"duplicate table alias {alias}")
        types = {f.name: info.column_type(f.name) for f in info.data.schema}
        self.sources.append(_Source(alias, info.name, types))
        return Scan(info.name, alias, tuple(types))

    # ---------------------------------------------------------------- SELECT list
    def _select_items(self, q: exp.Select) -> list[tuple[exp.Expression, str]]:
        out: list[tuple[exp.Expression, str]] = []
        for i, e in enumerate(q.expressions):
            if isinstance(e, exp.Star):
                out += [(exp.column(c, s.alias), c) for s in self.sources for c in s.types]
            elif isinstance(e, exp.Column) and isinstance(e.this, exp.Star):
                src = self._source(e.table)
                out += [(exp.column(c, src.alias), c) for c in src.types]
            elif isinstance(e, exp.Alias):
                out.append((e.this, e.alias.lower()))
            elif isinstance(e, exp.Column):
                out.append((e, e.name.lower()))
            else:
                out.append((e, f"col{i}"))
        names = [n for _, n in out]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise BindError(f"duplicate output column names {sorted(dupes)}; use AS to rename")
        return out

    def _source(self, alias: str) -> _Source:
        for s in self.sources:
            if s.alias == alias.lower():
                return s
        raise BindError(f"unknown table or alias {alias}")

    # ---------------------------------------------------------------- aggregates
    def _rewrite_agg(self, e: exp.Expression, groups: tuple[tuple[Expr, str], ...]) -> Expr:
        """Bind an expression in an aggregate query: aggregates and group expressions become columns."""
        agg = self._agg(e)
        if agg is not None:
            for existing, name in self.aggs:
                if existing == agg:
                    return Col(name, agg.type)
            name = f"agg{len(self.aggs)}"
            self.aggs.append((agg, name))
            return Col(name, agg.type)
        if not e.find(exp.AggFunc):
            try:
                bound = self._expr(e)
            except BindError:
                bound = None
            if bound is not None:
                for g, name in groups:
                    if g == bound:
                        return Col(name, g.type)
                if not columns(bound):
                    return bound
        if isinstance(e, exp.Column):
            raise BindError(f"column {e.sql()} must appear in GROUP BY or be inside an aggregate")
        return self._expr(e, rewrite=lambda sub: self._rewrite_agg(sub, groups))

    def _agg(self, e: exp.Expression) -> Agg | None:
        if isinstance(e, exp.Count):
            arg = e.this
            if isinstance(arg, exp.Star):
                return Agg("count_all", None, "int64")
            if isinstance(arg, exp.Distinct):
                inner = self._expr(arg.expressions[0])
                return Agg("count_distinct", inner, "int64")
            return Agg("count", self._expr(arg), "int64")
        for cls, name in _AGGS.items():
            if isinstance(e, cls):
                if e.find(exp.Distinct):
                    raise BindError(f"unsupported: {name.upper()}(DISTINCT ...)")
                arg = self._expr(e.this)
                if name in {"sum", "avg"} and not _numeric(arg.type):
                    raise BindError(f"{name.upper()} needs a number, got {arg.type}")
                out_type = "double" if name == "avg" else arg.type
                return Agg(name, arg, out_type)
        if isinstance(e, exp.AggFunc):
            raise BindError(f"unsupported aggregate {e.sql()}")
        return None

    # ---------------------------------------------------------------- ORDER BY
    def _order_key(self, o: exp.Ordered, items: tuple[tuple[Expr, str], ...]) -> tuple[str, bool]:
        desc = bool(o.args.get("desc"))
        e = o.this
        names = [n for _, n in items]
        if isinstance(e, exp.Literal) and not e.is_string:
            pos = int(e.this)
            if not 1 <= pos <= len(items):
                raise BindError(f"ORDER BY position {pos} is out of range")
            return names[pos - 1], desc
        if isinstance(e, exp.Column) and not e.table and e.name.lower() in names:
            return e.name.lower(), desc
        if self.in_aggregate_context:
            raise BindError(
                "ORDER BY in an aggregate query must name a selected column or position"
            )
        bound = self._expr(e)
        for expr_, name in items:
            if expr_ == bound:
                return name, desc
        raise BindError(f"ORDER BY {e.sql()} must refer to a selected column (M1 limitation)")

    # ---------------------------------------------------------------- expressions
    def _require_bool(self, e: Expr, where: str) -> None:
        if e.type != "bool":
            raise BindError(f"{where} needs a boolean condition, got {e.type}")

    def _column(self, c: exp.Column) -> Col:
        name = c.name.lower()
        if c.table:
            src = self._source(c.table)
            if name not in src.types:
                near = difflib.get_close_matches(name, list(src.types), n=1)
                raise BindError(
                    f"unknown column {c.table}.{name}"
                    + (f" (did you mean {near[0]}?)" if near else "")
                )
            return Col(f"{src.alias}.{name}", src.types[name])
        matches = [s for s in self.sources if name in s.types]
        if not matches:
            everything = [col for s in self.sources for col in s.types]
            near = difflib.get_close_matches(name, everything, n=1)
            raise BindError(
                f"unknown column {name}" + (f" (did you mean {near[0]}?)" if near else "")
            )
        if len(matches) > 1:
            raise BindError(
                f"column {name} is ambiguous: qualify it with one of {[s.alias for s in matches]}"
            )
        return Col(f"{matches[0].alias}.{name}", matches[0].types[name])

    def _expr(self, e: exp.Expression, rewrite: object = None) -> Expr:
        sub = rewrite if callable(rewrite) else self._expr
        if isinstance(e, exp.Paren):
            return sub(e.this)  # type: ignore[no-any-return,unused-ignore]
        if isinstance(e, exp.Column):
            if isinstance(e.this, exp.Star):
                raise BindError("t.* is only allowed in the SELECT list")
            return self._column(e)
        if isinstance(e, exp.Literal):
            if e.is_string:
                return Lit(e.this, "string")
            text = e.this
            return (
                Lit(float(text), "double")
                if any(ch in text for ch in ".eE")
                else Lit(int(text), "int64")
            )
        if isinstance(e, exp.Boolean):
            return Lit(bool(e.this), "bool")
        if isinstance(e, exp.Null):
            return Lit(None, "null")
        if isinstance(e, exp.Cast):
            return self._cast(e)
        if isinstance(e, exp.Neg):
            arg = sub(e.this)
            return self._arith("-", Lit(0, "int64"), arg)
        if type(e) in _ARITH:
            if isinstance(e.expression, exp.Interval) or isinstance(e.this, exp.Interval):
                if isinstance(e.this, exp.Interval) or not isinstance(e, exp.Add | exp.Sub):
                    raise BindError("only DATE constant +/- INTERVAL is supported")
                return self._date_interval(e, sub(e.this))
            return self._arith(_ARITH[type(e)], sub(e.this), sub(e.expression))
        if type(e) in _CMP:
            return self._compare(_CMP[type(e)], sub(e.this), sub(e.expression))
        if isinstance(e, exp.And | exp.Or):
            left, right = sub(e.this), sub(e.expression)
            op = "AND" if isinstance(e, exp.And) else "OR"
            for side in (left, right):
                self._require_bool(side, op)
            return Bin(op, left, right, "bool")
        if isinstance(e, exp.Not):
            arg = sub(e.this)
            if isinstance(arg, IsNull | InList | Like):
                return type(arg)(**{**arg.__dict__, "negated": not arg.negated})
            self._require_bool(arg, "NOT")
            return Not(arg)
        if isinstance(e, exp.Is) and isinstance(e.expression, exp.Null):
            return IsNull(sub(e.this), negated=False)
        if isinstance(e, exp.Between):
            x, low, high = sub(e.this), sub(e.args["low"]), sub(e.args["high"])
            return Bin("AND", self._compare(">=", x, low), self._compare("<=", x, high), "bool")
        if isinstance(e, exp.In):
            if e.args.get("query"):
                raise BindError("unsupported: IN (subquery)")
            arg = sub(e.this)
            values = []
            for v in e.expressions:
                lit = sub(v)
                if not isinstance(lit, Lit):
                    raise BindError("IN lists must contain constants")
                self._check_comparable(arg, lit)
                values.append(lit)
            return InList(arg, tuple(values), negated=False)
        if isinstance(e, exp.Like):
            arg, pat = sub(e.this), sub(e.expression)
            if arg.type != "string" or not isinstance(pat, Lit) or pat.type != "string":
                raise BindError("LIKE needs a text column and a text pattern")
            return Like(arg, str(pat.value), negated=False)
        if isinstance(e, exp.Interval):
            raise BindError("an INTERVAL can only be added to or subtracted from a DATE constant")
        if isinstance(e, exp.AggFunc):
            raise BindError(f"aggregate {e.sql()} is not allowed here")
        raise BindError(f"unsupported expression: {e.sql()}")

    def _cast(self, e: exp.Cast) -> Expr:
        to = e.args["to"]
        if (
            to.this == exp.DataType.Type.DATE
            and isinstance(e.this, exp.Literal)
            and e.this.is_string
        ):
            try:
                return Lit(dt.date.fromisoformat(e.this.this), "date32")
            except ValueError as err:
                raise BindError(f"invalid date {e.this.this!r}") from err
        raise BindError(f"unsupported cast: {e.sql()}")

    def _date_interval(self, e: exp.Expression, left: Expr) -> Expr:
        interval = e.expression
        if (
            not isinstance(interval, exp.Interval)
            or not isinstance(left, Lit)
            or left.type != "date32"
        ):
            raise BindError("only DATE constant +/- INTERVAL is supported")
        amount = int(
            str(interval.this.this if isinstance(interval.this, exp.Literal) else interval.this)
        )
        unit_node = interval.args.get("unit")
        unit = str(unit_node.this if unit_node is not None else "DAY").upper().rstrip("S")
        sign = -1 if isinstance(e, exp.Sub) else 1
        base = left.value
        if not isinstance(base, dt.date):
            raise BindError("only DATE constant +/- INTERVAL is supported")
        if unit == "DAY":
            return Lit(base + dt.timedelta(days=sign * amount), "date32")
        if unit in {"MONTH", "YEAR"}:
            months = amount * (12 if unit == "YEAR" else 1) * sign
            y, m = divmod(base.month - 1 + months, 12)
            year, month = base.year + y, m + 1
            # Like DuckDB, Jan 31 + 1 month is the last day of February.
            day = min(base.day, calendar.monthrange(year, month)[1])
            return Lit(dt.date(year, month, day), "date32")
        raise BindError(f"unsupported interval unit {unit}")

    def _arith(self, op: str, left: Expr, right: Expr) -> Expr:
        if not (_numeric(left.type) or left.type == "null") or not (
            _numeric(right.type) or right.type == "null"
        ):
            raise BindError(f"cannot apply {op} to {left.type} and {right.type}")
        out = "double" if op == "/" or "double" in {left.type, right.type} else "int64"
        if (
            isinstance(left, Lit)
            and isinstance(right, Lit)
            and left.value is not None
            and right.value is not None
        ):
            a, b = left.value, right.value
            if not isinstance(a, int | float) or not isinstance(b, int | float):
                raise BindError(f"cannot apply {op} to {left.type} and {right.type}")
            if op == "/" and b == 0:
                # IEEE 754 division, as in DuckDB: x / 0 is +-inf and 0 / 0 is nan.
                zero_sign = math.copysign(1.0, float(b))
                return Lit(math.nan if a == 0 else math.copysign(math.inf, a) * zero_sign, "double")
            value = {"+": a + b, "-": a - b, "*": a * b, "/": a / b if op == "/" else 0}[op]
            return Lit(float(value) if out == "double" else int(value), out)
        return Bin(op, left, right, out)

    def _check_comparable(self, left: Expr, right: Expr) -> None:
        if "null" in {left.type, right.type}:
            return
        if left.type != right.type and not (_numeric(left.type) and _numeric(right.type)):
            raise BindError(f"cannot compare {left.type} with {right.type}")

    def _compare(self, op: str, left: Expr, right: Expr) -> Expr:
        self._check_comparable(left, right)
        return Bin(op, left, right, "bool")


def bind(catalog: Catalog, sql: str) -> Plan:
    """Bind one SELECT statement into a logical plan."""
    return Binder(catalog).bind(sql)
