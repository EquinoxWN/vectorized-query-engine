.PHONY: setup lint test explain report bench audit ci

PY ?= python

setup:
	$(PY) -m pip install --upgrade pip && $(PY) -m pip install -e ".[dev]"

# ruff, ruff format and mypy --strict over the engine and the tests.
lint:
	$(PY) -m ruff check . && $(PY) -m ruff format --check . && $(PY) -m mypy

# 277 tests: binder errors, plan shapes, 21 TPC-H-style queries and 150 random queries checked
# against DuckDB with the optimizer on and off, NULL semantics, safety limits and the CLI.
test:
	$(PY) -m pytest -q

# The plan for TPC-H Q3 without and with the optimizer.
explain:
	vqe "$(Q3)" --explain --no-optimize --scale 1
	vqe "$(Q3)" --explain --scale 1

# Every query with the optimizer off and on and on DuckDB: rows, equality, cells read, time.
report:
	vqe-report --scale 1 --repeat 3

bench:
	@echo "M3: TPC-H scale factor 1 from Parquet with row-group skipping, against DuckDB"

# Known vulnerabilities in the installed Python dependencies.
audit:
	$(PY) -m pip_audit --skip-editable --cache-dir .tmp/pip-audit

ci: setup lint test

Q3 = SELECT l_orderkey, sum(l_extendedprice * (1 - l_discount)) AS revenue, o_orderdate FROM customer, orders, lineitem WHERE c_mktsegment = 'BUILDING' AND c_custkey = o_custkey AND l_orderkey = o_orderkey AND o_orderdate < DATE '1995-03-15' AND l_shipdate > DATE '1995-03-15' GROUP BY l_orderkey, o_orderdate ORDER BY revenue DESC, o_orderdate, l_orderkey LIMIT 10
