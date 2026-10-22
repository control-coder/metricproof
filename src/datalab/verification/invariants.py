"""不依赖参考值的结果不变量：只用输出契约、计划和结果自身的数字检查自洽性。

不需要知道正确值，所以任务放开到写不出参考 SQL 的分析后仍然适用。每条规则都由输出契约
（roles/prompts.py 与 verification/reference.py）直接推出，正确结果不会被拒绝；
问题只报告字段路径与封闭词表内的类型，不含期望值或实际值。
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from datalab.contracts import AnalysisPlan

STATUSES = ("paid", "refunded", "cancelled")
QUALITY_KEYS = ("kind", "row_count", "duplicate_orders", "missing_values", "status_counts")
TOTAL_KEYS = ("order_count", "amount_cents")
COMPARISON_KEYS = ("previous_order_count", "previous_amount_cents", "delta_cents", "growth")
# 问题类型的封闭词表；工作流转发前还会按自己的白名单再过滤一次。
ISSUES = frozenset({
    "type", "missing", "unexpected", "kind_mismatch", "negative", "bound", "zero_count_amount",
    "sum_mismatch", "ratio_mismatch", "share_sum", "delta_mismatch", "unsorted", "duplicate_group",
    "invalid_group", "group_out_of_range", "empty_group", "rows_not_empty"})
MAX_ISSUES = 12


def _int(value) -> bool:
    return type(value) is int


def ratio_text(numerator: int, denominator: int) -> str | None:
    """契约规定的 6 位小数比值（与分析框架 ratio、SQL 参考一致）；分母为 0 时为 None。"""
    return None if denominator == 0 else format(Decimal(numerator) / Decimal(denominator), ".6f")


def group_total(result: dict) -> bool:
    """分组行的订单数与金额之和等于总计；结构不对时视为不平衡。group_total 检查与不变量共用。"""
    try:
        rows, total = result.get("rows", []), result.get("total", {})
        return (sum(row["amount_cents"] for row in rows) == total["amount_cents"]
                and sum(row["order_count"] for row in rows) == total["order_count"])
    except (KeyError, TypeError, AttributeError):
        return False


def _keys(found: list, value: dict, expected: tuple, prefix: str = ""):
    found += [{"path": prefix + key, "issue": "missing"} for key in expected if key not in value]
    found += [{"path": prefix + str(key), "issue": "unexpected"} for key in value if key not in expected]


def _counts(found: list, value: dict, keys: tuple, prefix: str = "", signed: tuple = ()) -> bool:
    """逐个检查整数字段；全部是整数时返回 True，便于后续做算术关系。"""
    ok = True
    for key in keys:
        if key not in value:
            ok = False
            continue
        if not _int(value[key]):
            found.append({"path": prefix + key, "issue": "type", "expected_type": "int"})
            ok = False
        elif value[key] < 0 and key not in signed:
            found.append({"path": prefix + key, "issue": "negative"})
    return ok


def _quality(found: list, result: dict):
    _keys(found, result, QUALITY_KEYS)
    if not _counts(found, result, ("row_count", "duplicate_orders", "missing_values")):
        return
    rows = result["row_count"]
    # 重复数 = 行数 − 不同编号数，至多 行数−1；缺失只统计五个文本字段。
    if result["duplicate_orders"] > max(rows - 1, 0):
        found.append({"path": "duplicate_orders", "issue": "bound"})
    if result["missing_values"] > 5 * rows:
        found.append({"path": "missing_values", "issue": "bound"})
    counts = result.get("status_counts")
    if not isinstance(counts, dict):
        if "status_counts" in result:
            found.append({"path": "status_counts", "issue": "type", "expected_type": "object"})
        return
    found += [{"path": f"status_counts.{key}", "issue": "unexpected"} for key in counts if key not in STATUSES]
    known = tuple(key for key in STATUSES if key in counts)
    if _counts(found, counts, known, "status_counts.") and all(key in STATUSES for key in counts):
        if sum(counts[key] for key in known) != rows:
            found.append({"path": "status_counts", "issue": "sum_mismatch"})


def _last_date(end: str) -> str:
    # 左闭右开：终点前一秒所在的日期是最后一个可能出现的日期。
    return (datetime.fromisoformat(end) - timedelta(seconds=1)).date().isoformat()


def _rows(found: list, plan: AnalysisPlan, result: dict, total_ok: bool):
    rows = result.get("rows")
    if not isinstance(rows, list):
        if "rows" in result:
            found.append({"path": "rows", "issue": "type", "expected_type": "list"})
        return
    if not plan.group_by:
        if rows:
            found.append({"path": "rows", "issue": "rows_not_empty"})
        return
    columns = ("group", "order_count", "amount_cents") + (("share",) if plan.kind == "share" else ())
    valid, seen = [], set()
    for index, row in enumerate(rows):
        prefix = f"rows[{index}]."
        if not isinstance(row, dict):
            found.append({"path": f"rows[{index}]", "issue": "type", "expected_type": "object"})
            continue
        _keys(found, row, columns, prefix)
        numbers = _counts(found, row, TOTAL_KEYS, prefix)
        group = row.get("group")
        if "group" in row and not isinstance(group, str):
            found.append({"path": prefix + "group", "issue": "type", "expected_type": "str"})
            continue
        if group is None:
            continue
        if group in seen:
            found.append({"path": prefix + "group", "issue": "duplicate_group"})
        seen.add(group)
        if plan.group_by == "payment_date":
            try:
                day = date.fromisoformat(group).isoformat() == group and len(group) == 10
            except ValueError:
                day = False
            if not day:
                found.append({"path": prefix + "group", "issue": "invalid_group"})
            elif (plan.start and group < plan.start[:10]) or (plan.end and group > _last_date(plan.end)):
                found.append({"path": prefix + "group", "issue": "group_out_of_range"})
        elif not group:
            # 导入时拒绝空的渠道与品类，空分组名不可能来自正确计算。
            found.append({"path": prefix + "group", "issue": "invalid_group"})
        if numbers:
            # 分组只由被选中的订单产生，每组至少一单；没有订单就不会有金额。
            if row["order_count"] == 0:
                found.append({"path": prefix + "order_count", "issue": "empty_group"})
            valid.append((index, row))
    keys = [(-row["amount_cents"], row["group"]) if plan.kind == "ranking" else row["group"]
            for _, row in valid if isinstance(row.get("group"), str)]
    if len(keys) == len(valid):
        for position in range(1, len(keys)):
            if keys[position] < keys[position - 1]:
                found.append({"path": f"rows[{valid[position][0]}]", "issue": "unsorted"})
                break
    if total_ok and len(valid) == len(rows) and not group_total(result):
        found.append({"path": "total", "issue": "sum_mismatch"})
    if plan.kind == "share" and total_ok:
        _shares(found, rows, result["total"]["amount_cents"])


def _shares(found: list, rows: list, total: int):
    values = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or "share" not in row:
            continue
        share = row["share"]
        want_null = total == 0
        if (share is None) != want_null or (share is not None and not isinstance(share, str)):
            found.append({"path": f"rows[{index}].share", "issue": "type", "expected_type": "null" if want_null else "str"})
            continue
        if share is not None and _int(row.get("amount_cents")):
            if share != ratio_text(row["amount_cents"], total):
                found.append({"path": f"rows[{index}].share", "issue": "ratio_mismatch"})
            try:
                values.append(Decimal(share))
            except ArithmeticError:
                pass
    # 每个占比四舍五入误差至多 0.5e-6，合计与 1 的偏差不超过 行数 × 0.5e-6。
    if total > 0 and values and len(values) == len(rows):
        if abs(sum(values) - 1) > Decimal("0.0000005") * len(values):
            found.append({"path": "rows", "issue": "share_sum"})


def _comparison(found: list, result: dict, total_ok: bool):
    value = result.get("comparison")
    if not isinstance(value, dict):
        if "comparison" in result:
            found.append({"path": "comparison", "issue": "type", "expected_type": "object"})
        return
    _keys(found, value, COMPARISON_KEYS, "comparison.")
    if not _counts(found, value, COMPARISON_KEYS[:3], "comparison.", signed=("delta_cents",)):
        return
    previous = value["previous_amount_cents"]
    if value["previous_order_count"] == 0 and previous != 0:
        found.append({"path": "comparison.previous_amount_cents", "issue": "zero_count_amount"})
    if total_ok and value["delta_cents"] != result["total"]["amount_cents"] - previous:
        found.append({"path": "comparison.delta_cents", "issue": "delta_mismatch"})
    if "growth" in value:
        growth, want_null = value["growth"], previous == 0
        if (growth is None) != want_null or (growth is not None and not isinstance(growth, str)):
            found.append({"path": "comparison.growth", "issue": "type", "expected_type": "null" if want_null else "str"})
        elif growth is not None and growth != ratio_text(value["delta_cents"], previous):
            found.append({"path": "comparison.growth", "issue": "ratio_mismatch"})


def violations(plan: AnalysisPlan, result) -> list[dict]:
    """返回全部不变量问题；空列表表示结果在契约内自洽（不代表数值正确）。"""
    if not isinstance(result, dict):
        return [{"path": "$", "issue": "type", "expected_type": "object"}]
    found = []
    if result.get("kind") != plan.kind:
        found.append({"path": "kind", "issue": "kind_mismatch"})
    if plan.kind == "quality":
        _quality(found, result)
        return found
    _keys(found, result, ("kind", "total", "rows") + (("comparison",) if plan.kind == "comparison" else ()))
    total = result.get("total")
    total_ok = False
    if isinstance(total, dict):
        _keys(found, total, TOTAL_KEYS, "total.")
        total_ok = _counts(found, total, TOTAL_KEYS, "total.")
        if total_ok and total["order_count"] == 0 and total["amount_cents"] != 0:
            found.append({"path": "total.amount_cents", "issue": "zero_count_amount"})
        total_ok = total_ok and set(total) == set(TOTAL_KEYS)
    elif "total" in result:
        found.append({"path": "total", "issue": "type", "expected_type": "object"})
    _rows(found, plan, result, total_ok)
    if plan.kind == "comparison":
        _comparison(found, result, total_ok)
    return found


def check(plan: AnalysisPlan, result) -> dict:
    found = violations(plan, result)
    item = {"name": "invariants", "passed": not found,
            "detail": "结果满足输出契约的自洽关系" if not found else "结果违反输出契约的自洽关系"}
    if found:
        item["issues"] = found[:MAX_ISSUES]
        item["issue_count"] = len(found)
    return item
