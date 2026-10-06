"""Twenty-one TPC-H-style queries (name, ordered, sql) shared by the tests and `vqe-report`."""

from __future__ import annotations

QUERIES: list[tuple[str, bool, str]] = [
    (
        "q1_pricing_summary",
        True,
        """SELECT l_returnflag, l_linestatus, sum(l_quantity) AS sum_qty, sum(l_extendedprice) AS sum_base,
                  sum(l_extendedprice * (1 - l_discount)) AS sum_disc,
                  sum(l_extendedprice * (1 - l_discount) * (1 + l_tax)) AS sum_charge,
                  avg(l_quantity) AS avg_qty, avg(l_discount) AS avg_disc, count(*) AS n
           FROM lineitem WHERE l_shipdate <= DATE '1998-12-01' - INTERVAL 90 DAY
           GROUP BY l_returnflag, l_linestatus ORDER BY l_returnflag, l_linestatus""",
    ),
    (
        "q3_shipping_priority",
        True,
        """SELECT l_orderkey, sum(l_extendedprice * (1 - l_discount)) AS revenue, o_orderdate
           FROM customer, orders, lineitem
           WHERE c_mktsegment = 'BUILDING' AND c_custkey = o_custkey AND l_orderkey = o_orderkey
             AND o_orderdate < DATE '1995-03-15' AND l_shipdate > DATE '1995-03-15'
           GROUP BY l_orderkey, o_orderdate ORDER BY revenue DESC, o_orderdate, l_orderkey LIMIT 10""",
    ),
    (
        "q5_volume_by_nation",
        True,
        """SELECT n_name, sum(l_extendedprice * (1 - l_discount)) AS revenue
           FROM customer c JOIN orders o ON c.c_custkey = o.o_custkey
             JOIN lineitem l ON l.l_orderkey = o.o_orderkey
             JOIN nation n ON c.c_nationkey = n.n_nationkey
             JOIN region r ON n.n_regionkey = r.r_regionkey
           WHERE r_name = 'ASIA' AND o_orderdate >= DATE '1994-01-01'
             AND o_orderdate < DATE '1994-01-01' + INTERVAL 1 YEAR
           GROUP BY n_name ORDER BY revenue DESC""",
    ),
    (
        "q6_forecast_revenue",
        True,
        """SELECT sum(l_extendedprice * l_discount) AS revenue FROM lineitem
           WHERE l_shipdate >= DATE '1994-01-01' AND l_shipdate < DATE '1995-01-01'
             AND l_discount BETWEEN 0.05 AND 0.07 AND l_quantity < 24""",
    ),
    (
        "q10_returned_items",
        True,
        """SELECT c_custkey, c_name, sum(l_extendedprice * (1 - l_discount)) AS revenue, c_acctbal, n_name
           FROM customer, orders, lineitem, nation
           WHERE c_custkey = o_custkey AND l_orderkey = o_orderkey AND o_orderdate >= DATE '1993-10-01'
             AND o_orderdate < DATE '1993-10-01' + INTERVAL 3 MONTH AND l_returnflag = 'R'
             AND c_nationkey = n_nationkey
           GROUP BY c_custkey, c_name, c_acctbal, n_name ORDER BY revenue DESC, c_custkey LIMIT 20""",
    ),
    (
        "q12_order_priority",
        True,
        """SELECT o_orderpriority, count(*) AS order_count FROM orders
           WHERE o_orderdate >= DATE '1993-07-01' AND o_orderdate < DATE '1993-10-01'
           GROUP BY o_orderpriority ORDER BY o_orderpriority""",
    ),
    (
        "distinct_buyers_per_segment",
        True,
        """SELECT c_mktsegment, count(DISTINCT o_custkey) AS buyers, count(*) AS orders
           FROM customer JOIN orders ON c_custkey = o_custkey GROUP BY c_mktsegment ORDER BY c_mktsegment""",
    ),
    (
        "having",
        True,
        """SELECT o_custkey, count(*) AS n, sum(o_totalprice) AS total FROM orders
           GROUP BY o_custkey HAVING count(*) >= 15 ORDER BY n DESC, o_custkey""",
    ),
    (
        "in_list",
        True,
        """SELECT o_orderkey, o_orderstatus FROM orders
           WHERE o_orderstatus IN ('F', 'P') AND o_orderkey < 50 ORDER BY o_orderkey""",
    ),
    (
        "not_in_and_like",
        True,
        """SELECT c_custkey, c_name FROM customer
           WHERE c_mktsegment NOT IN ('BUILDING', 'MACHINERY') AND c_name LIKE 'Customer#0000000%'
           ORDER BY c_custkey""",
    ),
    (
        "arithmetic_projection",
        True,
        """SELECT l_orderkey, l_linenumber, l_extendedprice / l_quantity AS unit_price,
                  l_quantity * 2 - 1 AS q
           FROM lineitem WHERE l_orderkey <= 20 ORDER BY l_orderkey, l_linenumber""",
    ),
    (
        "global_aggregates",
        False,
        """SELECT min(o_orderdate) AS first, max(o_orderdate) AS last, avg(o_totalprice) AS avg_price,
                  count(*) AS n FROM orders""",
    ),
    (
        "self_join",
        True,
        """SELECT a.l_orderkey, count(*) AS pairs
           FROM lineitem a JOIN lineitem b ON a.l_orderkey = b.l_orderkey AND a.l_linenumber < b.l_linenumber
           WHERE a.l_orderkey <= 100 GROUP BY a.l_orderkey ORDER BY 1""",
    ),
    (
        "join_condition_in_where",
        True,
        """SELECT r_name, n_name FROM region, nation
           WHERE n_regionkey = r_regionkey AND r_name LIKE 'A%' ORDER BY n_name""",
    ),
    ("cross_product", False, "SELECT count(*) AS n FROM region CROSS JOIN nation"),
    (
        "or_across_tables",
        False,
        """SELECT count(*) AS n FROM customer c JOIN orders o ON c.c_custkey = o.o_custkey
           WHERE c.c_acctbal < 0 OR o.o_totalprice > 500000""",
    ),
    ("select_star", True, "SELECT * FROM nation WHERE n_regionkey = 2 ORDER BY n_nationkey"),
    (
        "group_by_expression",
        True,
        """SELECT l_quantity > 25 AS big, count(*) AS n FROM lineitem
           GROUP BY l_quantity > 25 ORDER BY big""",
    ),
    (
        "not_and_is_not_null",
        False,
        """SELECT count(*) AS n FROM orders
           WHERE NOT (o_orderstatus = 'F') AND o_totalprice IS NOT NULL""",
    ),
    (
        "zero_discount_lines",
        False,
        "SELECT count(*) AS n, sum(l_quantity) AS q FROM lineitem WHERE l_discount = 0.0",
    ),
    (
        "folded_constants",
        False,
        """SELECT count(*) AS n FROM orders
           WHERE o_orderdate BETWEEN DATE '1995-01-01' AND DATE '1995-01-01' + INTERVAL 6 MONTH
             AND o_totalprice > 1000 * 100""",
    ),
]
