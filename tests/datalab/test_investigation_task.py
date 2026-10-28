"""A9 调查任务验收：离线假模型 + 假仓库，不连 PostgreSQL、不发网络请求。

纯函数部分（截断、结果汇总、报告组装）直接构造输入；run_task 的状态序列用
tests/datalab/test_investigation.py 的 ScriptModel 与合成数据，经 client_factory /
store_factory / memory 注入，不经过 Celery 与数据库。
"""
import json
import threading
import unittest
from pathlib import Path

import httpx

from datalab.memory.local import InMemoryMemoryRepository
from datalab.memory.service import MemoryService, Scope
from datalab.orchestration.investigation_task import (collect_results, report_payload,
                                                      run_task, trim_value)
from datalab.orchestration.workflow import MetricSnapshot, State, Task
from datalab.roles.llm import ChatClient
from test_hitl import GOOD_REPORT
from test_investigation import (B, P, SNAPSHOT, ScriptModel, lead_script_basic,
                                make_folder, sub_script_contribution, tool_call)
from test_model_backend import profile

ROOT = Path(__file__).resolve().parents[2]


class FakeRepository:
    """记录 add_artifact 与 save 的假仓库；行锁与围栏由真实 PostgreSQL 保证，这里只留调用痕迹。"""

    def __init__(self):
        self.artifacts = []
        self.saves = []

    def add_artifact(self, task_id, run_id, name, relative_path, token):
        self.artifacts.append({'task_id': task_id, 'run_id': run_id, 'name': name,
                               'relative_path': relative_path, 'token': token})

    def save(self, task, token, *, release=False):
        self.saves.append({'task_id': task.task_id, 'state': task.state.value,
                           'failure': task.failure, 'release': release})


class FakeSettings:
    database_url = 'postgresql://unused'
    artifact_root = ROOT / '.tmp' / 'investigation-task-test'
    executor_image = 'unused'


def make_task(folder):
    """构造一个调查任务（metric 快照与合成数据签名一致）。"""
    version = json.loads((folder / 'metadata.json').read_text(encoding='utf-8'))
    metric = MetricSnapshot('test:v1', SNAPSHOT.included_statuses, 'test',
                            version['project_id'], version['dataset_family'],
                            version['schema_signature'], SNAPSHOT.amount_source)
    return Task(version['project_id'], version['dataset_version'], '2017 年 5 月订单量环比变化？',
                metric=metric)


def run_row(task, question='2017 年 5 月订单量环比变化？'):
    return {'request': {'type': 'investigation', 'mode': 'model',
                        'model': {'provider': 'mimo', 'model': 'mimo-v2.6-flash'},
                        'investigation': {'question': question, 'metric': 'order_count',
                                          'period': P, 'base_period': B, 'clarifications': []}},
            'fence': 1}


def run_investigation_task(task, row, folder, model, repository=None):
    """一次离线 run_task：注入假模型与假仓库，返回 (task, repository)。"""
    repository = repository or FakeRepository()
    client = ChatClient(profile(), transport=httpx.MockTransport(model))
    store = type('Store', (), {'directory': lambda self, version: folder,
                               'get': lambda self, version, project: type('V', (), {'profile': {}})()})()
    run_task(task, row, repository, 'test-token', FakeSettings(), threading.Event(),
             client_factory=lambda provider: client,
             store_factory=lambda: store,
             memory=(MemoryService(InMemoryMemoryRepository()),
                     Scope(task.project_id, task.metric.dataset_family, task.metric.schema_signature)))
    return task, repository


class TrimTests(unittest.TestCase):
    def test_long_list_cut_to_twenty(self):
        value = {'rows': [{'v': index} for index in range(25)]}
        trimmed, truncated = trim_value(value)
        self.assertTrue(truncated)
        self.assertEqual(len(trimmed['rows']), 20)
        self.assertEqual([row['v'] for row in trimmed['rows']], list(range(20)))

    def test_short_list_untouched(self):
        value = {'rows': [{'v': 1}], 'note': 'ok'}
        trimmed, truncated = trim_value(value)
        self.assertFalse(truncated)
        self.assertEqual(trimmed, value)

    def test_contribution_rows_sorted_by_abs_contribution(self):
        rows = [{'group': 'a', 'contribution': '0.1'},
                {'group': 'b', 'contribution': '-0.9'},
                {'group': 'c', 'contribution': '0.5'}]
        trimmed, _ = trim_value({'rows': rows})
        self.assertEqual([row['group'] for row in trimmed['rows']], ['b', 'c', 'a'])

    def test_nested_lists_trimmed(self):
        value = {'outer': [{'inner': list(range(30))}]}
        trimmed, truncated = trim_value(value)
        self.assertTrue(truncated)
        self.assertEqual(len(trimmed['outer'][0]['inner']), 20)


class CollectResultsTests(unittest.TestCase):
    def test_results_and_metric_tree_from_store(self):
        from datalab.agents.tools import ResultStore
        store = ResultStore()
        store.add('list_metrics', {}, None, {'rows': []})
        store.add('decompose_metric', {'metric': 'order_count'}, 'L1', {'factors': [1, 2]})
        store.add('contribution', {'dimension': 'customer_type'}, 'L1', {'rows': list(range(25))})
        store.add('finish', {}, None, {'report': '{}'})
        results, metric_tree = collect_results(store)
        self.assertEqual(set(results), {'r2-decompose_metric', 'r3-contribution'})
        self.assertEqual(metric_tree['result'], {'factors': [1, 2]})
        self.assertEqual(len(results['r3-contribution']['result']['rows']), 20)
        self.assertTrue(results['r3-contribution']['truncated'])

    def test_no_decompose_metric_gives_none(self):
        from datalab.agents.tools import ResultStore
        store = ResultStore()
        store.add('query_metric', {'metric': 'order_count'}, 'L1', {'total': 1})
        _, metric_tree = collect_results(store)
        self.assertIsNone(metric_tree)


class ReportPayloadTests(unittest.TestCase):
    def test_keys_and_snapshot(self):
        from datalab.agents.investigation import InvestigationResult
        from datalab.agents.tools import ResultStore
        task = make_task(make_folder(self))
        row = run_row(task)
        result = InvestigationResult(
            question=task.question, report={'summary': 'x'}, stop_reason='finish', parallel=True,
            steps={'lead': 1, 'subagents': [], 'reviewer': 0}, model_calls=3, tokens=100, seconds=1.5,
            tool_stats={'total': 4, 'accuracy': 1.0, 'denied': 0, 'duplicate': 0, 'duplicate_ratio': 0.0},
            verification={'r1-x': {'passed': True}}, trace=[], branches=[], pending=None, episode_id='ep-1')
        payload = report_payload(task, result, ResultStore(), request=row['request'])
        self.assertEqual(payload['type'], 'investigation')
        self.assertEqual(payload['task_id'], task.task_id)
        self.assertEqual(payload['state'], task.state.value)
        self.assertEqual(payload['metric'], 'order_count')
        self.assertEqual(payload['snapshot']['version'], 'test:v1')
        self.assertEqual(payload['snapshot']['amount_source'], SNAPSHOT.amount_source)
        self.assertEqual(payload['usage'], {'model_calls': 3, 'tokens': 100, 'seconds': 1.5})
        self.assertEqual(payload['episode_id'], 'ep-1')
        self.assertIsNone(payload['metric_tree'])


class StateSequenceTests(unittest.TestCase):
    def test_finish_valid_report_succeeds(self):
        folder = make_folder(self)
        task = make_task(folder)
        # 好报告：引用 dispatch 展开结果，取值与数字一致，代码核对通过。
        lead_steps = lead_script_basic()[:2] + [
            [tool_call('finish', {'report': GOOD_REPORT})]]
        model = ScriptModel(lead_steps, sub_script_contribution())
        task, repository = run_investigation_task(task, run_row(task), folder, model)
        states = [item['to'] for item in task.timeline]
        self.assertEqual(states, ['PROFILING', 'PLANNING', 'EXECUTING', 'VERIFYING', 'SUCCEEDED'])
        self.assertIsNone(task.failure)
        self.assertEqual(len(repository.artifacts), 1)
        self.assertEqual(repository.artifacts[0]['name'], '调查报告.json')
        self.assertEqual(repository.saves[-1]['state'], 'SUCCEEDED')
        self.assertTrue(repository.saves[-1]['release'])

    def test_finish_invalid_report_fails_but_saves_report(self):
        folder = make_folder(self)
        task = make_task(folder)
        # 基础报告取值与引用结果对不上，代码核对判为无效（同 ReviewTests 的 bad 报告）。
        model = ScriptModel(lead_script_basic(), sub_script_contribution())
        # 用 test_hitl 的 bad 报告：value_not_found。
        bad_report = json.dumps({
            'summary': '2017 年 5 月订单量变化（r2-decompose_metric）。',
            'root_causes': [{'dimension': 'customer_type', 'value': 'new',
                             'contribution': '0.800000', 'result_ids': ['r2-decompose_metric']}],
            'findings': [], 'recommendations': []}, ensure_ascii=False)
        model.lead_steps = [lead_script_basic()[0], lead_script_basic()[1],
                            [tool_call('finish', {'report': bad_report})]]
        task, repository = run_investigation_task(task, run_row(task), folder, model)
        states = [item['to'] for item in task.timeline]
        self.assertEqual(states, ['PROFILING', 'PLANNING', 'EXECUTING', 'VERIFYING', 'FAILED'])
        self.assertEqual(task.failure, 'REPORT_INVALID')
        self.assertEqual(len(repository.artifacts), 1)  # 报告仍保存
        self.assertEqual(repository.saves[-1]['state'], 'FAILED')

    def test_waiting_confirmation_stops(self):
        folder = make_folder(self)
        task = make_task(folder)
        model = ScriptModel([[tool_call('ask_user', {'question': '指哪个指标？', 'options': ['gmv']})]],
                            sub_script_contribution())
        task, repository = run_investigation_task(task, run_row(task), folder, model)
        states = [item['to'] for item in task.timeline]
        self.assertEqual(states, ['PROFILING', 'PLANNING', 'WAITING_CONFIRMATION'])
        self.assertEqual(task.state, State.WAITING_CONFIRMATION)
        self.assertEqual(task.confirmation, '指哪个指标？')
        # 等待态也写调查报告，界面从中读取 pending 的问题与选项。
        self.assertEqual([item['name'] for item in repository.artifacts], ['调查报告.json'])
        self.assertEqual(len(repository.saves), 1)
        self.assertEqual(repository.saves[0]['state'], 'WAITING_CONFIRMATION')
        self.assertTrue(repository.saves[0]['release'])

        # 答复后（repository.answer_investigation 只改数据库状态，快照仍是等待态）重新执行：
        # 从 PLANNING 接续，不重复 PROFILING。
        row = run_row(task)
        row['request']['investigation']['clarifications'] = [{'question': '指哪个指标？', 'answer': 'gmv'}]
        model = ScriptModel(lead_script_basic()[:2] + [[tool_call('finish', {'report': GOOD_REPORT})]],
                            sub_script_contribution())
        task, repository = run_investigation_task(task, row, folder, model)
        states = [item['to'] for item in task.timeline]
        self.assertEqual(states, ['PROFILING', 'PLANNING', 'WAITING_CONFIRMATION',
                                  'PLANNING', 'EXECUTING', 'VERIFYING', 'SUCCEEDED'])
        first_lead = next(body for who, body in model.requests if who == 'lead')
        self.assertIn('答「gmv」', first_lead['messages'][1]['content'])


if __name__ == '__main__':
    unittest.main()
