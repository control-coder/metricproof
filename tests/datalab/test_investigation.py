"""A5 主 Agent 与子 Agent 验收：离线假模型 + olist_bundle 合成数据，不发网络请求。

假模型按「系统提示区分主/子 Agent」返回预设 tool_calls：主 Agent 脚本与子 Agent 脚本分开；
工具 handler 是真实的（经 toolset 接到语义层引擎），因此 ResultStore、按引用核验与
80% 停止提示都是真实路径。
"""
import json
import random
import tempfile
import time
import unittest
from pathlib import Path

import httpx

import olist_bundle
from datalab.agents.investigation import (InvestigationBudget, run_investigation)
from datalab.datasets.service import DatasetStore
from datalab.roles.llm import ChatClient
from datalab.semantic.snapshot import SemanticSnapshot
from test_model_backend import profile

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = SemanticSnapshot("test-v1", ("delivered", "shipped", "invoiced", "processing",
                                        "approved", "created"), "payments")
P = {'start': '2017-05-01T00:00:00', 'end': '2017-06-01T00:00:00'}
B = {'start': '2017-04-01T00:00:00', 'end': '2017-05-01T00:00:00'}
HISTORY = [{'start': f'2017-0{m}-01T00:00:00',
            'end': f'2017-0{m + 1}-01T00:00:00'} for m in range(1, 5)]


def tool_call(name, arguments, call_id=None):
    return {'id': call_id or f'call_{name}', 'type': 'function', 'name': name,
            'arguments': json.dumps(arguments, ensure_ascii=False)}


def _response(calls, content=None):
    message = {'content': content, 'tool_calls': [
        {'id': call['id'], 'type': 'function',
         'function': {'name': call['name'], 'arguments': call['arguments']}} for call in calls]}
    return httpx.Response(200, json={'choices': [{'message': message, 'finish_reason': 'stop'}],
                                     'usage': {'total_tokens': 60}})


class ScriptModel:
    """按系统提示分流：主 Agent 用 lead 脚本，子 Agent 用 sub 脚本；脚本用尽返回纯文本。

    脚本项为 None 表示该步返回纯文本（无 tool_calls）。lead_delay 给主 Agent 每次请求
    加延迟（用于并行/串行时延对照；子 Agent 用 sub_delay）。
    """

    def __init__(self, lead_steps=None, sub_steps=None, sub_delay=0.0, review_content=None):
        self.lead_steps = list(lead_steps or [])
        self.sub_steps = list(sub_steps or [])
        self.sub_delay = sub_delay
        self.review_content = review_content or json.dumps({'verdict': 'pass', 'issues': []})
        self.requests = []  # (身份, body)

    def __call__(self, request):
        body = json.loads(request.content)
        system = body['messages'][0]['content']
        if '你是 MetricProof 的审查 Agent' in system:
            self.requests.append(('reviewer', body))
            return _response([], content=self.review_content)
        # 子 Agent 系统提示里也会提到「主 Agent」，用开头身份句区分。
        is_lead = '你是 MetricProof 的主 Agent' in system
        self.requests.append(('lead' if is_lead else 'sub', body))
        steps = self.lead_steps if is_lead else self.sub_steps
        if not is_lead and self.sub_delay:
            time.sleep(self.sub_delay)
        step = steps.pop(0) if len(steps) > 1 else (steps[0] if steps else None)
        if step is None:
            return _response([], content='没有需要调用的工具')
        return _response(step)


def finish_report(result_id='r2-decompose_metric'):
    return json.dumps({
        'summary': f'2017 年 5 月 GMV 环比变化，主要来自订单量（{result_id}）。',
        'root_causes': [{'dimension': 'customer_type', 'value': 'new',
                         'contribution': '0.800000', 'result_ids': [result_id]}],
        'findings': [{'text': f'订单量拆解见 {result_id}', 'result_ids': [result_id]}],
        'recommendations': ['关注新客订单变化']}, ensure_ascii=False)


def lead_script_basic():
    dispatch = {'branch': '新老客结构是否解释了订单量变化', 'dimensions': ['customer_type'],
                'metric': 'order_count', 'period': P, 'base_period': B}
    return [
        [tool_call('detect_anomaly', {'metric': 'order_count', 'period': P, 'history': HISTORY}),
         tool_call('decompose_metric', {'metric': 'order_count', 'period': P, 'base_period': B},
                   call_id='call_decomp')],
        [tool_call('dispatch_subagent', dispatch)],
        [tool_call('finish', {'report': finish_report()})],
    ]


def sub_script_contribution():
    return [
        [tool_call('contribution', {'metric': 'order_count', 'dimension': 'customer_type',
                                    'period': P, 'base_period': B})],
        [tool_call('finish', {'report': json.dumps({
            'dimension': 'customer_type',
            'top_contributors': [{'value': 'new', 'contribution': '0.800000',
                                  'result_id': 'r3-contribution'}],
            'explained_ratio': '0.900000', 'notes': ''}, ensure_ascii=False)})],
    ]


def make_folder(test, tables=None):
    """olist_bundle 原始结构经 DatasetStore.import_olist 规范化（语义层读取规范化表）。"""
    scratch = ROOT / '.tmp'
    scratch.mkdir(exist_ok=True)
    temp = tempfile.TemporaryDirectory(dir=scratch, prefix='investigation-')
    test.addCleanup(temp.cleanup)
    root = Path(temp.name)
    raw = olist_bundle.write(root / 'raw', tables or olist_bundle.bundle(random.Random(20261027), orders=60))
    store = DatasetStore(root / 'datasets')
    version = store.import_olist(raw, project_id='demo', dataset_family='olist')
    return store.directory(version.dataset_version)


def run(question, folder, model, *, parallel=True, budget=None, metric='order_count'):
    client = ChatClient(profile(), transport=httpx.MockTransport(model))
    return run_investigation(question, folder, SNAPSHOT, client, parallel=parallel,
                             budget=budget or InvestigationBudget(60, 400_000, 600),
                             metric=metric, period=P, base_period=B,
                             dimensions_hint=['customer_type', 'customer_state'])


class BasicInvestigationTests(unittest.TestCase):
    def test_dispatch_parallel_refill_and_finish(self):
        folder = make_folder(self)
        model = ScriptModel(lead_script_basic(), sub_script_contribution())
        result = run('2017 年 5 月订单量环比变化，主要来自哪些客群？', folder, model)
        self.assertEqual(result.stop_reason, 'finish')
        self.assertIsNotNone(result.report, result.trace)
        self.assertEqual(len(result.branches), 1)
        self.assertEqual(result.branches[0].stop_reason, 'finish')
        findings = result.branches[0].findings
        self.assertEqual(findings['dimension'], 'customer_type')
        # 发现回填：主 Agent 第三次请求的最后一条消息是 dispatch 的 tool 结果，内容即子 Agent 发现。
        lead_bodies = [body for role, body in model.requests if role == 'lead']
        self.assertIn('top_contributors', json.dumps(lead_bodies[2]['messages'][-1], ensure_ascii=False))
        # 报告结构与核验等级
        report = result.report
        self.assertEqual(report['root_causes'][0]['dimension'], 'customer_type')
        self.assertIn(report['root_causes'][0]['verification'], ('L1', 'L3'))
        self.assertEqual(report['causal_assertions'], [])
        # 按引用核验：报告引用了 decompose_metric 的 result_id，应已核验且通过。
        verified = result.verification
        self.assertTrue(any(item.get('passed') for item in verified.values()), verified)
        # trace 不含提示与响应原文
        trace_text = json.dumps(result.trace, ensure_ascii=False)
        self.assertNotIn('环比变化', trace_text)
        self.assertNotIn('reasoning_content', trace_text)
        kinds = {(entry['agent'], entry['kind']) for entry in result.trace}
        self.assertIn(('lead', 'model_call'), kinds)
        self.assertIn(('subagent-1', 'model_call'), kinds)
        self.assertTrue(all(entry.get('seq') for entry in result.trace))

    def test_subagent_context_isolated(self):
        folder = make_folder(self)
        model = ScriptModel(lead_script_basic(), sub_script_contribution())
        run('2017 年 5 月订单量环比变化？', folder, model)
        sub_bodies = [body for role, body in model.requests if role == 'sub']
        self.assertTrue(sub_bodies)
        for body in sub_bodies:
            text = json.dumps(body['messages'], ensure_ascii=False)
            # 子 Agent 不应看到主 Agent 的历史（异动检测观察、主 Agent 的计划文字）。
            self.assertNotIn('detect_anomaly', [call.get('name') for m in body['messages']
                                                for call in (m.get('tool_calls') or [])])
            self.assertNotIn('subagent_summaries', text)
            self.assertNotIn('decompose_metric 的观察', text)


class ReviewTests(unittest.TestCase):
    def test_bad_report_revised_once_and_marked_invalid(self):
        # 主 Agent 的报告把 customer_type=new 绑到 decompose_metric 结果上（取值与数字都对不上）：
        # 代码核对发现问题 → 修订一次（脚本重复同一 finish）→ 不再送审，最终判为无效。
        folder = make_folder(self)
        model = ScriptModel(lead_script_basic(), sub_script_contribution())
        result = run('2017 年 5 月订单量环比变化？', folder, model)
        review = result.report['review']
        self.assertEqual(review['verdict'], 'pass')
        self.assertIn('value_not_found', [item['issue'] for item in review['code_issues_before']])
        self.assertTrue(review['revised'])
        self.assertFalse(result.report['valid'])
        self.assertEqual(sum(1 for role, _ in model.requests if role == 'reviewer'), 1)
        # 审查 Agent 只看报告与代码核对结果，看不到主 Agent 与子 Agent 的工具调用历史。
        reviewer_body = next(body for role, body in model.requests if role == 'reviewer')
        self.assertEqual([m['role'] for m in reviewer_body['messages']], ['system', 'user'])
        self.assertEqual({tool['function']['name'] for tool in reviewer_body['tools']},
                         {'verify_claims', 'read_result'})

    def test_good_report_passes_without_revision(self):
        # 报告只引用子 Agent 分支（dispatch 展开为其 contribution 结果），取值与数字一致。
        def good_finish():
            return json.dumps({'summary': '订单量变化集中在新客（r3-dispatch_subagent）。',
                               'root_causes': [{'dimension': 'customer_type', 'value': 'new',
                                                'result_ids': ['r3-dispatch_subagent']}],
                               'findings': [], 'recommendations': []}, ensure_ascii=False)
        lead_steps = lead_script_basic()[:2] + [[tool_call('finish', {'report': good_finish()})]]
        model = ScriptModel(lead_steps, sub_script_contribution())
        result = run('2017 年 5 月订单量环比变化？', make_folder(self), model)
        self.assertTrue(result.report['valid'], result.report['claims_check'])
        self.assertFalse(result.report['review']['revised'])
        self.assertEqual(result.report['root_causes'][0]['verification'], 'L1')


class ReplannAndLimitTests(unittest.TestCase):
    def test_replan_once_and_third_batch_rejected(self):
        dispatch = {'branch': 'b', 'dimensions': ['customer_type'], 'metric': 'order_count',
                    'period': P, 'base_period': B}
        lead_steps = [
            [tool_call('dispatch_subagent', dispatch)],   # 第一批
            [tool_call('dispatch_subagent', dispatch, call_id='call_2')],  # 重规划：第二批
            [tool_call('dispatch_subagent', dispatch, call_id='call_3')],  # 第三批：拒绝
            [tool_call('finish', {'report': finish_report()})],
        ]
        folder = make_folder(self)
        model = ScriptModel(lead_steps, sub_script_contribution())
        result = run('x', folder, model)
        self.assertEqual(result.stop_reason, 'finish')
        self.assertEqual(len(result.branches), 2)  # 只有前两批真正执行
        # 第三批观察中带拒绝提示
        rejected = [entry for entry in result.trace
                    if entry['kind'] == 'dispatch' and entry.get('outcome') == 'rejected']
        self.assertEqual(len(rejected), 1)

    def test_duplicate_action_warning_and_stop(self):
        lead_steps = [
            [tool_call('query_metric', {'metric': 'order_count', 'period': P})],
            [tool_call('query_metric', {'metric': 'order_count', 'period': P}, call_id='call_q2')],
            [tool_call('query_metric', {'metric': 'order_count', 'period': P}, call_id='call_q3')],
        ]
        folder = make_folder(self)
        model = ScriptModel(lead_steps, [])
        result = run('x', folder, model)
        self.assertTrue(result.stop_reason.startswith('repeated_action'), result.stop_reason)
        # 降级报告：没有分支也没有 finish → 报告为 None 或 degraded
        if result.report:
            self.assertTrue(result.report.get('degraded') or result.stop_reason.endswith('degraded'))


class SubagentHintTests(unittest.TestCase):
    def test_stop_hint_at_80_percent(self):
        folder = make_folder(self)
        captured = []

        class InspectModel(ScriptModel):
            def __call__(self, request):
                body = json.loads(request.content)
                for message in body['messages']:
                    if message.get('role') == 'tool':
                        try:
                            payload = json.loads(message.get('content') or '{}')
                        except ValueError:
                            continue
                        if payload.get('hint'):
                            captured.append(payload['hint'])
                        # hint 也可以嵌在观察 content 字符串内部（loop 回填结构）。
                        try:
                            inner = json.loads(payload.get('content') or '{}')
                        except ValueError:
                            continue
                        if inner.get('hint'):
                            captured.append(inner['hint'])
                return super().__call__(request)

        sub_steps = [
            [tool_call('contribution', {'metric': 'order_count', 'dimension': 'customer_type',
                                        'period': P, 'base_period': B})],
            [tool_call('finish', {'report': json.dumps({'dimension': 'customer_type',
                                                        'top_contributors': [],
                                                        'explained_ratio': '0.9',
                                                        'notes': ''}, ensure_ascii=False)})],
        ]
        model = InspectModel(lead_script_basic(), sub_steps)
        run('x', folder, model)
        # 合成数据 customer_type 只有 new/returning 两组，贡献覆盖全部变化 → 触发 80% 停止提示。
        self.assertTrue(any('停止继续下钻' in hint for hint in captured), captured)


class BudgetDegradeTests(unittest.TestCase):
    def test_budget_exhaustion_degraded_report(self):
        folder = make_folder(self)
        model = ScriptModel(lead_script_basic(), sub_script_contribution())
        # 只允许 2 次模型调用：主 Agent 第一步检测后，分派阶段子 Agent 或后续调用超预算。
        budget = InvestigationBudget(2, 400_000, 600)
        result = run('x', folder, model, budget=budget)
        self.assertTrue(result.stop_reason.startswith('budget'), result.stop_reason)
        self.assertEqual(result.model_calls, 2)

    def test_budget_exhaustion_with_finished_branch(self):
        # 预算卡在第 3 次调用（子 Agent contribution 后 finish 超预算）→ 有分支发现 → 降级报告。
        folder = make_folder(self)
        model = ScriptModel(lead_script_basic(), sub_script_contribution())
        budget = InvestigationBudget(3, 400_000, 600)
        result = run('x', folder, model, budget=budget)
        self.assertTrue('budget' in result.stop_reason, result.stop_reason)


class ParallelTimingTests(unittest.TestCase):
    def test_parallel_faster_than_serial(self):
        dispatch2 = {'branch': '州维度', 'dimensions': ['customer_state'], 'metric': 'order_count',
                     'period': P, 'base_period': B}
        dispatch1 = {'branch': '新老客', 'dimensions': ['customer_type'], 'metric': 'order_count',
                     'period': P, 'base_period': B}

        def lead_steps():
            return [
                [tool_call('dispatch_subagent', dispatch1),
                 tool_call('dispatch_subagent', dispatch2, call_id='call_d2')],
                [tool_call('finish', {'report': finish_report()})],
            ]

        def sub_steps():
            return [
                [tool_call('contribution', {'metric': 'order_count', 'dimension': 'customer_type',
                                            'period': P, 'base_period': B})],
                [tool_call('finish', {'report': json.dumps(
                    {'dimension': 'customer_type', 'top_contributors': [],
                     'explained_ratio': '0.9', 'notes': ''}, ensure_ascii=False)})],
            ]

        folder = make_folder(self)
        model = ScriptModel(lead_steps(), sub_steps(), sub_delay=0.5)
        started = time.perf_counter()
        parallel_result = run('x', folder, model, parallel=True)
        parallel_seconds = time.perf_counter() - started

        folder2 = make_folder(self)
        model2 = ScriptModel(lead_steps(), sub_steps(), sub_delay=0.5)
        started = time.perf_counter()
        run('x', folder2, model2, parallel=False)
        serial_seconds = time.perf_counter() - started

        self.assertEqual(len(parallel_result.branches), 2)
        # 两个子 Agent 各睡 0.5s：并行时两个延迟重叠，串行时相加；
        # 一次性建表开销两者都有，因此用差值而不是绝对阈值判定确实并发。
        self.assertLess(parallel_seconds, serial_seconds)
        self.assertGreaterEqual(serial_seconds - parallel_seconds, 0.3)
        self.assertGreaterEqual(serial_seconds, 0.9)


class TraceTests(unittest.TestCase):
    def test_trace_has_no_prompts_or_raw_content(self):
        folder = make_folder(self)
        model = ScriptModel(lead_script_basic(), sub_script_contribution())
        result = run('2017 年 5 月订单量为什么变？', folder, model)
        for entry in result.trace:
            self.assertIn(entry['kind'], ('model_call', 'tool_call', 'dispatch', 'note'))
            self.assertNotIn('content', entry)
            self.assertNotIn('messages', entry)
            self.assertNotIn('report', entry)


if __name__ == '__main__':
    unittest.main()
