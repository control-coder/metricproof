"""指标树：声明指标之间的拆解关系，供 A2 的 decompose_metric 使用；本轮只做声明与结构校验，不做归因算法。

两种拆解：
- multiplicative 乘法型：parent = factor_a × factor_b，如 gmv = order_count × aov（A2 用 LMDI 拆贡献）。
- additive 加法型：parent = Σ children，如 order_count = new_customer_orders + returning_customer_orders
  （按 customer_type 维度的加法拆分，各组之和等于总计）；比率型指标的「分子/分母」关系不算拆解，
  但声明出来供归因时分别计算分子分母的变化。
"""
from __future__ import annotations

from dataclasses import dataclass

from datalab.semantic.errors import SemanticError
from datalab.semantic.metrics import get_metric

MULTIPLICATIVE = "multiplicative"
ADDITIVE_SPLIT = "additive"


@dataclass(frozen=True)
class Decomposition:
    parent: str
    kind: str  # multiplicative / additive
    # multiplicative：按顺序的因子指标名；additive：子指标名（覆盖 parent 的全部，且互不重叠）。
    children: tuple[str, ...]
    # additive 拆分的依据维度（如 customer_type）；multiplicative 为 None。
    by_dimension: str | None = None
    # multiplicative 拆解的目标指标（被拆的值）；None 表示 parent 本身。
    # 用于乘积只在某个分子口径上精确成立的情形，如 item_gmv = item_count × avg_item_price
    # 的精确目标是不含运费的 item_price_sum（item_gmv 含运费），target 声明为 item_price_sum。
    target: str | None = None
    note: str = ""


_TREE = (
    Decomposition("gmv", MULTIPLICATIVE, ("order_count", "aov"),
                  note="GMV = 订单量 × 客单价；A2 用 LMDI 计算各因子贡献"),
    Decomposition("order_count", ADDITIVE_SPLIT, ("order_count",), by_dimension="customer_type",
                  note="订单量按新老客加法拆分：new + returning = 全部（同一名称表示按维度值分组求和）"),
    Decomposition("item_gmv", MULTIPLICATIVE, ("item_count", "avg_item_price"), target="item_price_sum",
                  note="价格口径拆解：item_price_sum = 明细件数 × 件单价（不含运费）；"
                       "item_gmv 含运费，因此拆解的目标值为分子 item_price_sum 而非指标值本身"),
    Decomposition("aov", MULTIPLICATIVE, ("gmv", "order_count"),
                  note="客单价的分子/分母关系：归因时分别计算 gmv 与 order_count 的变化再合成"),
    Decomposition("cancel_rate", MULTIPLICATIVE, ("cancel_count", "all_order_count"),
                  note="取消率的分子/分母关系"),
    Decomposition("late_delivery_rate", MULTIPLICATIVE, ("late_count", "delivered_dated_count"),
                  note="送达延迟率的分子/分母关系"),
    Decomposition("avg_delivery_days", MULTIPLICATIVE, ("delivery_days_sum", "delivered_dated_count"),
                  note="平均配送天数的分子/分母关系"),
    Decomposition("avg_item_price", MULTIPLICATIVE, ("item_price_sum", "item_count"), note="件单价的分子/分母关系"),
    Decomposition("items_per_order", MULTIPLICATIVE, ("item_count", "item_order_count"), note="每单件数的分子/分母关系"),
    Decomposition("freight_ratio", MULTIPLICATIVE, ("freight_sum", "item_gmv"), note="运费占比的分子/分母关系"),
    Decomposition("avg_review_score", MULTIPLICATIVE, ("review_score_sum", "review_count"), note="平均评分的分子/分母关系"),
    Decomposition("bad_review_rate", MULTIPLICATIVE, ("bad_review_count", "review_count"), note="差评率的分子/分母关系"),
    Decomposition("repeat_rate", MULTIPLICATIVE, ("repeat_customer_count", "customer_count"), note="复购率的分子/分母关系"),
)

_BY_PARENT = {item.parent: item for item in _TREE}


def decomposition(parent: str) -> Decomposition:
    """按父指标名取拆解声明；没有拆解时抛 SemanticError（叶子指标）。"""
    get_metric(parent)  # 未知名称统一报错
    if parent not in _BY_PARENT:
        raise SemanticError(f"指标 {parent} 没有声明拆解（叶子指标）；可直接按维度下钻")
    return _BY_PARENT[parent]


def validate() -> list[str]:
    """结构校验：子指标与 target 都存在、加法拆解的维度在父指标允许的维度内。返回问题列表（空为通过）。"""
    from datalab.semantic.metrics import metric_dimensions
    problems = []
    for item in _TREE:
        for child in item.children:
            try:
                get_metric(child)
            except SemanticError:
                problems.append(f"{item.parent}: 未知子指标 {child}")
        if item.target is not None:
            try:
                get_metric(item.target)
            except SemanticError:
                problems.append(f"{item.parent}: 未知拆解目标 {item.target}")
        if item.kind == ADDITIVE_SPLIT and item.by_dimension not in metric_dimensions(get_metric(item.parent)):
            problems.append(f"{item.parent}: 拆分维度 {item.by_dimension} 不在允许的维度内")
        if item.kind not in (MULTIPLICATIVE, ADDITIVE_SPLIT):
            problems.append(f"{item.parent}: 未知拆解类型 {item.kind}")
    return problems


def metric_tree() -> list[dict]:
    """指标树的纯数据视图（供工具与文档使用）。"""
    return [{"parent": item.parent, "kind": item.kind, "children": list(item.children),
             "by_dimension": item.by_dimension, "target": item.target, "note": item.note}
            for item in _TREE]
