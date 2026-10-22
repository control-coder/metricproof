"""蜕变关系核验：在同一受限沙箱里用服务端变换过的输入重跑模型自己的代码，只比较几次输出彼此是否满足关系。

不需要知道正确值，所以也适用于写不出参考计算的分析。三条关系：
- permutation：订单行顺序按固定种子打乱，结果必须完全相同；
- irrelevant_rows：追加按计划必然被排除的合成订单（状态不在口径内，或支付时间恰在期间终点、起点前一秒等期间外位置），
  结果必须完全相同；对比分析同时避开本期与上期，quality 统计全部行，不适用；
- window_split：可加且不含比值的类型（summary、trend、ranking）在数据中的某个支付时间处把期间切成两段，
  两段的总计与各组之和必须等于全期间结果；share、comparison 含比值，quality 不按期间，均不适用。
每条关系多一次（window_split 两次）沙箱运行，剩余时间不足时跳过并记为 skipped；失败只报告关系名与问题类型。
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

from datalab.contracts import AnalysisPlan
from datalab.datasets.service import NORMALIZED_FIELDS
from datalab.execution import analysis_frame, codegen
from datalab.execution.runner import OutputSpec
from datalab.verification.reference import differences

RELATIONS = ("permutation", "irrelevant_rows", "window_split")
ISSUES = frozenset({"result_changed", "not_additive", "execution_failed"})
ADDITIVE_KINDS = ("summary", "trend", "ranking")
STATUSES = ("paid", "refunded", "cancelled")
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


def _project(result) -> tuple | None:
    """把结果投影为 (总计, {组: (订单数, 金额)})，用于比较可加性；结构不对时返回 None。"""
    try:
        total = (result["total"]["order_count"], result["total"]["amount_cents"])
        groups = {}
        for row in result["rows"]:
            if row["group"] in groups or not all(type(value) is int for value in (row["order_count"], row["amount_cents"])):
                return None
            groups[row["group"]] = (row["order_count"], row["amount_cents"])
        if not all(type(value) is int for value in total):
            return None
        return total, groups
    except (KeyError, TypeError):
        return None


def _merge(first: tuple, second: tuple) -> tuple:
    groups = dict(first[1])
    for name, (count, amount) in second[1].items():
        previous = groups.get(name, (0, 0))
        groups[name] = (previous[0] + count, previous[1] + amount)
    return (first[0][0] + second[0][0], first[0][1] + second[0][1]), groups


def check(plan: AnalysisPlan, orders: Path, baseline: dict, sandbox: Sandbox) -> dict:
    rows = read_orders(orders)
    relations, issues = [], []

    def run(script: str, data: bytes):
        remaining = sandbox.remaining()
        if remaining < MIN_SECONDS:
            raise _Skip
        return sandbox.execute(script, data, min(RUN_TIMEOUT, remaining))

    def same(data: bytes) -> str:
        output = run(sandbox.script, data)
        if output is None:
            return "execution_failed"
        return "result_changed" if differences(baseline, output) else ""

    def permutation() -> str:
        return same(encode(permuted(rows)))

    def irrelevant() -> str | None:
        extended = irrelevant_rows(rows, plan)
        return None if extended is None else same(encode(extended))

    def window() -> str | None:
        mid = split_point(rows, plan)
        if mid is None:
            return None
        scripts = [rebind(sandbox.script, plan, {"end": mid}), rebind(sandbox.script, plan, {"start": mid})]
        whole = _project(baseline)
        if None in scripts or whole is None:
            return None
        data = orders.read_bytes()
        parts = []
        for script in scripts:
            output = run(script, data)
            if output is None:
                return "execution_failed"
            parts.append(_project(output))
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

    def execute(script: str, data: bytes, timeout: float):
        run_id = str(uuid4())
        manifest = runner.execute_python(run_id, plan.dataset_version, script, timeout, OutputSpec(),
                                         project_id=project_id, metric_version=plan.metric_version, orders_csv=data)
        if manifest.status != "COMPLETED":
            return None
        try:
            return json.loads((runner.root / run_id / "output/result.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None

    return execute
