"""把已核验结果的关键字段套入固定中文模板；不经模型，句中每个数都能指回 result.json。"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

GROUP_TITLES = {"channel": "渠道", "category": "品类", "payment_date": "支付日期",
                "customer_state": "州", "purchase_date": "下单日期", "purchase_month": "下单月份", "product_category": "品类"}
STATUS_TITLES = {"paid": "已支付", "refunded": "已退款", "cancelled": "已取消"}
# Olist 订单状态与金额来源的中文名；只用于 Olist 计划，单表说明不变。
OLIST_STATUS_TITLES = {"delivered": "已送达", "shipped": "已发货", "canceled": "已取消", "unavailable": "不可用",
                       "invoiced": "已开票", "processing": "处理中", "created": "已创建", "approved": "已批准"}
AMOUNT_SOURCE_TITLES = {"payments": "该单全部支付之和", "items": "该单全部明细价格加运费之和"}


def _money(cents: int) -> str:
    return f"{Decimal(cents) / 100:,.2f}"


def _signed_money(cents: int) -> str:
    return ("+" if cents > 0 else "") + _money(cents)


def _percent(ratio) -> str:
    value = (Decimal(ratio) * 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return f"{value}%"


def _stamp(value: str) -> str:
    return value.replace("T", " ")


def period_text(start: str | None, end: str | None, data_range: tuple[str, str] | None = None) -> str:
    """期间按左闭右开书写；未限定时写出数据自身的起止（含终点）。"""
    if start and end:
        return f"{_stamp(start)} 至 {_stamp(end)}（不含终点）"
    if start:
        return f"{_stamp(start)} 起"
    if end:
        return f"{_stamp(end)} 之前"
    if data_range:
        return f"全部数据期间 {_stamp(data_range[0])} 至 {_stamp(data_range[1])}"
    return "全部数据期间"


def describe(result: dict, plan: dict, *, metric_version: str, included_statuses: tuple[str, ...] | list[str],
             currency: str, data_range: tuple[str, str] | None = None) -> dict:
    """返回 2–3 句说明及每句引用的结果字段；输入相同则输出相同。"""
    kind = plan["kind"]
    if plan.get("schema") == "olist":
        return _olist(result, plan, metric_version=metric_version, included_statuses=included_statuses,
                       currency=currency, data_range=data_range)
    statuses = "、".join(STATUS_TITLES.get(item, item) for item in included_statuses)
    scope = f"口径版本 {metric_version}（纳入{statuses}订单）"
    period = period_text(plan.get("start"), plan.get("end"), data_range)
    sentences: list[tuple[str, list[str]]] = []
    if kind == "quality":
        counts = result["status_counts"]
        parts = "、".join(f"{STATUS_TITLES[key]} {counts.get(key, 0):,}" for key in STATUS_TITLES)
        sentences.append((f"数据质量检查覆盖全部 {result['row_count']:,} 行（{period_text(None, None, data_range)}），"
                          f"不按订单状态过滤；本任务关联{scope}。", ["/row_count"]))
        sentences.append((f"其中{parts} 笔；缺失值 {result['missing_values']:,} 个，"
                          f"重复订单编号 {result['duplicate_orders']:,} 个。",
                          ["/status_counts", "/missing_values", "/duplicate_orders"]))
        return _pack(sentences, metric_version, period_text(None, None, data_range))
    return _amounts(result, plan, scope, period, currency, metric_version)


def _amounts(result: dict, plan: dict, scope: str, period: str, currency: str, metric_version: str) -> dict:
    """订单级金额分析（单表与 Olist 共用）；只有 Olist 按月趋势的措辞不同。"""
    kind = plan["kind"]
    sentences: list[tuple[str, list[str]]] = []
    total = result["total"]
    if kind == "comparison":
        comparison = result["comparison"]
        previous = period_text(plan.get("previous_start"), plan.get("previous_end"))
        sentences.append((f"按{scope}，本期 {period}共 {total['order_count']:,} 笔订单、金额 "
                          f"{_money(total['amount_cents'])} {currency}；上期 {previous}共 "
                          f"{comparison['previous_order_count']:,} 笔、金额 {_money(comparison['previous_amount_cents'])} {currency}。",
                          ["/total", "/comparison/previous_order_count", "/comparison/previous_amount_cents"]))
        growth = comparison["growth"]
        rate = "上期金额为 0，不计算环比变化率" if growth is None else f"环比变化率 {('+' if Decimal(growth) > 0 else '')}{_percent(growth)}"
        count_delta = total["order_count"] - comparison["previous_order_count"]
        sentences.append((f"金额较上期变化 {_signed_money(comparison['delta_cents'])} {currency}，{rate}；"
                          f"订单数变化 {('+' if count_delta > 0 else '')}{count_delta:,} 笔。",
                          ["/comparison/delta_cents", "/comparison/growth"]))
        return _pack(sentences, metric_version, period, previous)
    sentences.append((f"按{scope}，{period}，共有 {total['order_count']:,} 笔订单，金额合计 "
                      f"{_money(total['amount_cents'])} {currency}。", ["/total"]))
    rows = result.get("rows") or []
    if total["order_count"] == 0:
        sentences.append(("该期间没有符合口径的订单，未生成分组结论。", ["/total/order_count"]))
        return _pack(sentences, metric_version, period)
    if kind == "summary":
        average = (Decimal(total["amount_cents"]) / total["order_count"]).quantize(Decimal("1"), ROUND_HALF_UP)
        sentences.append((f"平均每笔订单金额 {_money(int(average))} {currency}。", ["/total"]))
        return _pack(sentences, metric_version, period)
    name = GROUP_TITLES.get(plan.get("group_by"), "分组")
    ordered = sorted(rows, key=lambda row: (-row["amount_cents"], str(row["group"])))
    top = ordered[0]
    if kind == "trend":
        unit, single = ("月份", "单月") if plan.get("group_by") == "purchase_month" else ("日期", "单日")
        low = min(rows, key=lambda row: (row["amount_cents"], str(row["group"])))
        sentences.append((f"共 {len(rows):,} 个有订单的{unit}，{single}金额最高为 {top['group']}（{_money(top['amount_cents'])} {currency}），"
                          f"最低为 {low['group']}（{_money(low['amount_cents'])} {currency}）。", ["/rows"]))
        first, last = rows[0], rows[-1]
        if len(rows) > 1:
            sentences.append((f"首个{unit} {first['group']} 与最后{unit} {last['group']} 的{single}金额相差 "
                              f"{_signed_money(last['amount_cents'] - first['amount_cents'])} {currency}，仅为描述性比较。",
                              ["/rows/0/amount_cents", f"/rows/{len(rows) - 1}/amount_cents"]))
        return _pack(sentences, metric_version, period)
    ratio = (Decimal(top["amount_cents"]) / Decimal(total["amount_cents"])) if total["amount_cents"] else None
    share = f"，占合计 {_percent(ratio)}" if ratio is not None else ""
    sentences.append((f"共 {len(rows):,} 个{name}，金额最高的是“{top['group']}”（{_money(top['amount_cents'])} {currency}{share}）。",
                      ["/rows"]))
    if len(ordered) > 1:
        second = ordered[1]
        if kind == "share" and total["amount_cents"] and len(ordered) > 2:
            head = sum(row["amount_cents"] for row in ordered[:3])
            sentences.append((f"金额前三的{name}合计占 {_percent(Decimal(head) / Decimal(total['amount_cents']))}；"
                              "分组差异为描述性分解，不推断原因。", ["/rows"]))
        else:
            sentences.append((f"其次为“{second['group']}”（{_money(second['amount_cents'])} {currency}）；"
                              "分组差异为描述性分解，不推断原因。", ["/rows"]))
    return _pack(sentences, metric_version, period)


def _rate(value) -> str:
    return "不计算（分母为 0）" if value is None else _percent(value)


def _olist(result: dict, plan: dict, *, metric_version: str, included_statuses, currency: str,
           data_range: tuple[str, str] | None) -> dict:
    """Olist 多表：首句写明口径版本、纳入状态（与订单金额来源）和期间，其余句子只引用 result.json 中的字段。"""
    kind = plan["kind"]
    statuses = "、".join(OLIST_STATUS_TITLES.get(item, item) for item in included_statuses)
    source = AMOUNT_SOURCE_TITLES.get(plan.get("amount_source"))
    scope = f"口径版本 {metric_version}（纳入{statuses}订单" + (f"，订单金额为{source}" if source else "") + "）"
    field = "送达日期" if plan.get("time_field") == "order_delivered_customer_date" else "下单时间"
    period = period_text(plan.get("start"), plan.get("end"), data_range)
    if kind not in ("repeat_rate", "cohort_retention", "delivery_delay", "category_sales"):
        return _amounts(result, plan, scope, period, currency, metric_version)
    total, rows = result["total"], result.get("rows") or []
    sentences: list[tuple[str, list[str]]] = []
    if kind == "repeat_rate":
        sentences.append((f"按{scope}，{period}（按{field}）的下单客户（按 customer_unique_id 计）共 {total['customer_count']:,} 位，"
                          f"其中 {total['repeat_customer_count']:,} 位至少下单 2 次，复购率 {_rate(total['repeat_rate'])}。", ["/total"]))
        if rows:
            top = sorted(rows, key=lambda row: (-row["customer_count"], str(row["group"])))[0]
            sentences.append((f"共 {len(rows):,} 个州，下单客户最多的是 {top['group']}（{top['customer_count']:,} 位，复购率 "
                              f"{_rate(top['repeat_rate'])}）；客户按期间内第一笔订单归州，分组差异为描述性分解，不推断原因。", ["/rows"]))
        return _pack(sentences, metric_version, period)
    if kind == "cohort_retention":
        cohorts = [row for row in rows if row["offset"] == 0]
        observed = f"观察至 {total['last_complete_month']}（含），之后不完整的月份右删失、不输出"
        if not cohorts:
            sentences.append((f"按{scope}，没有首单落在{period}且首单月份完整的客户；{observed}。", ["/total"]))
            return _pack(sentences, metric_version, period)
        sentences.append((f"按{scope}，首单在 {cohorts[0]['cohort']} 至 {cohorts[-1]['cohort']} 的 {len(cohorts):,} 个月度 cohort "
                          f"共 {total['customer_count']:,} 位客户；{observed}。", ["/total", "/rows"]))
        first = [row for row in rows if row["offset"] == 1]
        if first:
            retained = sum(row["retained_count"] for row in first)
            size = sum(row["customer_count"] for row in first)
            best = max(first, key=lambda row: (Decimal(row["retained_count"]) / Decimal(row["customer_count"]), row["cohort"]))
            sentences.append((f"首单后第 1 个月仍有下单的客户合计 {retained:,} 位，占这些 cohort 的 "
                              f"{_percent(Decimal(retained) / Decimal(size))}；比例最高的是 {best['cohort']}（{_rate(best['retention'])}）。",
                              ["/rows"]))
        return _pack(sentences, metric_version, period)
    if kind == "delivery_delay":
        sentences.append((f"按{scope}，{period}（按{field}）的已送达订单（送达与预计送达日期都已知）共 {total['delivered_count']:,} 笔，"
                          f"其中 {total['late_count']:,} 笔晚于预计送达日期，延迟率 {_rate(total['late_rate'])}；按日期比较，同一天送达不算延迟。",
                          ["/total"]))
        if rows:
            top = sorted(rows, key=lambda row: (-row["delivered_count"], str(row["group"])))[0]
            sentences.append((f"共 {len(rows):,} 个州，已送达订单最多的是 {top['group']}（{top['delivered_count']:,} 笔，延迟率 "
                              f"{_rate(top['late_rate'])}）；分组差异为描述性分解，不推断原因。", ["/rows"]))
        return _pack(sentences, metric_version, period)
    sentences.append((f"按{scope}，{period}（按{field}）的纳入订单共 {total['order_count']:,} 笔、明细 {total['item_count']:,} 件，"
                      f"商品金额 {_money(total['price_cents'])} {currency}，运费 {_money(total['freight_cents'])} {currency}。", ["/total"]))
    if rows:
        top = rows[0]
        share = f"，占商品金额 {_percent(Decimal(top['price_cents']) / Decimal(total['price_cents']))}" if total["price_cents"] else ""
        sentences.append((f"共 {len(rows):,} 个品类，商品金额最高的是“{top['group']}”（{_money(top['price_cents'])} {currency}{share}）；"
                          "一单可以包含多个品类，各品类订单数之和可以大于总订单数；分组差异为描述性分解，不推断原因。", ["/rows"]))
    return _pack(sentences, metric_version, period)


def _pack(sentences, metric_version, period, previous_period=None) -> dict:
    return {"text": "".join(text for text, _ in sentences), "sentences": [
        {"text": text, "sources": sources} for text, sources in sentences[:3]],
        "metric_version": metric_version, "period": period, "previous_period": previous_period,
        "generator": "template-v1"}
