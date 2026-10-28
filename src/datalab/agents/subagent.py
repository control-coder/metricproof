"""子 Agent（ReAct）：负责一组维度的归因排查，独立上下文，只回传结构化发现。

每个子 Agent 只收到分支规格（metric、dimensions、period、base_period、假设）与该指标允许的
维度，不看主 Agent 的历史（上下文隔离）。固定三步（A5 调优后的做法，见 docs/evaluation.md）：

1. 同一条消息里并行调用 find_root_cause（分支全部维度）与每个分支维度的 contribution；
2. 对解释力最大、方向与总变化一致的维度值加 filters 再调用一次 find_root_cause，维度取该
   指标其余允许维度（不限本分支），看变化是否集中在某个二维组合；
3. 调用 finish 回传结构化发现。已解释的贡献达到 80% 时由代码在观察中追加停止提示。

主 Agent 只收到 finish 的这份摘要（不含完整轨迹）。
"""
from __future__ import annotations

from datalab.semantic.metrics import list_metrics

TIME_DIMENSIONS = ('purchase_date', 'purchase_month')

SYSTEM = """你是 MetricProof 的子 Agent，负责一组维度的归因排查（ReAct）。你只看到本分支的规格，
看不到主 Agent 的历史；结论通过 finish 回传给主 Agent。按下面三步走，不要多走：

第 1 步（一条消息里并行发起）：find_root_cause(dimensions=分支全部维度) 与每个分支维度各一次
contribution。
第 2 步：从结果中取解释力（share_of_change 或 explanatory_power）最大、且方向与总变化一致的
一个维度值 X=v，调用 find_root_cause(dimensions=该指标其余允许维度, filters={"where":
[{"dimension": X, "value": v}]})，看变化是否集中在 X=v 内的某个值上（二维组合）。
如果第 1 步观察提示已解释 ≥ 80%，仍做这一次下钻，然后收尾。
第 3 步：调用 finish。

规则：
- 期间参数写成 JSON 对象 {"start": ..., "end": ...}，不要写成字符串。
- 不要调用 read_result 翻页；工具结果已按变化大小排好序，前几行就是主要贡献项。
- 数字都来自工具结果并带 result_id；维度取值只能来自工具观察。
- 比率型指标（如客单价、差评率、运费占比）看 structure（组间/结构效应）与 within（组内效应）
  哪个为主，写进 effect：between 或 within。
- 只写描述性结论（贡献、相关、集中在），不写「导致」「因为」。

finish 的 report 参数为 JSON 字符串：
{
  "top_contributors": [
    {"dimension": "维度名", "value": "维度取值", "explanatory_power": "解释力（6 位小数）",
     "effect": "between|within（比率型才写）", "result_id": "rX-…"}
  ]（按解释力降序，至多 5 条；二维组合时把外层维度值放第一条、内层值放第二条）,
  "drilldown": {"filter": "X=v", "top": "内层维度=取值", "result_id": "rY-…"}（没有集中时写 null）,
  "explained_ratio": "第一条的解释力（6 位小数）",
  "notes": "一句补充说明"
}"""


def allowed_dimensions(metric: str) -> list[str]:
    for item in list_metrics():
        if item['name'] == metric:
            return [name for name in (item['dimensions'] or []) if name not in TIME_DIMENSIONS]
    return []


def branch_message(branch: dict, catalog: dict) -> str:
    """子 Agent 的首条用户消息：分支规格 + 该指标允许的维度（纯数据视图，不含主 Agent 历史）。"""
    period = branch['period']
    base = branch.get('base_period') or {}
    dimensions = list(branch.get('dimensions') or [])
    others = [name for name in allowed_dimensions(branch['metric']) if name not in dimensions]
    lines = [
        f"分支任务：{branch.get('branch') or '排查这组维度的贡献'}",
        f"指标：{branch['metric']}；分支维度：{'、'.join(dimensions)}",
        f"本期 period = {{\"start\": \"{period['start']}\", \"end\": \"{period['end']}\"}}（左闭右开）",
    ]
    if base:
        lines.append(f"对比期 base_period = {{\"start\": \"{base.get('start')}\", \"end\": \"{base.get('end')}\"}}")
    lines.append("第 2 步下钻可用的其余维度：" + ("、".join(others) if others else "无（直接 finish）"))
    lines.append("请执行第 1 步。")
    return "\n".join(lines)
