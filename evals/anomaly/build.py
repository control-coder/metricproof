"""合成异动评测集的构建脚本：物化注入案例、质量检查、计算 gold、冻结题集。

用法（仓库根目录，datalab 环境）：

    python -B evals/anomaly/build.py

流程：
1. 基础数据版本：.tmp/a3/datasets 下已有则复用，否则从 artifacts/olist/raw 导入（八张表，含卖家表）。
2. 候选组合：每类注入类型给出一批候选（月份 2017-03 至 2018-08，避开数据稀疏的 2016 年与尾部月份），
   逐个在内存中注入并用 A1 语义层在 gold 维度切片上做质量检查：物化后 gold 值的 |本期 − 基期|
   必须不小于该切片其他值最大 |变化| 的 RATIO_THRESHOLD 倍；取每类前 5 个通过的组合，共 30 个注入案例。
3. gold 直接取自注入参数与注入时实际改动的行计算出的量（删除/增加的金额、订单数），不做人工标注，
   也不调用 A2 算法。
4. 原始数据问题（10 个）的 gold 由 A2 的 ReferenceSource + find_root_cause / contribution 在原始数据上计算；
   A2 未提交时跳过该步（题集不冻结）。
5. 划分与冻结：固定种子分层抽样 dev 10（7 注入 + 3 原始）/ test 30（23 注入 + 7 原始），
   写入 evals/anomaly/anomaly_cases.json（含基础数据原始文件的 SHA-256 与冻结日期）。

物化目录默认在 artifacts/anomaly/（被忽略），可随时重建；加 --materialize-only 只物化不写题集。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from datalab.datasets import olist  # noqa: E402
from datalab.datasets.service import DatasetStore  # noqa: E402
from datalab.semantic.engine import SemanticEngine  # noqa: E402
from datalab.semantic.snapshot import SemanticSnapshot  # noqa: E402

import inject as injector  # noqa: E402

STATUSES = ("delivered", "shipped", "invoiced", "processing", "approved", "created")
SNAPSHOT = SemanticSnapshot("anomaly-v1", STATUSES, "payments")
# 质量检查阈值：gold 值物化后的 |变化| ≥ 该切片其他值最大 |变化| × 1.3（次大者显著小于 gold）。
RATIO_THRESHOLD = 1.3
SEED = 20261027

PROJECT_ID = "anomaly-eval"
BASE_ROOT = ROOT / ".tmp" / "a3" / "datasets"
TARGET_ROOT = ROOT / "artifacts" / "anomaly"
FROZEN_FILE = Path(__file__).with_name("anomaly_cases.json")


def base_folder() -> Path:
    """复用或导入基础数据版本（八张表）；返回版本目录。"""
    if BASE_ROOT.is_dir():
        for child in BASE_ROOT.iterdir():
            if (child / "metadata.json").is_file():
                return child
    store = DatasetStore(BASE_ROOT)
    version = store.import_olist(ROOT / "artifacts" / "olist" / "raw", project_id=PROJECT_ID,
                                 dataset_family="olist")
    return store.directory(version.dataset_version)


def month_prev(month: str) -> str:
    year, mm = int(month[:4]), int(month[5:7])
    mm -= 1
    if mm == 0:
        year, mm = year - 1, 12
    return f"{year}-{mm:02d}"


def period_of(month: str) -> dict:
    start, end = injector.month_range(month)
    return {"start": start, "end": end}


# ---------------------------------------------------------------- 质量检查

def _delta_map(engine: SemanticEngine, metric: str, dimension: str,
               period: dict, base: dict, filters: dict | None) -> dict[str, dict]:
    """切片内各维度值的变化：delta（本期 − 基期）、规模（两期分母均值；加法型为分子均值）。

    比率型切片的比较只纳入规模不小于 gold 组 20% 的组：小样本组的比率噪声不纳入次大值比较
    （例如某些小州一个月只有几单，late_delivery_rate 在 0 与 1 之间摆动，不能当作自然波动基准）。
    """
    current = engine.query(metric, (period["start"], period["end"]), filters=filters, group_by=dimension)
    previous = engine.query(metric, (base["start"], base["end"]), filters=filters, group_by=dimension)
    kind = current["type"]
    before = {row["group"]: row for row in previous["rows"]}
    after = {row["group"]: row for row in current["rows"]}

    def level(row):
        if row is None:
            return None
        if kind == "additive":
            return Decimal(row["numerator"])
        return Decimal(row["value"]) if row["value"] is not None else None

    def size(row):
        if row is None:
            return Decimal(0)
        return Decimal(row["denominator"] if kind == "ratio" else row["numerator"])

    deltas: dict[str, dict] = {}
    for group in set(before) | set(after):
        now, old = level(after.get(group)), level(before.get(group))
        if now is None and old is None:
            continue
        delta = (now or Decimal(0)) - (old or Decimal(0)) if now is not None and old is not None \
            else (now if old is None else -old)
        deltas[group] = {"delta": delta, "size": (size(after.get(group)) + size(before.get(group))) / 2}
    return deltas


# 比率型切片次大值比较的最小相对规模：规模小于 gold 组 × MIN_REL_SIZE 的组不参与次大值比较。
MIN_REL_SIZE = Decimal("0.2")


def check_dominance(engine: SemanticEngine, metric: str, dimension: str, value: str,
                    month: str, filters: dict | None = None) -> tuple[bool, dict]:
    """质量检查：gold 值的 |Δ| 为切片内最大，且 ≥ 次大 × RATIO_THRESHOLD；返回 (通过, 详情)。"""
    period, base = period_of(month), period_of(month_prev(month))
    deltas = _delta_map(engine, metric, dimension, period, base, filters)
    if value not in deltas:
        return False, {"reason": f"gold 值 {value} 在切片 {dimension} 中不存在或比值缺失",
                       "deltas": {key: str(item["delta"]) for key, item in deltas.items()}}
    gold = deltas[value]
    # 参与比较的组：与 gold 组规模可比（≥ 20%）；加法型切片全部纳入（整数变化不受小样本噪声影响）。
    comparable = {group: item for group, item in deltas.items()
                  if item["size"] >= gold["size"] * MIN_REL_SIZE} if gold["size"] > 0 else deltas
    ranked = sorted(comparable.items(), key=lambda pair: -abs(pair[1]["delta"]))
    others = [abs(item["delta"]) for group, item in ranked if group != value]
    runner = others[0] if others else Decimal(0)
    gold_abs = abs(gold["delta"])
    ratio = float(gold_abs / runner) if runner else float("inf")
    rank = next(index for index, (group, _) in enumerate(ranked, 1) if group == value)
    return gold_abs > 0 and ratio >= RATIO_THRESHOLD, {
        "gold_delta": str(gold["delta"]), "runner_up": str(runner), "ratio": round(ratio, 3), "rank": rank,
        "top": [(group, str(item["delta"])) for group, item in ranked[:4]]}


# ---------------------------------------------------------------- 候选与 gold

def _removed_amount(tables_before: dict, tables_after: dict, month: str) -> int:
    """注入前后目标月 item_gmv 之差（删除造成的金额变化，负数）。"""
    def total(tables):
        orders = {row["order_id"]: row for row in tables["orders"]}
        start, end = injector.month_range(month)
        return sum(int(item["price_cents"]) + int(item["freight_cents"])
                   for item in tables["order_items"]
                   if start <= orders[item["order_id"]]["order_purchase_timestamp"] < end)
    return total(tables_after) - total(tables_before)


def order_drop_gold(before: dict, after: dict, spec: dict) -> dict:
    """目标组合（品类×州×月）被删订单中该品类明细的金额（负数）；评分切片按品类过滤按州拆、
    或按州过滤按品类拆，该值与两个切片 gold 值的实际变化一致（删除只影响目标组合）。"""
    month = spec["month"]
    start, end = injector.month_range(month)
    removed = {row["order_id"] for row in before["orders"]} - {row["order_id"] for row in after["orders"]}
    orders = {row["order_id"]: row for row in before["orders"]}
    customers = {row["customer_id"]: row for row in before["customers"]}
    english = {row["product_category_name"]: row["product_category_name_english"]
               for row in before["category_translation"]}
    raw_category = {row["product_id"]: row["product_category_name"] for row in before["products"]}

    def category_of(product_id: str) -> str:
        name = raw_category.get(product_id, "")
        return english.get(name, name) if name else olist.UNKNOWN_CATEGORY

    delta = 0
    for item in before["order_items"]:
        order = orders[item["order_id"]]
        if item["order_id"] in removed and start <= order["order_purchase_timestamp"] < end \
                and category_of(item["product_id"]) == spec["product_category"] \
                and customers[order["customer_id"]]["customer_state"] == spec["customer_state"]:
            delta -= int(item["price_cents"]) + int(item["freight_cents"])
    return {"dimensions": [{"dimension": "product_category", "value": spec["product_category"]},
                           {"dimension": "customer_state", "value": spec["customer_state"]}],
            "direction": "down", "magnitude": delta, "unit": "cents",
            "note": "目标组合（品类×州）被删明细的金额；订单级 gmv 不能按品类拆，"
                    "customer_state 维对应订单级 gmv 按州拆或 item_gmv 按州拆"}


def freight_up_gold(before: dict, after: dict, spec: dict) -> dict:
    month = spec["month"]
    start, end = injector.month_range(month)
    orders_before = {row["order_id"]: row for row in before["orders"]}

    def freight(tables):
        return sum(int(item["freight_cents"]) for item in tables["order_items"]
                   if start <= orders_before[item["order_id"]]["order_purchase_timestamp"] < end)

    delta = freight(after) - freight(before)
    return {"dimensions": [{"dimension": "seller_state", "value": spec["seller_state"]}],
            "direction": "up", "magnitude": delta, "unit": "cents", "note": "运费增量（分）"}


def delivery_delay_gold(before: dict, after: dict, spec: dict) -> dict:
    delayed = 0
    orders_before = {row["order_id"]: row for row in before["orders"]}
    for order in after["orders"]:
        old = orders_before.get(order["order_id"])
        if old and old["order_delivered_customer_date"] != order["order_delivered_customer_date"]:
            delayed += 1
    return {"dimensions": [{"dimension": "customer_state", "value": spec["customer_state"]}],
            "direction": "up", "magnitude": delayed, "unit": "count",
            "note": "新增延迟送达订单数；附带关联事实：差评集中在 delivery_status=late 的订单上",
            "correlate": {"metric": "bad_review_rate", "by": "delivery_status", "expect": "late"}}


def mix_shift_gold(before: dict, after: dict, spec: dict) -> dict:
    month = spec["month"]
    start, end = injector.month_range(month)
    added = sum(1 for order in after["orders"]
                if order["order_id"].endswith(spec.get("suffix", "zz")) and start <= order["order_purchase_timestamp"] < end)
    return {"dimensions": [{"dimension": spec["dimension"], "value": spec["value"]}],
            "direction": "down", "effect": "between", "magnitude": added, "unit": "count",
            "note": "复制的订单数（该组订单数翻倍）；aov 变化全部来自结构（between）效应，组内客单价不变"}


def price_cut_gold(before: dict, after: dict, spec: dict) -> dict:
    month = spec["month"]
    start, end = injector.month_range(month)
    orders = {row["order_id"]: row for row in before["orders"]}
    before_price = sum(int(item["price_cents"]) for item in before["order_items"]
                       if start <= orders[item["order_id"]]["order_purchase_timestamp"] < end)
    after_price = sum(int(item["price_cents"]) for item in after["order_items"]
                      if start <= orders[item["order_id"]]["order_purchase_timestamp"] < end)
    return {"dimensions": [{"dimension": "product_category", "value": spec["product_category"]}],
            "direction": "down", "magnitude": after_price - before_price, "unit": "cents",
            "note": "价格下调造成的明细价格变化（分，不含运费）"}


def seller_drop_gold(before: dict, after: dict, spec: dict) -> dict:
    delta = _removed_amount(before, after, spec["month"])
    seller_state = next((row["seller_state"] for row in before["sellers"] if row["seller_id"] == spec["seller_id"]),
                        "unknown")
    return {"dimensions": [{"dimension": "seller_id", "value": spec["seller_id"]}],
            "direction": "down", "magnitude": delta, "unit": "cents",
            "note": f"该卖家当月全部明细金额（层级上属于 seller_state={seller_state}）",
            "hierarchy": {"seller_state": seller_state}}


GOLDERS = {"order_drop": order_drop_gold, "freight_up": freight_up_gold,
           "delivery_delay": delivery_delay_gold, "mix_shift": mix_shift_gold,
           "price_cut": price_cut_gold, "seller_drop": seller_drop_gold}


# ---------------------------------------------------------------- 候选池

def candidates() -> list[dict]:
    """每类注入类型的候选组合（月份避开 2016 与 2018-09 之后的尾部）。每项含问题模板与检查切片。"""
    pool = []
    # order_drop：品类×州×月，按 customer_state 拆（过滤品类）检查；share 0.35–0.5。
    for category, state, month, share in [
            ("health_beauty", "SP", "2018-04", 0.45), ("bed_bath_table", "SP", "2018-01", 0.45),
            ("watches_gifts", "SP", "2018-07", 0.45), ("computers_accessories", "SP", "2018-02", 0.45),
            ("health_beauty", "RJ", "2018-06", 0.5), ("bed_bath_table", "RJ", "2018-04", 0.5),
            ("sports_leisure", "MG", "2018-05", 0.5), ("watches_gifts", "RJ", "2018-03", 0.5),
            ("health_beauty", "MG", "2018-02", 0.5), ("computers_accessories", "RJ", "2018-06", 0.5),
            ("sports_leisure", "SP", "2018-08", 0.45), ("bed_bath_table", "MG", "2018-07", 0.5)]:
        pool.append({"type": "order_drop",
                     "injection": {"type": "order_drop", "month": month, "product_category": category,
                                   "customer_state": state, "share": share},
                     "metric": "item_gmv",
                     "question": f"{month[:4]} 年 {int(month[5:7])} 月销售额环比下降，主要是什么原因？",
                     "checks": [("customer_state", state, {"product_category": category}),
                                ("product_category", category, {"customer_state": state})]})
    # freight_up：卖家州×月，按 seller_state 拆 freight_ratio 的分子 freight_sum 检查。
    for state, month, factor in [("SP", "2018-03", 1.5), ("SP", "2018-05", 1.45), ("SP", "2018-07", 1.4),
                                 ("RJ", "2018-04", 1.6), ("MG", "2018-06", 1.6), ("SP", "2017-11", 1.5),
                                 ("PR", "2018-05", 1.6), ("SP", "2018-01", 1.45)]:
        pool.append({"type": "freight_up",
                     "injection": {"type": "freight_up", "month": month, "seller_state": state, "factor": factor},
                     "metric": "freight_ratio",
                     "question": f"{month[:4]} 年 {int(month[5:7])} 月运费占比明显上升，主要集中在哪些卖家？",
                     "checks": [("seller_state", state, None)]})
    # delivery_delay：客户州×月，按 customer_state 拆 late_count 检查。
    for state, month, share in [("SP", "2018-03", 0.4), ("RJ", "2018-04", 0.5), ("MG", "2018-05", 0.5),
                                ("SP", "2018-06", 0.4), ("RS", "2018-04", 0.55), ("PR", "2018-05", 0.55),
                                ("MG", "2018-03", 0.45), ("RJ", "2018-07", 0.5), ("SP", "2017-11", 0.4)]:
        pool.append({"type": "delivery_delay",
                     "injection": {"type": "delivery_delay", "month": month, "customer_state": state, "share": share},
                     "metric": "late_delivery_rate",
                     "question": f"{month[:4]} 年 {int(month[5:7])} 月送达延迟率上升，主要集中在哪些地区？"
                                 f"同期差评率也有变化，是否相关？",
                     "checks": [("customer_state", state, None)]})
    # mix_shift：payment_type=boleto ×月（份额大、aov 低于全部），按 payment_type 拆 gmv 检查结构主导。
    for value, month in [("boleto", "2018-03"), ("boleto", "2018-05"), ("boleto", "2018-01"),
                         ("boleto", "2018-06"), ("boleto", "2018-04"), ("boleto", "2017-11"),
                         ("boleto", "2018-02"), ("boleto", "2018-07"), ("boleto", "2017-10"),
                         ("boleto", "2018-08"), ("boleto", "2017-09")]:
        pool.append({"type": "mix_shift",
                     "injection": {"type": "mix_shift", "month": month, "dimension": "payment_type", "value": value},
                     "metric": "aov",
                     "question": f"{month[:4]} 年 {int(month[5:7])} 月客单价环比下降，主要原因是客群结构变化还是"
                                 f"各组内部变化？",
                     "checks": [("payment_type", value, None)]})
    # price_cut：品类×月，按 product_category 拆 item_gmv 检查（候选按探查过的平稳月份挑选）。
    for category, month, factor in [
            ("health_beauty", "2018-07", 0.65), ("watches_gifts", "2018-06", 0.65),
            ("watches_gifts", "2018-08", 0.65), ("bed_bath_table", "2018-07", 0.65),
            ("sports_leisure", "2018-04", 0.65), ("computers_accessories", "2018-04", 0.65),
            ("health_beauty", "2017-06", 0.65), ("health_beauty", "2017-10", 0.65),
            ("computers_accessories", "2017-06", 0.65), ("bed_bath_table", "2017-12", 0.65),
            ("computers_accessories", "2017-04", 0.65), ("watches_gifts", "2018-04", 0.65)]:
        pool.append({"type": "price_cut",
                     "injection": {"type": "price_cut", "month": month, "product_category": category,
                                   "factor": factor},
                     "metric": "item_gmv",
                     "question": f"{month[:4]} 年 {int(month[5:7])} 月销售额环比下降，主要是什么原因？",
                     "checks": [("product_category", category, None)]})
    # seller_drop：大卖家×月，按 seller_id 拆 item_gmv 检查（候选按探查过的金额与平稳度挑选）。
    for seller, month in [("7d13fca15225358621be4086e1eb0964", "2018-05"),
                          ("53243585a1d6dc2643021fd1853d8905", "2017-09"),
                          ("53243585a1d6dc2643021fd1853d8905", "2017-10"),
                          ("53243585a1d6dc2643021fd1853d8905", "2017-08"),
                          ("955fee9216a65b617aa5c0531780ce60", "2018-05"),
                          ("955fee9216a65b617aa5c0531780ce60", "2018-04"),
                          ("7d13fca15225358621be4086e1eb0964", "2018-03"),
                          ("7e93a43ef30c4f03f38b393420bc753a", "2017-05"),
                          ("3d871de0142ce09b7081e2b9d1733cb1", "2018-01"),
                          ("da8622b14eb17ae2831f4ac5b9dab84a", "2018-01"),
                          ("4869f7a5dfa277a7dca6462dcf3b52b2", "2018-04"),
                          ("59417c56835dd8e2e72f91f809cd4092", "2017-04")]:
        pool.append({"type": "seller_drop",
                     "injection": {"type": "seller_drop", "month": month, "seller_id": seller},
                     "metric": "item_gmv",
                     "question": f"{month[:4]} 年 {int(month[5:7])} 月销售额环比下降，主要是什么原因？",
                     "checks": [("seller_id", seller, None)]})
    return pool


# ---------------------------------------------------------------- 主流程

def build_injected(folder: Path, log) -> list[dict]:
    """扫描候选池：注入 → 质量检查 → gold，返回通过的注入案例（每类前 5 个）。"""
    tables = olist.read_tables(folder)
    picked: dict[str, list[dict]] = {}
    for candidate in candidates():
        kind = candidate["type"]
        if len(picked.get(kind, [])) >= 5:
            continue
        case_id = f"inj-{kind}-{len(picked.get(kind, [])) + 1:02d}"
        spec = candidate["injection"]
        rng = injector.rng_for(case_id)
        after = injector.inject(tables, spec, rng)
        # 用临时目录 + 语义层引擎做质量检查（物化到内存 SQLite，不写盘）。
        with tempfile.TemporaryDirectory(prefix="a3-check-", dir=ROOT / ".tmp") as scratch:
            scratch_path = Path(scratch)
            olist.write_tables(scratch_path, after)
            engine = SemanticEngine(scratch_path, SNAPSHOT)
            try:
                month = spec["month"]
                detail = {}
                for dimension, value, filters in candidate["checks"]:
                    _, info = check_dominance(engine, _check_metric(candidate), dimension, value, month, filters)
                    detail[dimension] = info
                if len(candidate["checks"]) > 1:
                    # 二维根因（order_drop）：至少一个切片主导（ratio ≥ 阈值），其余切片 gold 值进入前三。
                    passed_all = any(info.get("ratio", 0) >= RATIO_THRESHOLD for info in detail.values()) \
                        and all(info.get("rank", 99) <= 3 for info in detail.values())
                else:
                    passed_all = all(info.get("ratio", 0) >= RATIO_THRESHOLD for info in detail.values())
            finally:
                engine.close()
        if not passed_all:
            log(f"  未通过 {kind} {spec}: " + "; ".join(
                f"{dim}: gold={info.get('gold_delta')} runner={info.get('runner_up')} ratio={info.get('ratio')}"
                for dim, info in detail.items()))
            continue
        gold = GOLDERS[kind](tables, after, spec)
        case = {"id": case_id, "split": None, "kind": "injected", "injection_type": kind,
                "injection": spec, "question": candidate["question"], "metric": candidate["metric"],
                "period": period_of(spec["month"]), "base_period": period_of(month_prev(spec["month"])),
                "gold": gold, "quality": {dim: {"gold_delta": info["gold_delta"], "runner_up": info["runner_up"],
                                                "ratio": info["ratio"], "rank": info.get("rank")}
                                          for dim, info in detail.items()}}
        picked.setdefault(kind, []).append(case)
        log(f"  通过 {case_id}: " + "; ".join(
            f"{dim} gold={info['gold_delta']} runner={info['runner_up']} ratio={info['ratio']}"
            for dim, info in detail.items()))
    missing = [kind for kind in ("order_drop", "freight_up", "delivery_delay", "mix_shift", "price_cut", "seller_drop")
               if len(picked.get(kind, [])) < 5]
    if missing:
        raise SystemExit(f"以下注入类型通过质量检查的候选不足 5 个：{missing}；请调整候选池或阈值")
    return [case for kind in sorted(picked) for case in picked[kind]]


def _check_metric(candidate: dict) -> str:
    """质量检查用的切片指标：直接用目标指标（允许对应维度分组）；mix_shift 用 gmv 切片看组规模主导。

    mix_shift 复制后该组组内 aov 不变（aov 切片的组内 |Δ| ≈ 0），主导性体现在该组 gmv 变化上，
    故用 gmv 按 payment_type 拆检查 boleto 组规模变化是否最大（结构效应的代理检查，只用 A1 语义层）。
    """
    return "gmv" if candidate["type"] == "mix_shift" else candidate["metric"]


def materialize_all(cases: list[dict], folder: Path, log) -> None:
    TARGET_ROOT.mkdir(parents=True, exist_ok=True)
    for case in cases:
        started = time.time()
        injector.materialize(case, folder, TARGET_ROOT)
        log(f"  物化 {case['id']} → {TARGET_ROOT / case['id']}（{time.time() - started:.1f} 秒）")


def freeze(cases: list[dict], raw_cases: list[dict], folder: Path, log) -> None:
    """固定种子分层划分 dev/test，写冻结文件。"""
    all_cases = cases + raw_cases
    rng = random.Random(SEED)
    injected = [case for case in all_cases if case["kind"] == "injected"]
    raw = [case for case in all_cases if case["kind"] == "raw"]
    # 分层划分：dev 的 7 个注入案例先保证六类各 1 个（覆盖尽量多的注入类型），第 7 个从剩余中随机。
    by_type: dict[str, list[dict]] = {}
    for case in injected:
        by_type.setdefault(case["injection_type"], []).append(case)
    dev_injected = set()
    for kind in sorted(by_type):
        dev_injected.add(rng.choice(by_type[kind])["id"])
    rest = [case for case in injected if case["id"] not in dev_injected]
    dev_injected.add(rng.choice(rest)["id"])
    dev_raw = set(case["id"] for case in rng.sample(raw, 3))
    for case in all_cases:
        chosen = case["id"] in dev_injected or case["id"] in dev_raw
        case["split"] = "dev" if chosen else "test"
    metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    frozen = {"meta": {"frozen_at": time.strftime("%Y-%m-%d"),
                       "base_version": metadata["dataset_version"],
                       "source_sha256": metadata["profile"]["source_sha256"],
                       "snapshot": {"version": SNAPSHOT.version, "included_statuses": list(SNAPSHOT.included_statuses),
                                    "amount_source": SNAPSHOT.amount_source},
                       "ratio_threshold": RATIO_THRESHOLD, "seed": SEED,
                       "note": "题集先于 Agent 开发冻结；冻结后不按 Agent 表现修改，"
                               "任何修改都需重新运行 build.py 的质量检查"},
              "cases": all_cases}
    FROZEN_FILE.write_text(json.dumps(frozen, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"冻结 {len(all_cases)} 个案例 → {FROZEN_FILE}"
        f"（dev {sum(case['split'] == 'dev' for case in all_cases)} / "
        f"test {sum(case['split'] == 'test' for case in all_cases)}）")
    import cases as cases_module
    cases_module.validate(frozen)
    log("加载校验通过")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--materialize-only", action="store_true", help="只物化注入案例，不冻结题集")
    parser.add_argument("--skip-materialize", action="store_true", help="跳过物化（物化目录已存在时复用）")
    parser.add_argument("--skip-raw", action="store_true", help="跳过原始数据问题（A2 未就绪时）")
    args = parser.parse_args()
    started = time.time()

    def log(message):
        print(f"[{time.time() - started:7.1f}s] {message}", flush=True)

    folder = base_folder()
    log(f"基础数据版本目录：{folder}")
    injected = build_injected(folder, log)
    if args.skip_materialize:
        log(f"注入案例 {len(injected)} 个通过质量检查；--skip-materialize，复用已有物化目录")
    else:
        log(f"注入案例 {len(injected)} 个通过质量检查，开始物化")
        materialize_all(injected, folder, log)
    if args.materialize_only:
        log("仅物化，结束")
        return
    raw_cases = []
    if not args.skip_raw:
        raw_cases = build_raw(folder, log)
    else:
        log("跳过原始数据问题（--skip-raw）")
    if raw_cases:
        freeze(injected, raw_cases, folder, log)
    else:
        log("原始数据问题 gold 待 A2；题集未冻结")


def build_raw(folder: Path, log) -> list[dict]:
    """原始数据问题（10 个）的 gold：由 A2 的 ReferenceSource + find_root_cause / contribution 计算。"""
    from raw_questions import RAW_QUESTIONS, compute_gold
    results = []
    for index, spec in enumerate(RAW_QUESTIONS, 1):
        case_id = f"raw-{index:02d}"
        gold = compute_gold(folder, SNAPSHOT, spec)
        question = gold.pop("question")
        gold_period = gold.pop("period")
        gold_base = gold.pop("base_period")
        results.append({"id": case_id, "split": None, "kind": "raw", "injection_type": "raw",
                        "question": question, "metric": spec["metric"],
                        "period": gold_period, "base_period": gold_base, "gold": gold})
        log(f"  原始问题 {case_id}（{gold['month']}，首位 EP={gold['top_ep']}）: " +
            ", ".join(f"{item['dimension']}={item['value']}" for item in gold["dimensions"]))
    return results


if __name__ == "__main__":
    main()
