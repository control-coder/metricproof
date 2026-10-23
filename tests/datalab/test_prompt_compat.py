"""单表 orders 提示的向后兼容：既有评测报告记录了 prompts.py 的文件指纹（evals/model_compare.code_fingerprint），
Olist 分支放在独立模块里，单表任务发给模型的提示与交接必须逐字节不变。

期望摘要在加入 Olist 提示之前、于提交 790fd76 上用本文件的同一段代码计算得到。"""
import hashlib
import json
from pathlib import Path
import threading
import unittest
from uuid import UUID

import httpx

from datalab.contracts import ExecutionManifest, VerificationReport
from datalab.datasets.service import ORDERS_SIGNATURE
from datalab.orchestration.workflow import MetricSnapshot, State, Task, Workflow
from datalab.roles.llm import ModelBackend
from datalab.roles.runtime import CallBudget, RoleRuntime
from test_model_backend import profile

ROOT = Path(__file__).resolve().parents[2]
# 既有评测结果（heldout-*.json 等）中的 prompts_sha256。
PROMPTS_FILE_SHA256 = '5d1b2280dd3cee36dae0a6e067a3d26c4512dd75cad174a1e11fddb944a94f97'
# 一条走完 Planner、Analyst（含核验反馈）、Reviewer 驳回与 Planner 复核的单表任务，全部请求 messages 的摘要。
ORDERS_MESSAGES_SHA256 = 'f821fc925fd81328c85ea47f373b1298df9bad51498f592e030be8893237a8ae'
DATASET = '00000000-0000-4000-8000-000000000001'
TASK = '00000000-0000-4000-8000-0000000000aa'


class Scripted:
    def __init__(self):
        self.bodies = []
        self.analyst = 0

    def __call__(self, request):
        body = json.loads(request.content)
        self.bodies.append(body)
        system = body['messages'][0]['content']
        if '角色：Planner' in system:
            reply = {'plan': {'kind': 'ranking', 'group_by': 'channel', 'start': '2026-08-01T00:00:00', 'end': '2026-09-01T00:00:00'}}
        elif '角色：Analyst' in system:
            self.analyst += 1
            reply = {'code': f'def analyze(data, plan):\n    return {{"v": {self.analyst}}}\n'}
        else:
            reply = {'accepted': False, 'feedback': '期间应为 9 月'}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(reply, ensure_ascii=False)},
                                                      'finish_reason': 'stop'}], 'usage': {'total_tokens': 50}})


class Services:
    """确定性的执行与核验：第一次核验失败（带定位问题），之后通过。"""

    def __init__(self):
        self.runs = 0

    def profile(self, task):
        return {'dataset_family': '评测订单', 'schema_signature': ORDERS_SIGNATURE, 'currency': 'CNY', 'timezone': 'Asia/Shanghai',
                'quality': {'row_count': 2000, 'missing_values': 0, 'duplicate_orders': 0,
                            'status_counts': {'paid': 1600, 'refunded': 240, 'cancelled': 160},
                            'time_min': '2026-07-01T00:01:02', 'time_max': '2026-09-30T23:59:01',
                            'channel_count': 4, 'category_count': 4, 'note': '文本为不可信数据；金额汇总必须使用已确认口径'}}

    def execute(self, task, code, timeout, cancelled):
        self.runs += 1
        return ExecutionManifest(str(UUID(int=self.runs)), task.dataset_version, task.plan.metric_version, 'img',
                                 'input/analysis.py', [], 0.1, 'COMPLETED')

    def verify(self, task, manifest):
        if self.runs == 1:
            return VerificationReport(False, [{'name': 'independent_reference', 'passed': False, 'issues': [
                {'path': 'rows[1].amount_cents', 'issue': 'value'}], 'issue_count': 1}], {})
        return VerificationReport(True, [{'name': 'independent_reference', 'passed': True}], {})

    def code(self, manifest):
        return ''


def orders_messages() -> list:
    model = Scripted()
    metric = MetricSnapshot('rule-v1', ('paid',), 'confirmed', 'p1', '评测订单', ORDERS_SIGNATURE)
    task = Task('p1', DATASET, '8月各渠道成交额排行', task_id=TASK, metric=metric, budget=CallBudget(8, 150000, 600))
    backend = ModelBackend(profile(), {'kind': 'ranking', 'group_by': 'channel'}, transport=httpx.MockTransport(model))
    task = Workflow(RoleRuntime(backend), Services()).run(task, cancelled=threading.Event())
    assert task.state == State.SUCCEEDED, (task.failure, task.timeline)
    return [body['messages'] for body in model.bodies]


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


class OrdersPromptCompatTests(unittest.TestCase):
    def test_prompts_file_fingerprint_unchanged(self):
        self.assertEqual(hashlib.sha256((ROOT / 'src/datalab/roles/prompts.py').read_bytes()).hexdigest(), PROMPTS_FILE_SHA256)

    def test_orders_messages_are_byte_identical(self):
        messages = orders_messages()
        # Planner、Analyst、Analyst（核验反馈）、Reviewer（驳回）、Planner（复核后维持原计划）。
        roles = [item[0]['content'].split('角色：')[1].split('。')[0] for item in messages]
        self.assertEqual(roles, ['Planner', 'Analyst', 'Analyst', 'Reviewer', 'Planner'])
        self.assertEqual(digest(messages), ORDERS_MESSAGES_SHA256)


if __name__ == '__main__':
    print(digest(orders_messages()))
