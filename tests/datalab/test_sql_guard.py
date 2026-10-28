"""A8 自由查询 SQL 白名单校验：只读单条 SELECT、表列在白名单内、外层 LIMIT 受限。"""
import unittest

from datalab.execution.sql_guard import GuardedQuery, SqlRejected, guard_sql


def code_of(sql):
    try:
        guard_sql(sql)
    except SqlRejected as exc:
        return exc.code
    return None


class RejectWriteTests(unittest.TestCase):
    def test_write_and_ddl(self):
        cases = {
            "INSERT INTO orders VALUES ('a','b','c','d','e','f','g','h')": "not_select",
            "UPDATE orders SET order_status='x' WHERE order_id='a'": "not_select",
            "DELETE FROM orders WHERE order_id='a'": "not_select",
            "CREATE TABLE t AS SELECT * FROM orders LIMIT 1": "not_select",
            "DROP TABLE orders": "not_select",
            "COPY orders TO '/output/x.csv'": "not_select",
            "ATTACH 'x.db'": "not_select",
            "PRAGMA database_list": "not_select",
            "SET threads=4": "not_select",
            "INSTALL httpfs": "not_select",
            "LOAD httpfs": "not_select",
            "CREATE MACRO m(a) AS a+1": "not_select",
        }
        for sql, code in cases.items():
            with self.subTest(sql=sql):
                self.assertEqual(code_of(sql), code)

    def test_data_modifying_cte(self):
        # sqlglot 能解析 data-modifying CTE；根是 SELECT 但树中有 Delete，须按 forbidden_node 拒绝
        sql = "WITH t AS (DELETE FROM orders RETURNING *) SELECT * FROM t LIMIT 1"
        self.assertEqual(code_of(sql), "forbidden_node")

    def test_select_into(self):
        self.assertEqual(code_of("SELECT * INTO t FROM orders LIMIT 1"), "forbidden_node")


class RejectTableTests(unittest.TestCase):
    def test_unauthorized_tables(self):
        cases = {
            "SELECT * FROM information_schema.tables LIMIT 1": "qualified_table",
            "SELECT * FROM main.orders LIMIT 1": "qualified_table",
            "SELECT * FROM pg_catalog.orders LIMIT 1": "qualified_table",
            "SELECT * FROM sqlite_master LIMIT 1": "unknown_table",
            "SELECT * FROM users LIMIT 1": "unknown_table",
            "SELECT * FROM duckdb_settings() LIMIT 1": "table_function",
            "SELECT * FROM duckdb_tables() LIMIT 1": "table_function",
            "SELECT * FROM read_csv('/etc/passwd') LIMIT 1": "table_function",
            "SELECT * FROM glob('*') LIMIT 1": "table_function",
            "SELECT * FROM range(10) LIMIT 1": "table_function",
            "SELECT * FROM '/input/orders.csv' LIMIT 1": "table_function",
        }
        for sql, code in cases.items():
            with self.subTest(sql=sql):
                self.assertEqual(code_of(sql), code)


class RejectFunctionTests(unittest.TestCase):
    def test_forbidden_functions(self):
        cases = [
            "SELECT getenv('HOME') FROM orders LIMIT 1",
            "SELECT current_setting('threads') FROM orders LIMIT 1",
            "SELECT which_secret('x', 'y') FROM orders LIMIT 1",
            "SELECT read_text('/etc/passwd') FROM orders LIMIT 1",
            "SELECT install_extension('httpfs') FROM orders LIMIT 1",
            "SELECT duckdb_version() FROM orders LIMIT 1",
        ]
        for sql in cases:
            with self.subTest(sql=sql):
                self.assertEqual(code_of(sql), "forbidden_function")


class RejectLimitTests(unittest.TestCase):
    def test_missing_limit(self):
        cases = [
            "SELECT order_id FROM orders",
            "SELECT order_id FROM orders UNION ALL SELECT order_id FROM orders",
            "SELECT order_id FROM orders LIMIT (SELECT 1)",
            "WITH t AS (SELECT order_id FROM orders) SELECT * FROM t",
        ]
        for sql in cases:
            with self.subTest(sql=sql):
                self.assertEqual(code_of(sql), "missing_limit")

    def test_limit_too_large(self):
        self.assertEqual(code_of("SELECT order_id FROM orders LIMIT 201"), "limit_too_large")
        self.assertEqual(code_of("SELECT order_id FROM orders LIMIT 0"), "limit_too_large")


class RejectStructureTests(unittest.TestCase):
    def test_multiple_statements(self):
        cases = [
            "SELECT order_id FROM orders LIMIT 1; DROP TABLE orders",
            "SELECT order_id FROM orders LIMIT 1; SELECT order_id FROM orders LIMIT 1",
        ]
        for sql in cases:
            with self.subTest(sql=sql):
                self.assertEqual(code_of(sql), "multiple_statements")

    def test_unknown_column(self):
        cases = [
            "SELECT password FROM customers LIMIT 5",
            # 自由文本列在规范化时已剔除，不在白名单
            "SELECT review_comment_message FROM order_reviews LIMIT 5",
            "SELECT customer_city FROM customers LIMIT 5",
        ]
        for sql in cases:
            with self.subTest(sql=sql):
                self.assertEqual(code_of(sql), "unknown_column")

    def test_too_long(self):
        sql = "SELECT order_id FROM orders WHERE order_id != '" + "x" * 4096 + "' LIMIT 1"
        self.assertEqual(code_of(sql), "too_long")

    def test_parse_error(self):
        self.assertEqual(code_of("SELECT FROM WHERE LIMIT"), "parse_error")

    def test_plan_categories_summary(self):
        # 四类拒绝原因一览：写操作、未授权表、无 LIMIT、多语句
        cases = [
            ("DELETE FROM orders WHERE order_id='a'", "not_select"),            # 写操作
            ("SELECT * FROM users LIMIT 1", "unknown_table"),                  # 未授权表
            ("SELECT order_id FROM orders", "missing_limit"),                  # 无 LIMIT
            ("SELECT order_id FROM orders LIMIT 1; SELECT 1", "multiple_statements"),  # 多语句
        ]
        for sql, code in cases:
            with self.subTest(sql=sql):
                self.assertEqual(code_of(sql), code)


class AcceptTests(unittest.TestCase):
    def test_join_group_order_limit(self):
        sql = ("SELECT o.order_id, s.seller_state, sum(i.price_cents) AS total "
               "FROM orders o JOIN order_items i ON o.order_id = i.order_id "
               "JOIN sellers s ON i.seller_id = s.seller_id "
               "GROUP BY o.order_id, s.seller_state ORDER BY total DESC LIMIT 20")
        result = guard_sql(sql)
        self.assertIsInstance(result, GuardedQuery)
        self.assertEqual(result.limit, 20)
        self.assertEqual(result.tables["orders"], ("order_id",))
        self.assertEqual(result.tables["order_items"], ("order_id", "price_cents", "seller_id"))
        self.assertEqual(result.tables["sellers"], ("seller_id", "seller_state"))
        self.assertEqual(set(result.tables), {"orders", "order_items", "sellers"})

    def test_cte_aggregation(self):
        sql = ("WITH per_order AS (SELECT order_id, count(*) AS n FROM orders GROUP BY 1) "
               "SELECT order_id, n FROM per_order ORDER BY n DESC LIMIT 10")
        result = guard_sql(sql)
        self.assertEqual(result.limit, 10)
        self.assertEqual(result.tables, {"orders": ("order_id",)})

    def test_window_function(self):
        sql = ("SELECT order_id, row_number() OVER (PARTITION BY customer_id "
               "ORDER BY order_purchase_timestamp) AS rn FROM orders LIMIT 10")
        result = guard_sql(sql)
        self.assertEqual(result.tables["orders"], ("customer_id", "order_id", "order_purchase_timestamp"))

    def test_uncorrelated_subquery(self):
        sql = ("SELECT order_id FROM orders WHERE customer_id IN "
               "(SELECT customer_id FROM customers WHERE customer_state = 'SP') LIMIT 5")
        result = guard_sql(sql)
        self.assertEqual(result.tables["orders"], ("customer_id", "order_id"))
        self.assertEqual(result.tables["customers"], ("customer_id", "customer_state"))

    def test_correlated_subquery(self):
        sql = ("SELECT order_id FROM orders o WHERE EXISTS "
               "(SELECT 1 FROM customers c WHERE c.customer_id = o.customer_id) LIMIT 5")
        result = guard_sql(sql)
        self.assertEqual(result.tables["orders"], ("customer_id", "order_id"))
        self.assertEqual(result.tables["customers"], ("customer_id",))

    def test_union_all_with_outer_limit(self):
        sql = "SELECT order_id FROM orders UNION ALL SELECT customer_id FROM customers LIMIT 10"
        result = guard_sql(sql)
        self.assertEqual(result.limit, 10)
        self.assertEqual(result.tables["orders"], ("order_id",))
        self.assertEqual(result.tables["customers"], ("customer_id",))

    def test_select_star(self):
        result = guard_sql("SELECT * FROM sellers LIMIT 5")
        self.assertEqual(result.tables["sellers"], ("seller_id", "seller_state"))

    def test_count_star(self):
        # 表被引用但没有列：给第一列保证沙箱里能加载
        result = guard_sql("SELECT count(*) FROM orders LIMIT 1")
        self.assertEqual(result.tables["orders"], ("order_id",))

    def test_trailing_semicolon(self):
        result = guard_sql("SELECT order_id FROM orders LIMIT 3;")
        self.assertEqual(result.limit, 3)
        self.assertNotIn(";", result.sql)

    def test_case_insensitive_table_names(self):
        result = guard_sql("SELECT order_id FROM ORDERS LIMIT 2")
        self.assertEqual(result.tables, {"orders": ("order_id",)})

    def test_limit_boundary(self):
        self.assertEqual(guard_sql("SELECT order_id FROM orders LIMIT 200").limit, 200)
        self.assertEqual(guard_sql("SELECT order_id FROM orders LIMIT 1 OFFSET 50").limit, 1)

    def test_date_functions_allowed(self):
        cases = [
            "SELECT date_trunc('month', order_purchase_timestamp) AS m, count(*) FROM orders GROUP BY 1 LIMIT 12",
            "SELECT strftime(order_purchase_timestamp, '%Y-%m') FROM orders LIMIT 5",
        ]
        for sql in cases:
            with self.subTest(sql=sql):
                result = guard_sql(sql)
                self.assertIn("order_purchase_timestamp", result.tables["orders"])

    def test_normalized_sql_roundtrip(self):
        sql = "SELECT o.order_id FROM orders o WHERE o.order_status='delivered' LIMIT 5"
        result = guard_sql(sql)
        # 规范化 SQL 仍是无前缀表名的原查询，可在沙箱 DuckDB 直接执行
        self.assertIn("FROM orders AS o", result.sql)
        self.assertNotIn("main.", result.sql)


if __name__ == "__main__":
    unittest.main()
