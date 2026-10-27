"""原始数据问题（10 个）：不写死答案，gold 由 A2 的确定性参考实现在原始数据上计算。

每个问题给出 metric、分析维度与月份候选池（2017-03 至 2018-08）；``compute_gold`` 对候选池中
每个月用 ReferenceSource + find_root_cause 计算，取首位候选解释力（EP）最高的月份作为该问题的
实际月份——原始数据的真实异动大多比注入案例分散，固定月份可能根因过于分散（如 2017 年 11 月
黑五全国普涨，各州解释力都只有 0.1 上下），选 EP 最高的月份保证题目能做。月份候选池固定，
选择规则固定（EP 最高，并列取月份早者），不写死答案、不做人工标注。

gold.dimensions 取计算结果的前 3 个候选（维度、维度值、解释力、惊奇度），首位为最强根因；
magnitude 为首位候选对总变化的贡献（contribution 的 change 字段）。
评分与注入案例共用 AC@k：gold 的 (维度, 值) 出现在前 k 个候选中即命中。
"""
from __future__ import annotations

from inject import month_range

from datalab.attribution import ReferenceSource, contribution, find_root_cause

ALL_MONTHS = ["2017-03", "2017-04", "2017-05", "2017-06", "2017-07", "2017-08", "2017-09", "2017-10",
              "2017-11", "2017-12", "2018-01", "2018-02", "2018-03", "2018-04", "2018-05", "2018-06",
              "2018-07", "2018-08"]


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


def _spec(template: str, metric: str, dimensions: list[str], months: list[str]) -> dict:
    return {"template": template, "metric": metric, "dimensions": dimensions, "months": months}


# 10 个原始数据问题：指标 × 分析维度 × 月份候选池；问题模板中 {Y}/{M} 由实际选中月份填充。
RAW_QUESTIONS = [
    _spec("{Y} 年 {M} 月 GMV 环比上升，主要来自哪些地区？", "gmv", ["customer_state"],
          _months("2017-07", "2017-09", "2017-11", "2018-01", "2018-04")),
    _spec("{Y} 年 {M} 月 GMV 环比回落，主要是哪些地区的变化？", "gmv", ["customer_state"],
          _months("2017-06", "2017-12", "2018-02", "2018-06")),
    _spec("{Y} 年 {M} 月销售额环比下降，主要是哪些品类的变化？", "item_gmv", ["product_category"],
          _months("2017-12", "2018-02", "2018-04", "2018-06", "2018-08")),
    _spec("{Y} 年 {M} 月销售额环比上升，主要是哪些品类贡献的？", "item_gmv", ["product_category"],
          _months("2017-10", "2017-11", "2018-03", "2018-05", "2018-07")),
    _spec("{Y} 年 {M} 月送达延迟率相比上月明显上升，主要集中在哪些州？", "late_delivery_rate",
          ["customer_state"], _months("2017-06", "2017-10", "2018-02", "2018-03", "2018-06")),
    _spec("{Y} 年 {M} 月送达延迟率相比上月改善，主要是哪些州好转的？", "late_delivery_rate",
          ["customer_state"], _months("2017-07", "2017-11", "2018-04", "2018-07")),
    _spec("{Y} 年 {M} 月客单价相比上月上升，主要来自哪些支付方式的变化？", "aov", ["payment_type"],
          _months("2017-09", "2017-11", "2018-01", "2018-04", "2018-07")),
    _spec("{Y} 年 {M} 月客单价环比下降，主要是哪些支付方式的变化？", "aov", ["payment_type"],
          _months("2017-08", "2017-12", "2018-03", "2018-05", "2018-06")),
    _spec("{Y} 年 {M} 月差评率相比上月上升，主要集中在哪些州？", "bad_review_rate", ["customer_state"],
          _months("2017-06", "2017-10", "2018-02", "2018-05", "2018-08")),
    _spec("{Y} 年 {M} 月运费占比相比上月的变化，主要来自哪些卖家州？", "freight_ratio", ["seller_state"],
          _months("2017-06", "2017-11", "2018-02", "2018-05", "2018-08")),
]


def compute_gold(folder, snapshot, spec: dict) -> dict:
    """在月份候选池中选首位候选解释力最高的月份，返回 gold 与实际月份、期间、问题文本。"""
    source = ReferenceSource(folder, snapshot)
    best = None
    for month_text in spec["months"]:
        period = (month(month_text)["start"], month(month_text)["end"])
        prev = month(month_prev(month_text))
        base = (prev["start"], prev["end"])
        result = find_root_cause(source, spec["metric"], spec["dimensions"], period, base)
        if not result["candidates"]:
            continue
        ep = float(result["candidates"][0]["explanatory_power"])
        if best is None or ep > best["ep"]:
            best = {"ep": ep, "month": month_text, "result": result, "period": period, "base": base}
    if best is None:
        raise ValueError(f"{spec['template']}：候选月份的总变化都为 0，无法归因")
    result, top = best["result"], best["result"]["candidates"][0]
    dimensions = [{"dimension": item["dimension"], "value": item["value"],
                   "explanatory_power": item["explanatory_power"], "surprise": item["surprise"]}
                  for item in result["candidates"][:3]]
    magnitude = _top_contribution(source, spec, best["period"], best["base"], top)
    total_change = str(result["total_change"])
    year, mm = best["month"][:4], int(best["month"][5:7])
    return {"dimensions": dimensions,
            "direction": "down" if total_change.startswith("-") else "up",
            "magnitude": magnitude, "unit": "cents",
            "note": "gold 由 A2 的 ReferenceSource + find_root_cause 在原始数据上计算（Top 候选）",
            "total_change": total_change,
            "month": best["month"], "top_ep": round(best["ep"], 6),
            "question": spec["template"].format(Y=year, M=mm),
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
