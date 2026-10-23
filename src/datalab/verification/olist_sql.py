"""Olist 多表的 SQL 参考：把规范化表载入内存 SQLite，用 SQL 连接与聚合独立复算，不导入或执行分析代码。

各分析类型的口径（与 verification/olist_python.py、execution/fixed_olist.py 同一契约，三处独立实现）：
- 共同：纳入订单 = order_status 属于口径状态，且计划时间字段非空并落在 [start, end) 内（start/end 为空表示不限）。
- summary/trend/ranking/share/comparison：订单级，金额来源由已确认口径 amount_source 决定——payments 为该单全部
  支付的 payment_value_cents 之和，items 为全部明细 price_cents + freight_cents 之和，没有子行时为 0。
  分组 customer_state（经 customer_id 连接客户）、purchase_date（时间前 10 位）、purchase_month（前 7 位）。
  输出与单表契约相同。
- repeat_rate：客户 = customer_unique_id；期间内至少 1 笔纳入订单的客户计入 customer_count，至少 2 笔计入
  repeat_customer_count；按州分组时，客户归入其期间内第一笔纳入订单（按时间、再按 order_id）所在的州，
  因此各州之和等于总计。
- cohort_retention：观察终点 H = end（未给 end 时为全部订单中最晚下单时间 + 1 秒）；活动 = H 之前的纳入订单；
  客户首单 = 全部活动中最早的一笔（不受 start 限制），cohort = 首单所在月；只统计首单不早于 start 的客户。
  月份 M 完整当且仅当下个月一号零点不晚于 H；只输出 cohort + offset（offset 0–6）为完整月份的行，
  之后的月份右删失、不输出。retained_count = 该 cohort 中在第 offset 个月有活动的客户数；offset 0 恒等于 customer_count。
- delivery_delay：只统计 order_status 为 delivered、送达日期与预计送达日期都不为空的纳入订单；按日期比较，
  送达日期（前 10 位）晚于预计送达日期即为延迟，同一天送达不算延迟。
- category_sales：纳入订单的全部明细，品类 = 商品品类的英文翻译；有品类但没有翻译时用原品类名，
  品类为空或商品不存在时为 unknown。order_count 为不同订单数（总计按订单去重，一单跨多个品类时各品类各算一次），
  item_count 为明细行数；行按 price_cents 降序、品类名升序。
比值都是 6 位小数字符串，分母为 0 时为 null；金额为整数分。
"""
import csv
from decimal import Decimal
from pathlib import Path
import sqlite3

from datalab.contracts import AMOUNT_KINDS, AnalysisPlan
from datalab.datasets import olist

OFFSETS = 6
INTEGER_COLUMNS = {"price_cents", "freight_cents", "payment_value_cents"}


def _fraction(numerator: int, denominator: int) -> str | None:
    return None if not denominator else format(Decimal(numerator) / Decimal(denominator), ".6f")


def _load(db: sqlite3.Connection, folder: Path, names):
    for name in names:
        columns = olist.columns(name)
        definition = ", ".join(f'"{column}" {"INTEGER" if column in INTEGER_COLUMNS else "TEXT"}' for column in columns)
        db.execute(f'CREATE TABLE "{name}" ({definition})')
        with (folder / f"{name}.csv").open(encoding="utf-8", newline="") as handle:
            db.executemany(f'INSERT INTO "{name}" VALUES ({", ".join("?" for _ in columns)})',
                           ([int(row[column]) if column in INTEGER_COLUMNS else row[column] for column in columns]
                            for row in csv.DictReader(handle)))


def _where(plan: AnalysisPlan, start, end, alias="o") -> tuple[str, list]:
    field = f'{alias}."{plan.time_field}"'
    clause = f"{alias}.order_status IN ({','.join('?' for _ in plan.included_statuses)}) AND {field} <> ''"
    args = list(plan.included_statuses)
    if start:
        clause += f" AND {field} >= ?"
        args.append(start)
    if end:
        clause += f" AND {field} < ?"
        args.append(end)
    return clause, args


def reference_result(folder: Path, plan: AnalysisPlan) -> dict:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        _load(db, folder, olist.needed(plan))
        if plan.kind in AMOUNT_KINDS:
            return _orders(db, plan)
        return {"repeat_rate": _repeat, "cohort_retention": _cohort, "delivery_delay": _delivery,
                "category_sales": _category}[plan.kind](db, plan)
    finally:
        db.close()


def _orders(db, plan: AnalysisPlan) -> dict:
    if plan.amount_source == "payments":
        amounts = "SELECT order_id, SUM(payment_value_cents) AS a FROM order_payments GROUP BY order_id"
    else:
        amounts = "SELECT order_id, SUM(price_cents + freight_cents) AS a FROM order_items GROUP BY order_id"
    join = 'LEFT JOIN customers c ON c.customer_id = o.customer_id' if plan.group_by == "customer_state" else ""
    base = f'FROM orders o LEFT JOIN ({amounts}) m ON m.order_id = o.order_id {join}'

    def totals(start, end):
        clause, args = _where(plan, start, end)
        row = db.execute(f"SELECT COUNT(*), COALESCE(SUM(COALESCE(m.a, 0)), 0) {base} WHERE {clause}", args).fetchone()
        return row[0], row[1]

    count, amount = totals(plan.start, plan.end)
    rows = []
    if plan.group_by:
        group = {"customer_state": "c.customer_state", "purchase_date": f'substr(o."{plan.time_field}", 1, 10)',
                 "purchase_month": f'substr(o."{plan.time_field}", 1, 7)'}[plan.group_by]
        sort = 'amount_cents DESC, "group"' if plan.kind == "ranking" else '"group"'
        clause, args = _where(plan, plan.start, plan.end)
        rows = [dict(row) for row in db.execute(
            f'SELECT {group} AS "group", COUNT(*) AS order_count, COALESCE(SUM(COALESCE(m.a, 0)), 0) AS amount_cents '
            f'{base} WHERE {clause} GROUP BY {group} ORDER BY {sort}', args)]
        if plan.kind == "share":
            for row in rows:
                row["share"] = _fraction(row["amount_cents"], amount)
    result = {"kind": plan.kind, "total": {"order_count": count, "amount_cents": amount}, "rows": rows}
    if plan.kind == "comparison":
        previous_count, previous_amount = totals(plan.previous_start, plan.previous_end)
        delta = amount - previous_amount
        result["comparison"] = {"previous_order_count": previous_count, "previous_amount_cents": previous_amount,
                                "delta_cents": delta, "growth": _fraction(delta, previous_amount)}
    return result


def _repeat(db, plan: AnalysisPlan) -> dict:
    clause, args = _where(plan, plan.start, plan.end)
    state = "c.customer_state" if plan.group_by else "''"
    db.execute(f'CREATE TEMP TABLE sel AS SELECT o.order_id, o."{plan.time_field}" AS t, c.customer_unique_id AS u, '
               f'{state} AS s FROM orders o JOIN customers c ON c.customer_id = o.customer_id WHERE {clause}', args)
    db.execute("CREATE TEMP TABLE per AS SELECT u, COUNT(*) AS n FROM sel GROUP BY u")
    customers, repeat = db.execute("SELECT COUNT(*), COALESCE(SUM(n >= 2), 0) FROM per").fetchone()
    rows = []
    if plan.group_by:
        query = ('SELECT f.s AS "group", COUNT(*) AS customer_count, SUM(p.n >= 2) AS repeat_customer_count FROM per p '
                 'JOIN (SELECT u, s FROM (SELECT u, s, ROW_NUMBER() OVER (PARTITION BY u ORDER BY t, order_id) AS rn '
                 'FROM sel) WHERE rn = 1) f ON f.u = p.u GROUP BY f.s ORDER BY f.s')
        rows = [{**dict(row), "repeat_rate": _fraction(row["repeat_customer_count"], row["customer_count"])}
                for row in db.execute(query)]
    return {"kind": "repeat_rate", "total": {"customer_count": customers, "repeat_customer_count": repeat,
                                             "repeat_rate": _fraction(repeat, customers)}, "rows": rows}


MONTH = "(CAST(substr({0}, 1, 4) AS INTEGER) * 12 + CAST(substr({0}, 6, 2) AS INTEGER) - 1)"


def _cohort(db, plan: AnalysisPlan) -> dict:
    field = f'o."{plan.time_field}"'
    if plan.end:
        horizon = plan.end
    else:
        latest = db.execute(f'SELECT MAX("{plan.time_field}") FROM orders').fetchone()[0]
        # 与时间字段相同的秒级格式；SQLite 的 datetime() 用空格分隔，这里用 strftime 固定为 T。
        horizon = db.execute("SELECT strftime('%Y-%m-%dT%H:%M:%S', ?, '+1 second')", (latest,)).fetchone()[0]
    limit = db.execute(f"SELECT {MONTH.format('?')}", (horizon, horizon)).fetchone()[0]
    statuses = ",".join("?" for _ in plan.included_statuses)
    db.execute(f"CREATE TEMP TABLE act AS SELECT c.customer_unique_id AS u, {field} AS t, {MONTH.format(field)} AS m "
               f"FROM orders o JOIN customers c ON c.customer_id = o.customer_id "
               f"WHERE o.order_status IN ({statuses}) AND {field} <> '' AND {field} < ?",
               [*plan.included_statuses, horizon])
    db.execute(f"CREATE TEMP TABLE coh AS SELECT u, {MONTH.format('MIN(t)')} AS cm FROM act GROUP BY u "
               f"HAVING (? IS NULL OR MIN(t) >= ?) AND {MONTH.format('MIN(t)')} < ?", (plan.start, plan.start, limit))
    offsets = " UNION ALL ".join(f"SELECT {index} AS k" for index in range(OFFSETS + 1))
    query = (f"WITH offs AS ({offsets}), size AS (SELECT cm, COUNT(*) AS n FROM coh GROUP BY cm), "
             "ret AS (SELECT coh.cm, act.m - coh.cm AS k, COUNT(DISTINCT coh.u) AS r FROM coh JOIN act ON act.u = coh.u "
             f"WHERE act.m - coh.cm BETWEEN 0 AND {OFFSETS} GROUP BY coh.cm, act.m - coh.cm) "
             "SELECT printf('%04d-%02d', size.cm / 12, size.cm % 12 + 1) AS cohort, offs.k AS \"offset\", "
             "size.n AS customer_count, COALESCE(ret.r, 0) AS retained_count FROM size JOIN offs ON size.cm + offs.k < ? "
             "LEFT JOIN ret ON ret.cm = size.cm AND ret.k = offs.k ORDER BY size.cm, offs.k")
    rows = [{**dict(row), "retention": _fraction(row["retained_count"], row["customer_count"])}
            for row in db.execute(query, (limit,))]
    customers = db.execute("SELECT COUNT(*) FROM coh").fetchone()[0]
    last = db.execute("SELECT printf('%04d-%02d', (? - 1) / 12, (? - 1) % 12 + 1)", (limit, limit)).fetchone()[0]
    return {"kind": "cohort_retention", "total": {"customer_count": customers, "last_complete_month": last}, "rows": rows}


def _delivery(db, plan: AnalysisPlan) -> dict:
    clause, args = _where(plan, plan.start, plan.end)
    clause += (" AND o.order_status = 'delivered' AND o.order_delivered_customer_date <> ''"
               " AND o.order_estimated_delivery_date <> ''")
    late = "substr(o.order_delivered_customer_date, 1, 10) > substr(o.order_estimated_delivery_date, 1, 10)"
    join = "JOIN customers c ON c.customer_id = o.customer_id" if plan.group_by else ""
    delivered, late_count = db.execute(f"SELECT COUNT(*), COALESCE(SUM({late}), 0) FROM orders o {join} WHERE {clause}",
                                       args).fetchone()
    rows = []
    if plan.group_by:
        rows = [{**dict(row), "late_rate": _fraction(row["late_count"], row["delivered_count"])} for row in db.execute(
            f'SELECT c.customer_state AS "group", COUNT(*) AS delivered_count, SUM({late}) AS late_count '
            f'FROM orders o {join} WHERE {clause} GROUP BY c.customer_state ORDER BY c.customer_state', args)]
    return {"kind": "delivery_delay", "total": {"delivered_count": delivered, "late_count": late_count,
                                                "late_rate": _fraction(late_count, delivered)}, "rows": rows}


def _category(db, plan: AnalysisPlan) -> dict:
    clause, args = _where(plan, plan.start, plan.end)
    db.execute("CREATE TEMP TABLE it AS SELECT i.order_id, i.price_cents AS p, i.freight_cents AS f, "
               "CASE WHEN pr.product_category_name IS NULL OR pr.product_category_name = '' THEN ? "
               "ELSE COALESCE(tr.product_category_name_english, pr.product_category_name) END AS g "
               "FROM order_items i JOIN orders o ON o.order_id = i.order_id "
               "LEFT JOIN products pr ON pr.product_id = i.product_id "
               "LEFT JOIN category_translation tr ON tr.product_category_name = pr.product_category_name "
               f"WHERE {clause}", [olist.UNKNOWN_CATEGORY, *args])
    total = dict(db.execute("SELECT COUNT(DISTINCT order_id) AS order_count, COUNT(*) AS item_count, "
                            "COALESCE(SUM(p), 0) AS price_cents, COALESCE(SUM(f), 0) AS freight_cents FROM it").fetchone())
    rows = [dict(row) for row in db.execute(
        'SELECT g AS "group", COUNT(DISTINCT order_id) AS order_count, COUNT(*) AS item_count, SUM(p) AS price_cents, '
        'SUM(f) AS freight_cents FROM it GROUP BY g ORDER BY price_cents DESC, g')]
    return {"kind": "category_sales", "total": total, "rows": rows}
