"""原始数据问题（10 个）：不写死答案，gold 由 A2 的确定性参考实现在原始数据上计算。

修订（2026-10-27，revision 2）：初版按「首位候选解释力 EP 最高」选月份是错的——
EP = change_i / total_change，总变化接近 0 时 EP 爆炸，系统性地选中了几乎没变化的月份
（例如 GMV 总变化 +0.03% 而 EP 4.70），对这样的月份问「主要来自哪些地区」是病态问题。

现在的月份选择分两步（阈值写进冻结文件 meta）：
1. 变化显著：加法型 |环比变化率| ≥ MIN_REL_CHANGE（5%）；比率型 |变化| ≥ RATIO_MIN_CHANGE
   （各指标按自然波动水平给绝对阈值，见 RATIO_MIN_CHANGE）。
2. 变化集中：首位候选 EP ∈ [EP_MIN, EP_MAX]（0.3–1.5；EP > 1.5 说明各组贡献互相抵消，
   总变化是噪声，问题病态）。
在满足条件的月份中取变化显著程度最高者（并列取月份早者）；候选池固定、规则固定，
不写死答案、不做人工标注。

问题文本的方向词（上升/下降/改善/回落）由实际数据的总变化方向生成，不手写。
gold.dimensions 取计算结果的前 3 个候选（维度、维度值、解释力、惊奇度），首位为最强根因；
magnitude 为首位候选对总变化的贡献（contribution 的 change 字段）；
unit 取语义层指标定义的单位（cents/count/ratio/days/score）。
评分与注入案例共用 AC@k：gold 的 (维度, 值) 出现在前 k 个候选中即命中。
"""
from __future__ import annotations

from inject import month_range

from datalab.attribution import ReferenceSource, contribution, find_root_cause
from datalab.semantic.metrics import get_metric

# 变化显著阈值：加法型 |环比变化率| 最小值；比率型 |本期 − 基期| 最小值（按指标自然波动水平）。
MIN_REL_CHANGE = 0.05
RATIO_MIN_CHANGE = {"late_delivery_rate": 0.01, "bad_review_rate": 0.005, "freight_ratio": 0.005,
                    "aov": 500.0, "avg_delivery_days": 0.5, "avg_review_score": 0.1,
                    "avg_item_price": 200.0, "items_per_order": 0.02, "cancel_rate": 0.005,
                    "repeat_rate": 0.005}
# 变化集中：首位候选解释力合理区间（EP 过高说明各组贡献互相抵消，总变化是噪声）。
EP_MIN, EP_MAX = 0.3, 1.5

ALL_MONTHS = ["2017-03", "2017-04", "2017-05", "2017-06", "2017-07", "2017-08", "2017-09", "2017-10",
              "2017-11", "2017-12", "2018-01", "2018-02", "2018-03", "2018-04", "2018-05", "2018-06",
              "2018-07", "2018-08"]

# 指标中文名与方向词：direction 由实际总变化方向生成，问题模板不含方向。
METRIC_LABEL = {"gmv": "GMV", "item_gmv": "销售额", "order_count": "订单量", "aov": "客单价",
                "late_delivery_rate": "送达延迟率", "bad_review_rate": "差评率",
                "freight_ratio": "运费占比", "avg_delivery_days": "平均配送天数",
                "avg_review_score": "平均评分"}
UP_WORD = {"gmv": "上升", "item_gmv": "上升", "aov": "上升", "late_delivery_rate": "上升",
           "bad_review_rate": "上升", "freight_ratio": "上升", "avg_delivery_days": "变长",
           "avg_review_score": "上升"}
DOWN_WORD = {"gmv": "回落", "item_gmv": "下降", "aov": "下降", "late_delivery_rate": "改善",
             "bad_review_rate": "改善", "freight_ratio": "下降", "avg_delivery_days": "缩短",
             "avg_review_score": "下降"}


def _months(*months: str) -> list[str]:
    return list(months)


def month(month_text: str) -> dict:
    start, end = month_range(month_text)
    return {"start": start, "end": end}


def month_prev(m: str) -> str:
    year, mm = int(m[:4]), int(m[5:7])
    mm -= 1
    if mm == 0:
        year, mm = year - 1, 12
    return f"{year}-{mm:02d}"


def _spec(metric: str, dimensions: list[str], months: list[str], group_label: str) -> dict:
    return {"metric": metric, "dimensions": dimensions, "months": months, "group_label": group_label}


# 10 个原始数据问题：指标 × 分析维度 × 月份候选池；方向词由实际总变化方向生成。
# 候选池按全月扫描结果（显著性与 EP 区间）挑选，保证每题至少有一个月份满足条件。
RAW_QUESTIONS = [
    _spec("gmv", ["customer_state"], _months("2017-07", "2017-11", "2018-01", "2018-04", "2018-05",
                                            "2018-03"), "地区"),
    _spec("gmv", ["customer_state"], _months("2017-06", "2017-12", "2018-02", "2018-06"), "地区"),
    _spec("item_gmv", ["product_category"], _months("2017-07", "2017-12", "2018-02", "2018-06",
                                                    "2018-08"), "品类"),
    _spec("item_gmv", ["product_category"], _months("2017-08", "2017-10", "2017-11", "2018-03",
                                                    "2018-05"), "品类"),
    _spec("late_delivery_rate", ["customer_state"], _months("2017-06", "2017-10", "2018-02", "2018-03",
                                                            "2018-06", "2018-05", "2018-08", "2017-04"),
          "州"),
    _spec("late_delivery_rate", ["customer_state"], _months("2018-01", "2017-12", "2018-06", "2017-05",
                                                            "2018-04", "2017-07"), "州"),
    _spec("aov", ["payment_type"], _months("2017-09", "2017-11", "2018-01", "2018-04", "2018-07",
                                           "2018-03"), "支付方式"),
    _spec("aov", ["payment_type"], _months("2017-05", "2017-07", "2017-11", "2018-08", "2018-02",
                                           "2017-08", "2017-12", "2018-06"), "支付方式"),
    _spec("bad_review_rate", ["customer_state"], _months("2017-06", "2017-10", "2018-02", "2018-05",
                                                         "2018-08", "2017-11", "2018-03", "2018-01"),
          "州"),
    _spec("freight_ratio", ["seller_state"], _months("2017-06", "2017-11", "2018-02", "2018-05",
                                                     "2018-08", "2017-05", "2018-04"), "卖家州"),
]


def question_of(spec: dict, month_text: str, direction: str) -> str:
    """问题文本：方向词由实际总变化方向生成（改善/回落类词按指标语义取）。"""
    label = METRIC_LABEL[spec["metric"]]
    word = (UP_WORD if direction == "up" else DOWN_WORD)[spec["metric"]]
    year, mm = month_text[:4], int(month_text[5:7])
    return f"{year} 年 {mm} 月{label}环比{word}，主要是哪些{spec['group_label']}的变化？"


def _significance(metric_name: str, current_total: dict, base_total: dict) -> tuple[float, float, float] | None:
    """(变化量, 变化显著程度, 变化率)；不满足显著阈值返回 None。"""
    kind = get_metric(metric_name).kind
    if kind == "additive":
        now, old = int(current_total["value"]), int(base_total["value"])
        if old == 0:
            return None
        change = now - old
        rate = change / old
        if abs(rate) < MIN_REL_CHANGE:
            return None
        return change, abs(rate), rate
    # 比率型：value 为 6 位小数字符串（分母 0 为 null）。
    if current_total["value"] is None or base_total["value"] is None:
        return None
    now, old = float(current_total["value"]), float(base_total["value"])
    change = now - old
    floor = RATIO_MIN_CHANGE[metric_name]
    if abs(change) < floor:
        return None
    return change, abs(change) / floor, (change / old if old else 0.0)


def compute_gold(folder, snapshot, spec: dict) -> dict:
    """按「变化显著 → 变化集中」选月份，返回 gold 与实际月份、期间、问题文本。"""
    source = ReferenceSource(folder, snapshot)
    eligible = []
    for month_text in spec["months"]:
        period = (month(month_text)["start"], month(month_text)["end"])
        prev = month(month_prev(month_text))
        base = (prev["start"], prev["end"])
        current = source.query(spec["metric"], period)
        previous = source.query(spec["metric"], base)
        significance = _significance(spec["metric"], current["total"], previous["total"])
        if significance is None:
            continue
        change, strength, rate = significance
        result = find_root_cause(source, spec["metric"], spec["dimensions"], period, base)
        if not result["candidates"]:
            continue
        ep = float(result["candidates"][0]["explanatory_power"])
        if not (EP_MIN <= ep <= EP_MAX):
            continue
        eligible.append({"month": month_text, "strength": strength, "change": change, "rate": rate,
                         "ep": ep, "result": result, "period": period, "base": base})
    if not eligible:
        raise ValueError(f"{spec['metric']} {spec['dimensions']}：候选池中没有满足显著与集中条件的月份")
    # 变化显著程度最高者（并列取月份早者）。
    best = max(eligible, key=lambda item: (item["strength"], [-int(item["month"][:4]), -int(item["month"][5:7])]))
    result, top = best["result"], best["result"]["candidates"][0]
    dimensions = [{"dimension": item["dimension"], "value": item["value"],
                   "explanatory_power": item["explanatory_power"], "surprise": item["surprise"]}
                  for item in result["candidates"][:3]]
    magnitude = _top_contribution(source, spec, best["period"], best["base"], top)
    direction = "down" if best["change"] < 0 else "up"
    return {"dimensions": dimensions, "direction": direction,
            "magnitude": magnitude, "unit": get_metric(spec["metric"]).unit,
            "note": "gold 由 A2 的 ReferenceSource + find_root_cause 在原始数据上计算（Top 候选）",
            "total_change": str(result["total_change"]), "change_rate": round(best["rate"], 6),
            "month": best["month"], "top_ep": round(best["ep"], 6),
            "question": question_of(spec, best["month"], direction),
            "period": month(best["month"]), "base_period": month(month_prev(best["month"]))}


def _top_contribution(source, spec: dict, period, base, top: dict) -> str | None:
    """首位候选对总变化的贡献（加法型为整数分；比率型为 6 位小数字符串）。"""
    try:
        drilled = contribution(source, spec["metric"], top["dimension"], period, base)
    except Exception:
        return None
    for row in drilled["rows"]:
        if row["group"] == top["value"]:
            return str(row.get("change"))
    return None
