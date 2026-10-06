"""The vqe command runs, explains and reports errors with exit codes."""

from __future__ import annotations

import pytest

from vectorized_query_engine.cli import main

SQL = "SELECT o_orderpriority, count(*) AS n FROM orders GROUP BY o_orderpriority ORDER BY o_orderpriority"


def test_runs_a_query_and_reports_rows_time_and_cells(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([SQL, "--scale", "0.01"]) == 0
    out = capsys.readouterr().out
    assert "1-URGENT" in out
    assert "5 rows in" in out
    assert "cells" in out


def test_explain_prints_the_optimized_plan(capsys: pytest.CaptureFixture[str]) -> None:
    sql = (
        "SELECT c_name FROM customer, orders WHERE c_custkey = o_custkey AND o_totalprice > 500000"
    )
    assert main([sql, "--explain", "--scale", "0.01"]) == 0
    out = capsys.readouterr().out
    assert "HashJoin ON" in out
    assert "Scan orders AS orders [o_custkey] WHERE (orders.o_totalprice > 500000)" in out


def test_duckdb_comparison(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([SQL, "--scale", "0.01", "--duckdb"]) == 0
    assert "DuckDB:" in capsys.readouterr().out


def test_invalid_queries_exit_with_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["SELECT * FROM ordrs", "--scale", "0.01"]) == 2
    assert "error: unknown table ordrs (did you mean orders?)" in capsys.readouterr().err


def test_report_compares_every_query_with_duckdb(capsys: pytest.CaptureFixture[str]) -> None:
    from vectorized_query_engine.report import main as report

    assert report(["--scale", "0.01", "--repeat", "1"]) == 0
    out = capsys.readouterr().out
    assert "21 of 21 results equal DuckDB's." in out
    row = next(line for line in out.splitlines() if line.startswith("| q3_shipping_priority "))
    assert "refused (too large)" in row, "Q3 as written is a cross product the executor refuses"
