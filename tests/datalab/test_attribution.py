"""归因算法（A2）：手算小样本校验、合成数据不变量、语义层与独立参考两条路径一致（L1）。

手算小样本用最小假数据源（直接返回手写的语义层结构结果）：Delta 法、组间组内（含一期
缺席的组）、LMDI（V0=100=10×10、V1=150=12×12.5）、MAD z 分数、Adtributor 选元素与排序、
Pearson/Spearman 已知值。不变量在 olist_bundle 合成数据（多种子）上跑 contribution 与
decompose_metric，Σ = 总变化。L1：同一合成数据上 SemanticEngine 与 ReferenceSource 结果
一致（verify_attribution passed），篡改结果被检出。
"""
import math
import random
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from olist_bundle import bundle, write
from datalab.attribution import (ReferenceSource, contribution, correlate, decompose_metric,
                                 detect_anomaly, find_root_cause, verify_attribution)
from datalab.datasets.service import DatasetStore
from datalab.semantic.engine import SemanticEngine
from datalab.semantic.metrics import get_metric
from datalab.semantic.snapshot import SemanticSnapshot

ROOT = Path(__file__).resolve().parents[2]
STATUSES = ("delivered", "shipped", "invoiced", "canceled")
SNAPSHOT = SemanticSnapshot("test-v1", STATUSES, "payments")


class FakeSource:
    """最小假数据源：按 (metric, period, group_by) 返回手写的语义层结构结果。

    未登记的期间返回该指标的 0 值空结果（语义层对无数据期间的正常行为）；
    手算样例只登记有数据的期间，未覆盖的历史期按无数据处理。
    """

    def __init__(self, results: dict):
        self.results = results

    def query(self, metric, period, compare_period=None, filters=None, group_by=None):
        if (metric, period, group_by) not in self.results:
            return fake_run(metric, period, (0, 0) if get_metric(metric).kind == "ratio" else 0)
        return self.results[(metric, period, group_by)]


def fake_run(metric: str, period, total, rows=(), kind="additive", unit="count", grain="order"):
    """构造一次查询结果；total/rows 的 value 由分子分母按 6 位小数给出。"""
    def assemble(amounts):
        numerator, denominator = amounts if isinstance(amounts, tuple) else (amounts, None)
        if kind == "additive":
            return {"numerator": numerator, "denominator": None, "value": numerator}
        value = None if not denominator else f"{numerator / denominator:.6f}"
        return {"numerator": numerator, "denominator": denominator, "value": value}

    return {"metric": metric, "grain": grain, "type": kind, "unit": unit,
            "period": {"start": period[0], "end": period[1]}, "total": assemble(total),
            "rows": [{"group": group, **assemble(amounts)} for group, amounts in rows]}


BASE = ("2017-01-01T00:00:00", "2017-02-01T00:00:00")
CUR = ("2017-02-01T00:00:00", "2017-03-01T00:00:00")


class ContributionHandTests(unittest.TestCase):
    """Delta 法与组间组内拆解的手算小样本。"""

    def test_delta_method(self):
        source = FakeSource({
            ("gmv", BASE, "customer_state"): fake_run("gmv", BASE, 2000,
                                                      [("RJ", 500), ("SP", 1500)], unit="cents"),
            ("gmv", CUR, "customer_state"): fake_run("gmv", CUR, 3000,
                                                     [("MG", 300), ("RJ", 700), ("SP", 2000)],
                                                     unit="cents"),
        })
        result = contribution(source, "gmv", "customer_state", CUR, BASE)
        self.assertEqual(result["method"], "delta")
        self.assertEqual(result["base_total"], 2000)
        self.assertEqual(result["current_total"], 3000)
        self.assertEqual(result["total_change"], 1000)
        self.assertEqual(result["change_rate"], "0.500000")
        rows = {row["group"]: row for row in result["rows"]}
        # 排序：|change| 降序（SP 500 > MG 300 > RJ 200）。
        self.assertEqual([row["group"] for row in result["rows"]], ["SP", "MG", "RJ"])
        self.assertEqual(rows["SP"]["change"], 500)
        self.assertEqual(rows["SP"]["contribution"], "0.250000")
        self.assertEqual(rows["SP"]["share_of_change"], "0.500000")
        self.assertEqual(rows["MG"]["base"], 0)  # 基期缺席按 0
        self.assertEqual(rows["RJ"]["contribution"], "0.100000")
        # 不变量：Σchange = 总变化，Σcontribution = 总变化率，精确成立。
        self.assertTrue(result["invariant"]["passed"])
        self.assertEqual(result["invariant"]["residual"], 0)
        total_contribution = sum(Fraction(row["contribution"]) for row in result["rows"])
        self.assertEqual(total_contribution, Fraction("0.5"))

    def test_delta_no_change(self):
        source = FakeSource({
            ("gmv", BASE, "customer_state"): fake_run("gmv", BASE, 1000, [("SP", 1000)]),
            ("gmv", CUR, "customer_state"): fake_run("gmv", CUR, 1000, [("SP", 1000)]),
        })
        result = contribution(source, "gmv", "customer_state", CUR, BASE)
        self.assertEqual(result["status"], "no_change")
        self.assertEqual(result["rows"][0]["share_of_change"], None)
        self.assertTrue(result["invariant"]["passed"])

    def test_between_within_with_absent_group(self):
        # 手算：R0 = 900/3000 = 0.3，R1 = 1500/2500 = 0.6；C 本期缺席（w=0、r 取另一期）。
        # A: structure 11/75≈0.146667、within 7/150≈0.046667；B: −0.04、0.18；C: −1/30、0。
        source = FakeSource({
            ("bad_review_rate", BASE, "customer_state"): fake_run(
                "bad_review_rate", BASE, (900, 3000),
                [("A", (500, 1000)), ("B", (300, 1500)), ("C", (100, 500))], kind="ratio",
                unit="ratio", grain="review"),
            ("bad_review_rate", CUR, "customer_state"): fake_run(
                "bad_review_rate", CUR, (1500, 2500),
                [("A", (900, 1500)), ("B", (600, 1000))], kind="ratio", unit="ratio",
                grain="review"),
        })
        result = contribution(source, "bad_review_rate", "customer_state", CUR, BASE)
        self.assertEqual(result["method"], "between_within")
        self.assertEqual(result["base_total"], "0.300000")
        self.assertEqual(result["current_total"], "0.600000")
        self.assertEqual(result["total_change"], "0.300000")
        rows = {row["group"]: row for row in result["rows"]}
        self.assertEqual(rows["A"]["structure"], f"{11/75:.6f}")
        self.assertEqual(rows["A"]["within"], f"{7/150:.6f}")
        self.assertEqual(rows["B"]["structure"], "-0.040000")
        self.assertEqual(rows["B"]["within"], "0.180000")
        # 缺席组：全部变化计入结构项，within 为 0；缺席期比值为 null。
        self.assertEqual(rows["C"]["structure"], f"{-1/30:.6f}")
        self.assertEqual(rows["C"]["within"], "0.000000")
        self.assertEqual(rows["C"]["current"], None)
        self.assertEqual(rows["C"]["base"], "0.200000")
        # 排序：|structure+within| 降序（A 0.193333 > B 0.14 > C 0.033333）。
        self.assertEqual([row["group"] for row in result["rows"]], ["A", "B", "C"])
        # 不变量：Σ(structure+within) = R1−R0 精确成立。
        self.assertTrue(result["invariant"]["passed"])
        total = sum(Fraction(row["change"]) for row in result["rows"])
        self.assertEqual(total, Fraction("0.3"))

    def test_delta_zero_base_total(self):
        source = FakeSource({
            ("gmv", BASE, "customer_state"): fake_run("gmv", BASE, 0, []),
            ("gmv", CUR, "customer_state"): fake_run("gmv", CUR, 100, [("SP", 100)]),
        })
        result = contribution(source, "gmv", "customer_state", CUR, BASE)
        self.assertEqual(result["rows"][0]["contribution"], None)  # 基期总量 0：相对贡献无意义
        self.assertEqual(result["rows"][0]["share_of_change"], "1.000000")
        self.assertEqual(result["change_rate"], None)


class DecomposeHandTests(unittest.TestCase):
    """LMDI 与加法拆分的手算小样本（V0=100=10×10，V1=150=12×12.5）。"""

    def source(self):
        return FakeSource({
            ("gmv", BASE, None): fake_run("gmv", BASE, 100, unit="cents"),
            ("gmv", CUR, None): fake_run("gmv", CUR, 150, unit="cents"),
            ("order_count", BASE, None): fake_run("order_count", BASE, 10),
            ("order_count", CUR, None): fake_run("order_count", CUR, 12),
            ("aov", BASE, None): fake_run("aov", BASE, (100, 10), kind="ratio", unit="cents"),
            ("aov", CUR, None): fake_run("aov", CUR, (150, 12), kind="ratio", unit="cents"),
            ("order_count", BASE, "customer_type"): fake_run(
                "order_count", BASE, 10, [("new", 4), ("returning", 6)]),
            ("order_count", CUR, "customer_type"): fake_run(
                "order_count", CUR, 12, [("new", 9), ("returning", 3)]),
        })

    def test_lmdi_gmv(self):
        result = decompose_metric(self.source(), "gmv", CUR, BASE)
        self.assertEqual(result["kind"], "multiplicative")
        self.assertEqual(result["method"], "lmdi")
        self.assertEqual(result["total_change"], 50.0)
        factors = {row["metric"]: row for row in result["factors"]}
        log_mean = 50 / math.log(1.5)
        self.assertAlmostEqual(factors["order_count"]["contribution"],
                               log_mean * math.log(1.2), places=9)
        self.assertAlmostEqual(factors["aov"]["contribution"], log_mean * math.log(1.25), places=9)
        self.assertAlmostEqual(factors["order_count"]["share"],
                               math.log(1.2) / math.log(1.5), places=9)
        # 不变量：Σ贡献 = V1−V0（浮点残差随结果输出）。
        self.assertTrue(result["invariant"]["passed"])
        self.assertAlmostEqual(sum(row["contribution"] for row in result["factors"]), 50.0, places=9)

    def test_lmdi_ratio_parent_uses_inverse_denominator(self):
        # aov = gmv × order_count^(−1)：分母因子贡献取符号相反的对数项。
        result = decompose_metric(self.source(), "aov", CUR, BASE)
        self.assertEqual(result["method"], "lmdi")
        log_mean = (12.5 - 10) / math.log(1.25)
        factors = {row["metric"]: row for row in result["factors"]}
        self.assertAlmostEqual(factors["gmv"]["contribution"], log_mean * math.log(1.5), places=9)
        self.assertAlmostEqual(factors["order_count"]["contribution"],
                               -log_mean * math.log(1.2), places=9)
        self.assertTrue(result["invariant"]["passed"])
        self.assertAlmostEqual(sum(row["contribution"] for row in result["factors"]), 2.5, places=9)

    def test_lmdi_undefined_on_zero_factor(self):
        source = self.source()
        source.results[("order_count", BASE, None)] = fake_run("order_count", BASE, 0)
        result = decompose_metric(source, "gmv", CUR, BASE)
        self.assertEqual(result["status"], "undefined")
        self.assertEqual(result["factors"], [])

    def test_additive_split(self):
        result = decompose_metric(self.source(), "order_count", CUR, BASE)
        self.assertEqual(result["kind"], "additive")
        self.assertEqual(result["method"], "additive")
        self.assertEqual(result["total_change"], 2)
        factors = {row["metric"]: row for row in result["factors"]}
        self.assertEqual(factors["order_count[customer_type=new]"]["contribution"], 5)
        self.assertEqual(factors["order_count[customer_type=returning]"]["contribution"], -3)
        self.assertTrue(result["invariant"]["passed"])
        self.assertEqual(result["invariant"]["residual"], 0)


class DetectAnomalyHandTests(unittest.TestCase):
    """MAD z 分数、环比、同比与状态的手算小样本。"""

    def source(self, monthly: list[int], year: int = 2016):
        """monthly 为自 year-01 起各月值；FakeSource 未登记的期间按无数据（0 值）处理。"""

        def month(index):
            return f"{year + index // 12}-{index % 12 + 1:02d}"

        results = {}
        for index, value in enumerate(monthly):
            period = (f"{month(index)}-01T00:00:00", f"{month(index + 1)}-01T00:00:00")
            results[("order_count", period, None)] = fake_run("order_count", period, value)
        return FakeSource(results)

    def test_robust_z_and_mom_yoy(self):
        # 历史 2017-01..06 = [10,12,11,13,12,11]，本期 2017-07 = 40。
        source = self.source([10, 12, 11, 13, 12, 11], year=2017)
        period = ("2017-07-01T00:00:00", "2017-08-01T00:00:00")
        source.results[("order_count", period, None)] = fake_run("order_count", period, 40)
        result = detect_anomaly(source, "order_count", period, history=6)
        self.assertEqual(result["value"], 40)
        self.assertEqual(len(result["history"]), 6)
        # 历史期按日历月平移：最早 2017-01，紧邻上一期 2017-06。
        self.assertEqual(result["history"][0]["period"]["start"], "2017-01-01T00:00:00")
        self.assertEqual(result["history"][-1]["period"]["start"], "2017-06-01T00:00:00")
        # 环比：与 2017-06 的 11 比。
        self.assertEqual(result["mom"]["base"], 11)
        self.assertEqual(result["mom"]["change"], 29)
        # 同比：2016-07 无数据（历史未覆盖）为 null。
        self.assertEqual(result["yoy"], None)
        # 中位数 11.5、MAD 0.5；z = 0.6745×28.5/0.5 = 38.4465，判异动。
        self.assertEqual(result["median"], "11.500000")
        self.assertEqual(result["mad"], "0.500000")
        self.assertAlmostEqual(result["robust_z"], 38.4465, places=4)
        self.assertTrue(result["is_anomaly"])
        self.assertEqual(result["status"], "ok")

    def test_yoy_when_covered(self):
        # 覆盖 2016-01..2017-06 共 18 个月；history=6 时历史窗口只到 2017-01，去年同期
        # 2016-07 不在窗口内但有数据：仍给出同比（同比按去年同期另查一次，不要求在窗口内，
        # 只要求不早于窗口起点——2016-07 < 2017-01，因此为 null）。history=12 时窗口到
        # 2016-07，去年同期可比。
        source = self.source([10] * 12 + [12] * 6 + [13])
        period = ("2017-07-01T00:00:00", "2017-08-01T00:00:00")
        result = detect_anomaly(source, "order_count", period, history=12)
        self.assertEqual(result["yoy"]["base"], 10)
        self.assertEqual(result["yoy"]["change"], 3)
        self.assertEqual(result["yoy"]["period"]["start"], "2016-07-01T00:00:00")
        result = detect_anomaly(source, "order_count", period, history=6)
        self.assertEqual(result["yoy"], None)

    def test_data_floor_trims_history(self):
        # history 窗口起点早于 floor 时裁剪：data_floor=2017-01 时，2016 各月全部丢弃。
        source = self.source([10] * 12 + [12] * 6)
        source.data_floor = "2017-01-01T00:00:00"
        period = ("2017-07-01T00:00:00", "2017-08-01T00:00:00")
        source.results[("order_count", period, None)] = fake_run("order_count", period, 30)
        result = detect_anomaly(source, "order_count", period, history=12)
        self.assertEqual(len(result["history"]), 6)
        self.assertEqual(result["history"][0]["period"]["start"], "2017-01-01T00:00:00")
        self.assertEqual(result["status"], "zero_mad")  # 裁剪后 6 期全为 12，MAD=0
        # 数据源声明最早可用起点 data_floor：起点早于它的历史期按「还没有这份数据」丢弃，
        # 不计入有效历史（语义层对没有数据的期间返回 0，归因需要靠 floor 区分）。
        # 数据 2017-07 才开始：period 2017-09 的历史窗口裁剪后只剩 2017-07、2017-08 两期。
        source = self.source([10, 11], year=2017)
        source.data_floor = "2017-07-01T00:00:00"
        period = ("2017-09-01T00:00:00", "2017-10-01T00:00:00")
        source.results[("order_count", period, None)] = fake_run("order_count", period, 50)
        result = detect_anomaly(source, "order_count", period, history=6)
        self.assertEqual(result["status"], "insufficient_history")
        self.assertFalse(result["is_anomaly"])
        self.assertEqual(result["robust_z"], None)
        # MAD 为 0：历史相同，偏离即异动，z 为 null。
        source = self.source([10, 10, 10, 10, 10, 10], year=2017)
        source.data_floor = "2017-01-01T00:00:00"
        period = ("2017-08-01T00:00:00", "2017-09-01T00:00:00")
        source.results[("order_count", period, None)] = fake_run("order_count", period, 25)
        result = detect_anomaly(source, "order_count", period, history=6)
        self.assertEqual(result["status"], "zero_mad")
        self.assertTrue(result["is_anomaly"])
        self.assertEqual(result["robust_z"], None)

    def test_non_month_aligned_uses_timedelta(self):
        # 非月初零点：按时间差平移。period 为 10 天，历史期逐个前移 10 天。
        results = {}
        for index, value in enumerate([100] * 6 + [150]):
            start_day = 1 + 10 * index
            results[("order_count", (f"2017-01-{start_day:02d}T00:00:00",
                                     f"2017-01-{start_day + 10:02d}T00:00:00"), None)] = \
                fake_run("order_count", (f"2017-01-{start_day:02d}T00:00:00",
                                         f"2017-01-{start_day + 10:02d}T00:00:00"), value)
        source = FakeSource(results)
        result = detect_anomaly(source, "order_count",
                                ("2017-01-61T00:00:00".replace("01-61", "03-01"),
                                 "2017-03-11T00:00:00"), history=6)
        self.assertEqual(len(result["history"]), 6)
        self.assertEqual(result["history"][-1]["period"]["end"], "2017-03-01T00:00:00")


class FindRootCauseHandTests(unittest.TestCase):
    """Adtributor 选元素、维度排序与扁平候选的手算小样本。"""

    def test_selection_and_ranking(self):
        # 加法型 gmv：基期 SP=800、RJ=200；本期 SP=1200、RJ=100。总变化 +300。
        # EP：SP = 400/300 = 4/3（>1，单组可超过 1），RJ = −100/300 = −1/3，ΣEP = 1。
        # 份额：SP 0.8→0.9231，RJ 0.2→0.0769；S 按 JS 散度项，RJ 相对变化更大、S 更高。
        source = FakeSource({
            ("gmv", BASE, None): fake_run("gmv", BASE, 1000, unit="cents"),
            ("gmv", CUR, None): fake_run("gmv", CUR, 1300, unit="cents"),
            ("gmv", BASE, "customer_state"): fake_run("gmv", BASE, 1000, [("RJ", 200), ("SP", 800)],
                                                      unit="cents"),
            ("gmv", CUR, "customer_state"): fake_run("gmv", CUR, 1300, [("RJ", 100), ("SP", 1200)],
                                                     unit="cents"),
            ("gmv", BASE, "payment_type"): fake_run("gmv", BASE, 1000, [("boleto", 1000)],
                                                    unit="cents"),
            ("gmv", CUR, "payment_type"): fake_run("gmv", CUR, 1300, [("boleto", 1300)],
                                                   unit="cents"),
        })
        result = find_root_cause(source, "gmv", ["customer_state", "payment_type"], CUR, BASE,
                                 tep=0.8, teep=0.1)
        self.assertEqual(result["total_change"], 300)
        states = {entry["dimension"]: entry for entry in result["dimensions"]}
        elements = {item["value"]: item for item in states["customer_state"]["elements"]}
        self.assertEqual(elements["SP"]["explanatory_power"], f"{4/3:.6f}")
        self.assertEqual(elements["RJ"]["explanatory_power"], f"{-1/3:.6f}")
        # ΣEP = 1 精确成立。
        total_ep = sum(Fraction(item["explanatory_power"])
                       for item in states["customer_state"]["elements"])
        self.assertEqual(total_ep, 1)
        # 惊奇度：payment_type 份额不变（S=0）；customer_state 有变化（S>0），维度排第一。
        self.assertEqual(states["payment_type"]["surprise"], 0.0)
        self.assertGreater(states["customer_state"]["surprise"], 0.0)
        self.assertEqual(result["dimensions"][0]["dimension"], "customer_state")
        # 选元素：元素按 S 降序，RJ 的 S 高于 SP；EP ≥ teep 加入，累计 ≥ tep 停止。
        # RJ 的 EP 为负、达不到 teep，跳过；SP 加入（EP 4/3 ≥ 0.8 后停止）。
        self.assertEqual([item["value"] for item in states["customer_state"]["elements"]],
                         ["RJ", "SP"])
        # 扁平候选：customer_state 的 SP 在前；payment_type 无元素满足 teep（EP=1 但 S=0 也
        # 参与：EP 1 ≥ 0.1，加入后 explained=1 ≥ 0.8）。
        self.assertEqual(result["candidates"][0]["dimension"], "customer_state")
        self.assertEqual(result["candidates"][0]["value"], "SP")
        self.assertEqual(result["candidates"][0]["explanatory_power"], f"{4/3:.6f}")

    def test_no_change_returns_empty(self):
        source = FakeSource({
            ("gmv", BASE, None): fake_run("gmv", BASE, 1000),
            ("gmv", CUR, None): fake_run("gmv", CUR, 1000),
        })
        result = find_root_cause(source, "gmv", ["customer_state"], CUR, BASE)
        self.assertEqual(result["status"], "no_change")
        self.assertEqual(result["candidates"], [])


class CorrelateHandTests(unittest.TestCase):
    """Pearson / Spearman 已知值与样本量边界。"""

    def test_known_values_with_ties(self):
        # x = [1,2,3,4]，y = [1,2,2,4]：Pearson 手算；Spearman 平均秩（y 的 2,2 并列秩 2.5）。
        rows_a = [(f"g{index}", value) for index, value in enumerate((1, 2, 3, 4))]
        rows_b = [(f"g{index}", value) for index, value in enumerate((1, 2, 2, 4))]
        source = FakeSource({
            ("order_count", CUR, "customer_state"): fake_run("order_count", CUR, 10, rows_a),
            ("item_count", CUR, "customer_state"): fake_run("item_count", CUR, 9, rows_b,
                                                            grain="item"),
        })
        result = correlate(source, "order_count", "item_count", "customer_state", CUR)
        xs, ys = [1, 2, 3, 4], [1, 2, 2, 4]
        mean_x, mean_y = 2.5, 2.25
        cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        expected = cov / math.sqrt(sum((x - mean_x) ** 2 for x in xs)
                                   * sum((y - mean_y) ** 2 for y in ys))
        self.assertEqual(result["n"], 4)
        self.assertAlmostEqual(result["pearson"], expected, places=6)
        # Spearman 平均秩：y 的 2,2 并列秩 2.5；x 的秩 [1,2,3,4]，y 的秩 [1,2.5,2.5,4]。
        rank_x, rank_y = [1, 2, 3, 4], [1, 2.5, 2.5, 4]
        mean_rx, mean_ry = 2.5, 2.5
        cov_r = sum((x - mean_rx) * (y - mean_ry) for x, y in zip(rank_x, rank_y))
        expected_r = cov_r / math.sqrt(sum((x - mean_rx) ** 2 for x in rank_x)
                                       * sum((y - mean_ry) ** 2 for y in rank_y))
        self.assertAlmostEqual(result["spearman"], expected_r, places=6)
        self.assertIn("相关不代表因果", result["note"])

    def test_small_sample_and_missing_groups(self):
        source = FakeSource({
            ("order_count", CUR, "customer_state"): fake_run("order_count", CUR, 3,
                                                             [("RJ", 1), ("SP", 2)]),
            ("item_count", CUR, "customer_state"): fake_run("item_count", CUR, 5,
                                                            [("MG", 2), ("SP", 3)], grain="item"),
        })
        result = correlate(source, "order_count", "item_count", "customer_state", CUR)
        # 交集只有 SP（n=1 < 3）：系数为 null。
        self.assertEqual(result["n"], 1)
        self.assertEqual(result["pearson"], None)
        self.assertEqual(result["spearman"], None)


class SyntheticFixture(unittest.TestCase):
    """olist_bundle 合成数据：导入数据版本，给引擎与独立参考两条路径。"""

    def setUp(self):
        scratch = (ROOT / ".tmp").resolve()
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch, prefix="attribution-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = DatasetStore(self.root / "datasets")

    def dataset(self, tables, name="raw"):
        version = self.store.import_olist(write(self.root / name, tables), project_id="demo",
                                          dataset_family="olist")
        return self.store.directory(version.dataset_version)


class InvariantTests(SyntheticFixture):
    """多种子合成数据上 contribution 与 decompose_metric 的 Σ = 总变化不变量。"""

    def test_contribution_invariant_across_metrics_and_dimensions(self):
        period = ("2017-03-01T00:00:00", "2017-04-01T00:00:00")
        base = ("2017-02-01T00:00:00", "2017-03-01T00:00:00")
        checked = 0
        for seed in range(6):
            rng = random.Random(1000 + seed)
            folder = self.dataset(bundle(rng, rng.choice([60, 120]), months=6), f"inv{seed}")
            engine = SemanticEngine(folder, SNAPSHOT)
            try:
                combos = [("gmv", "customer_state"), ("gmv", "purchase_month"),
                          ("order_count", "customer_type"), ("item_gmv", "product_category"),
                          ("item_gmv", "seller_state"), ("item_count", "price_band"),
                          ("bad_review_rate", "customer_state"), ("aov", "customer_state"),
                          ("late_delivery_rate", "customer_state"), ("repeat_rate", "customer_state")]
                for metric, dimension in combos:
                    with self.subTest(seed=seed, metric=metric, dimension=dimension):
                        result = contribution(engine, metric, dimension, period, base)
                        self.assertTrue(result["invariant"]["passed"],
                                        f"{metric}/{dimension} 残差 {result['invariant']['residual']}")
                        # 分组行之和等于总变化（精确）；不为 0 时各组 share 之和为 1
                        # （6 位小数字符串各自舍入，合计允许行数级的 1e-6 尾差）。
                        if result["total_change"] not in (0, "0.000000"):
                            shares = [Fraction(row["share_of_change"]) for row in result["rows"]
                                      if row["share_of_change"] is not None]
                            self.assertLessEqual(abs(sum(shares) - 1),
                                                 Fraction(len(shares), 2_000_000))
                        checked += 1
            finally:
                engine.close()
        self.assertGreater(checked, 50)

    def test_decompose_invariant(self):
        period = ("2017-03-01T00:00:00", "2017-04-01T00:00:00")
        base = ("2017-02-01T00:00:00", "2017-03-01T00:00:00")
        for seed in range(3):
            rng = random.Random(2000 + seed)
            folder = self.dataset(bundle(rng, 90, months=6), f"dec{seed}")
            engine = SemanticEngine(folder, SNAPSHOT)
            try:
                for metric in ("gmv", "item_gmv", "order_count", "aov"):
                    with self.subTest(seed=seed, metric=metric):
                        result = decompose_metric(engine, metric, period, base)
                        if result.get("status") == "undefined":
                            continue
                        self.assertTrue(result["invariant"]["passed"],
                                        f"{metric} 残差 {result['invariant']['residual']}")
            finally:
                engine.close()


class L1VerifyTests(SyntheticFixture):
    """同一合成数据上引擎与独立参考两条路径一致（verify_attribution）；篡改结果被检出。"""

    def test_verify_passes_and_detects_tampering(self):
        rng = random.Random(3000)
        folder = self.dataset(bundle(rng, 100, months=6))
        period = ("2017-03-01T00:00:00", "2017-04-01T00:00:00")
        base = ("2017-02-01T00:00:00", "2017-03-01T00:00:00")
        engine = SemanticEngine(folder, SNAPSHOT)
        try:
            result = contribution(engine, "item_gmv", "product_category", period, base)
            verified = verify_attribution("contribution", result, folder, SNAPSHOT,
                                          metric="item_gmv", dimension="product_category",
                                          period=period, base_period=base)
            self.assertTrue(verified["passed"], verified["differences"])
            self.assertTrue(verified["invariant"]["passed"])
            # 篡改一个数字：检出不一致。
            tampered = {**result, "rows": [dict(row) for row in result["rows"]]}
            tampered["rows"][0]["change"] = result["rows"][0]["change"] + 1 \
                if isinstance(result["rows"][0]["change"], int) else "0.000001"
            detected = verify_attribution("contribution", tampered, folder, SNAPSHOT,
                                          metric="item_gmv", dimension="product_category",
                                          period=period, base_period=base)
            self.assertFalse(detected["passed"])
            self.assertTrue(detected["differences"])
            # 其余四类函数同样走通两条路径一致。
            checks = [("decompose_metric", {"metric": "gmv", "period": period, "base_period": base}),
                      ("detect_anomaly", {"metric": "gmv", "period": period, "history": 3}),
                      ("find_root_cause", {"metric": "item_gmv",
                                           "dimensions": ["product_category", "seller_state"],
                                           "period": period, "base_period": base}),
                      ("correlate", {"metric_a": "item_gmv", "metric_b": "item_count",
                                     "by": "product_category", "period": period})]
            for kind, arguments in checks:
                with self.subTest(kind=kind):
                    outcome = {"decompose_metric": decompose_metric, "detect_anomaly": detect_anomaly,
                               "find_root_cause": find_root_cause, "correlate": correlate}[kind]
                    result = outcome(engine, **arguments)
                    verified = verify_attribution(kind, result, folder, SNAPSHOT, **arguments)
                    self.assertTrue(verified["passed"], verified["differences"])
        finally:
            engine.close()

    def test_reference_source_matches_engine_directly(self):
        rng = random.Random(3001)
        folder = self.dataset(bundle(rng, 60, months=6))
        period = ("2017-03-01T00:00:00", "2017-04-01T00:00:00")
        engine = SemanticEngine(folder, SNAPSHOT)
        reference = ReferenceSource(folder, SNAPSHOT)
        try:
            for group_by in (None, "customer_state"):
                with self.subTest(group_by=group_by):
                    self.assertEqual(engine.query("gmv", period, group_by=group_by),
                                     reference.query("gmv", period, group_by=group_by))
            # compare_period 结构一致。
            both = reference.query("gmv", period, compare_period=("2017-01-01T00:00:00",
                                                                  "2017-02-01T00:00:00"))
            self.assertIn("compare", both)
            self.assertIn("delta", both)
        finally:
            engine.close()


if __name__ == "__main__":
    unittest.main()
