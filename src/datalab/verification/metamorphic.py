"""蜕变关系核验：在同一受限沙箱里用服务端变换过的输入重跑模型自己的代码，只比较几次输出彼此是否满足关系。

不需要知道正确值，所以也适用于写不出参考计算的分析。三条关系：
- permutation：订单行顺序按固定种子打乱，结果必须完全相同；
- irrelevant_rows：追加按计划必然被排除的合成订单（状态不在口径内，或支付时间恰在期间终点、起点前一秒等期间外位置），
  结果必须完全相同；对比分析同时避开本期与上期，quality 统计全部行，不适用；
- window_split：可加且不含比值的类型（summary、trend、ranking）在数据中的某个支付时间处把期间切成两段，
  两段的总计与各组之和必须等于全期间结果；share、comparison 含比值，quality 不按期间，均不适用。
每条关系多一次（window_split 两次）沙箱运行，剩余时间不足时跳过并记为 skipped；失败只报告关系名与问题类型。

Olist 多表数据同样三条：permutation 每张表各自打乱；irrelevant_rows 追加属于新客户的合成订单及其全部子行
（口径外状态放在期间内，口径内状态放在期间外）；window_split 用于订单级 summary/trend/ranking、品类销售与送达延迟
的计数，复购率与留存按客户跨期间计数、不可加，不适用。变换后的规范化表经同一服务端投影成输入表再运行。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import random
from typing import Callable
from uuid import uuid4

from datalab.contracts import OLIST_STATUSES, AnalysisPlan
from datalab.datasets import olist
from datalab.datasets.service import NORMALIZED_FIELDS
from datalab.execution import analysis_frame, codegen
from datalab.execution.runner import OutputSpec
from datalab.verification.reference import differences

RELATIONS = ("permutation", "irrelevant_rows", "window_split")
ISSUES = frozenset({"result_changed", "not_additive", "execution_failed"})
ADDITIVE_KINDS = ("summary", "trend", "ranking")
STATUSES = ("paid", "refunded", "cancelled")
TOTAL_FIELDS = ("order_count", "amount_cents")
# 期间切分可加的类型与比较的字段（Olist 另有品类销售与送达延迟的计数；比值不比较）。
ADDITIVE_FIELDS = {**dict.fromkeys(ADDITIVE_KINDS, TOTAL_FIELDS),
                   "category_sales": ("order_count", "item_count", "price_cents", "freight_cents"),
                   "delivery_delay": ("delivered_count", "late_count")}
SEED = 20261022
RUN_TIMEOUT = 30
# 剩余时间低于该值时不再启动新的沙箱运行。
MIN_SECONDS = 5

# execute(脚本, 订单 CSV 字节, 时限) -> result.json 对象；执行失败或产物无法解析时返回 None。
Execute = Callable[[str, bytes, float], "dict | None"]


@dataclass(frozen=True)
class Sandbox:
    script: str
    execute: Execute
    remaining: Callable[[], float] = field(default=lambda: float(RUN_TIMEOUT * 4))


class _Skip(Exception):
    pass


def read_orders(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def encode(rows: list[dict]) -> bytes:
    # 与 DatasetStore 写出规范化 CSV 的方式一致。
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=NORMALIZED_FIELDS)
    writer.writeheader()
    writer.writerows({key: row[key] for key in NORMALIZED_FIELDS} for row in rows)
    return out.getvalue().encode("utf-8")


def permuted(rows: list[dict], seed: int = SEED) -> list[dict]:
    shuffled = list(rows)
    random.Random(seed).shuffle(shuffled)
    return shuffled


def _shift(stamp: str, seconds: int) -> str:
    return (datetime.fromisoformat(stamp) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _periods(plan: AnalysisPlan) -> list[tuple]:
    periods = [(plan.start, plan.end)]
    if plan.kind == "comparison":
        periods.append((plan.previous_start, plan.previous_end))
    return periods


def _inside(stamp: str, periods: list[tuple]) -> bool:
    return any((not start or stamp >= start) and (not end or stamp < end) for start, end in periods)


def irrelevant_rows(rows: list[dict], plan: AnalysisPlan) -> list[dict] | None:
    """返回追加了必然被排除的合成订单的新数据；计划下构造不出这样的订单时返回 None。"""
    if plan.kind == "quality" or not rows:
        return None
    periods = _periods(plan)
    extra = []
    # 口径外状态：放在各期间内部（优先放在起点上），只有状态过滤能把它们排除。
    excluded = [status for status in STATUSES if status not in plan.included_statuses]
    for start, end in periods:
        stamp = start or (_shift(end, -1) if end else rows[0]["payment_time"])
        extra += [(stamp, status) for status in excluded]
    # 口径内状态、期间之外：终点本身与起点前一秒，专门探测左闭右开边界。
    candidates = [stamp for start, end in periods for stamp in ((end,) if end else ()) + ((_shift(start, -1),) if start else ())]
    outside = [stamp for stamp in dict.fromkeys(candidates) if not _inside(stamp, periods)]
    extra += [(stamp, plan.included_statuses[index % len(plan.included_statuses)]) for index, stamp in enumerate(outside)]
    if not extra:
        return None
    ids = {row["order_id"] for row in rows}
    channels = sorted({row["channel"] for row in rows})
    categories = sorted({row["category"] for row in rows})
    synthetic = []
    for index, (stamp, status) in enumerate(extra):
        order_id = f"MR{index:06d}"
        while order_id in ids:
            order_id += "x"
        ids.add(order_id)
        synthetic.append({"order_id": order_id, "payment_time": stamp, "amount_cents": str(1000 + 137 * index),
                          "status": status, "channel": channels[index % len(channels)],
                          "category": categories[index % len(categories)]})
    return rows + synthetic


def split_point(rows: list[dict], plan: AnalysisPlan) -> str | None:
    """取被选中订单里位于中间的一个支付时间作为切点：切点上恰有订单，两段边界都会被用到。"""
    if plan.kind not in ADDITIVE_KINDS:
        return None
    window = [(plan.start, plan.end)]
    times = sorted({row["payment_time"] for row in rows if row["status"] in plan.included_statuses
                    and _inside(row["payment_time"], window) and (not plan.start or row["payment_time"] > plan.start)})
    return times[len(times) // 2] if times else None


def rebind(script: str, plan: AnalysisPlan, changes: dict) -> str | None:
    """只改期间边界、保留同一份代码；能识别的只有服务端框架脚本与固定脚本。"""
    return analysis_frame.rebind(script, changes) or codegen.rebind(script, plan, changes)


def _project(result, keys: tuple[str, ...] = TOTAL_FIELDS) -> tuple | None:
    """把结果投影为 (总计, {组: 可加字段})，用于比较可加性；结构不对时返回 None。

    单表与 Olist 订单级为 (订单数, 金额)；品类销售的订单数按订单去重，但一单只落在一个期间，切分后仍可加。
    """
    try:
        total = tuple(result["total"][key] for key in keys)
        groups = {}
        for row in result["rows"]:
            values = tuple(row[key] for key in keys)
            if row["group"] in groups or not all(type(value) is int for value in values):
                return None
            groups[row["group"]] = values
        if not all(type(value) is int for value in total):
            return None
        return total, groups
    except (KeyError, TypeError):
        return None


def _merge(first: tuple, second: tuple) -> tuple:
    groups = dict(first[1])
    for name, values in second[1].items():
        previous = groups.get(name, (0,) * len(values))
        groups[name] = tuple(left + right for left, right in zip(previous, values))
    return tuple(left + right for left, right in zip(first[0], second[0])), groups


def permuted_tables(tables: dict[str, list[dict]], seed: int = SEED) -> dict[str, list[dict]]:
    """每张表各自按固定种子打乱行序。"""
    return {name: permuted(rows, seed + index) for index, (name, rows) in enumerate(sorted(tables.items()))}


def irrelevant_tables(tables: dict[str, list[dict]], plan: AnalysisPlan) -> dict[str, list[dict]] | None:
    """Olist：追加必然被排除的合成订单及其客户、明细、支付与评价子行；构造不出时返回 None。

    合成订单属于新的客户（新的 customer_unique_id），不会改变已有客户的复购、首单或归州；
    口径外状态放在期间内，口径内状态放在期间终点或起点前一秒。留存未给终点时观察终点取最晚下单时间，
    合成订单不能晚于它。全部日期字段都取同一时间，所以无论计划用哪个时间字段都落在同一位置。
    """
    orders = tables["orders"]
    field = plan.time_field
    stamps = sorted(order[field] for order in orders if order[field])
    if not stamps:
        return None
    periods = _periods(plan)
    excluded = [status for status in OLIST_STATUSES if status not in plan.included_statuses]
    extra = []
    for start, end in periods:
        stamp = start or (_shift(end, -1) if end else stamps[0])
        extra += [(stamp, status) for status in excluded]
    candidates = [stamp for start, end in periods for stamp in ((end,) if end else ()) + ((_shift(start, -1),) if start else ())]
    outside = [stamp for stamp in dict.fromkeys(candidates) if not _inside(stamp, periods)]
    extra += [(stamp, plan.included_statuses[index % len(plan.included_statuses)]) for index, stamp in enumerate(outside)]
    if plan.kind == "cohort_retention" and not plan.end:
        extra = [(stamp, status) for stamp, status in extra if stamp <= stamps[-1]]
    if not extra:
        return None
    result = {name: list(rows) for name, rows in tables.items()}
    taken = {name: {row[column] for row in rows} for name, rows, column in (
        ("orders", orders, "order_id"), ("customers", tables["customers"], "customer_id"),
        ("people", tables["customers"], "customer_unique_id"), ("reviews", tables["order_reviews"], "review_id"))}

    def fresh(kind: str, prefix: str, index: int) -> str:
        value = f"{prefix}{index:06d}"
        while value in taken[kind]:
            value += "x"
        taken[kind].add(value)
        return value
    state = min((row["customer_state"] for row in tables["customers"]), default="SP")
    product = min((row["product_id"] for row in tables["products"]), default="MRP000000")
    for index, (stamp, status) in enumerate(extra):
        order_id, customer_id = fresh("orders", "MR", index), fresh("customers", "MRC", index)
        cents = 1000 + 137 * index
        result["orders"].append({"order_id": order_id, "customer_id": customer_id, "order_status": status,
                                 "order_purchase_timestamp": stamp, "order_approved_at": stamp,
                                 "order_delivered_carrier_date": stamp, "order_delivered_customer_date": stamp,
                                 "order_estimated_delivery_date": stamp[:10] + "T00:00:00"})
        result["customers"].append({"customer_id": customer_id, "customer_unique_id": fresh("people", "MRU", index),
                                    "customer_state": state})
        result["order_items"].append({"order_id": order_id, "order_item_id": "1", "product_id": product, "seller_id": "MRS",
                                      "shipping_limit_date": stamp, "price_cents": str(cents), "freight_cents": "100"})
        result["order_payments"].append({"order_id": order_id, "payment_sequential": "1", "payment_type": "credit_card",
                                         "payment_installments": "1", "payment_value_cents": str(cents + 100)})
        result["order_reviews"].append({"review_id": fresh("reviews", "MRR", index), "order_id": order_id, "review_score": "5",
                                        "review_creation_date": stamp, "review_answer_timestamp": stamp})
    return result


def olist_split_point(tables: dict[str, list[dict]], plan: AnalysisPlan) -> str | None:
    if plan.kind not in ADDITIVE_FIELDS:
        return None
    field, window = plan.time_field, [(plan.start, plan.end)]
    times = sorted({order[field] for order in tables["orders"] if order["order_status"] in plan.included_statuses
                    and order[field] and _inside(order[field], window) and (not plan.start or order[field] > plan.start)})
    return times[len(times) // 2] if times else None


def check(plan: AnalysisPlan, orders: Path, baseline: dict, sandbox: Sandbox) -> dict:
    relations, issues = [], []
    if plan.schema == "olist":
        # 多表：每次运行的输入都是变换后的规范化表经服务端同一投影得到的输入表。
        tables = olist.read_tables(orders.parent)
        original = olist.sandbox_inputs(tables, plan)
        shuffled = lambda: olist.sandbox_inputs(permuted_tables(tables), plan)  # noqa: E731
        extended = irrelevant_tables(tables, plan)
        extended_data = None if extended is None else olist.sandbox_inputs(extended, plan)
        mid = olist_split_point(tables, plan)
    else:
        rows = read_orders(orders)
        original = orders.read_bytes()
        shuffled = lambda: encode(permuted(rows))  # noqa: E731
        extended = irrelevant_rows(rows, plan)
        extended_data = None if extended is None else encode(extended)
        mid = split_point(rows, plan)
    keys = ADDITIVE_FIELDS.get(plan.kind, TOTAL_FIELDS)

    def run(script: str, data):
        remaining = sandbox.remaining()
        if remaining < MIN_SECONDS:
            raise _Skip
        return sandbox.execute(script, data, min(RUN_TIMEOUT, remaining))

    def same(data) -> str:
        output = run(sandbox.script, data)
        if output is None:
            return "execution_failed"
        return "result_changed" if differences(baseline, output) else ""

    def permutation() -> str:
        return same(shuffled())

    def irrelevant() -> str | None:
        return None if extended_data is None else same(extended_data)

    def window() -> str | None:
        if mid is None:
            return None
        scripts = [rebind(sandbox.script, plan, {"end": mid}), rebind(sandbox.script, plan, {"start": mid})]
        whole = _project(baseline, keys)
        if None in scripts or whole is None:
            return None
        parts = []
        for script in scripts:
            output = run(script, original)
            if output is None:
                return "execution_failed"
            parts.append(_project(output, keys))
        if None in parts:
            return "not_additive"
        return "" if _merge(*parts) == whole else "not_additive"

    for name, relation in zip(RELATIONS, (permutation, irrelevant, window)):
        try:
            issue = relation()
        except _Skip:
            relations.append({"relation": name, "status": "skipped"})
            continue
        if issue is None:
            relations.append({"relation": name, "status": "not_applicable"})
        elif issue:
            relations.append({"relation": name, "status": "failed"})
            issues.append({"relation": name, "issue": issue})
        else:
            relations.append({"relation": name, "status": "passed"})
    item = {"name": "metamorphic", "passed": not issues, "relations": relations,
            "detail": "变换输入后的重跑结果满足蜕变关系" if not issues else "变换输入后的重跑结果违反蜕变关系"}
    if issues:
        item["issues"] = issues
    return item


def runner_execute(runner, plan: AnalysisPlan, project_id: str) -> Execute:
    """用同一个 DockerRunner（同样的隔离参数）执行变换后的输入；每次运行使用新的任务目录。"""

    def execute(script: str, data, timeout: float):
        run_id = str(uuid4())
        # 单表为订单 CSV 字节，多表为服务端投影的输入表。
        extra = {"input_files": data} if isinstance(data, dict) else {"orders_csv": data}
        manifest = runner.execute_python(run_id, plan.dataset_version, script, timeout, OutputSpec(),
                                         project_id=project_id, metric_version=plan.metric_version, **extra)
        if manifest.status != "COMPLETED":
            return None
        try:
            return json.loads((runner.root / run_id / "output/result.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None

    return execute
