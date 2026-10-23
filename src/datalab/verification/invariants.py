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
    "invalid_group", "group_out_of_range", "empty_group", "rows_not_empty",
    # Olist 新类型：留存右删失越界或不完整、offset 0 留存不为 1。
    "censoring", "offset_zero"})
MAX_ISSUES = 12


def _int(value) -> bool:
    return type(value) is int


def ratio_text(numerator: int, denominator: int) -> str | None:
    """契约规定的 6 位小数比值（与分析框架 ratio、SQL 参考一致）；分母为 0 时为 None。"""
    return None if denominator == 0 else format(Decimal(numerator) / Decimal(denominator), ".6f")


def group_total(result: dict, keys: tuple[str, ...] = ("amount_cents", "order_count")) -> bool:
    """分组行的可加字段之和等于总计；结构不对时视为不平衡。group_total 检查与不变量共用。

    默认是订单数与金额；Olist 新类型的可加字段见 ADDITIVE（如品类销售只有明细数与金额可加，订单数按订单去重）。
    """
    try:
        rows, total = result.get("rows", []), result.get("total", {})
        return all(sum(row[key] for row in rows) == total[key] for key in keys)
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
        if plan.group_by in ("payment_date", "purchase_date"):
            try:
                day = date.fromisoformat(group).isoformat() == group and len(group) == 10
            except ValueError:
                day = False
            if not day:
                found.append({"path": prefix + "group", "issue": "invalid_group"})
            elif (plan.start and group < plan.start[:10]) or (plan.end and group > _last_date(plan.end)):
                found.append({"path": prefix + "group", "issue": "group_out_of_range"})
        elif plan.group_by == "purchase_month":
            if not _is_month(group):
                found.append({"path": prefix + "group", "issue": "invalid_group"})
            elif (plan.start and group < plan.start[:7]) or (plan.end and group > _last_date(plan.end)[:7]):
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
    if plan.kind in TOTALS:
        _olist(found, plan, result)
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


def _is_month(value) -> bool:
    return (isinstance(value, str) and len(value) == 7 and value[4] == "-" and value[:4].isdigit()
            and value[5:].isdigit() and 1 <= int(value[5:]) <= 12)


def _month_index(value: str) -> int:
    return int(value[:4]) * 12 + int(value[5:7]) - 1


# Olist 新类型的输出契约（口径见 verification/olist_sql.py）：总计键、行键、可加字段与比值 (字段, 分子, 分母)。
TOTALS = {
    "repeat_rate": ("customer_count", "repeat_customer_count", "repeat_rate"),
    "cohort_retention": ("customer_count", "last_complete_month"),
    "delivery_delay": ("delivered_count", "late_count", "late_rate"),
    "category_sales": ("order_count", "item_count", "price_cents", "freight_cents"),
}
ROWS = {
    "repeat_rate": ("group", "customer_count", "repeat_customer_count", "repeat_rate"),
    "cohort_retention": ("cohort", "offset", "customer_count", "retained_count", "retention"),
    "delivery_delay": ("group", "delivered_count", "late_count", "late_rate"),
    "category_sales": ("group", "order_count", "item_count", "price_cents", "freight_cents"),
}
ADDITIVE = {"repeat_rate": ("customer_count", "repeat_customer_count"), "delivery_delay": ("delivered_count", "late_count"),
            "category_sales": ("item_count", "price_cents", "freight_cents")}
RATIOS = {"repeat_rate": ("repeat_rate", "repeat_customer_count", "customer_count"),
          "delivery_delay": ("late_rate", "late_count", "delivered_count"),
          "cohort_retention": ("retention", "retained_count", "customer_count")}
# 部分 ≤ 整体：(部分, 整体)。
PARTS = {"repeat_rate": ("repeat_customer_count", "customer_count"), "delivery_delay": ("late_count", "delivered_count"),
         "cohort_retention": ("retained_count", "customer_count"), "category_sales": ("order_count", "item_count")}
# 每行至少有一个对象：分组只由被统计的客户、订单或明细产生。
NONEMPTY = {"repeat_rate": "customer_count", "delivery_delay": "delivered_count", "category_sales": "item_count",
            "cohort_retention": "customer_count"}
MAX_OFFSET = 6


def _ratio(found: list, value: dict, kind: str, prefix: str):
    name, numerator, denominator = RATIOS[kind]
    if name not in value or not (_int(value.get(numerator)) and _int(value.get(denominator))):
        return
    text, want_null = value[name], value[denominator] == 0
    if (text is None) != want_null or (text is not None and not isinstance(text, str)):
        found.append({"path": prefix + name, "issue": "type", "expected_type": "null" if want_null else "str"})
    elif text is not None and text != ratio_text(value[numerator], value[denominator]):
        found.append({"path": prefix + name, "issue": "ratio_mismatch"})


def _parts(found: list, value: dict, kind: str, prefix: str):
    part, whole = PARTS[kind]
    if _int(value.get(part)) and _int(value.get(whole)) and value[part] > value[whole]:
        found.append({"path": prefix + part, "issue": "bound"})


def _olist(found: list, plan: AnalysisPlan, result: dict):
    kind = plan.kind
    _keys(found, result, ("kind", "total", "rows"))
    total = result.get("total")
    total_ok = False
    if isinstance(total, dict):
        _keys(found, total, TOTALS[kind], "total.")
        counts = tuple(key for key in TOTALS[kind] if key not in ("repeat_rate", "late_rate", "last_complete_month"))
        total_ok = _counts(found, total, counts, "total.") and set(total) == set(TOTALS[kind])
        if total_ok and kind in RATIOS:
            _ratio(found, total, kind, "total.")
        if total_ok and kind in PARTS and kind != "cohort_retention":
            _parts(found, total, kind, "total.")
        if total_ok and kind == "category_sales" and total["item_count"] == 0 and (total["price_cents"] or total["freight_cents"]):
            found.append({"path": "total.price_cents", "issue": "zero_count_amount"})
        if kind == "cohort_retention" and "last_complete_month" in total:
            last = total["last_complete_month"]
            if not _is_month(last):
                found.append({"path": "total.last_complete_month", "issue": "type", "expected_type": "str"})
                total_ok = False
            elif plan.end and _month_index(last) != _month_index(plan.end) - 1:
                # 有终点时最后一个完整月份由计划直接决定：终点所在月的上一个月。
                found.append({"path": "total.last_complete_month", "issue": "censoring"})
    elif "total" in result:
        found.append({"path": "total", "issue": "type", "expected_type": "object"})
    rows = result.get("rows")
    if not isinstance(rows, list):
        if "rows" in result:
            found.append({"path": "rows", "issue": "type", "expected_type": "list"})
        return
    if not plan.group_by and kind in ("repeat_rate", "delivery_delay"):
        if rows:
            found.append({"path": "rows", "issue": "rows_not_empty"})
        return
    columns = ROWS[kind]
    valid, keys = [], []
    for index, row in enumerate(rows):
        prefix = f"rows[{index}]."
        if not isinstance(row, dict):
            found.append({"path": f"rows[{index}]", "issue": "type", "expected_type": "object"})
            continue
        _keys(found, row, columns, prefix)
        numbers = tuple(key for key in columns if key not in ("group", "cohort", "repeat_rate", "late_rate", "retention"))
        if not _counts(found, row, numbers, prefix):
            continue
        label = row.get("cohort" if kind == "cohort_retention" else "group")
        name = "cohort" if kind == "cohort_retention" else "group"
        if not isinstance(label, str):
            found.append({"path": prefix + name, "issue": "type", "expected_type": "str"})
            continue
        if kind == "cohort_retention" and not _is_month(label) or kind != "cohort_retention" and not label \
                or plan.group_by == "customer_state" and not (len(label) == 2 and label.isupper()):
            found.append({"path": prefix + name, "issue": "invalid_group"})
            continue
        if row[NONEMPTY[kind]] == 0:
            found.append({"path": prefix + NONEMPTY[kind], "issue": "empty_group"})
        if kind in RATIOS:
            _ratio(found, row, kind, prefix)
        _parts(found, row, kind, prefix)
        valid.append((index, row))
        keys.append((label, row["offset"]) if kind == "cohort_retention" else
                    (-row["price_cents"], label) if kind == "category_sales" else label)
    if len(set(keys)) != len(keys):
        found.append({"path": "rows", "issue": "duplicate_group"})
    for position in range(1, len(keys)):
        if keys[position] < keys[position - 1]:
            found.append({"path": f"rows[{valid[position][0]}]", "issue": "unsorted"})
            break
    complete = len(valid) == len(rows)
    if kind == "cohort_retention":
        _cohorts(found, plan, result, valid, total_ok)
    elif total_ok and complete:
        if not group_total(result, ADDITIVE[kind]):
            found.append({"path": "total", "issue": "sum_mismatch"})
        if kind == "category_sales":
            # 一单可跨多个品类：各品类订单数之和不少于去重后的总订单数，单个品类不超过总数。
            if sum(row["order_count"] for _, row in valid) < total["order_count"] or \
                    any(row["order_count"] > total["order_count"] for _, row in valid):
                found.append({"path": "total.order_count", "issue": "bound"})


def _cohorts(found: list, plan: AnalysisPlan, result: dict, valid: list, total_ok: bool):
    """每个 cohort 的 offset 从 0 连续递增且不超过 6，人数一致，offset 0 留存为 1，不越过最后一个完整月份。"""
    last = result["total"]["last_complete_month"] if total_ok else None
    groups = {}
    for index, row in valid:
        groups.setdefault(row["cohort"], []).append((index, row))
    for cohort, items in groups.items():
        offsets = [row["offset"] for _, row in items]
        if offsets != list(range(len(offsets))) or offsets[-1] > MAX_OFFSET:
            found.append({"path": f"rows[{items[0][0]}].offset", "issue": "censoring" if offsets[-1] > MAX_OFFSET else "bound"})
        if len({row["customer_count"] for _, row in items}) != 1:
            found.append({"path": f"rows[{items[0][0]}].customer_count", "issue": "sum_mismatch"})
        if items[0][1]["offset"] == 0 and items[0][1]["retained_count"] != items[0][1]["customer_count"]:
            found.append({"path": f"rows[{items[0][0]}].retained_count", "issue": "offset_zero"})
        if plan.start and cohort < plan.start[:7]:
            found.append({"path": f"rows[{items[0][0]}].cohort", "issue": "group_out_of_range"})
        if last and _is_month(last) and _month_index(cohort) + offsets[-1] > _month_index(last):
            found.append({"path": f"rows[{items[-1][0]}].offset", "issue": "censoring"})
    if total_ok and len(valid) == len(result["rows"]):
        if sum(items[0][1]["customer_count"] for items in groups.values()) != result["total"]["customer_count"]:
            found.append({"path": "total.customer_count", "issue": "sum_mismatch"})
        if last and _is_month(last) and groups:
            # 右删失之外的部分必须完整输出：最后一个 offset 要么是 6，要么下一个月份已越过最后完整月份。
            for cohort, items in groups.items():
                offset = items[-1][1]["offset"]
                if offset < MAX_OFFSET and _month_index(cohort) + offset < _month_index(last):
                    found.append({"path": f"rows[{items[-1][0]}].offset", "issue": "censoring"})


def check(plan: AnalysisPlan, result) -> dict:
    found = violations(plan, result)
    item = {"name": "invariants", "passed": not found,
            "detail": "结果满足输出契约的自洽关系" if not found else "结果违反输出契约的自洽关系"}
    if found:
        item["issues"] = found[:MAX_ISSUES]
        item["issue_count"] = len(found)
    return item
