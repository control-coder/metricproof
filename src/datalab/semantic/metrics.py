"""指标定义：名称、中文名、粒度、类型（additive 加法型 / ratio 比率型）、允许的维度、单位与口径约束。

粒度决定指标统计的对象：order 订单级、item 明细级、review 评价级、customer 客户级。
加法型指标（如 gmv）只有分子；比率型指标（如 aov）给出分子、分母两个加法型成分名，
输出的分子、分母都是整数，比值为 6 位小数字符串（分母 0 为 null），金额一律为整数分。

关键口径（与 verification/olist_sql.py 同一契约，另有说明的除外）：
- 纳入订单：order_status 属于已确认口径的 included_statuses，且下单时间落在 [start, end)。
  cancel_rate 是例外：它描述「期间内全部订单里取消单占多少」，与经营口径无关，
  分母为期间内全部状态的订单、分子为其中 canceled 订单，不受 included_statuses 约束。
- gmv 的订单金额来源由已确认口径 amount_source 决定：payments 为该单全部支付之和，
  items 为全部明细 price_cents + freight_cents 之和；没有子行时计 0（与 olist_sql._orders 一致）。
- avg_delivery_days：只统计 delivered 且有送达日期的订单，按日期差取整天
  （送达日期前 10 位 − 下单日期前 10 位的天数），分子为天数之和、分母为订单数；
  不用时间戳秒差，避免小时部分造成不可复算的小数。
- 评价级指标经 order_id 归到订单，期间与状态按订单过滤；同一订单多条评价全部计入
  （评分均值按评价条数加权），不按订单去重。
- 客户级：repeat_rate 与 olist_sql._repeat 同口径；new/returning 按 customer_unique_id
  全部历史（不受 start 限制）中第一笔纳入订单判定，首单落在期间内为新客，否则为老客。
"""
from __future__ import annotations

from dataclasses import dataclass

from datalab.semantic.errors import SemanticError

GRAINS = ("order", "item", "review", "customer")
# 粒度自细到粗的层级：细粒度可以汇总到粗粒度，反之不允许。
GRAIN_LEVEL = {"item": 0, "order": 1, "review": 1, "customer": 2}

ADDITIVE = "additive"
RATIO = "ratio"


@dataclass(frozen=True)
class Metric:
    name: str
    label: str
    grain: str
    kind: str
    unit: str
    # 比率型的分子与分母成分（都是加法型指标名）；加法型为 None。
    numerator: str | None = None
    denominator: str | None = None
    # 是否受已确认口径 included_statuses 约束（cancel_rate 不受约束）。
    status_constrained: bool = True


# 加法型指标。gmv 的金额来源在编译时按 snapshot.amount_source 决定，这里不展开。
ADDITIVE_METRICS = {
    "gmv": Metric("gmv", "订单金额合计", "order", ADDITIVE, "cents"),
    "order_count": Metric("order_count", "订单量", "order", ADDITIVE, "count"),
    "cancel_count": Metric("cancel_count", "取消订单数", "order", ADDITIVE, "count", status_constrained=False),
    "all_order_count": Metric("all_order_count", "全部订单数", "order", ADDITIVE, "count", status_constrained=False),
    "late_count": Metric("late_count", "延迟送达订单数", "order", ADDITIVE, "count"),
    "delivered_dated_count": Metric("delivered_dated_count", "已送达且有日期订单数", "order", ADDITIVE, "count"),
    "delivery_days_sum": Metric("delivery_days_sum", "配送天数合计", "order", ADDITIVE, "days"),
    "item_gmv": Metric("item_gmv", "明细金额合计", "item", ADDITIVE, "cents"),
    "item_count": Metric("item_count", "明细件数", "item", ADDITIVE, "count"),
    "item_price_sum": Metric("item_price_sum", "明细价格合计", "item", ADDITIVE, "cents"),
    "freight_sum": Metric("freight_sum", "运费合计", "item", ADDITIVE, "cents"),
    "item_order_count": Metric("item_order_count", "有明细的不同订单数", "item", ADDITIVE, "count"),
    "review_score_sum": Metric("review_score_sum", "评分合计", "review", ADDITIVE, "score"),
    "review_count": Metric("review_count", "评价数", "review", ADDITIVE, "count"),
    "bad_review_count": Metric("bad_review_count", "差评数（评分 ≤ 2）", "review", ADDITIVE, "count"),
    "customer_count": Metric("customer_count", "下单客户数", "customer", ADDITIVE, "count"),
    "repeat_customer_count": Metric("repeat_customer_count", "复购客户数", "customer", ADDITIVE, "count"),
    "new_customers": Metric("new_customers", "新客数", "customer", ADDITIVE, "count"),
    "returning_customers": Metric("returning_customers", "老客数", "customer", ADDITIVE, "count"),
}

# 比率型指标：name → (分子成分, 分母成分, 中文名, 单位, 是否受纳入状态约束)。
RATIO_METRICS = {
    "aov": ("gmv", "order_count", "客单价", "cents", True),
    "cancel_rate": ("cancel_count", "all_order_count", "取消率", "ratio", False),
    "late_delivery_rate": ("late_count", "delivered_dated_count", "送达延迟率", "ratio", True),
    "avg_delivery_days": ("delivery_days_sum", "delivered_dated_count", "平均配送天数", "days", True),
    "avg_item_price": ("item_price_sum", "item_count", "件单价", "cents", True),
    "items_per_order": ("item_count", "item_order_count", "每单件数", "count", True),
    "freight_ratio": ("freight_sum", "item_gmv", "运费占比", "ratio", True),
    "avg_review_score": ("review_score_sum", "review_count", "平均评分", "score", True),
    "bad_review_rate": ("bad_review_count", "review_count", "差评率", "ratio", True),
    "repeat_rate": ("repeat_customer_count", "customer_count", "复购率", "ratio", True),
}

# 指标允许的维度（编译时还要按粒度再校验，见 dimensions.check_grain）。
# category_gmv 是 item_gmv 按品类分组的常用别名，共用同一加法成分。
def metric_dimensions(metric) -> tuple[str, ...]:
    return ALLOWED_DIMENSIONS.get(metric.name, ())
ALLOWED_DIMENSIONS = {
    "gmv": ("customer_state", "purchase_date", "purchase_month", "payment_type", "payment_installments",
            "customer_type", "delivery_status"),
    "order_count": ("customer_state", "purchase_date", "purchase_month", "payment_type", "payment_installments",
                    "customer_type", "delivery_status"),
    "aov": ("customer_state", "purchase_date", "purchase_month", "payment_type", "payment_installments",
            "customer_type", "delivery_status"),
    "cancel_rate": ("customer_state", "purchase_date", "purchase_month"),
    "late_delivery_rate": ("customer_state", "purchase_date", "purchase_month", "customer_type"),
    "avg_delivery_days": ("customer_state", "purchase_date", "purchase_month"),
    "item_gmv": ("product_category", "seller_id", "seller_state", "price_band",
                 "customer_state", "purchase_date", "purchase_month"),
    "category_gmv": ("product_category", "seller_id", "seller_state", "price_band"),
    "item_count": ("product_category", "seller_id", "seller_state", "price_band",
                   "customer_state", "purchase_date", "purchase_month"),
    "avg_item_price": ("product_category", "seller_id", "seller_state", "price_band"),
    "items_per_order": ("product_category", "seller_id", "seller_state"),
    "freight_ratio": ("product_category", "seller_id", "seller_state", "price_band"),
    "avg_review_score": ("customer_state", "purchase_date", "purchase_month", "delivery_status"),
    "bad_review_rate": ("customer_state", "purchase_date", "purchase_month", "delivery_status"),
    "repeat_rate": ("customer_state",),
    "new_customers": ("customer_state",),
    "returning_customers": ("customer_state",),
}

# category_gmv 只是「item_gmv 按 product_category 分组」的别名，编译时映射到同一成分。
ALIASES = {"category_gmv": "item_gmv"}


def get_metric(name: str) -> Metric:
    """按名取指标定义；未知名称抛 SemanticError。比率型在这里展开成分子分母两个成分。"""
    name = ALIASES.get(name, name)
    if name in ADDITIVE_METRICS:
        return ADDITIVE_METRICS[name]
    if name in RATIO_METRICS:
        numerator, denominator, label, unit, constrained = RATIO_METRICS[name]
        base = ADDITIVE_METRICS[numerator]
        return Metric(name, label, base.grain, RATIO, unit, numerator, denominator, constrained)
    raise SemanticError(f"未知指标 {name!r}；可用指标见 list_metrics()")


def components(metric: Metric) -> tuple[str, str | None]:
    """返回编译需要的成分：(分子, 分母)；加法型分母为 None。"""
    return (metric.numerator, metric.denominator) if metric.kind == RATIO else (metric.name, None)


def list_metrics() -> list[dict]:
    """指标元数据的纯数据视图（后续包装为工具）；不含计算逻辑。"""
    rows = []
    for name in sorted(set(ADDITIVE_METRICS) | set(RATIO_METRICS) | set(ALIASES)):
        metric = get_metric(name)
        rows.append({"name": name, "label": metric.label, "grain": metric.grain, "type": metric.kind,
                     "unit": metric.unit, "numerator": metric.numerator, "denominator": metric.denominator,
                     "status_constrained": metric.status_constrained,
                     "dimensions": sorted(ALLOWED_DIMENSIONS.get(name, ()))})
    return rows
