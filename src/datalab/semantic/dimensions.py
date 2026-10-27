"""维度定义与粒度校验：每个维度声明取值来源（编译器据此生成 SQL，参考据此计算）与可用粒度。

取值规则（与 verification/olist_sql.py 一致或显式说明）：
- customer_state：订单经 customer_id 连接客户表取州；客户级指标按 customer_unique_id
  全部历史中第一笔纳入订单所在客户行的州归州（与 _repeat 按期间内首单归州类似，
  但首单不受期间限制，见 metrics.py 客户级口径）。
- seller_id / seller_state：明细经 seller_id 连接卖家表；层级 seller_state → seller_id。
- product_category：商品品类的英文翻译，缺翻译用原品类名，空品类为 unknown（与 _category 一致）。
- price_band：按明细 price_cents 分带，边界写死：<5000（<50 BRL）、5000–9999、10000–19999、
  20000–49999、≥50000（≥500 BRL），组名即 'lt_50'、'50_100'、'100_200'、'200_500'、'ge_500'。
- payment_type / payment_installments：订单级维度，取该订单 payment_sequential 最小的那笔支付；
  没有支付的订单归入 'unknown'（type）或 '-1'（installments 输出为字符串 'unknown'，见编译器）。
- customer_type：订单级，该订单是否为该 customer_unique_id 全部历史（不受期间限制）中的
  第一笔纳入订单：new / returning。
- delivery_status：订单级，late（送达日期晚于预计送达日期，按日期比较）/ on_time（已送达不晚）/
  not_delivered（未送达或缺日期，含 canceled）。
- purchase_date / purchase_month：下单时间前 10 位 / 前 7 位。

粒度规则：维度粒度比指标粒度细时拒绝（各组之和不再等于总计），例如订单级 gmv 按
product_category 拆——一单可含多个品类；建议改用 item_gmv/category_gmv。客户级指标
只允许 customer_state（按首单归州，不会重复计数），其余维度都会让客户重复计数，拒绝。
"""
from __future__ import annotations

from dataclasses import dataclass

from datalab.semantic.errors import SemanticError
from datalab.semantic.metrics import GRAIN_LEVEL, Metric

# 维度粒度：维度本身统计的对象。purchase_date/purchase_month 与订单同级，customer_state 订单级与客户级都可用。
DIMENSION_GRAIN = {
    "customer_state": "order",
    "seller_id": "item",
    "seller_state": "item",
    "product_category": "item",
    "price_band": "item",
    "payment_type": "order",
    "payment_installments": "order",
    "customer_type": "order",
    "delivery_status": "order",
    "purchase_date": "order",
    "purchase_month": "order",
}

# 维度层级：父维度 → 子维度（下钻方向）。只做声明与校验，A2 的 decompose/drill 会用。
HIERARCHY = (("seller_state", "seller_id"),)

# 价格带边界（整数分）与组名；边界写死，保证 SQL 与 Python 参考逐字节一致。
PRICE_BANDS = ((5000, "lt_50"), (10000, "50_100"), (20000, "100_200"), (50000, "200_500"))
PRICE_BAND_TOP = "ge_500"


@dataclass(frozen=True)
class Dimension:
    name: str
    label: str
    grain: str


DIMENSIONS = {name: Dimension(name, label, grain) for (name, grain), label in {
    ("customer_state", "order"): "客户所在州",
    ("seller_id", "item"): "卖家",
    ("seller_state", "item"): "卖家所在州",
    ("product_category", "item"): "商品品类",
    ("price_band", "item"): "价格带",
    ("payment_type", "order"): "支付方式",
    ("payment_installments", "order"): "分期数",
    ("customer_type", "order"): "新老客",
    ("delivery_status", "order"): "送达状态",
    ("purchase_date", "order"): "下单日期",
    ("purchase_month", "order"): "下单月份",
}.items()}

# 客户级指标只允许按 customer_state 分组（按首单归州，不会重复计数）。
CUSTOMER_SAFE = ("customer_state",)


def check_grain(metric: Metric, group_by: str | None) -> None:
    """校验分组维度与指标粒度是否匹配；不匹配时抛带原因与建议的 SemanticError。"""
    if group_by is None:
        return
    if group_by not in DIMENSIONS:
        raise SemanticError(f"未知维度 {group_by!r}；可用维度见 describe_dimensions()")
    if metric.grain == "customer":
        if group_by not in CUSTOMER_SAFE:
            raise SemanticError(
                f"客户级指标 {metric.name} 不能按 {group_by} 分组：客户会重复计数；"
                f"只允许 customer_state（按首单归州）")
        return
    # 明细级指标可以用订单级维度（明细属于订单），反过来不行。
    if GRAIN_LEVEL[DIMENSIONS[group_by].grain] < GRAIN_LEVEL[metric.grain] and metric.grain != "item":
        suggestion = "；建议改用明细级指标 item_gmv/category_gmv" if DIMENSIONS[group_by].grain == "item" else ""
        raise SemanticError(
            f"{metric.grain} 级指标 {metric.name} 不能按 {group_by}（{DIMENSIONS[group_by].grain} 级）拆分："
            f"一个{metric.grain == 'order' and '订单' or '评价'}可含多个{group_by}取值，各组之和不等于总计{suggestion}")


def describe_dimensions() -> list[dict]:
    """维度元数据的纯数据视图（后续包装为工具）。"""
    return [{"name": item.name, "label": item.label, "grain": item.grain} for item in
            (DIMENSIONS[name] for name in sorted(DIMENSIONS))]
