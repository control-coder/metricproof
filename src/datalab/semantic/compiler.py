"""编译器：把 (指标, 期间, 过滤器, 分组) 编译为参数化 SQLite SQL 与参数列表。

表名、列名、维度表达式全部来自服务端常量（本模块与 dimensions.py），取值一律参数绑定，
不接受任何外部 SQL 片段。过滤器只允许「维度 = 值」或「维度 IN 值列表」，维度名必须在白名单内。

编译思路：每个加法型成分（分子/分母）各生成一条 SELECT；比率型指标编译为两条成分查询，
分子、分母与比值由引擎在结果层组装。基表按指标粒度固定：
- 订单级：orders o [+ customers c] [+ 首笔支付 p] [+ 金额子查询 pay/it] [+ 首单 f]
- 明细级：order_items i JOIN orders o [+ customers c] [+ products+category_translation] [+ sellers s]
- 评价级：order_reviews r JOIN orders o [+ customers c] [+ 首单 f]
- 客户级：由「全部历史纳入订单」派生的客户首单集合 f，再按期间过滤（见 metrics.py 客户级口径）。
期间固定按下单时间 order_purchase_timestamp，左闭右开 [start, end)；
同一订单的多条评价全部计入（不按订单去重）。
"""
from __future__ import annotations

import re
from datetime import datetime

from datalab.semantic.dimensions import PRICE_BAND_TOP, PRICE_BANDS, check_grain
from datalab.semantic.errors import SemanticError
from datalab.semantic.metrics import ADDITIVE, Metric, components, get_metric, metric_dimensions
from datalab.semantic.snapshot import SemanticSnapshot

MONTH = re.compile(r"\d{4}-\d{2}")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
STATE = re.compile(r"[A-Z]{2}")
WORD = re.compile(r"[0-9A-Za-z_]{1,64}")

# 加法型成分的聚合表达式；gmv 按金额来源在编译时展开为 gmv_payments / gmv_items。
_EXPRESSIONS = {
    "gmv_payments": "COALESCE(SUM(COALESCE(pay.a, 0)), 0)",
    "gmv_items": "COALESCE(SUM(COALESCE(it.a, 0)), 0)",
    "order_count": "COUNT(*)",
    "cancel_count": "COUNT(*)",
    "all_order_count": "COUNT(*)",
    "late_count": "COALESCE(SUM(CASE WHEN substr(o.order_delivered_customer_date, 1, 10) > "
                  "substr(o.order_estimated_delivery_date, 1, 10) THEN 1 ELSE 0 END), 0)",
    "delivered_dated_count": "COUNT(*)",
    # 按日期差取整天（不用时间戳秒差），保证可精确复算。
    "delivery_days_sum": "COALESCE(SUM(CAST(julianday(substr(o.order_delivered_customer_date, 1, 10)) - "
                         "julianday(substr(o.order_purchase_timestamp, 1, 10)) AS INTEGER)), 0)",
    "item_gmv": "COALESCE(SUM(i.price_cents + i.freight_cents), 0)",
    "item_count": "COUNT(*)",
    "item_price_sum": "COALESCE(SUM(i.price_cents), 0)",
    "freight_sum": "COALESCE(SUM(i.freight_cents), 0)",
    "item_order_count": "COUNT(DISTINCT i.order_id)",
    "review_score_sum": "COALESCE(SUM(CAST(r.review_score AS INTEGER)), 0)",
    "review_count": "COUNT(*)",
    "bad_review_count": "COALESCE(SUM(CASE WHEN r.review_score IN ('1', '2') THEN 1 ELSE 0 END), 0)",
}


def dimension_sql(name: str, grain: str) -> str:
    """维度表达式：值都来自固定列与固定 CASE 词表，不含外部输入。grain 为指标粒度（基表）。"""
    if name == "customer_state":
        return "f.state" if grain == "customer" else "c.customer_state"
    if name == "purchase_date":
        return "substr(f.first_t, 1, 10)" if grain == "customer" else "substr(o.order_purchase_timestamp, 1, 10)"
    if name == "purchase_month":
        return "substr(f.first_t, 1, 7)" if grain == "customer" else "substr(o.order_purchase_timestamp, 1, 7)"
    if name == "payment_type":
        return "COALESCE(p.payment_type, 'unknown')"
    if name == "payment_installments":
        return "COALESCE(CAST(p.payment_installments AS TEXT), 'unknown')"
    if name == "customer_type":
        return "CASE WHEN o.order_id = f.first_order THEN 'new' ELSE 'returning' END"
    if name == "delivery_status":
        return ("CASE WHEN o.order_status <> 'delivered' OR o.order_delivered_customer_date = '' THEN 'not_delivered' "
                "WHEN substr(o.order_delivered_customer_date, 1, 10) > substr(o.order_estimated_delivery_date, 1, 10) "
                "THEN 'late' ELSE 'on_time' END")
    if name == "product_category":
        return ("CASE WHEN pr.product_category_name IS NULL OR pr.product_category_name = '' THEN 'unknown' "
                "ELSE COALESCE(tr.product_category_name_english, pr.product_category_name) END")
    if name == "price_band":
        parts = " ".join(f"WHEN i.price_cents < {limit} THEN '{label}'" for limit, label in PRICE_BANDS)
        return f"CASE {parts} ELSE '{PRICE_BAND_TOP}' END"
    if name == "seller_id":
        return "i.seller_id"
    if name == "seller_state":
        return "COALESCE(s.seller_state, 'unknown')"
    raise SemanticError(f"未知维度 {name!r}")


def _dimension_pattern(name: str) -> re.Pattern:
    if name == "purchase_month":
        return MONTH
    if name == "purchase_date":
        return DATE
    if name in ("customer_state", "seller_state"):
        return STATE
    if name == "price_band":
        return re.compile(r"(lt_50|50_100|100_200|200_500|ge_500)")
    if name == "customer_type":
        return re.compile(r"(new|returning)")
    if name == "delivery_status":
        return re.compile(r"(late|on_time|not_delivered)")
    if name == "payment_installments":
        return re.compile(r"\d{1,3}|unknown")
    return WORD


def _check_period(start: str | None, end: str | None) -> None:
    for value in (start, end):
        if value is not None and (len(value) != 19 or "T" not in value or datetime.fromisoformat(value).tzinfo):
            raise SemanticError("期间边界须为无偏移的秒级 ISO 时间（YYYY-MM-DDTHH:MM:SS）")
    if start and end and start >= end:
        raise SemanticError("期间起点必须早于终点")


def _check_filters(metric: Metric, filters: dict | None) -> dict[str, tuple[str, ...]]:
    """过滤器只允许 {维度: 值} 或 {维度: [值, …]}；维度在指标白名单内，值按词表校验。"""
    if not filters:
        return {}
    allowed = set(metric_dimensions(metric))
    checked = {}
    for name, value in filters.items():
        if name not in allowed:
            raise SemanticError(f"指标 {metric.name} 不支持按 {name!r} 过滤；允许的维度：{', '.join(sorted(allowed))}")
        values = (value,) if isinstance(value, str) else tuple(value) if isinstance(value, (list, tuple)) else ()
        if not values or not all(isinstance(item, str) and item for item in values):
            raise SemanticError(f"过滤器 {name} 的取值须为非空字符串或非空字符串列表")
        pattern = _dimension_pattern(name)
        if not all(pattern.fullmatch(item) for item in values):
            raise SemanticError(f"过滤器 {name} 的取值格式不合法")
        checked[name] = values
    return checked


def _first_order_sql(snapshot: SemanticSnapshot) -> str:
    """客户首单子查询：每个 customer_unique_id 全部历史（不受期间限制）中的第一笔纳入订单。

    列：u（customer_unique_id）、first_order、first_t（首单下单时间）、state（首单所在客户行的州）。
    同一时间取最小 order_id，与 _repeat 的（时间, order_id）次序一致；窗口函数实现，included_statuses 出现一次。
    """
    statuses = ",".join("?" for _ in snapshot.included_statuses)
    return ("SELECT u, first_order, first_t, state FROM ("
            "SELECT c.customer_unique_id AS u, o.order_id AS first_order, o.order_purchase_timestamp AS first_t, "
            "c.customer_state AS state, ROW_NUMBER() OVER (PARTITION BY c.customer_unique_id "
            "ORDER BY o.order_purchase_timestamp, o.order_id) AS rn "
            "FROM orders o JOIN customers c ON c.customer_id = o.customer_id "
            f"WHERE o.order_status IN ({statuses})) ranked WHERE rn = 1")


def _first_order_args(snapshot: SemanticSnapshot) -> list:
    return list(snapshot.included_statuses)


def _period_terms(metric: Metric, snapshot: SemanticSnapshot, start: str | None, end: str | None,
                  alias: str = "o") -> tuple[list[str], list]:
    """期间与纳入状态条件（cancel/all 成分不受 included_statuses 约束）。"""
    clauses, args = [], []
    if metric.status_constrained:
        clauses.append(f"{alias}.order_status IN ({','.join('?' for _ in snapshot.included_statuses)})")
        args.extend(snapshot.included_statuses)
    if start:
        clauses.append(f"{alias}.order_purchase_timestamp >= ?")
        args.append(start)
    if end:
        clauses.append(f"{alias}.order_purchase_timestamp < ?")
        args.append(end)
    return clauses, args


def _order_base(dims: set[str], snapshot: SemanticSnapshot, *, amount: bool) -> tuple[str, list]:
    """订单级 FROM/JOIN。amount=True 时按 snapshot.amount_source 加金额子查询。"""
    joins, args = [], []
    if "customer_state" in dims or "customer_type" in dims:
        joins.append("JOIN customers c ON c.customer_id = o.customer_id")
    if "customer_type" in dims:
        joins.append(f"JOIN ({_first_order_sql(snapshot)}) f ON f.u = c.customer_unique_id")
        args.extend(_first_order_args(snapshot))
    if "payment_type" in dims or "payment_installments" in dims:
        joins.append("LEFT JOIN order_payments p ON p.order_id = o.order_id AND p.payment_sequential = "
                     "(SELECT MIN(p2.payment_sequential) FROM order_payments p2 WHERE p2.order_id = o.order_id)")
    if amount:
        source = snapshot.require_amount_source()
        if source == "payments":
            joins.append("LEFT JOIN (SELECT order_id, SUM(payment_value_cents) AS a FROM order_payments "
                         "GROUP BY order_id) pay ON pay.order_id = o.order_id")
        else:
            joins.append("LEFT JOIN (SELECT order_id, SUM(price_cents + freight_cents) AS a FROM order_items "
                         "GROUP BY order_id) it ON it.order_id = o.order_id")
    return " ".join(["FROM orders o", *joins]), args


def _item_base(dims: set[str]) -> str:
    joins = []
    if "product_category" in dims:
        joins.append("LEFT JOIN products pr ON pr.product_id = i.product_id "
                     "LEFT JOIN category_translation tr ON tr.product_category_name = pr.product_category_name")
    if "seller_id" in dims or "seller_state" in dims:
        joins.append("LEFT JOIN sellers s ON s.seller_id = i.seller_id")
    if "customer_state" in dims:
        joins.append("JOIN customers c ON c.customer_id = o.customer_id")
    return " ".join(["FROM order_items i JOIN orders o ON o.order_id = i.order_id", *joins])


def _review_base(dims: set[str], snapshot: SemanticSnapshot) -> tuple[str, list]:
    joins, args = [], []
    if "customer_state" in dims or "customer_type" in dims:
        joins.append("JOIN customers c ON c.customer_id = o.customer_id")
    if "customer_type" in dims:
        joins.append(f"JOIN ({_first_order_sql(snapshot)}) f ON f.u = c.customer_unique_id")
        args.extend(_first_order_args(snapshot))
    return " ".join(["FROM order_reviews r JOIN orders o ON o.order_id = r.order_id", *joins]), args


def _select_component(component: str, metric: Metric, group_sql: tuple[str, str] | None,
                      snapshot: SemanticSnapshot, start: str | None, end: str | None,
                      filters: dict[str, tuple[str, ...]]) -> tuple[str, list]:
    """单个加法型成分的 SELECT（分组时输出 grp、value 两列，按 grp 升序）。"""
    grain = metric.grain
    dims = ({group_sql[0]} if group_sql else set()) | set(filters)
    group_expression = group_sql[1] if group_sql else None
    if grain == "customer":
        return _select_customer_component(component, metric, group_expression, snapshot, start, end, filters)

    join_args: list = []
    if grain == "order":
        base, join_args = _order_base(dims, snapshot, amount=component == "gmv")
    elif grain == "item":
        base = _item_base(dims)
    else:
        base, join_args = _review_base(dims, snapshot)

    clauses, where_args = _period_terms(metric, snapshot, start, end)
    if component in ("late_count", "delivered_dated_count", "delivery_days_sum"):
        clauses.append("o.order_status = 'delivered' AND o.order_delivered_customer_date <> '' "
                       "AND o.order_estimated_delivery_date <> ''")
    if component == "cancel_count":
        clauses.append("o.order_status = 'canceled'")
    for name, values in filters.items():
        clauses.append(f"{dimension_sql(name, grain)} IN ({','.join('?' for _ in values)})")
        where_args.extend(values)
    where = " AND ".join(f"({clause})" for clause in clauses) if clauses else "1=1"
    expression = _EXPRESSIONS[f"gmv_{snapshot.amount_source}"] if component == "gmv" else _EXPRESSIONS[component]
    if group_expression:
        sql = (f"SELECT {group_expression} AS grp, {expression} AS value {base} WHERE {where} "
               f"GROUP BY {group_expression} ORDER BY {group_expression}")
    else:
        sql = f"SELECT {expression} AS value {base} WHERE {where}"
    return sql, join_args + where_args


def _customer_query(source_sql: str, source_args: list, group_expression: str | None,
                    where: str = "", trailing_args: list | None = None) -> tuple[str, list]:
    """客户级成分的统一外形：grp 在前、value 在后；source_sql 为内层客户集合（含首单 state/first_t 列）。

    外层只能引用 src 的列：首单集合的 state / first_t 在内层已按别名暴露，外层把 f.xxx 改写为 src.xxx。
    """
    if group_expression:
        outer = group_expression.replace("f.", "src.")
        group_select = f"{outer} AS grp, "
        group_by = f" GROUP BY {outer} ORDER BY {outer}"
    else:
        group_select = group_by = ""
    sql = f"SELECT {group_select}COUNT(*) AS value FROM ({source_sql}) src{where and ' WHERE ' + where}{group_by}"
    return sql, source_args + (trailing_args or [])


def _select_customer_component(component: str, metric: Metric, group_expression: str | None,
                               snapshot: SemanticSnapshot, start: str | None, end: str | None,
                               filters: dict[str, tuple[str, ...]]) -> tuple[str, list]:
    """客户级成分：customer_count / repeat_customer_count / new_customers / returning_customers。

    期间内有纳入订单的客户计入 customer_count；至少 2 笔计入 repeat_customer_count（与 _repeat 同口径）。
    新客 = 全部历史首单落在期间内的客户；老客 = 期间内有纳入订单但首单在期间之前的客户。
    分组只允许 customer_state（按首单归州，见 dimensions.py）；表达式用 f.state，直接落在首单集合上。
    """
    first_sql, first_args = _first_order_sql(snapshot), _first_order_args(snapshot)
    statuses = ",".join("?" for _ in snapshot.included_statuses)
    period = []
    if start:
        period.append("o.order_purchase_timestamp >= ?")
    if end:
        period.append("o.order_purchase_timestamp < ?")
    period_clause = (" AND ".join(period) + " AND ") if period else ""
    period_args = ([start] if start else []) + ([end] if end else [])

    if component in ("customer_count", "repeat_customer_count"):
        having = " HAVING COUNT(*) >= 2" if component == "repeat_customer_count" else ""
        # 期间内有（至少 2 笔）纳入订单的客户，连接首单集合取归州。
        inner = (f"SELECT c.customer_unique_id AS u FROM orders o JOIN customers c ON c.customer_id = o.customer_id "
                 f"WHERE o.order_status IN ({statuses}) AND {period_clause}1=1 GROUP BY c.customer_unique_id{having}")
        source = (f"SELECT f.state, f.first_t FROM ({inner}) sel "
                  f"JOIN ({first_sql}) f ON f.u = sel.u")
        return _customer_query(source, list(snapshot.included_statuses) + period_args + first_args,
                               group_expression)

    if component == "new_customers":
        # 新客：首单落在期间内，直接查首单集合。
        clauses = []
        trailing = []
        if start:
            clauses.append("src.first_t >= ?")
            trailing.append(start)
        if end:
            clauses.append("src.first_t < ?")
            trailing.append(end)
        return _customer_query(first_sql, list(first_args), group_expression,
                               " AND ".join(clauses), trailing)

    # 老客：期间内有纳入订单，且首单早于期间起点（未给起点时恒为 0）。
    if not start:
        return _customer_query(first_sql, list(first_args), group_expression, "1=0")
    inner = (f"SELECT c.customer_unique_id AS u FROM orders o JOIN customers c ON c.customer_id = o.customer_id "
             f"WHERE o.order_status IN ({statuses}) AND {period_clause}1=1 GROUP BY c.customer_unique_id")
    source = (f"SELECT f.state, f.first_t FROM ({inner}) sel "
              f"JOIN ({first_sql}) f ON f.u = sel.u")
    return _customer_query(source, list(snapshot.included_statuses) + period_args + first_args,
                           group_expression, "src.first_t < ?", [start])


def compile_query(metric_name: str, period: tuple[str | None, str | None], filters: dict | None,
                  group_by: str | None, snapshot: SemanticSnapshot) -> list[tuple[str, str, list]]:
    """编译入口。返回 [(成分名, sql, params)]；加法型一条，比率型两条（分子、分母）。

    period 为左闭右开 [start, end) 的秒级 ISO 时间（None 表示不限）；期间字段固定为下单时间。
    """
    metric = get_metric(metric_name)
    start, end = period
    _check_period(start, end)
    check_grain(metric, group_by)
    if group_by is not None and group_by not in metric_dimensions(metric):
        raise SemanticError(f"指标 {metric.name} 不允许按 {group_by!r} 分组；"
                            f"允许的维度：{', '.join(sorted(metric_dimensions(metric))) or '（无）'}")
    checked = _check_filters(metric, filters)
    group_sql = (group_by, dimension_sql(group_by, metric.grain)) if group_by else None
    names = [metric.name] if metric.kind == ADDITIVE else list(components(metric))
    return [(component, *_select_component(component, metric, group_sql, snapshot, start, end, checked))
            for component in names]
