"""Front door: SQL in, Arrow table out, with EXPLAIN."""

from __future__ import annotations

import pyarrow as pa

from vectorized_query_engine.binder import bind
from vectorized_query_engine.catalog import Catalog
from vectorized_query_engine.executor import ExecStats, Executor
from vectorized_query_engine.optimizer import Optimizer
from vectorized_query_engine.plan import Plan, explain


class Engine:
    """Parse, bind, optimize and execute queries over a catalog."""

    def __init__(self, catalog: Catalog, optimize: bool = True) -> None:
        self.catalog = catalog
        self.optimize = optimize
        self.last_stats = ExecStats()

    def plan(self, sql: str, optimize: bool | None = None) -> Plan:
        """The logical plan, optimized unless asked otherwise."""
        plan = bind(self.catalog, sql)
        if self.optimize if optimize is None else optimize:
            plan = Optimizer(self.catalog).optimize(plan)
        return plan

    def explain(self, sql: str, optimize: bool | None = None) -> str:
        """The plan as an indented tree with estimated row counts."""
        plan = self.plan(sql, optimize)
        opt = Optimizer(self.catalog)
        return explain(plan, opt.estimate)

    def sql(self, sql: str, optimize: bool | None = None) -> pa.Table:
        """Run a query and return its result."""
        executor = Executor(self.catalog)
        result = executor.run(self.plan(sql, optimize))
        self.last_stats = executor.stats
        return result
