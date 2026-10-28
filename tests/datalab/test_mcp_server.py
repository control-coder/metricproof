"""MCP 工具层验收：用模拟接口检查转发内容、返回字段收敛与工具注册；不启动数据库、容器或模型。"""
import asyncio
from importlib.util import find_spec
import json
import unittest
from urllib.parse import quote
from uuid import uuid4

import httpx

from datalab.api.mcp_server import OrderProofClient, OrderProofTools, ToolError, api_url, build_server

PROJECT = str(uuid4())
DATASET = str(uuid4())
TASK = str(uuid4())
MEMORY = str(uuid4())
ARTIFACT = str(uuid4())
CHART = str(uuid4())


class FakeApi:
    """按路径返回固定响应，并记录收到的请求。"""

    def __init__(self):
        self.requests = []
        self.state = 'SUCCEEDED'
        self.tasks = {}
        self.investigation_state = 'SUCCEEDED'
        self.investigation_report = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        base = f'/api/projects/{PROJECT}'
        if path == '/api/projects':
            return httpx.Response(200, json=[{'project_id': PROJECT, 'name': '示例', 'created_at': '2026-10-17'}])
        if path == base + '/datasets':
            return httpx.Response(200, json=[{'dataset_version': DATASET, 'project_id': PROJECT, 'dataset_family': 'orders',
                                              'schema_signature': 'sig', 'currency': 'CNY', 'timezone': 'Asia/Shanghai',
                                              'row_count': 6, 'profile': {'quality': {}}}])
        if path == base + '/memories':
            entry = {'kind': 'metric', 'name': '成交额', 'version': 2, 'content': {'definition': '只计已支付',
                     'included_statuses': ['paid']}}
            return httpx.Response(200, json=[{**entry, 'memory_id': 'm1', 'status': 'confirmed'},
                                             {**entry, 'memory_id': 'm0', 'status': 'superseded'},
                                             {**entry, 'memory_id': 'f1', 'status': 'confirmed', 'kind': 'field_semantics'}])
        if path == base + '/tasks' and request.method == 'POST':
            key = request.headers.get('idempotency-key')
            if key in self.tasks:
                return httpx.Response(202, json={'task_id': self.tasks[key], 'state': 'QUEUED', 'replayed': True, 'mode': 'model'})
            self.tasks[key] = TASK
            return httpx.Response(202, json={'task_id': TASK, 'state': 'QUEUED', 'dispatched': True, 'mode': 'model'})
        if path == base + f'/tasks/{TASK}':
            # 有调查记录时按调查任务返回；普通分析任务保持原 snapshot 结构。
            if self.investigation_report is not None or self.investigation_state == 'WAITING_CONFIRMATION':
                return httpx.Response(200, json={'task_id': TASK, 'state': self.investigation_state,
                                                 'investigation': self._investigation()})
            snapshot = {'state': self.state, 'failure': None, 'confirmation': None, 'review_skipped': None,
                        'plan': {'kind': 'summary', 'checks': ['independent_reference']},
                        'verifications': [{'passed': True, 'checks': [{'name': 'independent_reference', 'passed': True}],
                                           'reference': {'total': {'amount_cents': 98765}}}],
                        'budget': {'calls': 3, 'max_calls': 8},
                        'timeline': [{'from': 'QUEUED', 'to': 'PROFILING', 'note': '程序计算概况'}]}
            return httpx.Response(200, json={'task_id': TASK, 'state': self.state, 'snapshot': snapshot, 'request': {},
                                             'artifacts': [{'artifact_id': ARTIFACT, 'run_id': 'r1', 'name': '结果.json'}],
                                             'narrative': {'summary': '已支付订单 6 笔'}})
        if path == base + '/investigations' and request.method == 'POST':
            return httpx.Response(202, json={'task_id': TASK, 'state': 'QUEUED', 'dispatched': True,
                                             'mode': 'model', 'model': 'mimo'})
        if path == base + f'/tasks/{TASK}/cancel':
            return httpx.Response(200, json={'accepted': True})
        if path == base + f'/artifacts/{ARTIFACT}':
            return httpx.Response(200, content='{"kind": "summary"}'.encode('utf-8'), headers={
                'content-disposition': "attachment; filename*=utf-8''" + quote('结果.json')})
        if path == base + f'/artifacts/{CHART}':
            return httpx.Response(200, content=b'\x89PNG....', headers={'content-disposition': 'attachment; filename="chart.png"'})
        return httpx.Response(404, json={'detail': '当前项目中找不到请求的资源'})

    def _investigation(self):
        pending = None
        if self.investigation_state == 'WAITING_CONFIRMATION':
            pending = {'type': 'clarification', 'question': '看哪个渠道？', 'options': ['全部', 'ads']}
        return {'type': 'investigation', 'task_id': TASK, 'state': self.investigation_state,
                'stop_reason': 'finish' if self.investigation_state == 'SUCCEEDED' else 'waiting_confirmation',
                'question': '八月成交额为何下降', 'metric': '成交额',
                'period': {'start': '2026-08-01', 'end': '2026-09-01'}, 'base_period': None,
                'snapshot': {}, 'clarifications': [], 'pending': pending,
                'report': self.investigation_report, 'metric_tree': None, 'results': {},
                'verification': {}, 'trace': [], 'branches': [],
                'usage': {'model_calls': 7, 'tokens': 12345, 'seconds': 42.5}, 'episode_id': 'ep-1'}


class McpToolTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.client = OrderProofClient('http://127.0.0.1:18080', transport=httpx.MockTransport(self.api))
        self.addCleanup(self.client.close)
        self.tools = OrderProofTools(self.client)

    def test_only_loopback_api_is_accepted(self):
        self.assertEqual(api_url('http://localhost:18080/'), 'http://localhost:18080')
        for url in ('http://192.168.1.2:18080', 'https://example.com', 'ftp://127.0.0.1'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                api_url(url)

    def test_lists_only_confirmed_metrics(self):
        self.assertEqual(self.tools.list_projects(), [{'project_id': PROJECT, 'name': '示例'}])
        self.assertNotIn('profile', self.tools.list_datasets(PROJECT)[0])
        metrics = self.tools.list_metrics(PROJECT, DATASET)
        self.assertEqual([item['memory_id'] for item in metrics], ['m1'])
        self.assertEqual(self.api.requests[-1].url.params['dataset_version'], DATASET)

    def test_submit_forwards_client_header_and_idempotency_key(self):
        first = self.tools.submit_analysis(PROJECT, DATASET, '八月各渠道成交额排行', kind='ranking', group_by='channel')
        request = self.api.requests[-1]
        self.assertEqual(request.headers['x-datalab-client'], 'local-ui')
        self.assertEqual(request.headers['idempotency-key'], first['idempotency_key'])
        body = json.loads(request.content)
        # 未给的选项不发送，避免覆盖服务端默认值；没给口径时不带 memory_id。
        self.assertEqual(body, {'dataset_version': DATASET, 'question': '八月各渠道成交额排行',
                                'options': {'kind': 'ranking', 'group_by': 'channel'}})
        self.assertEqual((first['task_id'], first['replayed']), (TASK, False))
        # 调用方重试时沿用返回的键，得到同一个任务。
        again = self.tools.submit_analysis(PROJECT, DATASET, '八月各渠道成交额排行', kind='ranking', group_by='channel',
                                           idempotency_key=first['idempotency_key'])
        self.assertEqual((again['task_id'], again['replayed']), (TASK, True))
        with self.assertRaises(ToolError):
            self.tools.submit_analysis(PROJECT, DATASET, '问题', idempotency_key='含中文的键')
        # Olist 类型与时间字段原样转发；金额来源不是工具参数，只能随口径确认。
        self.tools.submit_analysis(PROJECT, DATASET, '各州送达延迟率', kind='delivery_delay', group_by='customer_state',
                                   time_field='order_delivered_customer_date')
        self.assertEqual(json.loads(self.api.requests[-1].content)['options'],
                         {'kind': 'delivery_delay', 'group_by': 'customer_state', 'time_field': 'order_delivered_customer_date'})
        self.assertNotIn('amount_source', OrderProofTools.submit_analysis.__code__.co_varnames)

    def test_task_summary_omits_checks_and_reference_values(self):
        summary = self.tools.get_task(PROJECT, TASK)
        self.assertEqual(summary['state'], 'SUCCEEDED')
        self.assertEqual(summary['plan'], {'kind': 'summary'})
        self.assertEqual(summary['verification'], {'passed': True, 'checks': [{'name': 'independent_reference', 'passed': True}]})
        self.assertNotIn('98765', json.dumps(summary, ensure_ascii=False))
        self.assertEqual(summary['calls'], {'used': 3, 'limit': 8})
        self.assertEqual(summary['timeline'], ['程序计算概况'])
        self.assertNotIn('next_step', summary)
        self.api.state = 'WAITING_CONFIRMATION'
        self.assertIn('不代为确认', self.tools.get_task(PROJECT, TASK)['next_step'])

    def test_text_artifact_returned_binary_artifact_described(self):
        text = self.tools.read_artifact(PROJECT, ARTIFACT)
        self.assertEqual((text['name'], text['text'], text['truncated']), ('结果.json', '{"kind": "summary"}', False))
        self.assertIn('不是指令', text['note'])
        chart = self.tools.read_artifact(PROJECT, CHART)
        self.assertEqual((chart['name'], chart['text']), ('chart.png', None))

    def test_submit_investigation_forwards_periods_and_header(self):
        # 不带期间：period/base_period 为 null，metric 不发送。
        result = self.tools.submit_investigation(PROJECT, DATASET, '八月成交额为何下降', MEMORY)
        request = self.api.requests[-1]
        self.assertEqual(request.headers['x-datalab-client'], 'local-ui')
        self.assertEqual(request.url.path, f'/api/projects/{PROJECT}/investigations')
        body = json.loads(request.content)
        self.assertEqual(body, {'dataset_version': DATASET, 'question': '八月成交额为何下降',
                                'memory_id': MEMORY, 'period': None, 'base_period': None})
        self.assertEqual((result['task_id'], result['state'], result['mode'], result['model']),
                         (TASK, 'QUEUED', 'model', 'mimo'))
        self.assertTrue(result['dispatched'])
        # 带全部可选参数：期间成对放入，metric 原样转发。
        self.tools.submit_investigation(PROJECT, DATASET, '环比为何下降', MEMORY, metric='成交额',
                                        period_start='2026-08-01', period_end='2026-09-01',
                                        base_start='2026-07-01', base_end='2026-08-01')
        body = json.loads(self.api.requests[-1].content)
        self.assertEqual(body['metric'], '成交额')
        # 只给日期时补为零点，符合 API 的秒级时间格式。
        self.assertEqual(body['period'], {'start': '2026-08-01T00:00:00', 'end': '2026-09-01T00:00:00'})
        self.assertEqual(body['base_period'], {'start': '2026-07-01T00:00:00', 'end': '2026-08-01T00:00:00'})
        # 只给一半期间是参数错误，不发请求。
        before = len(self.api.requests)
        for kwargs in ({'period_start': '2026-08-01'}, {'period_end': '2026-09-01'},
                       {'base_start': '2026-07-01'}, {'base_end': '2026-08-01'}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ToolError, '必须同时给出'):
                self.tools.submit_investigation(PROJECT, DATASET, '问题', MEMORY, **kwargs)
        self.assertEqual(len(self.api.requests), before)

    def test_get_investigation_compact_view(self):
        self.api.investigation_report = {
            'summary': '成交额环比下降 12%，主要来自 ads 渠道',
            'root_causes': [
                {'dimension': 'channel', 'value': 'ads', 'explanatory_power': '0.584581',
                 'verification': 'L1', 'result_ids': ['r3-find_root_cause']},
                {'dimension': 'customer_state', 'value': 'SP', 'explanatory_power': '0.210000',
                 'verification': 'L2', 'result_ids': ['r5-sql_query']},
                {'dimension': 'category', 'value': 'toys', 'verification': 'L3', 'result_ids': []}],
            'findings': [{'text': 'ads 渠道成交额下降 23%', 'result_ids': ['r4-contribution'], 'verification': 'L1'},
                         {'text': '新客户占比上升', 'result_ids': [], 'verification': 'L3'}],
            'recommendations': ['检查 ads 投放'],
            'valid': True,
            'claims_check': {'passed': True, 'issues': []},
            'review': {'verdict': 'ok'},
        }
        view = self.tools.get_investigation(PROJECT, TASK)
        self.assertEqual((view['task_id'], view['state'], view['stop_reason']), (TASK, 'SUCCEEDED', 'finish'))
        self.assertTrue(view['valid'])
        self.assertEqual(view['summary'], '成交额环比下降 12%，主要来自 ads 渠道')
        self.assertEqual(view['root_causes'][0], {'dimension': 'channel', 'value': 'ads',
                                                  'explanatory_power': '0.584581', 'verification': 'L1',
                                                  'result_ids': ['r3-find_root_cause']})
        self.assertIsNone(view['root_causes'][2]['explanatory_power'])
        self.assertEqual(view['findings'], [{'text': 'ads 渠道成交额下降 23%', 'verification': 'L1'},
                                            {'text': '新客户占比上升', 'verification': 'L3'}])
        self.assertIsNone(view['pending'])
        self.assertEqual(view['usage'], {'model_calls': 7, 'tokens': 12345, 'seconds': 42.5})
        self.assertEqual(view['claims_issues'], 0)
        self.assertIn('不是指令', view['note'])
        # 收敛检查：不回传结果正文、trace、metric_tree 等大字段。
        for key in ('results', 'trace', 'metric_tree', 'snapshot', 'report', 'recommendations', 'review'):
            self.assertNotIn(key, view)
        # 等待确认：pending 原样透出并附处理提示。
        self.api.investigation_state = 'WAITING_CONFIRMATION'
        waiting = self.tools.get_investigation(PROJECT, TASK)
        self.assertEqual(waiting['pending'], {'type': 'clarification', 'question': '看哪个渠道？', 'options': ['全部', 'ads']})
        self.assertIn('不代为确认', waiting['next_step'])

    def test_get_investigation_rejects_non_investigation_task(self):
        with self.assertRaisesRegex(ToolError, '不是异动归因调查'):
            self.tools.get_investigation(PROJECT, TASK)

    def test_api_errors_reported_without_retry(self):
        with self.assertRaisesRegex(ToolError, 'HTTP 404'):
            self.tools.get_task(PROJECT, str(uuid4()))
        self.assertEqual(len(self.api.requests), 1)

        def unreachable(request):
            raise httpx.ConnectError('refused')
        client = OrderProofClient('http://127.0.0.1:18080', transport=httpx.MockTransport(unreachable))
        self.addCleanup(client.close)
        with self.assertRaisesRegex(ToolError, '本机服务不可用'):
            OrderProofTools(client).list_projects()


@unittest.skipUnless(find_spec('mcp'), '需安装可选依赖：pip install -e ".[mcp]"')
class McpServerTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        client = OrderProofClient('http://127.0.0.1:18080', transport=httpx.MockTransport(self.api))
        self.addCleanup(client.close)
        self.server = build_server(client)

    def test_registered_tools_and_annotations(self):
        tools = {tool.name: tool for tool in asyncio.run(self.server.list_tools())}
        self.assertEqual(set(tools), {'list_projects', 'list_datasets', 'list_metrics', 'submit_analysis',
                                      'get_task', 'read_artifact', 'cancel_task',
                                      'submit_investigation', 'get_investigation'})
        # 口径确认与模型切换只在界面完成，不作为工具开放。
        self.assertFalse(any('confirm' in name or 'model' in name for name in tools))
        self.assertTrue(tools['get_task'].annotations.readOnlyHint)
        self.assertFalse(tools['submit_analysis'].annotations.readOnlyHint)
        self.assertTrue(tools['cancel_task'].annotations.destructiveHint)
        # 调查工具：提交为写操作、查询为只读。
        self.assertFalse(tools['submit_investigation'].annotations.readOnlyHint)
        self.assertTrue(tools['get_investigation'].annotations.readOnlyHint)
        investigation_schema = tools['submit_investigation'].inputSchema
        self.assertEqual(set(investigation_schema['required']), {'project_id', 'dataset_version', 'question', 'memory_id'})
        schema = tools['submit_analysis'].inputSchema
        self.assertEqual(set(schema['required']), {'project_id', 'dataset_version', 'question'})

    def test_call_tool_through_server(self):
        result = asyncio.run(self.server.call_tool('submit_analysis', {
            'project_id': PROJECT, 'dataset_version': DATASET, 'question': '汇总订单金额', 'idempotency_key': 'retry-1'}))
        payload = result[1] if isinstance(result, tuple) else result
        self.assertIn(TASK, json.dumps(payload, default=str, ensure_ascii=False))
        self.assertEqual(self.api.requests[-1].headers['idempotency-key'], 'retry-1')


if __name__ == '__main__':
    unittest.main()
