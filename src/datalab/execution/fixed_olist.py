"""Olist 多表的固定分析，可独立放入受限容器；不依赖宿主项目导入，只读取服务端投影到输入目录的表。

口径与 verification/olist_sql.py 的模块说明一致；这是固定演示与 run_fixed 使用的正确实现，仍要接受两份独立参考的核验。
"""
import csv
import json
import os
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

AMOUNT_KINDS = ("summary", "trend", "ranking", "share", "comparison")
UNKNOWN = "unknown"
OFFSETS = 6


def ratio(numerator, denominator):
    if denominator == 0:
        return None
    return str((Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.000001")))


def load(folder):
    tables = {}
    for name in sorted(os.listdir(folder)):
        if name.endswith(".csv"):
            with open(os.path.join(folder, name), encoding="utf-8", newline="") as handle:
                tables[name[:-4]] = list(csv.DictReader(handle))
    return tables


def month_index(stamp):
    return int(stamp[:4]) * 12 + int(stamp[5:7]) - 1


def month_name(index):
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def analyze(folder, plan):
    data = load(folder)
    field = plan["time_field"]
    statuses = set(plan["included_statuses"])

    def selected(start, end):
        return [order for order in data["orders"] if order["order_status"] in statuses and order[field]
                and (not start or order[field] >= start) and (not end or order[field] < end)]

    customers = {row["customer_id"]: row for row in data.get("customers", [])}
    kind = plan["kind"]
    if kind in AMOUNT_KINDS:
        return order_amounts(plan, data, selected, customers)
    if kind == "repeat_rate":
        return repeat_rate(plan, selected(plan["start"], plan["end"]), customers)
    if kind == "cohort_retention":
        return cohort_retention(plan, data["orders"], statuses, customers)
    if kind == "delivery_delay":
        return delivery_delay(plan, selected(plan["start"], plan["end"]), customers)
    return category_sales(data, selected(plan["start"], plan["end"]))


def order_amounts(plan, data, selected, customers):
    amounts = defaultdict(int)
    if plan["amount_source"] == "payments":
        for row in data["order_payments"]:
            amounts[row["order_id"]] += int(row["payment_value_cents"])
    else:
        for row in data["order_items"]:
            amounts[row["order_id"]] += int(row["price_cents"]) + int(row["freight_cents"])
    rows = selected(plan["start"], plan["end"])
    total = {"order_count": len(rows), "amount_cents": sum(amounts[row["order_id"]] for row in rows)}
    groups = defaultdict(lambda: {"order_count": 0, "amount_cents": 0})
    group_by = plan["group_by"]
    if group_by:
        for row in rows:
            if group_by == "customer_state":
                key = customers[row["customer_id"]]["customer_state"]
            else:
                key = row[plan["time_field"]][:10] if group_by == "purchase_date" else row[plan["time_field"]][:7]
            groups[key]["order_count"] += 1
            groups[key]["amount_cents"] += amounts[row["order_id"]]
    result_rows = [{"group": key, **value} for key, value in sorted(groups.items())]
    if plan["kind"] == "ranking":
        result_rows.sort(key=lambda row: (-row["amount_cents"], row["group"]))
    if plan["kind"] == "share":
        for row in result_rows:
            row["share"] = ratio(row["amount_cents"], total["amount_cents"])
    result = {"kind": plan["kind"], "total": total, "rows": result_rows}
    if plan["kind"] == "comparison":
        previous = selected(plan["previous_start"], plan["previous_end"])
        previous_amount = sum(amounts[row["order_id"]] for row in previous)
        delta = total["amount_cents"] - previous_amount
        result["comparison"] = {"previous_order_count": len(previous), "previous_amount_cents": previous_amount,
                                "delta_cents": delta, "growth": ratio(delta, previous_amount)}
    return result


def repeat_rate(plan, rows, customers):
    counts, states = defaultdict(int), {}
    for row in sorted(rows, key=lambda item: (item[plan["time_field"]], item["order_id"])):
        customer = customers[row["customer_id"]]
        person = customer["customer_unique_id"]
        counts[person] += 1
        states.setdefault(person, customer.get("customer_state"))
    repeat = sum(1 for count in counts.values() if count >= 2)
    result_rows = []
    if plan["group_by"]:
        groups = defaultdict(lambda: [0, 0])
        for person, count in counts.items():
            groups[states[person]][0] += 1
            groups[states[person]][1] += count >= 2
        result_rows = [{"group": key, "customer_count": value[0], "repeat_customer_count": value[1],
                        "repeat_rate": ratio(value[1], value[0])} for key, value in sorted(groups.items())]
    return {"kind": "repeat_rate", "total": {"customer_count": len(counts), "repeat_customer_count": repeat,
                                             "repeat_rate": ratio(repeat, len(counts))}, "rows": result_rows}


def cohort_retention(plan, orders, statuses, customers):
    field = plan["time_field"]
    if plan["end"]:
        limit = month_index(plan["end"])
        horizon = plan["end"]
    else:
        latest = max(order[field] for order in orders if order[field])
        # 观察终点为最晚时间 + 1 秒；只影响它是否恰为某月最后一秒。
        limit = month_index(latest) + (1 if latest[8:] == month_end(latest) else 0)
        horizon = None
    activity = defaultdict(list)
    for order in orders:
        if order["order_status"] in statuses and order[field] and (horizon is None or order[field] < horizon):
            activity[customers[order["customer_id"]]["customer_unique_id"]].append(order[field])
    cohorts = defaultdict(list)
    for stamps in activity.values():
        first = min(stamps)
        if (plan["start"] and first < plan["start"]) or month_index(first) >= limit:
            continue
        cohorts[month_index(first)].append({month_index(stamp) for stamp in stamps})
    result_rows = []
    for cohort in sorted(cohorts):
        members = cohorts[cohort]
        for offset in range(OFFSETS + 1):
            if cohort + offset >= limit:
                break
            retained = sum(1 for months in members if cohort + offset in months)
            result_rows.append({"cohort": month_name(cohort), "offset": offset, "customer_count": len(members),
                                "retained_count": retained, "retention": ratio(retained, len(members))})
    return {"kind": "cohort_retention", "total": {"customer_count": sum(len(items) for items in cohorts.values()),
                                                  "last_complete_month": month_name(limit - 1)}, "rows": result_rows}


def month_end(stamp):
    year, month = int(stamp[:4]), int(stamp[5:7])
    days = [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return f"{days[month - 1]:02d}T23:59:59"


def delivery_delay(plan, rows, customers):
    groups = defaultdict(lambda: [0, 0])
    delivered = late = 0
    for row in rows:
        if row["order_status"] != "delivered" or not row["order_delivered_customer_date"] or not row["order_estimated_delivery_date"]:
            continue
        overdue = row["order_delivered_customer_date"][:10] > row["order_estimated_delivery_date"][:10]
        delivered += 1
        late += overdue
        if plan["group_by"]:
            key = customers[row["customer_id"]]["customer_state"]
            groups[key][0] += 1
            groups[key][1] += overdue
    result_rows = [{"group": key, "delivered_count": value[0], "late_count": value[1], "late_rate": ratio(value[1], value[0])}
                   for key, value in sorted(groups.items())]
    return {"kind": "delivery_delay", "total": {"delivered_count": delivered, "late_count": late,
                                                "late_rate": ratio(late, delivered)}, "rows": result_rows}


def category_sales(data, rows):
    wanted = {row["order_id"] for row in rows}
    english = {row["product_category_name"]: row["product_category_name_english"] for row in data["category_translation"]}
    names = {row["product_id"]: row["product_category_name"] for row in data["products"]}
    groups = defaultdict(lambda: {"orders": set(), "item_count": 0, "price_cents": 0, "freight_cents": 0})
    for item in data["order_items"]:
        if item["order_id"] not in wanted:
            continue
        name = names.get(item["product_id"]) or ""
        key = english.get(name, name) if name else UNKNOWN
        groups[key]["orders"].add(item["order_id"])
        groups[key]["item_count"] += 1
        groups[key]["price_cents"] += int(item["price_cents"])
        groups[key]["freight_cents"] += int(item["freight_cents"])
    result_rows = [{"group": key, "order_count": len(value["orders"]), "item_count": value["item_count"],
                    "price_cents": value["price_cents"], "freight_cents": value["freight_cents"]}
                   for key, value in groups.items()]
    result_rows.sort(key=lambda row: (-row["price_cents"], row["group"]))
    total = {"order_count": len(set().union(*(value["orders"] for value in groups.values()))),
             "item_count": sum(row["item_count"] for row in result_rows),
             "price_cents": sum(row["price_cents"] for row in result_rows),
             "freight_cents": sum(row["freight_cents"] for row in result_rows)}
    return {"kind": "category_sales", "total": total, "rows": result_rows}


def export_result(result, output):
    output = Path(output)
    output.mkdir(exist_ok=True)
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    rows = result.get("rows") or [result.get("total")]
    with (output / "table.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        # 防止表格软件把用户文本当成公式；数值不变。
        writer.writerows({key: ("'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")) else value)
                          for key, value in row.items()} for row in rows)
