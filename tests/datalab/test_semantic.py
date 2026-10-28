"""语义层：与 olist_sql 重叠指标逐行一致、与独立 Python 参考全指标一致、粒度校验与过滤器注入防护。"""
import random
import tempfile
import unittest
from pathlib import Path

from olist_bundle import bundle, write
from datalab.contracts import AnalysisPlan
from datalab.datasets.service import DatasetStore
from datalab.semantic.compiler import compile_query
from datalab.semantic.dimensions import describe_dimensions
from datalab.semantic.engine import SemanticEngine
from datalab.semantic.errors import SemanticError
from datalab.semantic.metrics import list_metrics
from datalab.semantic.reference import python_reference, verify
from datalab.semantic.snapshot import SemanticSnapshot
from datalab.semantic.tree import decomposition, metric_tree, validate
from datalab.verification.olist_sql import reference_result

ROOT = Path(__file__).resolve().parents[2]
STATUSES = ("delivered", "shipped", "invoiced", "canceled")
SNAPSHOT = SemanticSnapshot("test-v1", STATUSES, "payments")
SNAPSHOT_ITEMS = SemanticSnapshot("test-v1", STATUSES, "items")


class Fixture(unittest.TestCase):
    def setUp(self):
        scratch = (ROOT / ".tmp").resolve()
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch, prefix="semantic-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = DatasetStore(self.root / "datasets")

    def dataset(self, tables, name="raw"):
        version = self.store.import_olist(write(self.root / name, tables), project_id="demo", dataset_family="olist")
        return version.dataset_version, self.store.directory(version.dataset_version)


class OverlapTests(Fixture):
    """语义层与现有 olist_sql 参考的重叠指标逐行一致（固定种子合成数据，多组种子）。"""

    def compare(self, folder, metric_name, plan_fields, mapping, snapshot=SNAPSHOT):
        """mapping: 语义层行 → olist_sql 行 的字段对照；两边分组与期间相同。"""
        engine = SemanticEngine(folder, snapshot)
        try:
            plan = AnalysisPlan("00000000-0000-0000-0000-000000000000", "v1", schema="olist",
                                included_statuses=STATUSES, **plan_fields)
            expected = reference_result(folder, plan)
            period = (plan.start, plan.end)
            actual = engine.query(metric_name, period, group_by=plan.group_by)
            self.assertEqual(actual["total"], mapping["total"](expected["total"]))
            renamed = sorted((dict(zip(mapping["row"], (row[source] for source in mapping["row"].values())))
                              for row in expected["rows"]), key=lambda row: row["group"])
            self.assertEqual([{k: row[k] for k in mapping["row"]} for row in actual["rows"]], renamed)
        finally:
            engine.close()

    def test_gmv_and_order_count_match_orders(self):
        for seed in range(20):
            rng = random.Random(900 + seed)
            _, folder = self.dataset(bundle(rng, rng.choice([12, 40, 90]), months=rng.choice([3, 6])), f"raw{seed}")
            for source, snapshot in (("payments", SNAPSHOT), ("items", SNAPSHOT_ITEMS)):
                for group_by in (None, "customer_state", "purchase_month"):
                    with self.subTest(seed=seed, source=source, group_by=group_by):
                        kind = {None: "summary", "customer_state": "ranking", "purchase_month": "trend"}[group_by]
                        self.compare(folder, "gmv", {"kind": kind, "group_by": group_by, "amount_source": source},
                                     {"total": lambda t: {"numerator": t["amount_cents"], "denominator": None,
                                                          "value": t["amount_cents"]},
                                      "row": {"group": "group", "numerator": "amount_cents"}}, snapshot)
                        self.compare(folder, "order_count", {"kind": kind, "group_by": group_by, "amount_source": source},
                                     {"total": lambda t: {"numerator": t["order_count"], "denominator": None,
                                                          "value": t["order_count"]},
                                      "row": {"group": "group", "numerator": "order_count"}}, snapshot)

    def test_late_delivery_rate_matches_delivery_delay(self):
        for seed in range(10):
            rng = random.Random(700 + seed)
            _, folder = self.dataset(bundle(rng, 60), f"late{seed}")
            for group_by in (None, "customer_state"):
                with self.subTest(seed=seed, group_by=group_by):
                    self.compare(folder, "late_delivery_rate",
                                 {"kind": "delivery_delay", "group_by": group_by},
                                 {"total": lambda t: {"numerator": t["late_count"], "denominator": t["delivered_count"],
                                                      "value": t["late_rate"]},
                                  "row": {"group": "group", "numerator": "late_count", "denominator": "delivered_count",
                                          "value": "late_rate"}})

    def test_repeat_rate_matches_repeat(self):
        for seed in range(10):
            rng = random.Random(600 + seed)
            _, folder = self.dataset(bundle(rng, 80), f"rep{seed}")
            for group_by in (None, "customer_state"):
                with self.subTest(seed=seed, group_by=group_by):
                    self.compare(folder, "repeat_rate", {"kind": "repeat_rate", "group_by": group_by},
                                 {"total": lambda t: {"numerator": t["repeat_customer_count"],
                                                      "denominator": t["customer_count"], "value": t["repeat_rate"]},
                                  "row": {"group": "group", "numerator": "repeat_customer_count",
                                          "denominator": "customer_count", "value": "repeat_rate"}})

    def test_item_amounts_match_category_sales(self):
        for seed in range(10):
            rng = random.Random(500 + seed)
            _, folder = self.dataset(bundle(rng, 70), f"cat{seed}")
            engine = SemanticEngine(folder, SNAPSHOT)
            try:
                plan = AnalysisPlan("00000000-0000-0000-0000-000000000000", "v1", kind="category_sales",
                                    group_by="product_category", schema="olist", included_statuses=STATUSES)
                expected = reference_result(folder, plan)
                gmv = engine.query("item_gmv", (None, None), group_by="product_category")
                count = engine.query("item_count", (None, None), group_by="product_category")
                self.assertEqual(gmv["total"]["value"], expected["total"]["price_cents"] + expected["total"]["freight_cents"])
                self.assertEqual(count["total"]["value"], expected["total"]["item_count"])
                by_group = {row["group"]: row for row in expected["rows"]}
                self.assertEqual({row["group"] for row in gmv["rows"]}, set(by_group))
                for row in gmv["rows"]:
                    self.assertEqual(row["value"], by_group[row["group"]]["price_cents"] + by_group[row["group"]]["freight_cents"])
                for row in count["rows"]:
                    self.assertEqual(row["value"], by_group[row["group"]]["item_count"])
            finally:
                engine.close()


class ReferenceAgreementTests(Fixture):
    """语义层引擎与独立 Python 参考对全部指标 × 允许维度逐项一致（L1 核验方式）。"""

    def test_all_metrics_agree_with_python_reference(self):
        combos = 0
        for seed in range(6):
            rng = random.Random(300 + seed)
            _, folder = self.dataset(bundle(rng, rng.choice([30, 90]), months=rng.choice([3, 6])), f"all{seed}")
            snapshot = SNAPSHOT if seed % 2 == 0 else SNAPSHOT_ITEMS
            engine = SemanticEngine(folder, snapshot)
            try:
                for metric in list_metrics():
                    name = metric["name"]
                    dimensions = [None] + metric["dimensions"]
                    for group_by in dimensions:
                        for period in ((None, None), ("2017-02-01T00:00:00", "2017-05-01T00:00:00")):
                            with self.subTest(seed=seed, metric=name, group_by=group_by, period=period):
                                actual = engine.query(name, period, group_by=group_by)
                                result = verify(actual, folder, snapshot, group_by=group_by)
                                self.assertTrue(result["passed"], result["differences"])
                                expected = python_reference(folder, name, period, None, group_by, snapshot)
                                self.assertEqual(actual, expected)
                            combos += 1
            finally:
                engine.close()
        self.assertGreater(combos, 500)

    def test_filters_and_compare_period(self):
        rng = random.Random(42)
        _, folder = self.dataset(bundle(rng, 90, months=6))
        engine = SemanticEngine(folder, SNAPSHOT)
        try:
            for filters, group_by in (({"customer_state": "SP"}, None), ({"customer_state": ["SP", "RJ"]}, "purchase_month"),
                                      ({"product_category": "bed_bath_table"}, None), ({"price_band": ["lt_50"]}, "seller_state")):
                with self.subTest(filters=filters, group_by=group_by):
                    metric = "item_gmv" if "product_category" in filters or "price_band" in filters else "gmv"
                    actual = engine.query(metric, (None, None), compare_period=("2017-01-01T00:00:00", "2017-03-01T00:00:00"),
                                          filters=filters, group_by=group_by)
                    self.assertIn("compare", actual)
                    self.assertIn("delta", actual)
                    expected = python_reference(folder, metric, (None, None), filters, group_by, SNAPSHOT)
                    self.assertEqual({k: actual[k] for k in ("metric", "total", "rows")},
                                     {k: expected[k] for k in ("metric", "total", "rows")})
        finally:
            engine.close()


class RejectionTests(Fixture):
    def setUp(self):
        super().setUp()
        _, self.folder = self.dataset(bundle(random.Random(11), 40))

    def test_grain_mismatch_is_rejected_with_suggestion(self):
        engine = SemanticEngine(self.folder, SNAPSHOT)
        try:
            with self.assertRaises(SemanticError) as caught:
                engine.query("gmv", (None, None), group_by="product_category")
            self.assertIn("item_gmv", str(caught.exception))
            for metric, dimension in (("order_count", "seller_state"), ("repeat_rate", "purchase_month"),
                                      ("new_customers", "seller_id"), ("aov", "price_band")):
                with self.subTest(metric=metric, dimension=dimension):
                    with self.assertRaises(SemanticError):
                        engine.query(metric, (None, None), group_by=dimension)
        finally:
            engine.close()

    def test_interrupted_load_leaves_no_half_table(self):
        """载入中途失败后，引擎仍可在同一连接上重新载入（A10 raw-09：半成品表使引擎永久报错）。"""
        engine = SemanticEngine(self.folder, SNAPSHOT)
        try:
            original = engine._connection
            db = original()

            class Flaky:
                """第一次 executemany 抛错，模拟载入中途内存不足或中断。"""

                def __init__(self):
                    self.failed = False

                def execute(self, *args):
                    return db.execute(*args)

                def executemany(self, *args):
                    if not self.failed:
                        self.failed = True
                        raise MemoryError("模拟载入中断")
                    return db.executemany(*args)

            engine._db = Flaky()
            with self.assertRaises(MemoryError):
                engine._load("orders")
            self.assertNotIn("orders", engine._loaded)
            engine._load("orders")
            self.assertIn("orders", engine._loaded)
            engine._db = db
            self.assertIsNotNone(engine.data_floor)
        finally:
            engine.close()

    def test_close_then_reuse_reloads_tables(self):
        engine = SemanticEngine(self.folder, SNAPSHOT)
        first = engine.query("order_count", (None, None))["total"]
        engine.close()
        self.assertEqual(engine.query("order_count", (None, None))["total"], first)
        engine.close()

    def test_unknown_metric_dimension_and_bad_period(self):
        engine = SemanticEngine(self.folder, SNAPSHOT)
        try:
            for call in (lambda: engine.query("gmvv", (None, None)),
                         lambda: engine.query("gmv", (None, None), group_by="city"),
                         lambda: engine.query("gmv", ("2017-01-01", None)),
                         lambda: engine.query("gmv", ("2017-05-01T00:00:00", "2017-01-01T00:00:00")),
                         lambda: engine.query("gmv", ("2017-01-01T00:00:00+03:00", None))):
                with self.assertRaises(SemanticError):
                    call()
        finally:
            engine.close()

    def test_filter_injection_is_rejected_or_bound(self):
        engine = SemanticEngine(self.folder, SNAPSHOT)
        try:
            # 维度名为任意字符串：拒绝。
            with self.assertRaises(SemanticError):
                engine.query("gmv", (None, None), filters={"x' OR 1=1 --": "SP"})
            with self.assertRaises(SemanticError):
                engine.query("gmv", (None, None), filters={"order_status": "delivered"})
            # 取值含引号/注入片段：词表拒绝。
            with self.assertRaises(SemanticError):
                engine.query("gmv", (None, None), filters={"customer_state": "SP' OR '1'='1"})
            with self.assertRaises(SemanticError):
                engine.query("gmv", (None, None), filters={"customer_state": ["SP", "'; DROP TABLE orders; --"]})
            with self.assertRaises(SemanticError):
                engine.query("gmv", (None, None), filters={"customer_state": 5})
            # 合法取值安全绑定：不存在的值返回空结果而不是异常。
            result = engine.query("gmv", (None, None), filters={"customer_state": "ZZ"})
            self.assertEqual(result["total"]["value"], 0)
        finally:
            engine.close()
        # 编译产物全部是参数绑定：SQL 文本中不含任何过滤值。
        queries = compile_query("gmv", ("2017-01-01T00:00:00", "2018-01-01T00:00:00"),
                                {"customer_state": ["SP", "RJ"]}, "purchase_month", SNAPSHOT)
        for _, sql, params in queries:
            self.assertNotIn("SP", sql)
            self.assertNotIn("2017-01-01", sql)
            self.assertIn("SP", params)

    def test_missing_sellers_table_gives_clear_error(self):
        # 模拟旧签名数据版本：删掉 sellers.csv 后使用卖家维度。
        (self.folder / "sellers.csv").rename(self.folder / "sellers.csv.bak")
        engine = SemanticEngine(self.folder, SNAPSHOT)
        try:
            with self.assertRaises(SemanticError) as caught:
                engine.query("item_gmv", (None, None), group_by="seller_state")
            self.assertIn("没有卖家表", str(caught.exception))
        finally:
            engine.close()
            (self.folder / "sellers.csv.bak").rename(self.folder / "sellers.csv")


class TreeTests(unittest.TestCase):
    def test_tree_is_valid_and_metadata_views(self):
        self.assertEqual(validate(), [])
        tree = metric_tree()
        self.assertEqual(decomposition("gmv").children, ("order_count", "aov"))
        self.assertEqual(decomposition("gmv").kind, "multiplicative")
        self.assertEqual(decomposition("order_count").by_dimension, "customer_type")
        with self.assertRaises(SemanticError):
            decomposition("cancel_count")
        metrics = {row["name"] for row in list_metrics()}
        for expected in ("gmv", "order_count", "aov", "cancel_rate", "late_delivery_rate", "avg_delivery_days",
                         "item_gmv", "category_gmv", "item_count", "avg_item_price", "items_per_order", "freight_ratio",
                         "avg_review_score", "bad_review_rate", "repeat_rate", "new_customers", "returning_customers"):
            self.assertIn(expected, metrics)
        dimensions = {row["name"] for row in describe_dimensions()}
        self.assertIn("seller_state", dimensions)
        self.assertIn("price_band", dimensions)
        for row in tree:
            self.assertIn(row["kind"], ("multiplicative", "additive"))


if __name__ == "__main__":
    unittest.main()
