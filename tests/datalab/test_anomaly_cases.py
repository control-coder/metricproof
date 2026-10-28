"""合成异动评测集（A3）的离线检查：注入器确定性与语义、冻结题集加载校验、评分函数。

注入器测试在 olist_bundle 固定种子合成数据上进行（导入为数据版本后读回规范化表），
不调用模型、不需要真实数据；冻结题集测试只读提交到仓库的 anomaly_cases.json。
"""
import random
import sys
import tempfile
import unittest
from pathlib import Path

from olist_bundle import bundle, write
from datalab.datasets import olist
from datalab.datasets.service import DatasetStore

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evals" / "anomaly"))
import cases as cases_module  # noqa: E402
import inject as injector  # noqa: E402
import score as score_module  # noqa: E402


def make_tables(seed=41, orders=120):
    """olist_bundle 合成数据导入数据版本后读回的规范化表（与 inject 的输入结构一致）。"""
    scratch = (ROOT / ".tmp").resolve()
    scratch.mkdir(exist_ok=True)
    temp = tempfile.TemporaryDirectory(dir=scratch, prefix="anomaly-test-")
    root = Path(temp.name)
    store = DatasetStore(root / "datasets")
    version = store.import_olist(write(root / "raw", bundle(random.Random(seed), orders)),
                                 project_id="p", dataset_family="olist")
    tables = olist.read_tables(store.directory(version.dataset_version))
    return temp, tables


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp, self.tables = make_tables()
        self.addCleanup(self.temp.cleanup)
        self.orders = {row["order_id"]: row for row in self.tables["orders"]}
        self.customers = {row["customer_id"]: row for row in self.tables["customers"]}
        self.seller_state = {row["seller_id"]: row["seller_state"] for row in self.tables["sellers"]}
        english = {row["product_category_name"]: row["product_category_name_english"]
                   for row in self.tables["category_translation"]}
        raw_category = {row["product_id"]: row["product_category_name"] for row in self.tables["products"]}
        self.category_of = {pid: (english.get(name, name) if name else olist.UNKNOWN_CATEGORY)
                            for pid, name in raw_category.items()}

    def month_of(self, order_id):
        return self.orders[order_id]["order_purchase_timestamp"][:7]

    def target_month(self):
        """取订单最多的月份作为目标月。"""
        from collections import Counter
        return Counter(self.month_of(order_id) for order_id in self.orders).most_common(1)[0][0]


class DeterminismTests(Fixture):
    """每类注入器：相同 spec 与种子结果逐字节一致；不同种子可以不同。"""

    def check_deterministic(self, spec):
        first = injector.inject(self.tables, spec, injector.rng_for("case-x"))
        second = injector.inject(self.tables, spec, injector.rng_for("case-x"))
        self.assertEqual(first, second)
        # 原表不被修改（纯函数）。
        self.assertIsNot(first, self.tables)

    def test_all_injectors_deterministic(self):
        month = self.target_month()
        category = next(iter(set(self.category_of.values())))
        state = next(iter({row["customer_state"] for row in self.tables["customers"]}))
        seller_state = next(iter(set(self.seller_state.values())))
        seller = next(iter(self.seller_state))
        for spec in ({"type": "order_drop", "month": month, "product_category": category,
                      "customer_state": state, "share": 0.4},
                     {"type": "freight_up", "month": month, "seller_state": seller_state, "factor": 1.4},
                     {"type": "delivery_delay", "month": month, "customer_state": state, "share": 0.5},
                     {"type": "mix_shift", "month": month, "dimension": "payment_type", "value": "boleto"},
                     {"type": "price_cut", "month": month, "product_category": category, "factor": 0.7},
                     {"type": "seller_drop", "month": month, "seller_id": seller}):
            with self.subTest(type=spec["type"]):
                self.check_deterministic(spec)


class OrderDropTests(Fixture):
    def test_order_drop_removes_child_rows(self):
        month = self.target_month()
        category = max({self.category_of[item["product_id"]] for item in self.tables["order_items"]},
                       key=lambda c: sum(1 for item in self.tables["order_items"]
                                         if self.category_of[item["product_id"]] == c))
        state = max({row["customer_state"] for row in self.tables["customers"]},
                    key=lambda s: sum(1 for row in self.tables["customers"] if row["customer_state"] == s))
        spec = {"type": "order_drop", "month": month, "product_category": category,
                "customer_state": state, "share": 1.0}
        after = injector.inject(self.tables, spec, injector.rng_for("od"))
        # share=1.0：目标组合订单全部删除，其明细、支付、评价子行一并删除。
        target_orders = set()
        for item in self.tables["order_items"]:
            order = self.orders[item["order_id"]]
            if self.month_of(item["order_id"]) == month \
                    and self.category_of[item["product_id"]] == category \
                    and self.customers[order["customer_id"]]["customer_state"] == state:
                target_orders.add(item["order_id"])
        self.assertTrue(target_orders, "合成数据应存在目标组合订单")
        self.assertEqual({row["order_id"] for row in after["orders"]} & target_orders, set())
        for name in ("order_items", "order_payments", "order_reviews"):
            self.assertEqual({row["order_id"] for row in after[name]} & target_orders, set(),
                             f"{name} 应删除目标订单子行")
        # 非目标订单不受影响。
        remaining = {row["order_id"] for row in self.tables["orders"]} - target_orders
        self.assertEqual({row["order_id"] for row in after["orders"]}, remaining)


class FreightUpTests(Fixture):
    def test_freight_up_only_target_state_month(self):
        month = self.target_month()
        states = sorted(set(self.seller_state.values()))
        state = states[0]
        spec = {"type": "freight_up", "month": month, "seller_state": state, "factor": 1.5}
        after = injector.inject(self.tables, spec, injector.rng_for("fu"))
        before_items = {(item["order_id"], item["order_item_id"]): item for item in self.tables["order_items"]}
        changed = 0
        for item in after["order_items"]:
            old = before_items[(item["order_id"], item["order_item_id"])]
            in_target = self.month_of(item["order_id"]) == month \
                and self.seller_state[item["seller_id"]] == state
            if in_target:
                self.assertGreaterEqual(int(item["freight_cents"]), int(old["freight_cents"]) + 1)
                changed += 1
            else:
                self.assertEqual(item["freight_cents"], old["freight_cents"], "非目标组运费不变")
            self.assertEqual(item["price_cents"], old["price_cents"], "价格不受 freight_up 影响")
        self.assertTrue(changed, "目标组应有运费上调")


class DeliveryDelayTests(Fixture):
    def test_delivery_delay_late_and_bad_review(self):
        month = self.target_month()
        from collections import Counter
        counts = Counter(self.customers[order["customer_id"]]["customer_state"]
                         for order in self.tables["orders"]
                         if order["order_status"] == "delivered" and order["order_delivered_customer_date"]
                         and self.month_of(order["order_id"]) == month)
        if not counts:
            self.skipTest("合成数据目标月没有已送达订单")
        state = counts.most_common(1)[0][0]
        spec = {"type": "delivery_delay", "month": month, "customer_state": state, "share": 1.0}
        after = injector.inject(self.tables, spec, injector.rng_for("dd"))
        before_orders = {row["order_id"]: row for row in self.tables["orders"]}
        delayed = set()
        for order in after["orders"]:
            old = before_orders[order["order_id"]]
            if old["order_delivered_customer_date"] == order["order_delivered_customer_date"]:
                continue
            delayed.add(order["order_id"])
            self.assertEqual(old["order_status"], "delivered")
            self.assertEqual(self.month_of(order["order_id"]), month)
            self.assertEqual(self.customers[old["customer_id"]]["customer_state"], state)
            # 送达日期晚于预计送达日期（按日期比较）。
            self.assertGreater(order["order_delivered_customer_date"][:10],
                               order["order_estimated_delivery_date"][:10])
        self.assertTrue(delayed, "share=1.0 时目标组应全部推迟")
        # 被推迟订单的评价分数改为 1–2。
        scores = {row["review_score"] for row in after["order_reviews"] if row["order_id"] in delayed}
        self.assertTrue(scores <= {"1", "2"}, f"差评分数应为 1–2，实际 {scores}")


class MixShiftTests(Fixture):
    def test_mix_shift_within_group_ratio_exactly_unchanged(self):
        month = self.target_month()
        from collections import Counter
        first_pay = {}
        for row in self.tables["order_payments"]:
            key = row["order_id"]
            if key not in first_pay or int(row["payment_sequential"]) < int(first_pay[key]["payment_sequential"]):
                first_pay[key] = row
        types = Counter(first_pay[order["order_id"]]["payment_type"]
                        for order in self.tables["orders"]
                        if self.month_of(order["order_id"]) == month and order["order_id"] in first_pay)
        if not types:
            self.skipTest("合成数据目标月没有支付")
        value = types.most_common(1)[0][0]
        spec = {"type": "mix_shift", "month": month, "dimension": "payment_type", "value": value}

        def group_stats(tables):
            orders = {row["order_id"]: row for row in tables["orders"]}
            pays = {}
            for row in tables["order_payments"]:
                key = row["order_id"]
                if key not in pays or int(row["payment_sequential"]) < int(pays[key]["payment_sequential"]):
                    pays[key] = row
            amount = Counter()
            for row in tables["order_payments"]:
                stamp = orders[row["order_id"]]["order_purchase_timestamp"]
                if stamp[:7] == month:
                    amount[pays[row["order_id"]]["payment_type"]] += int(row["payment_value_cents"])
            count = Counter(pays[order["order_id"]]["payment_type"]
                            for order in tables["orders"]
                            if order["order_purchase_timestamp"][:7] == month and order["order_id"] in pays)
            return amount, count

        amount0, count0 = group_stats(self.tables)
        after = injector.inject(self.tables, spec, injector.rng_for("ms"))
        amount1, count1 = group_stats(after)
        # 目标组订单数与金额精确翻倍；其余组不变；各组组内客单价精确不变。
        self.assertEqual(count1[value], count0[value] * 2)
        self.assertEqual(amount1[value], amount0[value] * 2)
        for other in set(count0) | set(count1):
            if other != value:
                self.assertEqual(count1[other], count0[other])
                self.assertEqual(amount1[other], amount0[other])
        for group in set(count0) & set(count1):
            if count0[group] and count1[group]:
                self.assertEqual(amount0[group] / count0[group], amount1[group] / count1[group],
                                 f"{group} 组内客单价应精确不变")
        # 复制的订单使用新 order_id/customer_id/customer_unique_id。
        original_orders = {row["order_id"] for row in self.tables["orders"]}
        clones = [row for row in after["orders"] if row["order_id"] not in original_orders]
        self.assertEqual(len(clones), count0[value])
        original_customers = {row["customer_id"] for row in self.tables["customers"]}
        for clone in clones:
            self.assertNotIn(clone["customer_id"], original_customers)
        unique_ids = {row["customer_unique_id"] for row in self.tables["customers"]}
        new_customers = {row["customer_unique_id"] for row in after["customers"]} - unique_ids
        self.assertEqual(len(new_customers), len(clones))


class PriceCutTests(Fixture):
    def test_price_cut_only_target_category_month(self):
        month = self.target_month()
        categories = sorted({self.category_of[item["product_id"]] for item in self.tables["order_items"]})
        category = categories[0]
        spec = {"type": "price_cut", "month": month, "product_category": category, "factor": 0.7}
        after = injector.inject(self.tables, spec, injector.rng_for("pc"))
        before_items = {(item["order_id"], item["order_item_id"]): item for item in self.tables["order_items"]}
        changed = 0
        for item in after["order_items"]:
            old = before_items[(item["order_id"], item["order_item_id"])]
            in_target = self.month_of(item["order_id"]) == month \
                and self.category_of[item["product_id"]] == category
            if in_target and int(old["price_cents"]) > 1:
                self.assertLess(int(item["price_cents"]), int(old["price_cents"]))
                changed += 1
            else:
                self.assertEqual(item["price_cents"], old["price_cents"], "非目标组价格不变")
            self.assertEqual(item["freight_cents"], old["freight_cents"], "运费不受 price_cut 影响")
        self.assertTrue(changed, "目标组应有价格下调")


class SellerDropTests(Fixture):
    def test_seller_drop_removes_items_and_empty_orders(self):
        month = self.target_month()
        from collections import Counter
        counts = Counter(item["seller_id"] for item in self.tables["order_items"]
                         if self.month_of(item["order_id"]) == month)
        if not counts:
            self.skipTest("合成数据目标月没有明细")
        seller = counts.most_common(1)[0][0]
        spec = {"type": "seller_drop", "month": month, "seller_id": seller}
        after = injector.inject(self.tables, spec, injector.rng_for("sd"))
        # 目标月内该卖家的明细全部删除。
        for item in after["order_items"]:
            if self.month_of(item["order_id"]) == month:
                self.assertNotEqual(item["seller_id"], seller)
        # 其余月份的该卖家明细保留。
        kept = sum(1 for item in after["order_items"]
                   if item["seller_id"] == seller and self.month_of(item["order_id"]) != month)
        before_kept = sum(1 for item in self.tables["order_items"]
                          if item["seller_id"] == seller and self.month_of(item["order_id"]) != month)
        self.assertEqual(kept, before_kept)
        # 不含其他明细的订单整单删除（子行一并删除）。
        removed_orders = {row["order_id"] for row in self.tables["orders"]} \
            - {row["order_id"] for row in after["orders"]}
        self.assertTrue(removed_orders)
        for name in ("order_items", "order_payments", "order_reviews"):
            self.assertEqual({row["order_id"] for row in after[name]} & removed_orders, set())


class MaterializeTests(Fixture):
    def test_materialize_writes_readable_folder(self):
        month = self.target_month()
        case = {"id": "test-case",
                "injection": {"type": "price_cut", "month": month,
                              "product_category": sorted({self.category_of[i["product_id"]]
                                                          for i in self.tables["order_items"]})[0],
                              "factor": 0.7}}
        scratch = (ROOT / ".tmp").resolve()
        with tempfile.TemporaryDirectory(dir=scratch, prefix="anomaly-mat-") as temp:
            root = Path(temp)
            store = DatasetStore(root / "datasets")
            version = store.import_olist(write(root / "raw", bundle(random.Random(41), 40)),
                                         project_id="p", dataset_family="olist")
            folder = store.directory(version.dataset_version)
            target = injector.materialize(case, folder, root / "out")
            # 物化目录可读：八张表齐全，metadata 带注入说明。
            tables = olist.read_tables(target)
            self.assertEqual(set(tables), set(olist.TABLES))
            import json
            metadata = json.loads((target / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["profile"]["anomaly_injection"]["case"], "test-case")
            # 确定性：再物化一次，逐表一致。
            target2 = injector.materialize(case, folder, root / "out2")
            self.assertEqual(olist.read_tables(target2), tables)


class FrozenCasesTests(unittest.TestCase):
    """冻结题集文件的加载校验（只读仓库中的 anomaly_cases.json）。"""

    def test_load_and_validate(self):
        data = cases_module.load()
        self.assertEqual(len(data["cases"]), 40)
        dev = cases_module.by_split(data, "dev")
        test = cases_module.by_split(data, "test")
        self.assertEqual((len(dev), len(test)), (10, 30))
        self.assertEqual(sum(case["kind"] == "injected" for case in dev), 7)
        self.assertEqual(sum(case["kind"] == "raw" for case in dev), 3)
        # dev 覆盖全部六类注入类型。
        self.assertEqual({case["injection_type"] for case in dev if case["kind"] == "injected"},
                         set(cases_module.INJECTION_TYPES))
        self.assertIn("source_sha256", data["meta"])
        self.assertEqual(len(data["meta"]["source_sha256"]), 8)

    def test_validate_rejects_bad_counts(self):
        data = cases_module.load()
        broken = {"meta": data["meta"], "cases": data["cases"][:10]}
        with self.assertRaises(cases_module.CasesError):
            cases_module.validate(broken)


# 问题文本中的方向词与 gold.direction 的对应规则（规则匹配即可）。
DIRECTION_WORDS = {"up": ("上升", "变长"), "down": ("下降", "回落", "改善", "缩短")}


class FrozenQualityTests(unittest.TestCase):
    """冻结题集的构造质量检查（revision 2）：方向词一致、EP 区间、显著阈值、unit 与语义层一致。"""

    @classmethod
    def setUpClass(cls):
        cls.data = cases_module.load()
        cls.selection = cls.data["meta"]["raw_selection"]

    def test_revision_recorded(self):
        self.assertEqual(self.data["meta"]["frozen_at"], "2026-10-27")
        self.assertEqual(self.data["meta"]["revision"], 3)
        self.assertEqual(len(self.data["meta"]["revision_history"]), 3)

    def test_question_direction_matches_gold(self):
        for case in self.data["cases"]:
            with self.subTest(case=case["id"]):
                direction = case["gold"]["direction"]
                words = DIRECTION_WORDS[direction]
                opposite = DIRECTION_WORDS["down" if direction == "up" else "up"]
                self.assertTrue(any(word in case["question"] for word in words)
                                or not any(word in case["question"] for word in opposite),
                                f"{case['id']} 问题方向与 gold.direction={direction} 矛盾：{case['question']}")
                # 注入案例的问题一律不陈述方向（环比变化）。
                if case["kind"] == "injected":
                    self.assertNotIn("上升", case["question"])
                    self.assertNotIn("下降", case["question"])
                    self.assertNotIn("回落", case["question"])

    def test_raw_top_ep_in_range_and_significant(self):
        ep_min, ep_max = self.selection["ep_range"]
        for case in self.data["cases"]:
            if case["kind"] != "raw":
                continue
            with self.subTest(case=case["id"]):
                ep = case["gold"]["top_ep"]
                self.assertGreaterEqual(ep, ep_min)
                self.assertLessEqual(ep, ep_max)
                # 总变化显著：加法型 |环比变化率| ≥ min_rel_change；比率型 |变化| ≥ 指标阈值。
                if case["metric"] in ("gmv", "item_gmv", "order_count"):
                    self.assertGreaterEqual(abs(case["gold"]["change_rate"]), self.selection["min_rel_change"])
                else:
                    floor = self.selection["ratio_min_change"][case["metric"]]
                    self.assertGreaterEqual(abs(float(case["gold"]["total_change"])), floor)

    def test_gold_unit_matches_semantic_layer(self):
        sys.path.insert(0, str(ROOT / "src"))
        from datalab.semantic.metrics import get_metric
        for case in self.data["cases"]:
            with self.subTest(case=case["id"]):
                self.assertEqual(case["gold"]["unit"], get_metric(case["metric"]).unit)

    def test_injected_total_change_direction_matches_gold(self):
        for case in self.data["cases"]:
            if case["kind"] != "injected":
                continue
            with self.subTest(case=case["id"]):
                change = float(case["total_change"])
                self.assertNotEqual(change, 0.0)
                actual = "down" if change < 0 else "up"
                self.assertEqual(actual, case["gold"]["direction"],
                                 f"{case['id']} 物化后总变化方向 {actual} 与 gold.direction 不一致")


class ScoreTests(unittest.TestCase):
    def case(self, **gold):
        return {"id": "c1", "kind": "injected", "injection_type": "price_cut", "metric": "item_gmv",
                "gold": {"dimensions": [{"dimension": "product_category", "value": "health_beauty"}],
                         **gold}}

    def test_ac1_and_ac3(self):
        case = self.case()
        hit1 = [{"dimension": "product_category", "value": "health_beauty"}]
        self.assertEqual(score_module.score(case, hit1), {"ac1": 1, "ac3": 1, "contribution_error": None})
        hit3 = [{"dimension": "product_category", "value": "x"},
                {"dimension": "product_category", "value": "y"},
                {"dimension": "product_category", "value": "health_beauty"}]
        self.assertEqual(score_module.score(case, hit3)["ac1"], 0)
        self.assertEqual(score_module.score(case, hit3)["ac3"], 1)
        miss = [{"dimension": "product_category", "value": "z"}]
        self.assertEqual(score_module.score(case, miss)["ac3"], 0)

    def test_two_dimensional_any_hit(self):
        # order_drop 的二维根因：任一 gold 维度值命中即算（规则写死在 score 模块说明中）。
        case = {"id": "c2", "kind": "injected", "injection_type": "order_drop", "metric": "item_gmv",
                "gold": {"dimensions": [{"dimension": "product_category", "value": "bed_bath_table"},
                                        {"dimension": "customer_state", "value": "SP"}]}}
        by_state = [{"dimension": "customer_state", "value": "SP"}]
        self.assertEqual(score_module.score(case, by_state)["ac1"], 1)
        by_category = [{"dimension": "product_category", "value": "bed_bath_table"}]
        self.assertEqual(score_module.score(case, by_category)["ac1"], 1)
        wrong_pair = [{"dimension": "customer_state", "value": "bed_bath_table"}]
        self.assertEqual(score_module.score(case, wrong_pair)["ac1"], 0)

    def test_effect_must_match_when_gold_specifies(self):
        case = {"id": "c3", "kind": "injected", "injection_type": "mix_shift", "metric": "aov",
                "gold": {"dimensions": [{"dimension": "payment_type", "value": "boleto"}],
                         "effect": "between"}}
        within = [{"dimension": "payment_type", "value": "boleto", "effect": "within"}]
        self.assertEqual(score_module.score(case, within)["ac1"], 0)
        between = [{"dimension": "payment_type", "value": "boleto", "effect": "between"}]
        self.assertEqual(score_module.score(case, between)["ac1"], 1)
        no_effect = [{"dimension": "payment_type", "value": "boleto"}]
        self.assertEqual(score_module.score(case, no_effect)["ac1"], 1)

    def test_contribution_error(self):
        case = self.case(magnitude=-100)
        candidates = [{"dimension": "product_category", "value": "health_beauty", "contribution": -120}]
        self.assertEqual(score_module.score(case, candidates)["contribution_error"], 20.0)
        summary = score_module.summarize([score_module.score(case, candidates),
                                          score_module.score(case, [])])
        self.assertEqual(summary["cases"], 2)
        self.assertEqual(summary["ac1"], 0.5)
        self.assertEqual(summary["contribution_mae"], 20.0)


if __name__ == "__main__":
    unittest.main()
