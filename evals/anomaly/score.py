"""合成异动评测的评分函数（计划第七节）：AC@k 与贡献度绝对误差。

AC@k = gold 的 (维度, 值) 是否出现在前 k 个候选中：
- 单一 gold 维度：命中 (dimension, value) 即记 1。
- order_drop 的二维根因（product_category × customer_state）：两个 gold 维度值任一命中前 k 即记 1
  （判定规则写死为「任一命中」，对应 Agent 在任一 gold 维度上下钻到目标值都算定位成功）。
- mix_shift 要求效应类型为结构（between）效应时，候选带 effect 字段才会校验；不匹配不算命中。

``score(case, candidates) -> {"ac1": 0|1, "ac3": 0|1, "contribution_error": float|None}``：
候选形如 A2 find_root_cause 返回的扁平列表 [{dimension, value, explanatory_power, surprise, contribution?, effect?}, ...]。
贡献度绝对误差（可选）：gold 给出 magnitude（本期 − 基期的有符号变化量）且命中候选给出 contribution 时，
取两者之差的绝对值；未命中或候选未给出贡献时为 None。
"""
from __future__ import annotations


def _gold_pairs(case: dict) -> list[tuple[str, str]]:
    gold = case["gold"]
    return [(item["dimension"], str(item["value"])) for item in gold["dimensions"]]


def _hit(case: dict, candidates: list[dict], k: int) -> bool:
    pairs = set(_gold_pairs(case))
    for candidate in candidates[:k]:
        key = (candidate.get("dimension"), str(candidate.get("value")))
        if key not in pairs:
            continue
        effect = case["gold"].get("effect")
        if effect and candidate.get("effect") not in (None, effect):
            continue
        return True
    return False


def score(case: dict, candidates: list[dict]) -> dict:
    """对单个案例评分；candidates 按排名排序（第一个为 Top-1）。"""
    if not isinstance(candidates, list):
        raise TypeError("candidates 须为候选列表")
    result = {"ac1": int(_hit(case, candidates, 1)), "ac3": int(_hit(case, candidates, 3))}
    magnitude = case["gold"].get("magnitude")
    error = None
    if magnitude is not None:
        pairs = set(_gold_pairs(case))
        for candidate in candidates[:3]:
            if (candidate.get("dimension"), str(candidate.get("value"))) in pairs \
                    and candidate.get("contribution") is not None:
                try:
                    error = abs(float(candidate["contribution"]) - float(magnitude))
                except (TypeError, ValueError):
                    error = None
                break
    result["contribution_error"] = error
    return result


def summarize(rows: list[dict]) -> dict:
    """汇总多个案例的评分：AC@1、AC@3 与有贡献度样本的平均绝对误差。"""
    total = len(rows)
    errors = [row["contribution_error"] for row in rows if row["contribution_error"] is not None]
    return {"cases": total,
            "ac1": sum(row["ac1"] for row in rows) / total if total else 0.0,
            "ac3": sum(row["ac3"] for row in rows) / total if total else 0.0,
            "contribution_mae": sum(errors) / len(errors) if errors else None,
            "contribution_samples": len(errors)}
