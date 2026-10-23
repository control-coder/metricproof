"""Olist 多表的第二份参考：纯 Python 逐行连接与整数累加，不用 SQL，也不与 SQL 参考共享计算代码。

口径见 verification/olist_sql.py 的模块说明；月份用 datetime 推算，比值用整数银行家舍入（python_reference.ratio）。
运行时与 SQL 参考逐项比较（reference_agreement），不一致说明服务端参考本身有缺陷。
"""
from datetime import datetime, timedelta
from pathlib import Path

from datalab.contracts import AMOUNT_KINDS, AnalysisPlan
from datalab.datasets import olist
from datalab.verification.python_reference import ratio

OFFSETS = 6


def _month(stamp: str) -> tuple[int, int]:
    moment = datetime.fromisoformat(stamp)
    return moment.year, moment.month


def _advance(month: tuple[int, int], steps: int) -> tuple[int, int]:
    year, number = month
    number += steps
    while number > 12:
        year, number = year + 1, number - 12
    return year, number


def _first_day(month: tuple[int, int]) -> datetime:
    return datetime(month[0], month[1], 1)


def _name(month: tuple[int, int]) -> str:
    return f"{month[0]:04d}-{month[1]:02d}"


def python_reference(folder: Path, plan: AnalysisPlan) -> dict:
    tables = olist.read_tables(folder, olist.needed(plan))
    field = plan.time_field
    statuses = set(plan.included_statuses)

    def chosen(start, end) -> list[dict]:
        found = []
        for order in tables["orders"]:
            value = order[field]
            if order["order_status"] not in statuses or not value:
                continue
            if start and value < start or end and value >= end:
                continue
            found.append(order)
        return found

    customers = {row["customer_id"]: row for row in tables.get("customers", [])}
    if plan.kind in AMOUNT_KINDS:
        return _orders(plan, tables, chosen, customers)
    if plan.kind == "repeat_rate":
        return _repeat(plan, chosen(plan.start, plan.end), customers)
    if plan.kind == "cohort_retention":
        return _cohort(plan, tables["orders"], statuses, customers)
    if plan.kind == "delivery_delay":
        return _delivery(plan, chosen(plan.start, plan.end), customers)
    return _category(tables, chosen(plan.start, plan.end))


def _orders(plan, tables, chosen, customers) -> dict:
    amount = {}
    if plan.amount_source == "payments":
        for row in tables["order_payments"]:
            amount[row["order_id"]] = amount.get(row["order_id"], 0) + int(row["payment_value_cents"])
    else:
        for row in tables["order_items"]:
            amount[row["order_id"]] = amount.get(row["order_id"], 0) + int(row["price_cents"]) + int(row["freight_cents"])

    def tally(orders):
        return len(orders), sum(amount.get(order["order_id"], 0) for order in orders)

    current = chosen(plan.start, plan.end)
    count, total = tally(current)
    rows = []
    if plan.group_by:
        groups = {}
        for order in current:
            if plan.group_by == "customer_state":
                key = customers[order["customer_id"]]["customer_state"]
            else:
                key = order[plan.time_field][:10 if plan.group_by == "purchase_date" else 7]
            entry = groups.setdefault(key, [0, 0])
            entry[0] += 1
            entry[1] += amount.get(order["order_id"], 0)
        names = sorted(groups)
        if plan.kind == "ranking":
            names.sort(key=lambda name: -groups[name][1])
        rows = [{"group": name, "order_count": groups[name][0], "amount_cents": groups[name][1]} for name in names]
        if plan.kind == "share":
            for row in rows:
                row["share"] = ratio(row["amount_cents"], total)
    result = {"kind": plan.kind, "total": {"order_count": count, "amount_cents": total}, "rows": rows}
    if plan.kind == "comparison":
        previous_count, previous = tally(chosen(plan.previous_start, plan.previous_end))
        result["comparison"] = {"previous_order_count": previous_count, "previous_amount_cents": previous,
                                "delta_cents": total - previous, "growth": ratio(total - previous, previous)}
    return result


def _repeat(plan, orders, customers) -> dict:
    seen = {}
    # 按 (时间, 订单编号) 顺序扫描，第一次遇到客户时记下州。
    for order in sorted(orders, key=lambda row: (row[plan.time_field], row["order_id"])):
        customer = customers[order["customer_id"]]
        person = customer["customer_unique_id"]
        if person not in seen:
            seen[person] = [customer.get("customer_state", ""), 0]
        seen[person][1] += 1
    repeat = sum(1 for _, count in seen.values() if count >= 2)
    rows = []
    if plan.group_by:
        groups = {}
        for state, count in seen.values():
            entry = groups.setdefault(state, [0, 0])
            entry[0] += 1
            entry[1] += count >= 2
        rows = [{"group": state, "customer_count": groups[state][0], "repeat_customer_count": groups[state][1],
                 "repeat_rate": ratio(groups[state][1], groups[state][0])} for state in sorted(groups)]
    return {"kind": "repeat_rate", "total": {"customer_count": len(seen), "repeat_customer_count": repeat,
                                             "repeat_rate": ratio(repeat, len(seen))}, "rows": rows}


def _cohort(plan, orders, statuses, customers) -> dict:
    field = plan.time_field
    if plan.end:
        horizon = datetime.fromisoformat(plan.end)
    else:
        horizon = datetime.fromisoformat(max(order[field] for order in orders if order[field])) + timedelta(seconds=1)

    def complete(month) -> bool:
        return _first_day(_advance(month, 1)) <= horizon

    activity = {}
    for order in orders:
        value = order[field]
        if order["order_status"] in statuses and value and datetime.fromisoformat(value) < horizon:
            activity.setdefault(customers[order["customer_id"]]["customer_unique_id"], []).append(value)
    cohorts = {}
    for person, stamps in activity.items():
        first = min(stamps)
        if plan.start and first < plan.start or not complete(_month(first)):
            continue
        cohorts.setdefault(_month(first), []).append({_month(stamp) for stamp in stamps})
    rows = []
    for month in sorted(cohorts):
        members = cohorts[month]
        for offset in range(OFFSETS + 1):
            target = _advance(month, offset)
            if not complete(target):
                break
            retained = sum(1 for months in members if target in months)
            rows.append({"cohort": _name(month), "offset": offset, "customer_count": len(members),
                         "retained_count": retained, "retention": ratio(retained, len(members))})
    last = (horizon.year, horizon.month - 1) if horizon.month > 1 else (horizon.year - 1, 12)
    return {"kind": "cohort_retention", "total": {"customer_count": sum(len(items) for items in cohorts.values()),
                                                  "last_complete_month": _name(last)}, "rows": rows}


def _delivery(plan, orders, customers) -> dict:
    groups = {}
    delivered = late = 0
    for order in orders:
        arrived, promised = order["order_delivered_customer_date"], order["order_estimated_delivery_date"]
        if order["order_status"] != "delivered" or not arrived or not promised:
            continue
        overdue = datetime.fromisoformat(arrived).date() > datetime.fromisoformat(promised).date()
        delivered += 1
        late += overdue
        if plan.group_by:
            entry = groups.setdefault(customers[order["customer_id"]]["customer_state"], [0, 0])
            entry[0] += 1
            entry[1] += overdue
    rows = [{"group": state, "delivered_count": groups[state][0], "late_count": groups[state][1],
             "late_rate": ratio(groups[state][1], groups[state][0])} for state in sorted(groups)]
    return {"kind": "delivery_delay", "total": {"delivered_count": delivered, "late_count": late,
                                                "late_rate": ratio(late, delivered)}, "rows": rows}


def _category(tables, orders) -> dict:
    wanted = {order["order_id"] for order in orders}
    english = {row["product_category_name"]: row["product_category_name_english"] for row in tables["category_translation"]}
    category = {row["product_id"]: row["product_category_name"] for row in tables["products"]}
    groups, total_orders = {}, set()
    item_count = price = freight = 0
    for item in tables["order_items"]:
        if item["order_id"] not in wanted:
            continue
        name = category.get(item["product_id"], "")
        name = english.get(name, name) if name else olist.UNKNOWN_CATEGORY
        entry = groups.setdefault(name, [set(), 0, 0, 0])
        entry[0].add(item["order_id"])
        entry[1] += 1
        entry[2] += int(item["price_cents"])
        entry[3] += int(item["freight_cents"])
        total_orders.add(item["order_id"])
        item_count += 1
        price += int(item["price_cents"])
        freight += int(item["freight_cents"])
    names = sorted(groups)
    names.sort(key=lambda name: -groups[name][2])
    rows = [{"group": name, "order_count": len(groups[name][0]), "item_count": groups[name][1],
             "price_cents": groups[name][2], "freight_cents": groups[name][3]} for name in names]
    return {"kind": "category_sales", "total": {"order_count": len(total_orders), "item_count": item_count,
                                                "price_cents": price, "freight_cents": freight}, "rows": rows}
