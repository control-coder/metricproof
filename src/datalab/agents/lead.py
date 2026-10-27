"""主 Agent（Plan-and-Solve）：确认指标与对比期、异动检测、指标拆解、分派子 Agent、汇总报告。

系统提示要求模型按四步走：
1. 确认指标与对比期（问题含月份时直接给出，不含时用 list_metrics/get_profile 确认）；
2. 调用 detect_anomaly 与 decompose_metric 判断是否异动、主要因子是什么；
3. 写出分析计划并调用 dispatch_subagent 分派至多 3 个维度分支；
4. 收到子 Agent 摘要后汇总，观察与预期不符（如分支解释力都很低）时允许再分派一次
   （重规划一次，第三次分派由编排层拒绝并说明），最后调用 finish 提交报告。

报告结构（finish 参数为 JSON 字符串，A6 将在此基础上做数字核对与审查 Agent，
结构便于逐条核对）：
{
  "summary": "一段中文总结（不含未引用数字）",
  "root_causes": [
    {"dimension": "customer_state", "value": "SP",
     "contribution": 或 "explanatory_power": "…", "result_ids": ["r5-…"]}
  ]（按重要性排序，供 AC@k 评分）,
  "findings": [{"text": "…", "result_ids": ["r2-…"]}],
  "recommendations": ["…"]
}

硬性约束（提示词逐条写明）：
- 文字中出现的每个数字都必须引用 result_id（如「上升 12.3%（r3-detect_anomaly）」）；
- 只做描述性归因：不写「导致」「因为」「由于」这类因果断言，写「贡献」「相关」「集中在」；
- root_causes 至少给出 1 条、至多 3 条，每条带 result_ids；
- 分派至多 3 个分支，每个分支给 metric、dimension、period、base_period 与假设（focus）。
"""
from __future__ import annotations

SYSTEM = """你是 MetricProof 的主 Agent，负责电商经营指标异动的归因分析（Plan-and-Solve）。

工作步骤（严格按顺序）：
1. 异动检测与指标拆解：调用 detect_anomaly（history 给紧邻的前 6 个自然月期间，由远及近，
   起点不要早于数据最早日期）判断变化是否超出正常波动；再调用 decompose_metric 看主要因子
   （如 GMV = 订单量 × 客单价）。服务端已在首条消息给出指标、本期与对比期，不要再花步骤确认。
2. 制定分析计划并分派：用一句简短计划说明要排查哪些维度、为什么，然后对每个维度分支调用
   一次 dispatch_subagent（至多 3 个分支；同一批分支可以在一条消息里并行发起多个调用）。
   每个分支给出 metric、dimension、period、base_period 与一句假设（branch 参数）。
   收到全部子 Agent 摘要后：如果所有分支解释力都很低（主要贡献项的 |contribution| 都 < 0.1）
   或维度明显选错，可以再分派一次（重规划一次；第三批分派会被拒绝并说明）。
3. 汇总并调用 finish：报告只写有 result_id 支撑的数字。

报告为 JSON 字符串（finish 的 report 参数），结构如下（A6 会逐条核对数字与 result_id 的绑定）：
{
  "summary": "一段中文总结",
  "root_causes": [
    {"dimension": "维度名", "value": "维度取值",
     "contribution": "贡献度（6 位小数字符串；也可用 explanatory_power）",
     "result_ids": ["rX-contribution"]}
  ],
  "findings": [{"text": "一句发现，数字后括注 result_id", "result_ids": ["rX-…"]}],
  "recommendations": ["一条可执行建议"]
}

硬性约束：
- root_causes 按重要性排序，至少 1 条、至多 3 条；每条必须有 result_ids。
- 报告中出现的每个数字（含百分数、金额、比率）都必须在其后括注 result_id，例如
  「环比上升 12.345678%（r3-detect_anomaly）」；没有 result_id 支撑的数字不要写。
- 只做描述性归因：不写「导致」「因为」「由于」「使得」这类因果断言；写「贡献」「相关」
  「集中在」「主要来自」。
- 维度取值（州、品类、卖家 ID 等）只能来自工具观察或结果 rows，不要编造。
- 每个分支的 period/base_period 与主任务一致，除非问题另有说明。"""


def opening_message(question: str, *, metric: str | None = None,
                    period: dict | None = None, base_period: dict | None = None,
                    dimensions_hint: list[str] | None = None) -> str:
    """首条用户消息：问题原文 + 服务端已确认的指标、期间与候选维度（评测时由案例注入，
    避免主 Agent 再花一步「确认指标」）。"""
    parts = [f"问题：{question}"]
    if metric:
        parts.append(f"服务端已确认：指标 {metric}")
    if period:
        parts.append(f"本期 period = {{'start': '{period['start']}', 'end': '{period['end']}'}}（左闭右开）")
    if base_period:
        parts.append(f"对比期 base_period = {{'start': '{base_period['start']}', 'end': '{base_period['end']}'}}")
    if dimensions_hint:
        parts.append("可排查的维度（按粒度与问题选择）：" + "、".join(dimensions_hint))
    parts.append("请按步骤开始：先异动检测与拆解，再分派维度分支。")
    return "\n".join(parts)
