"""语义层的独立 Python 参考：纯 Python 逐行实现同一套指标与维度，不用 SQL、不与编译器共享计算代码。

供 L1 核验：python_reference 对每个查询独立复算，verify 逐项比较语义层结果与参考。
口径与 metrics.py / compiler.py 同一契约；比值用整数银行家舍入（verification.python_reference.ratio），
与 SQL 端 Decimal 格式化同义但实现独立。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from datalab.datasets import olist
from datalab.semantic.dimensions import PRICE_BAND_TOP, PRICE_BANDS
from datalab.semantic.metrics import ADDITIVE, components, get_metric
from datalab.semantic.snapshot import SemanticSnapshot
from datalab.verification.python_reference import ratio

_GRAIN_TABLES = {
    "order": ("orders", "customers", "order_payments", "order_items"),
    "item": ("order_items", "orders", "customers", "products", "category_translation", "sellers"),
    "review": ("order_reviews", "orders", "customers"),
    "customer": ("orders", "customers"),
}


def _included(order: dict, snapshot: SemanticSnapshot, start: str | None, end: str | None) -> bool:
    stamp = order["order_purchase_timestamp"]
    return (order["order_status"] in snapshot.included_statuses
            and (not start or stamp >= start) and (not end or stamp < end))


def _first_orders(tables: dict, snapshot: SemanticSnapshot) -> dict[str, dict]:
    """每个 customer_unique_id 全部历史中的第一笔纳入订单（按时间、再按 order_id）。"""
    customers = {row["customer_id"]: row for row in tables["customers"]}
    first: dict[str, dict] = {}
    for order in sorted(tables["orders"], key=lambda row: (row["order_purchase_timestamp"], row["order_id"])):
        if order["order_status"] not in snapshot.included_statuses:
            continue
        person = customers[order["customer_id"]]["customer_unique_id"]
        if person not in first:
            first[person] = {"order_id": order["order_id"], "t": order["order_purchase_timestamp"],
                             "state": customers[order["customer_id"]]["customer_state"]}
    return first


def _dimension_value(name: str, ctx: dict) -> str:
    """ctx 按粒度给出 order/item/review/customer/payment/category/seller/first 等键。"""
    order = ctx["order"]
    # 客户级 ctx 以首单为准（按首单归州、按首单时间分组）；其余粒度用本订单的字段。
    by_first = "customer" not in ctx
    if name == "customer_state":
        return ctx["first"]["state"] if by_first else ctx["customer"]["customer_state"]
    if name == "purchase_date":
        return (ctx["first"]["t"] if by_first else order["order_purchase_timestamp"])[:10]
    if name == "purchase_month":
        return (ctx["first"]["t"] if by_first else order["order_purchase_timestamp"])[:7]
    if name == "payment_type":
        return ctx["payment"]["payment_type"] if ctx["payment"] else "unknown"
    if name == "payment_installments":
        return ctx["payment"]["payment_installments"] if ctx["payment"] else "unknown"
    if name == "customer_type":
        # 没有纳入订单的客户没有首单；该订单不是首笔纳入订单，归 returning（与 SQL 的 INNER JOIN 语义一致时，
        # 这些行本不会出现在允许 customer_type 的指标里）。
        return "new" if ctx["first"] and order["order_id"] == ctx["first"]["order_id"] else "returning"
    if name == "delivery_status":
        arrived, promised = order["order_delivered_customer_date"], order["order_estimated_delivery_date"]
        if order["order_status"] != "delivered" or not arrived:
            return "not_delivered"
        return "late" if arrived[:10] > promised[:10] else "on_time"
    if name == "product_category":
        return ctx["category"]
    if name == "price_band":
        price = int(ctx["item"]["price_cents"])
        for limit, label in PRICE_BANDS:
            if price < limit:
                return label
        return PRICE_BAND_TOP
    if name == "seller_id":
        return ctx["item"]["seller_id"]
    if name == "seller_state":
        return ctx["seller"]["seller_state"] if ctx["seller"] else "unknown"
    raise AssertionError(name)


def _passes(ctx: dict, filters: dict[str, tuple[str, ...]]) -> bool:
    return all(_dimension_value(name, ctx) in values for name, values in filters.items())


def python_reference(folder: Path, metric_name: str, period: tuple[str | None, str | None],
                     filters: dict | None, group_by: str | None, snapshot: SemanticSnapshot) -> dict:
    """对单个期间逐行计算指标；返回与引擎 _run 相同的结构。group_by 传入维度名。"""
    metric = get_metric(metric_name)
    tables = olist.read_tables(Path(folder), _GRAIN_TABLES[metric.grain])
    start, end = period
    numerator, denominator = components(metric)
    checked = {name: ((value,) if isinstance(value, str) else tuple(value)) for name, value in (filters or {}).items()}
    parts_total: dict[str, int] = {}
    parts_group: dict[str, dict[str, int]] = {}

    def add(ctx: dict, amounts: dict[str, int]):
        if not _passes(ctx, checked):
            return
        targets = [parts_total]
        if group_by:
            targets.append(parts_group.setdefault(_dimension_value(group_by, ctx), {}))
        for target in targets:
            for component, amount in amounts.items():
                target[component] = target.get(component, 0) + amount

    if metric.grain == "order":
        _orders(tables, snapshot, start, end, numerator, denominator, add,
                need_first="customer_type" in checked or group_by == "customer_type")
    elif metric.grain == "item":
        _items(tables, snapshot, start, end, add)
        # item_order_count：有明细的不同订单数，总计按订单去重；分组时按组的维度值去重（一单跨组时各组各计 1）。
        if "item_order_count" in (numerator, denominator):
            distinct_total, distinct_group = _item_orders(tables, snapshot, start, end, checked, group_by)
            parts_total["item_order_count"] = len(distinct_total)
            for grp, ids in distinct_group.items():
                parts_group.setdefault(grp, {})["item_order_count"] = len(ids)
    elif metric.grain == "review":
        _reviews(tables, snapshot, start, end, add)
    else:
        _customers(tables, snapshot, start, end, numerator, denominator, add)

    total = _assemble(metric, parts_total, numerator, denominator)
    rows = [{"group": grp, **_assemble(metric, parts, numerator, denominator)}
            for grp, parts in sorted(parts_group.items())]
    return {"metric": metric.name, "grain": metric.grain, "type": metric.kind, "unit": metric.unit,
            "period": {"start": start, "end": end}, "total": total, "rows": rows}


def _orders(tables, snapshot, start, end, numerator, denominator, add, *, need_first=False):
    customers = {row["customer_id"]: row for row in tables["customers"]}
    amounts = {}
    if "gmv" in (numerator, denominator):
        source = snapshot.require_amount_source()
        if source == "payments":
            for row in tables["order_payments"]:
                amounts[row["order_id"]] = amounts.get(row["order_id"], 0) + int(row["payment_value_cents"])
        else:
            for row in tables["order_items"]:
                amounts[row["order_id"]] = amounts.get(row["order_id"], 0) \
                    + int(row["price_cents"]) + int(row["freight_cents"])
    payments: dict[str, dict] = {}
    for row in tables["order_payments"]:
        key = row["order_id"]
        if key not in payments or int(row["payment_sequential"]) < int(payments[key]["payment_sequential"]):
            payments[key] = row
    # customer_type 维度才需要首单；首单只覆盖有纳入订单的客户。
    first = _first_orders(tables, snapshot) if need_first else {}
    for order in tables["orders"]:
        person = customers[order["customer_id"]]["customer_unique_id"]
        ctx = {"order": order, "customer": customers[order["customer_id"]],
               "payment": payments.get(order["order_id"]), "first": first.get(person)}
        chosen = _included(order, snapshot, start, end)
        stamp = order["order_purchase_timestamp"]
        in_period = (not start or stamp >= start) and (not end or stamp < end)
        arrived, promised = order["order_delivered_customer_date"], order["order_estimated_delivery_date"]
        delivered_ok = chosen and order["order_status"] == "delivered" and arrived and promised
        # 每个成分是否选中本行（对应 SQL 的 WHERE）：分组行与总计都以此为准。
        selected = {"gmv": chosen, "order_count": chosen, "cancel_count": in_period and order["order_status"] == "canceled",
                    "all_order_count": in_period, "late_count": delivered_ok, "delivered_dated_count": delivered_ok,
                    "delivery_days_sum": delivered_ok}
        parts: dict[str, int] = {}
        for component in {numerator, denominator} - {None}:
            if not selected[component]:
                parts[component] = 0
            elif component == "gmv":
                parts["gmv"] = amounts.get(order["order_id"], 0)
            elif component in ("order_count", "cancel_count", "all_order_count", "delivered_dated_count"):
                parts[component] = 1
            elif component == "late_count":
                parts[component] = 1 if arrived[:10] > promised[:10] else 0
            elif component == "delivery_days_sum":
                parts[component] = (datetime.fromisoformat(arrived[:10])
                                    - datetime.fromisoformat(stamp[:10])).days
        # 成分值为 0 的选中行同样产生分组行与总计（与 SQL 的 COUNT/SUM 一致）。
        if any(selected.get(component, False) for component in parts):
            add(ctx, parts)


def _items(tables, snapshot, start, end, add):
    customers = {row["customer_id"]: row for row in tables["customers"]}
    orders = {row["order_id"]: row for row in tables["orders"]}
    english = {row["product_category_name"]: row["product_category_name_english"]
               for row in tables["category_translation"]}
    category = {row["product_id"]: row["product_category_name"] for row in tables["products"]}
    sellers = {row["seller_id"]: row for row in tables["sellers"]}
    for item in tables["order_items"]:
        order = orders[item["order_id"]]
        if not _included(order, snapshot, start, end):
            continue
        name = category.get(item["product_id"], "")
        name = english.get(name, name) if name else olist.UNKNOWN_CATEGORY
        ctx = {"order": order, "item": item, "customer": customers[order["customer_id"]],
               "category": name, "seller": sellers.get(item["seller_id"])}
        price, freight = int(item["price_cents"]), int(item["freight_cents"])
        add(ctx, {"item_gmv": price + freight, "item_count": 1, "item_price_sum": price, "freight_sum": freight})


def _item_orders(tables, snapshot, start, end, filters, group_by) -> tuple[set, dict]:
    """有明细的不同订单数：总计按订单去重；分组时按明细的维度值归组后去重（一单跨组时各组各计 1，与 SQL 的 COUNT(DISTINCT) 一致）。"""
    customers = {row["customer_id"]: row for row in tables["customers"]}
    orders = {row["order_id"]: row for row in tables["orders"]}
    english = {row["product_category_name"]: row["product_category_name_english"]
               for row in tables["category_translation"]}
    category = {row["product_id"]: row["product_category_name"] for row in tables["products"]}
    sellers = {row["seller_id"]: row for row in tables["sellers"]}
    total: set[str] = set()
    groups: dict[str, set] = {}
    for item in tables["order_items"]:
        order = orders[item["order_id"]]
        if not _included(order, snapshot, start, end):
            continue
        name = category.get(item["product_id"], "")
        name = english.get(name, name) if name else olist.UNKNOWN_CATEGORY
        ctx = {"order": order, "item": item, "customer": customers[order["customer_id"]],
               "category": name, "seller": sellers.get(item["seller_id"])}
        if not _passes(ctx, filters):
            continue
        total.add(item["order_id"])
        if group_by:
            groups.setdefault(_dimension_value(group_by, ctx), set()).add(item["order_id"])
    return total, groups


def _reviews(tables, snapshot, start, end, add):
    customers = {row["customer_id"]: row for row in tables["customers"]}
    orders = {row["order_id"]: row for row in tables["orders"]}
    first = _first_orders(tables, snapshot)
    for review in tables["order_reviews"]:
        order = orders[review["order_id"]]
        if not _included(order, snapshot, start, end):
            continue
        person = customers[order["customer_id"]]["customer_unique_id"]
        ctx = {"order": order, "review": review, "customer": customers[order["customer_id"]], "first": first[person]}
        score = int(review["review_score"])
        add(ctx, {"review_score_sum": score, "review_count": 1, "bad_review_count": 1 if score <= 2 else 0})


def _customers(tables, snapshot, start, end, numerator, denominator, add):
    customers = {row["customer_id"]: row for row in tables["customers"]}
    first = _first_orders(tables, snapshot)
    counts: dict[str, int] = {}
    for order in tables["orders"]:
        if _included(order, snapshot, start, end):
            person = customers[order["customer_id"]]["customer_unique_id"]
            counts[person] = counts.get(person, 0) + 1
    for person, first_order in first.items():
        ctx = {"first": first_order, "order": {"order_id": first_order["order_id"]}}
        parts: dict[str, int] = {}
        for component in {numerator, denominator} - {None}:
            if component == "customer_count":
                parts[component] = 1 if counts.get(person, 0) >= 1 else 0
            elif component == "repeat_customer_count":
                parts[component] = 1 if counts.get(person, 0) >= 2 else 0
            elif component == "new_customers":
                parts[component] = 1 if (not start or first_order["t"] >= start) \
                    and (not end or first_order["t"] < end) else 0
            elif component == "returning_customers":
                parts[component] = 1 if bool(start) and counts.get(person, 0) >= 1 and first_order["t"] < start else 0
        if any(parts.values()):
            add(ctx, parts)


def _assemble(metric, parts: dict[str, int], numerator: str, denominator: str | None) -> dict:
    if metric.kind == ADDITIVE:
        return {"numerator": parts.get(metric.name, 0), "denominator": None, "value": parts.get(metric.name, 0)}
    top, bottom = parts.get(numerator, 0), parts.get(denominator, 0)
    return {"numerator": top, "denominator": bottom, "value": ratio(top, bottom)}


def verify(engine_result: dict, folder: Path, snapshot: SemanticSnapshot, *,
           filters: dict | None = None, group_by: str | None = None) -> dict:
    """逐项比较语义层引擎结果与 Python 参考；返回 {passed, differences}（只含路径与问题类型）。"""
    period = (engine_result["period"]["start"], engine_result["period"]["end"])
    reference = python_reference(folder, engine_result["metric"], period, filters, group_by, snapshot)
    differences = _diff(engine_result, reference, "$")
    return {"passed": not differences, "differences": differences[:20]}


def _diff(left, right, path: str) -> list[dict]:
    if type(left) is not type(right):
        return [{"path": path, "issue": "type"}]
    if isinstance(left, dict):
        if set(left) != set(right):
            return [{"path": path, "issue": "keys"}]
        return [item for key in left for item in _diff(left[key], right[key], f"{path}.{key}")]
    if isinstance(left, list):
        if len(left) != len(right):
            return [{"path": path, "issue": "length"}]
        return [item for index, pair in enumerate(zip(left, right)) for item in _diff(*pair, f"{path}[{index}]")]
    return [] if left == right else [{"path": path, "issue": "value"}]
