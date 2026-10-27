"""子 Agent（ReAct）：负责一个维度分支的归因排查，独立上下文，只回传结构化发现。

每个子 Agent 只收到分支规格（metric、dimension、period、base_period、假设）与可用的
指标/维度目录，不看主 Agent 的历史（上下文隔离）。工作方式（ReAct：边算边想）：

1. 先用 contribution 或 find_root_cause 在分支维度上算贡献度；
2. 必要时对主要贡献项加 filters 继续下钻（如品类 → 客户州、卖家州 → 卖家）；
3. 关联排查用 correlate 或 query_metric 按 delivery_status 分组（问题提到关联时）；
4. 已解释的贡献占总变化达到 80% 时停止下钻（Adtributor 解释力阈值；工具观察中由
   编排层代码追加提示，不只靠提示词）；
5. 调用 finish 回传结构化发现。

finish 的 report 参数为 JSON 字符串，结构：
{
  "dimension": "分支维度",
  "top_contributors": [
    {"value": "维度取值", "contribution": "贡献度", "result_id": "rX-contribution"}
  ]（按 |贡献| 排序，至多 5 条）,
  "explained_ratio": "已解释贡献占总变化的比例（6 位小数字符串）",
  "notes": "一句补充说明（关联排查结论或无法解释的原因；无则空字符串）"
}

主 Agent 只收到这份摘要（不含完整轨迹）。
"""
from __future__ import annotations

SYSTEM = """你是 MetricProof 的子 Agent，负责一个维度分支的归因排查（ReAct）。你只看到本分支的
规格与工具目录，看不到主 Agent 的历史；你的结论通过 finish 回传给主 Agent。

工作方式：
1. 先用 contribution 在分支维度上算本期相对对比期的贡献度（必要时改用 find_root_cause 多维
   定位，维度集合围绕分支维度与层级相邻维度）。
2. 主要贡献项需要细分时，用 query_metric 或 contribution 加 filters 继续下钻
   （如先按 product_category 定位品类，再对该品类按 customer_state 拆）。
3. 问题提到关联（如差评与延迟）时，用 correlate 或 query_metric 按 delivery_status 分组排查。
4. 已解释的贡献（前几名 |share_of_change| 之和或分支内解释力）达到总变化的 80% 时停止下钻，
   直接 finish；工具观察会提示你当前累计解释力。
5. 数字都要来自工具结果并引用 result_id；维度取值只能来自工具观察。

finish 的 report 参数为 JSON 字符串，结构：
{
  "dimension": "分支维度",
  "top_contributors": [
    {"value": "维度取值", "contribution": "贡献度（6 位小数字符串）", "result_id": "rX-…"}
  ],
  "explained_ratio": "已解释贡献占总变化的比例（6 位小数字符串）",
  "notes": "一句补充说明（关联结论或无法解释的原因；无则空字符串）"
}
top_contributors 按 |贡献| 降序，至多 5 条；只写描述性结论（贡献、相关），不写因果断言
（不写「导致」「因为」）。"""


def branch_message(branch: dict, catalog: dict) -> str:
    """子 Agent 的首条用户消息：分支规格 + 指标/维度目录（纯数据视图，不含主 Agent 历史）。"""
    period = branch['period']
    base = branch.get('base_period') or {}
    metrics = [item['name'] for item in catalog.get('metrics', [])]
    dimensions = [item['name'] for item in catalog.get('dimensions', [])]
    lines = [
        f"分支任务：{branch.get('branch') or branch.get('focus') or '排查该维度的贡献'}",
        f"指标：{branch['metric']}；分支维度：{branch['dimension']}",
        f"本期 period = {{'start': '{period['start']}', 'end': '{period['end']}'}}（左闭右开）",
    ]
    if base:
        lines.append(f"对比期 base_period = {{'start': '{base.get('start')}', 'end': '{base.get('end')}'}}")
    lines.append("可用指标：" + "、".join(metrics))
    lines.append("可用维度：" + "、".join(dimensions))
    lines.append("先调用 contribution（或 find_root_cause）开始排查。")
    return "\n".join(lines)
