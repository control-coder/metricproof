"""第二份独立参考：纯 Python 按行过滤与整数累加，不用 SQL，也不与 SQL 参考共享任何计算代码。

两份参考由不同方式实现同一契约：SQL 聚合与 Python 逐行累加，比值分别用 Decimal 格式化与整数
银行家舍入。运行时二者必须逐项一致（reference_agreement），否则说明服务端参考本身有缺陷，
任务不能标为已核验。按项目规则，第二份实现必须是服务端代码，不能让模型再算一遍。
"""
import csv
from pathlib import Path

from datalab.contracts import AnalysisPlan
from datalab.verification.reference import MAX_DIFFS, differences

STATUSES = ("paid", "refunded", "cancelled")
TEXT_FIELDS = ("order_id", "payment_time", "status", "channel", "category")
SCALE = 10 ** 6


def ratio(numerator: int, denominator: int) -> str | None:
    """精确有理数按 6 位小数四舍六入五成双，与 Decimal 默认舍入同义但只用整数运算。"""
    if denominator == 0:
        return None
    negative = (numerator < 0) != (denominator < 0) and numerator != 0
    quotient, remainder = divmod(abs(numerator) * SCALE, abs(denominator))
    if 2 * remainder > abs(denominator) or (2 * remainder == abs(denominator) and quotient % 2):
        quotient += 1
    return ("-" if negative else "") + f"{quotient // SCALE}.{quotient % SCALE:06d}"


def python_reference(path: Path, plan: AnalysisPlan) -> dict:
    if plan.schema == "olist":
        # 多表实现复用本模块的整数比值，模块之间只能延迟导入。
        from datalab.verification.olist_python import python_reference as olist_reference
        return olist_reference(path.parent, plan)
    with path.open(encoding="utf-8", newline="") as handle:
        orders = list(csv.DictReader(handle))
    if plan.kind == "quality":
        counts = dict.fromkeys(STATUSES, 0)
        for order in orders:
            if order["status"] in counts:
                counts[order["status"]] += 1
        return {"kind": "quality", "row_count": len(orders),
                "duplicate_orders": len(orders) - len({order["order_id"] for order in orders}),
                "missing_values": sum(order[name] == "" for order in orders for name in TEXT_FIELDS),
                "status_counts": counts}

    def chosen(start, end):
        return [order for order in orders if order["status"] in plan.included_statuses
                and (not start or order["payment_time"] >= start) and (not end or order["payment_time"] < end)]

    current = chosen(plan.start, plan.end)
    total = {"order_count": len(current), "amount_cents": sum(int(order["amount_cents"]) for order in current)}
    rows = []
    if plan.group_by:
        groups = {}
        for order in current:
            key = order["payment_time"][:10] if plan.group_by == "payment_date" else order[plan.group_by]
            entry = groups.setdefault(key, [0, 0])
            entry[0] += 1
            entry[1] += int(order["amount_cents"])
        names = sorted(groups)
        if plan.kind == "ranking":
            names.sort(key=lambda name: -groups[name][1])
        rows = [{"group": name, "order_count": groups[name][0], "amount_cents": groups[name][1]} for name in names]
        if plan.kind == "share":
            for row in rows:
                row["share"] = ratio(row["amount_cents"], total["amount_cents"])
    result = {"kind": plan.kind, "total": total, "rows": rows}
    if plan.kind == "comparison":
        previous = chosen(plan.previous_start, plan.previous_end)
        amount = sum(int(order["amount_cents"]) for order in previous)
        delta = total["amount_cents"] - amount
        result["comparison"] = {"previous_order_count": len(previous), "previous_amount_cents": amount,
                                "delta_cents": delta, "growth": ratio(delta, amount)}
    return result


def agreement(path: Path, plan: AnalysisPlan, sql_reference: dict) -> dict:
    """两份服务端参考逐项比较；只报告字段路径与 disagreement，不含任何数值。"""
    try:
        diffs = differences(sql_reference, python_reference(path, plan))
    except (OSError, ValueError, KeyError, TypeError):
        diffs = [{"path": "$", "issue": "disagreement"}]
    item = {"name": "reference_agreement", "passed": not diffs,
            "detail": "SQL 与 Python 两份独立参考一致" if not diffs else "两份服务端参考不一致，任务不能标为已核验"}
    if diffs:
        item["issues"] = [{"path": diff["path"], "issue": "disagreement"} for diff in diffs[:MAX_DIFFS]]
        item["issue_count"] = len(diffs)
    return item
