"""调查编排（A5）：主 Agent（Plan-and-Solve）+ 至多 3 个并行子 Agent（ReAct）的一次完整调查。

入口：``run_investigation(question, folder, snapshot, client, *, parallel=True, budget=None,
metric=None, period=None, base_period=None, profile=None) -> InvestigationResult``。

结构与关键取舍：
- 主 Agent 与子 Agent 的循环都由本模块驱动（复用 agents.loop 的消息回填约定与 MiMo 适配）；
  通用循环（agents.loop.AgentLoop）不含分派与压缩，A5 的扩展集中在本模块，便于 A7 接入外层状态机。
- dispatch_subagent 是编排工具：toolset 的 handler 只回显分支规格，主循环在观察后真正
  启动子 Agent（parallel=True 时 ThreadPoolExecutor 至多 3 个并行，否则串行）；子 Agent 完成后，
  结构化发现摘要作为一条合成用户消息回填到主 Agent 上下文（不含完整轨迹）。
- 重规划：同一批分派之后允许再分派一批；第三批（及以后）dispatch_subagent 的观察被追加
  拒绝提示且不启动子 Agent（主 Agent 据此汇总 finish）。
- 子 Agent 上下文隔离：只收到分支规格与指标/维度目录（subagent.branch_message）。
- 80% 停止提示：子 Agent 每次 contribution 观察后，代码取该结果前 5 行的
  |share_of_change| 之和作为已解释比例，≥ 0.8 时在观察里追加停止提示（Adtributor 阈值，
  不只靠提示词）。
- 重复动作检测：同一 Agent 第二次完全相同的工具+原始参数时观察中追加警告（结果仍真实执行），
  第三次该 Agent 直接停止（主 Agent 走降级汇总，子 Agent 以已有发现收尾）。
- 上下文压缩：主 Agent 上下文估算字符数超过阈值（60k）时，把较早的 tool 观察替换为保留
  ok/result_id/error/summary 的摘要（result_id 仍在，完整内容可用 read_result 分页）。
- 预算：InvestigationBudget（线程安全）管模型调用次数、token 与墙钟时间；不复用
  roles.runtime.CallBudget（其 600 秒上限与并行子 Agent 共享语义不符）。超预算时优雅收尾：
  主 Agent 未 finish 时用已有子 Agent 发现生成降级报告并标注 stop_reason 与 degraded。
- trace：每次模型调用与工具调用记录 {seq, agent, kind, tool?, result_id?, tokens?, seconds,
  outcome}；不保存提示、响应原文或数据内容。
- 核验：结束后对报告 root_causes/findings 引用的 result_id 调用 toolset.verify_results
  （按引用核验）；每条结论标注核验等级：全部引用核验通过为 L1；未通过、无引用或
  引用不可核验为 L3（L2 留给 A8 的 run_sql/run_python）。
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from datalab.agents import lead, subagent
from datalab.agents.loop import BOUNDARY
from datalab.agents.toolset import InvestigationToolset
from datalab.semantic.snapshot import SemanticSnapshot

MAX_BRANCHES = 3
MAX_DISPATCH_ROUNDS = 2
LEAD_MAX_STEPS = 10
SUBAGENT_MAX_STEPS = 6
COMPACT_CHARS = 60_000
KEEP_OBSERVATIONS = 6
STOP_HINT_THRESHOLD = 0.8

REPORT_ID = re.compile(r"r\d+-[a-z_]+")
CAUSAL_WORDS = ("导致", "因为", "由于", "使得")


class BudgetExceeded(Exception):
    """调查预算（调用数、token 或墙钟时间）耗尽；编排层捕获后优雅收尾。"""


class InvestigationBudget:
    """任务级共享预算：模型调用次数、token、墙钟时间；线程安全（并行子 Agent 共享）。

    默认值按一次调查的预计用量（计划第八节约 15 次调用）留出余量；墙钟默认 20 分钟。
    """

    def __init__(self, max_calls: int = 30, max_tokens: int = 300_000, seconds: float = 600.0):
        if max_calls < 1 or max_tokens < 1 or seconds <= 0:
            raise ValueError('预算必须是正数')
        self.max_calls, self.max_tokens, self.seconds = max_calls, max_tokens, seconds
        self._lock = threading.Lock()
        self.calls = 0
        self.tokens = 0
        self.started = time.monotonic()

    def remaining_time(self) -> float:
        return self.seconds - (time.monotonic() - self.started)

    def remaining_calls(self) -> int:
        with self._lock:
            return self.max_calls - self.calls

    def begin_call(self) -> tuple[float, int]:
        """返回 (请求超时秒, 本次 token 上限)；超预算抛 BudgetExceeded。"""
        with self._lock:
            if self.calls >= self.max_calls or self.tokens >= self.max_tokens \
                    or self.remaining_time() <= 0:
                raise BudgetExceeded('调查预算已耗尽')
            self.calls += 1
            return max(1.0, self.remaining_time()), max(1, self.max_tokens - self.tokens)

    def finish_call(self, tokens: int):
        with self._lock:
            self.tokens += max(0, tokens)


@dataclass
class BranchResult:
    branch: dict
    findings: dict | None
    report: str | None
    steps: int
    stop_reason: str


@dataclass
class InvestigationResult:
    question: str
    report: dict | None
    stop_reason: str
    parallel: bool
    steps: dict
    model_calls: int
    tokens: int
    seconds: float
    tool_stats: dict
    verification: dict
    trace: list[dict] = field(default_factory=list)
    branches: list[BranchResult] = field(default_factory=list)


def _report_ids(report: dict | None) -> list[str]:
    """报告中引用的 result_id 全集（结构字段与文字中的 rN-tool 形式都算）。

    模型可能把 root_causes/findings 写成字符串列表（不合结构但可挽回）：
    非 dict 项按文字扫描 result_id。
    """
    if not report:
        return []
    ids: list[str] = []
    for item in (report.get('root_causes') or []):
        if isinstance(item, dict):
            ids.extend(item.get('result_ids') or [])
        else:
            ids.extend(REPORT_ID.findall(str(item)))
    findings = report.get('findings') or []
    for item in findings:
        if isinstance(item, dict):
            ids.extend(item.get('result_ids') or [])
        else:
            ids.extend(REPORT_ID.findall(str(item)))
    ids.extend(REPORT_ID.findall(report.get('summary') or ''))
    for item in findings:
        if isinstance(item, dict):
            ids.extend(REPORT_ID.findall(item.get('text') or ''))
    return list(dict.fromkeys(ids))


def _compact(messages: list[dict], *, keep: int = KEEP_OBSERVATIONS) -> list[dict]:
    """上下文压缩：较早的较长 tool 观察替换为保留 ok/result_id/error/summary 的摘要。"""
    tool_indexes = [index for index, item in enumerate(messages) if item.get('role') == 'tool']
    for index in tool_indexes[:-keep]:
        message = messages[index]
        content = message.get('content') or ''
        if len(content) < 2000:
            continue
        try:
            payload = json.loads(content)
        except ValueError:
            continue
        brief = {'ok': payload.get('ok'), 'result_id': payload.get('result_id'),
                 'error': payload.get('error'), 'summary': payload.get('summary'),
                 'note': '较早观察已压缩；result 完整内容可用 read_result 分页读取'}
        messages[index] = {**message, 'content': json.dumps(brief, ensure_ascii=False)}
    return messages


def _observation_note(observation, note: str):
    """在观察 content（JSON 字符串）中追加 hint 字段，供模型读到代码级提示。"""
    try:
        payload = json.loads(observation.content)
    except ValueError:
        return observation
    payload['hint'] = note
    observation.content = json.dumps(payload, ensure_ascii=False)
    return observation


def _explained_ratio(result: dict | None) -> float | None:
    """contribution 结果前 5 行 |share_of_change| 之和（已解释贡献占比）。"""
    if not isinstance(result, dict) or not isinstance(result.get('rows'), list):
        return None
    shares = []
    for row in result['rows'][:5]:
        try:
            shares.append(abs(float(row.get('share_of_change'))))
        except (TypeError, ValueError):
            pass
    return sum(shares) if shares else None


def _request_messages(history: list) -> list:
    """发给模型的消息：只保留最近一条 assistant 消息的 reasoning_content。

    MiMo 建议保留思考内容，但每轮思考有上万字符，全部回填会让提示随步数线性膨胀
    （A5 实测单次请求提示达 3.6 万 token、单次调用十几秒）；只留最近一条兼顾两者。
    """
    last = max((index for index, item in enumerate(history) if item.get('role') == 'assistant'), default=-1)
    return [item if index == last or 'reasoning_content' not in item
            else {key: value for key, value in item.items() if key != 'reasoning_content'}
            for index, item in enumerate(history)]


def _request_model(investigation: 'Investigation', agent_id: str, history: list, tools: list, *,
                   thinking: bool | None = None, max_tokens: int = 8192):
    """一次模型调用：预算检查、用量入账与 trace；失败抛回给调用方处理。"""
    timeout, token_limit = investigation.budget.begin_call()
    started = time.perf_counter()
    try:
        reply = investigation.client.complete_tools(
            _request_messages(history), tools, timeout=max(1.0, min(timeout, 180)),
            max_tokens=min(token_limit, max_tokens), thinking=thinking)
    except Exception as error:
        investigation.budget.finish_call(0)
        _trace(investigation, agent_id, 'model_call',
               seconds=round(time.perf_counter() - started, 3), outcome=type(error).__name__)
        raise
    investigation.budget.finish_call(reply.tokens)
    _trace(investigation, agent_id, 'model_call', tokens=reply.tokens,
           seconds=round(time.perf_counter() - started, 3), outcome='ok')
    return reply


def _trace(investigation: 'Investigation', agent: str, kind: str, **fields):
    with investigation._trace_lock:
        investigation.trace.append({'seq': len(investigation.trace) + 1, 'agent': agent,
                                    'kind': kind, **fields})


def _assistant_message(reply) -> dict:
    """assistant 历史消息：content 为 None 一律回填 ""（MiMo 约束），reasoning_content 原样保留。

    tool_calls 回填为 OpenAI 嵌套结构 {id, type, function: {name, arguments}}：扁平结构
    （llm.ToolReply 的内部表示）被 MiMo 拒绝（HTTP 400「`function` is not set」，A5 实测）。
    """
    calls = [{'id': call['id'], 'type': 'function',
              'function': {'name': call['name'], 'arguments': call['arguments']}}
             for call in reply.tool_calls]
    message = {'role': 'assistant', 'content': reply.content or '', 'tool_calls': calls}
    if reply.reasoning_content:
        message['reasoning_content'] = reply.reasoning_content
    return message


class _DuplicateGuard:
    """重复动作检测：同一 Agent 的工具+规范化参数签名计数；第二次警告、第三次停止。

    参数规范化与 tools.py 的重复统计一致（JSON 排序键）：同一语义参数不同字符串写法不重复。
    finish 与 dispatch_subagent 不计重复：finish 是收尾动作（内容随分析推进自然变化，
    步数上限已兜底），dispatch 的批次语义由 MAX_DISPATCH_ROUNDS 单独管理。
    """

    EXEMPT = frozenset({'finish', 'dispatch_subagent'})

    def __init__(self):
        self._counts: dict[str, int] = {}

    def check(self, call: dict, observation) -> bool:
        name = str(call.get('name'))
        if name in self.EXEMPT:
            return False
        raw = call.get('arguments')
        try:
            canonical = json.dumps(json.loads(raw), ensure_ascii=False, sort_keys=True) \
                if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, sort_keys=True)
        except ValueError:
            canonical = str(raw)
        key = name + '\x00' + canonical
        count = self._counts.get(key, 0) + 1
        self._counts[key] = count
        if count == 2:
            _observation_note(observation,
                              '警告：这是对同一工具同一参数的第二次调用；第三次将停止本 Agent。')
        return count >= 3


def run_subagent(investigation: 'Investigation', branch: dict, index: int) -> BranchResult:
    """子 Agent（ReAct）：独立上下文，只收到分支规格与目录；只回传结构化发现。"""
    agent_id = f'subagent-{index}'
    registry = investigation.toolset.registry
    history = [{'role': 'system', 'content': subagent.SYSTEM + '\n' + BOUNDARY},
               {'role': 'user', 'content': subagent.branch_message(branch, investigation.catalog_view)}]
    tools = registry.schemas('subagent')
    guard = _DuplicateGuard()
    report = None
    stop_reason = 'max_steps'
    steps = 0
    last_results: list[str] = []
    for step in range(1, SUBAGENT_MAX_STEPS + 1):
        steps = step
        try:
            reply = _request_model(investigation, agent_id, history, tools,
                                   thinking=investigation.subagent_thinking, max_tokens=4096)
        except BudgetExceeded:
            stop_reason = 'budget'
            break
        except Exception as error:
            stop_reason = f'model_error:{type(error).__name__}'
            break
        history.append(_assistant_message(reply))
        if not reply.tool_calls:
            stop_reason = 'no_tool_calls'
            report = reply.content
            break
        observations = []
        for call in reply.tool_calls:
            observation = registry.call('subagent', call)
            _trace(investigation, agent_id, 'tool_call', tool=call.get('name'),
                   result_id=observation.result_id, outcome='ok' if observation.ok else 'error')
            stopped = guard.check(call, observation)
            if observation.ok and call.get('name') in ('contribution', 'find_root_cause'):
                last_results.append(observation.result_id)
            if not stopped and observation.ok and observation.result_id \
                    and call.get('name') == 'contribution':
                entry = registry.store.get(observation.result_id)
                ratio = _explained_ratio(entry['result'] if entry else None)
                if ratio is not None and ratio >= STOP_HINT_THRESHOLD:
                    _observation_note(
                        observation,
                        f'已解释的贡献累计达到 {ratio:.3f}（≥ {STOP_HINT_THRESHOLD}）总变化；'
                        '请停止继续下钻并调用 finish。')
            observations.append((call, observation))
            if stopped:
                stop_reason = 'repeated_action'
        for call, observation in observations:
            history.append({'role': 'tool', 'tool_call_id': call['id'], 'name': call['name'],
                            'content': json.dumps(observation.message(), ensure_ascii=False)})
        if stop_reason == 'repeated_action':
            break
        for call, observation in observations:
            if call['name'] == 'finish' and observation.ok:
                stop_reason = 'finish'
                report = observation.content
                break
        if stop_reason == 'finish':
            break
    findings = _parse_findings(report)
    if findings is None and last_results:
        # 子 Agent 没有正常 finish（步数、预算、重复动作）：用它最后一次归因结果的前几项兜底，
        # 标注 fallback，主 Agent 仍能拿到可引用 result_id 的发现。
        findings = _fallback_findings(registry.store.get(last_results[-1]))
    return BranchResult(branch, findings, report, steps, stop_reason)


def _fallback_findings(entry: dict | None) -> dict | None:
    if not entry or not isinstance(entry.get('result'), dict):
        return None
    result, result_id = entry['result'], entry['result_id']
    if entry['tool'] == 'find_root_cause':
        top = [{'dimension': item['dimension'], 'value': item['value'],
                'explanatory_power': item['explanatory_power'], 'result_id': result_id}
               for item in (result.get('candidates') or [])[:3]]
    else:
        top = [{'dimension': result.get('dimension'), 'value': row['group'],
                'contribution': row.get('share_of_change'), 'result_id': result_id}
               for row in (result.get('rows') or [])[:3]]
    return {'top_contributors': top, 'notes': '子 Agent 未正常收尾，取其最后一次归因结果的前几项',
            'fallback': True}


def _parse_findings(report: str | None) -> dict | None:
    """子 Agent finish 观察 → 结构化发现；观察外壳 {result_id, result:{report}} 与直接 JSON 都接受。"""
    if not report:
        return None
    candidates = [report]
    try:
        outer = json.loads(report)
        if isinstance(outer, dict):
            if isinstance(outer.get('report'), str):
                candidates.append(outer['report'])
            result = outer.get('result')
            if isinstance(result, dict) and isinstance(result.get('report'), str):
                candidates.append(result['report'])
    except ValueError:
        pass
    for candidate in candidates:
        try:
            payload = json.loads(candidate, strict=False)
        except ValueError:
            continue
        if isinstance(payload, dict) and ('top_contributors' in payload or 'dimension' in payload):
            return payload
    return None


class Investigation:
    """一次调查的全部状态：toolset、预算、trace、分派与汇总。"""

    def __init__(self, question: str, folder: Path, snapshot: SemanticSnapshot, client, *,
                 parallel: bool, budget: InvestigationBudget | None, profile: dict | None,
                 toolset: InvestigationToolset | None = None, subagent_thinking: bool | None = False):
        self.question = question
        # 子 Agent 只按固定套路调用归因工具，默认关闭思考（MiMo thinking.disabled）以压低时延；
        # 主 Agent 负责计划与汇总，保留供应商默认的思考模式。
        self.subagent_thinking = subagent_thinking
        self.toolset = toolset or InvestigationToolset(Path(folder), snapshot, profile=profile)
        self.owns_toolset = toolset is None
        self.client = client
        self.parallel = parallel
        self.budget = budget or InvestigationBudget()
        self.trace: list[dict] = []
        self._trace_lock = threading.Lock()
        self.catalog_view = {'metrics': [{'name': name} for name in self.toolset.catalog['metrics']],
                             'dimensions': [{'name': name} for name in self.toolset.catalog['dimensions']]}
        self.branches: list[BranchResult] = []
        self.base_period: dict | None = None
        self._branch_index = 0
        self._branch_lock = threading.Lock()

    # ---------------------------------------------------------------- 主 Agent

    def run(self, *, metric: str | None, period: dict | None, base_period: dict | None,
            dimensions_hint: list[str] | None) -> InvestigationResult:
        started = time.perf_counter()
        self.base_period = base_period
        registry = self.toolset.registry
        history = [{'role': 'system', 'content': lead.SYSTEM + '\n' + BOUNDARY},
                   {'role': 'user', 'content': lead.opening_message(
                       self.question, metric=metric, period=period, base_period=base_period,
                       dimensions_hint=dimensions_hint)}]
        tools = registry.schemas('lead')
        guard = _DuplicateGuard()
        dispatch_rounds = 0
        report = None
        stop_reason = 'running'
        steps = 0
        nudged = False
        for step in range(1, LEAD_MAX_STEPS + 1):
            steps = step
            history = _compact(history)
            if not nudged and self.branches and (step >= LEAD_MAX_STEPS - 1 or self.budget.remaining_calls() <= 3
                                                 or self.budget.remaining_time() < 120):
                # 步数或预算将尽：要求主 Agent 立即用已有发现 finish，避免整份调查没有报告。
                history.append({'role': 'user', 'content': '步数或预算即将用尽：请立即用已有的子 Agent 发现调用 finish。'})
                nudged = True
            try:
                reply = _request_model(self, 'lead', history, tools)
            except BudgetExceeded:
                stop_reason = 'budget'
                break
            except Exception as error:
                stop_reason = f'model_error:{type(error).__name__}'
                break
            history.append(_assistant_message(reply))
            if not reply.tool_calls:
                stop_reason = 'no_tool_calls'
                report = self._parse_report(reply.content)
                break
            observations = []
            dispatches: list = []
            for call in reply.tool_calls:
                observation = registry.call('lead', call)
                _trace(self, 'lead', 'tool_call', tool=call.get('name'),
                       result_id=observation.result_id, outcome='ok' if observation.ok else 'error')
                if guard.check(call, observation):
                    stop_reason = 'repeated_action'
                if call.get('name') == 'dispatch_subagent':
                    dispatches.append((call, observation))
                observations.append((call, observation))
            # 分派批次：同一批 dispatch 全部观察后统一执行；批次超过 MAX_DISPATCH_ROUNDS 拒绝。
            # 只有至少一个分支规格合法的批次才计数（参数错误的批次不占重规划名额）。
            accepted = [(call, observation) for call, observation in dispatches if observation.ok]
            if accepted and stop_reason == 'running':
                dispatch_rounds += 1
                if dispatch_rounds > MAX_DISPATCH_ROUNDS:
                    for call, observation in accepted:
                        _observation_note(
                            observation,
                            f'已分派过 {MAX_DISPATCH_ROUNDS} 批分支；本批被拒绝。'
                            '请汇总已有子 Agent 发现并调用 finish。')
                        _trace(self, 'lead', 'dispatch', tool='dispatch_subagent',
                               outcome='rejected')
                else:
                    self._dispatch(accepted[:MAX_BRANCHES])
                    for call, observation in accepted[MAX_BRANCHES:]:
                        _observation_note(observation, f'每批至多 {MAX_BRANCHES} 个分支；本分支未执行。')
            for call, observation in observations:
                history.append({'role': 'tool', 'tool_call_id': call['id'], 'name': call['name'],
                                'content': json.dumps(observation.message(), ensure_ascii=False)})
            if stop_reason == 'repeated_action':
                break
            for call, observation in observations:
                if call['name'] == 'finish' and observation.ok:
                    stop_reason = 'finish'
                    report = self._parse_report(observation.content)
                    break
            if stop_reason == 'finish':
                break
        if stop_reason == 'running':
            stop_reason = 'max_steps'
        if report is None and self.branches:
            report = self._degraded_report()
            if stop_reason in ('budget', 'max_steps', 'no_tool_calls', 'repeated_action'):
                stop_reason = f'{stop_reason}_degraded'
        referenced = _report_ids(report)
        verification = self.toolset.verify_results(referenced)
        graded = self._grade(report, verification)
        seconds = round(time.perf_counter() - started, 3)
        stats = registry.stats()
        tool_stats = {'total': stats.total, 'accuracy': round(stats.accuracy, 6),
                      'denied': stats.denied, 'duplicate': stats.duplicate,
                      'duplicate_ratio': round(stats.duplicate_ratio, 6)}
        return InvestigationResult(
            self.question, graded, stop_reason, self.parallel,
            {'lead': steps, 'subagents': [branch.steps for branch in self.branches]},
            self.budget.calls, self.budget.tokens, seconds, tool_stats, verification,
            trace=list(self.trace), branches=self.branches)

    # ---------------------------------------------------------------- 分派

    def _dispatch(self, accepted: list) -> None:
        """运行一批子 Agent，并把每个 dispatch 观察的内容替换为对应分支的结构化发现。

        A5 实测：发现以单独的 user 消息回填时，主 Agent 会去 read_result(r-dispatch) 找结果、
        再次分派，白白耗掉步数；放进 dispatch 自己的 tool 结果里，符合 Function Calling 的语义。
        """
        runners = []
        for call, observation in accepted:
            spec = json.loads(observation.content)['result']
            if not spec.get('base_period') and self.base_period:
                spec['base_period'] = self.base_period  # 主 Agent 省略基期时沿用主任务的对比期
            with self._branch_lock:
                self._branch_index += 1
                index = self._branch_index
            runners.append((spec, index, observation))
        _trace(self, 'lead', 'dispatch', tool='dispatch_subagent', outcome=f'{len(runners)} branches')
        if self.parallel and len(runners) > 1:
            with ThreadPoolExecutor(max_workers=MAX_BRANCHES) as pool:
                results = list(pool.map(lambda item: run_subagent(self, item[0], item[1]), runners))
        else:
            results = [run_subagent(self, spec, index) for spec, index, _ in runners]
        self.branches.extend(results)
        for (_, _, observation), result in zip(runners, results):
            payload = {'result_id': observation.result_id, 'branch': result.branch.get('branch'),
                       'dimensions': result.branch.get('dimensions'), 'metric': result.branch.get('metric'),
                       'findings': result.findings, 'stop_reason': result.stop_reason,
                       'note': '子 Agent 结构化发现（摘要，不含完整轨迹）；数字的 result_id 可直接在报告中引用'}
            observation.content = json.dumps(payload, ensure_ascii=False)

    # ---------------------------------------------------------------- 报告

    @staticmethod
    def _parse_report(report_text: str | None) -> dict | None:
        """finish 观察 → 报告 dict；接受观察外壳 {result:{report: json}}、直接 JSON 或文字内嵌 JSON。"""
        if not report_text:
            return None
        candidates = [report_text]
        try:
            outer = json.loads(report_text)
            if isinstance(outer, dict):
                if isinstance(outer.get('report'), str):
                    candidates.append(outer['report'])
                result = outer.get('result')
                if isinstance(result, dict) and isinstance(result.get('report'), str):
                    candidates.append(result['report'])
        except ValueError:
            pass
        for candidate in candidates:
            try:
                payload = json.loads(candidate, strict=False)
            except ValueError:
                match = re.search(r'\{.*\}', candidate, re.DOTALL)
                if not match:
                    continue
                try:
                    payload = json.loads(match.group(0), strict=False)
                except ValueError:
                    continue
            if isinstance(payload, dict) and isinstance(payload.get('summary'), str):
                return payload
        return None

    def _degraded_report(self) -> dict:
        """预算耗尽/步数用尽/提前停止时的降级报告：用已有子 Agent 发现拼最小报告并标注。"""
        root_causes = []
        findings = []
        for branch in self.branches:
            if not branch.findings:
                continue
            dimensions = branch.branch.get('dimensions') or [None]
            for item in (branch.findings.get('top_contributors') or [])[:3]:
                dimension = item.get('dimension') or branch.findings.get('dimension') or dimensions[0]
                root_causes.append({'dimension': dimension, 'value': item.get('value'),
                                    'contribution': item.get('contribution'),
                                    'result_ids': [item['result_id']] if item.get('result_id') else []})
            if branch.findings.get('notes'):
                findings.append({'text': branch.findings['notes'], 'result_ids': []})
        return {'summary': '降级报告：调查因预算或步数限制提前结束，以下结论来自已完成的子 Agent 摘要。',
                'root_causes': root_causes[:3], 'findings': findings,
                'recommendations': [], 'degraded': True}

    def _grade(self, report: dict | None, verification: dict) -> dict | None:
        """每条结论标注核验等级：全部引用核验通过为 L1，否则 L3；附因果断言扫描结果。"""
        if report is None:
            return None
        graded = dict(report)

        def level_of(ids: list[str]) -> str:
            if not ids:
                return 'L3'
            for result_id in ids:
                outcome = verification.get(result_id) or {}
                if outcome.get('skipped') or not outcome.get('passed'):
                    return 'L3'
            return 'L1'

        def normalize(item):
            # 模型可能把条目写成纯字符串：保留原文并标 L3（无结构化 result_ids）。
            if isinstance(item, dict):
                return {**item, 'verification': level_of(item.get('result_ids') or [])}
            return {'text': str(item), 'result_ids': [], 'verification': 'L3'}

        graded['root_causes'] = [normalize(item) for item in graded.get('root_causes') or []]
        graded['findings'] = [normalize(item) for item in graded.get('findings') or []]
        hits = []
        texts = [graded.get('summary') or '']
        texts.extend(item.get('text') or '' for item in graded.get('findings') or [])
        for text in texts:
            hits.extend(word for word in CAUSAL_WORDS if word in text)
        graded['causal_assertions'] = sorted(set(hits))
        return graded

    def close(self):
        if self.owns_toolset:
            self.toolset.close()


def run_investigation(question: str, folder: Path, snapshot: SemanticSnapshot, client, *,
                      parallel: bool = True, budget: InvestigationBudget | None = None,
                      metric: str | None = None, period: dict | None = None,
                      base_period: dict | None = None, profile: dict | None = None,
                      dimensions_hint: list[str] | None = None,
                      toolset: InvestigationToolset | None = None,
                      subagent_thinking: bool | None = False) -> InvestigationResult:
    """一次完整调查：主 Agent 检测与拆解 → 分派 → 子 Agent 排查 → 汇总 → 按引用核验。

    client 为带 complete_tools 的模型客户端（roles.llm.ChatClient）；folder 为数据版本目录；
    snapshot 为已确认口径快照。parallel=True 时子 Agent 至多 3 个并行，否则串行（A5 时延对照）。
    """
    investigation = Investigation(question, folder, snapshot, client, parallel=parallel,
                                  budget=budget, profile=profile, toolset=toolset,
                                  subagent_thinking=subagent_thinking)
    try:
        return investigation.run(metric=metric, period=period, base_period=base_period,
                                 dimensions_hint=dimensions_hint)
    finally:
        investigation.close()
