"""run_sql 沙箱（sql_sandbox）的离线检查与真实容器集成；未开启集成时明确跳过。"""
import base64
import csv
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from datalab.datasets import olist
from datalab.execution.runner import DockerRunner
from datalab.execution.sql_sandbox import build_script, project_inputs, run_query

ROOT = Path(__file__).resolve().parents[2]
# 与 test_execution.py 相同的真实容器开关。
DOCKER = os.environ.get("DATALAB_DOCKER_TESTS") == "1"


def write_synthetic(folder: Path):
    """写一个小型合成多表数据集：三张表各几行，含可空时间与空串。"""
    orders = [
        {"order_id": "o1", "customer_id": "c1", "order_status": "delivered",
         "order_purchase_timestamp": "2017-01-01T10:00:00", "order_approved_at": "2017-01-01T11:00:00",
         "order_delivered_carrier_date": "", "order_delivered_customer_date": "2017-01-05T09:00:00",
         "order_estimated_delivery_date": "2017-01-10T00:00:00"},
        {"order_id": "o2", "customer_id": "c2", "order_status": "shipped",
         "order_purchase_timestamp": "2017-02-01T10:00:00", "order_approved_at": "",
         "order_delivered_carrier_date": "", "order_delivered_customer_date": "",
         "order_estimated_delivery_date": "2017-02-10T00:00:00"},
        {"order_id": "o3", "customer_id": "c1", "order_status": "delivered",
         "order_purchase_timestamp": "2017-03-01T10:00:00", "order_approved_at": "2017-03-01T12:00:00",
         "order_delivered_carrier_date": "2017-03-02T08:00:00", "order_delivered_customer_date": "2017-03-06T09:00:00",
         "order_estimated_delivery_date": "2017-03-10T00:00:00"},
    ]
    items = [
        {"order_id": "o1", "order_item_id": "1", "product_id": "p1", "seller_id": "s1",
         "shipping_limit_date": "2017-01-03T00:00:00", "price_cents": "1000", "freight_cents": "200"},
        {"order_id": "o1", "order_item_id": "2", "product_id": "p2", "seller_id": "s1",
         "shipping_limit_date": "2017-01-03T00:00:00", "price_cents": "2500", "freight_cents": "300"},
        {"order_id": "o3", "order_item_id": "1", "product_id": "p1", "seller_id": "s2",
         "shipping_limit_date": "2017-03-03T00:00:00", "price_cents": "1000", "freight_cents": "150"},
    ]
    products = [
        {"product_id": "p1", "product_category_name": "cama_mesa_banho"},
        {"product_id": "p2", "product_category_name": ""},
    ]
    olist.write_tables(folder, {"orders": orders, "order_items": items, "products": products})


class OfflineFixture(unittest.TestCase):
    def setUp(self):
        scratch = (ROOT / ".tmp").resolve()
        self.assertTrue(scratch.is_relative_to(ROOT))
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch, prefix="sqlsandbox-")
        self.addCleanup(self.temp.cleanup)
        self.dataset = Path(self.temp.name) / "datasets" / "v-synthetic"
        self.dataset.mkdir(parents=True)
        write_synthetic(self.dataset)
        with patch("datalab.execution.runner.shutil.which", return_value="docker"):
            self.runner = DockerRunner(None, Path(self.temp.name) / "runs")


class ProjectInputsTests(OfflineFixture):
    def test_projection_only_requested_columns_and_counts(self):
        tables = {"orders": ("order_id", "order_status"), "order_items": ("order_id", "price_cents")}
        inputs, counts = project_inputs(self.dataset, tables)
        self.assertEqual(set(inputs), {"orders.csv", "order_items.csv"})
        self.assertEqual(counts, {"orders": 3, "order_items": 3})
        for name, cols in tables.items():
            with self.subTest(name=name):
                rows = list(csv.reader(inputs[f"{name}.csv"].decode("utf-8").splitlines()))
                self.assertEqual(tuple(rows[0]), cols)
                self.assertEqual(len(rows) - 1, counts[name])

    def test_rejects_unknown_table_and_column(self):
        with self.assertRaises(ValueError):
            project_inputs(self.dataset, {"secrets": ("a",)})
        with self.assertRaises(ValueError):
            project_inputs(self.dataset, {"orders": ("not_a_column",)})
        with self.assertRaises(ValueError):
            project_inputs(self.dataset, {})


class BuildScriptTests(OfflineFixture):
    @staticmethod
    def payload_of(script: str) -> dict:
        marker = 'base64.b64decode("'
        start = script.index(marker) + len(marker)
        return json.loads(base64.b64decode(script[start:script.index('"', start)]).decode("utf-8"))

    def test_valid_python_and_sql_roundtrip_in_payload(self):
        # 含 '''、"""、换行与注释的 SQL 也只经 base64 载荷传入，不会破坏脚本。
        sql = 'SELECT "order_id", \'it\'\'\' AS t\nFROM orders WHERE note = """x""" -- 注释\nLIMIT 5'
        script = build_script(sql, {"orders": ("order_id",)}, 5)
        compile(script, "analysis.py", "exec")
        payload = self.payload_of(script)
        self.assertEqual(payload["sql"], sql)
        self.assertEqual(payload["limit"], 5)
        self.assertEqual(payload["tables"], {"orders": [["order_id", "id"]]})
        # SQL 没有被拼进脚本源码。
        self.assertNotIn("it'''", script)

    def test_uses_casts_for_cents_and_nullable_time(self):
        script = build_script("SELECT 1", {"order_items": ("price_cents",),
                                           "orders": ("order_approved_at",)}, 5)
        compile(script, "analysis.py", "exec")
        tables = self.payload_of(script)["tables"]
        self.assertIn(["price_cents", "cents"], tables["order_items"])
        self.assertIn(["order_approved_at", "time?"], tables["orders"])
        self.assertIn("NULLIF", script)

    def test_rejects_bad_limit_and_table(self):
        with self.assertRaises(ValueError):
            build_script("SELECT 1", {"orders": ("order_id",)}, 0)
        with self.assertRaises(ValueError):
            build_script("SELECT 1", {"unknown": ("a",)}, 5)


class ExecuteInputsValidationTests(OfflineFixture):
    """execute_inputs 与 execute_python 共用同一套校验；用与 test_execution.py 相同的 mock 风格。"""

    def test_bad_run_id_rejected_before_docker(self):
        with patch.object(self.runner, "_control") as control:
            with self.assertRaises(ValueError):
                self.runner.execute_inputs("not-a-uuid", {"orders.csv": b"a"}, "pass", 1,
                                           self._spec(), dataset_version="v", metric_version="trusted-v1")
            control.assert_not_called()

    def test_bad_file_name_rejected_before_docker(self):
        for name in ("Orders.csv", "a/b.csv", "x.py", "con.csv1"):
            with self.subTest(name=name), patch.object(self.runner, "_control") as control:
                with self.assertRaises(ValueError):
                    self.runner.execute_inputs(str(uuid4()), {name: b"a"}, "pass", 1,
                                               self._spec(), dataset_version="v", metric_version="trusted-v1")
                control.assert_not_called()

    def test_bad_timeout_and_code_size_rejected(self):
        with self.assertRaises(ValueError):
            self.runner.execute_inputs(str(uuid4()), {"orders.csv": b"a"}, "pass", 121,
                                       self._spec(), dataset_version="v", metric_version="trusted-v1")
        with self.assertRaises(ValueError):
            self.runner.execute_inputs(str(uuid4()), {"orders.csv": b"a"}, "x" * (256 * 1024 + 1), 1,
                                       self._spec(), dataset_version="v", metric_version="trusted-v1")

    def test_file_count_and_size_limits(self):
        many = {f"t{i}.csv": b"a" for i in range(9)}
        with self.assertRaises(ValueError):
            self.runner.execute_inputs(str(uuid4()), many, "pass", 1,
                                       self._spec(), dataset_version="v", metric_version="trusted-v1")
        with self.assertRaises(ValueError):
            self.runner.execute_inputs(str(uuid4()), {"orders.csv": b"x" * (33 * 1024 * 1024)}, "pass", 1,
                                       self._spec(), dataset_version="v", metric_version="trusted-v1")

    def test_store_optional_for_runner(self):
        self.assertIsNone(self.runner.store)

    @staticmethod
    def _spec():
        from datalab.execution.runner import OutputSpec
        return OutputSpec(files=("result.json",), max_bytes=1024)


@unittest.skipUnless(DOCKER, "需显式启用真实 Docker 集成")
class DockerIntegrationTests(OfflineFixture):
    """真实容器：GROUP BY 与蜕变关系、行数比对、SQL 错误回传与纵深防御。"""

    TABLES = {"orders": ("order_id", "order_status", "order_approved_at"),
              "order_items": ("order_id", "price_cents")}

    def query(self, sql, limit=100, timeout=30.0):
        return run_query(self.runner, self.dataset, sql, self.TABLES, limit,
                         metric_version="trusted-v1", timeout=timeout)

    def test_group_by_with_permutation_and_counts(self):
        sql = ("SELECT o.order_status, SUM(i.price_cents) AS total FROM orders o "
               "JOIN order_items i ON o.order_id = i.order_id "
               "GROUP BY o.order_status ORDER BY total DESC LIMIT 10")
        result = self.query(sql)
        self.assertNotIn("error", result, result)
        self.assertEqual(result["columns"], ["order_status", "total"])
        # 内连接：o1（1000+2500）与 o3（1000）都是 delivered，shipped 的 o2 没有明细不出现。
        self.assertEqual(result["rows"], [["delivered", 4500]])
        # 蜕变关系：行序置换后结果作为多重集合一致。
        self.assertEqual(sorted(map(tuple, result["rows"])), sorted(map(tuple, result["permuted_rows"])))
        self.assertEqual(result["table_counts"], result["host_counts"])
        self.assertEqual(result["host_counts"], {"orders": 3, "order_items": 3})

    def test_nullable_timestamp_loads_as_null(self):
        result = self.query("SELECT order_id, order_approved_at FROM orders ORDER BY order_id")
        self.assertNotIn("error", result, result)
        self.assertEqual([row[0] for row in result["rows"]], ["o1", "o2", "o3"])
        self.assertEqual(result["rows"][1][1], None)
        self.assertEqual(result["rows"][0][1], "2017-01-01T11:00:00")

    def test_sql_runtime_error_returned_as_observation(self):
        result = self.query("SELECT order_id / 2 FROM orders")
        self.assertIn("error", result)
        self.assertLessEqual(len(result["error"]), 340)
        self.assertEqual(result["host_counts"], {"orders": 3, "order_items": 3})

    def test_read_csv_outside_input_blocked(self):
        # 故意绕过 sql_guard 白名单：沙箱内的 DuckDB 配置必须拦住。
        result = self.query("SELECT * FROM read_csv('/etc/passwd') LIMIT 1")
        self.assertIn("error", result)
        self.assertIn("Permission", result["error"])

    def test_defense_in_depth_statements_all_fail(self):
        for sql in ("SELECT getenv('HOME')",
                    "COPY (SELECT 1) TO '/output/x.csv'",
                    "SET enable_external_access=true",
                    "INSTALL httpfs"):
            with self.subTest(sql=sql):
                result = self.query(sql)
                self.assertIn("error", result)
