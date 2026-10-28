"""A7 人在回路验收：澄清等待、新口径提议与确认、情景记忆，离线假模型，不发网络请求。

复用 test_investigation 的假模型与合成数据；调查入口直接调 run_investigation，
以便传 memory / clarifications 与不传 metric / period。
"""
import json
import unittest

import httpx

from datalab.agents.investigation import (InvestigationBudget, InvestigationToolset,
                                          run_investigation, snapshot_from_memory)
from datalab.datasets import olist
from datalab.memory.episodes import search_episodes
from datalab.memory.local import InMemoryMemoryRepository
from datalab.memory.service import MemoryService, Scope
from datalab.roles.llm import ChatClient
from datalab.semantic.snapshot import SemanticSnapshot
from test_investigation import (B, P, SNAPSHOT, ScriptModel, finish_report,
                                lead_script_basic, make_folder, sub_script_contribution,
                                tool_call)
from test_model_backend import profile

# Olist 多表签名：MemoryService.propose 据此识别 olist schema 并校验 OLIST_STATUSES。
SCOPE = Scope('demo', 'olist', olist.SIGNATURE)

# 与 test_investigation 的 ReviewTests.good 报告一致：只引用 dispatch 展开结果，能通过代码核对。
GOOD_REPORT = json.dumps({
    'summary': '订单量变化集中在新客（r3-dispatch_subagent）。',
    'root_causes': [{'dimension': 'customer_type', 'value': 'new',
                     'result_ids': ['r3-dispatch_subagent']}],
    'findings': [], 'recommendations': []}, ensure_ascii=False)


def make_memory():
    return MemoryService(InMemoryMemoryRepository()), SCOPE


def run_hitl(question, folder, model, *, memory=None, metric=None, period=None,
             base_period=None, clarifications=None, toolset=None):
    """人在回路入口：直接调 run_investigation，可给 memory 与 clarifications，也可缺指标/期间。"""
    client = ChatClient(profile(), transport=httpx.MockTransport(model))
    return run_investigation(question, folder, SNAPSHOT, client, parallel=True,
                             budget=InvestigationBudget(60, 400_000, 600),
                             metric=metric, period=period, base_period=base_period,
                             dimensions_hint=['customer_type', 'customer_state'],
                             memory=memory, clarifications=clarifications, toolset=toolset)


def ask_step(question, options, call_id='call_ask'):
    return [tool_call('ask_user', {'question': question, 'options': options}, call_id=call_id)]


def finish_basic():
    return [tool_call('finish', {'report': finish_report()})]


def finish_good():
    return [tool_call('finish', {'report': GOOD_REPORT})]


def lead_finish_only(finish=None):
    """主 Agent 一步 finish 的脚本：配合基础子 Agent 脚本收尾。"""
    return [finish or finish_basic()]


def propose_steps(**overrides):
    args = {'name': 'gmv_含取消', 'definition': '含已发货与已取消订单的成交总额',
            'included_statuses': ['delivered', 'canceled'], 'amount_source': 'payments'}
    args.update(overrides)
    return [[tool_call('propose_metric', args)], finish_basic()]


class ClarificationTests(unittest.TestCase):
    def test_ask_user_waits_then_resume_with_answer(self):
        folder = make_folder(self)
        question = '这个指标怎么算？'
        options = ['GMV', '订单量']
        model = ScriptModel([ask_step(question, options)], sub_script_contribution())
        result = run_hitl('最近卖得怎么样？', folder, model)
        # 澄清后暂停：无报告、无分派、无审查。
        self.assertEqual(result.stop_reason, 'waiting_confirmation')
        self.assertIsNone(result.report)
        pending = result.pending
        self.assertEqual(pending['type'], 'clarification')
        self.assertEqual(pending['question'], question)
        self.assertEqual(pending['options'], options)
        self.assertTrue(pending['result_id'])
        roles = [role for role, _ in model.requests]
        self.assertNotIn('sub', roles)
        self.assertNotIn('reviewer', roles)
        # 指标/本期/对比期缺失时首条消息带澄清要求。
        lead_first = next(body for role, body in model.requests if role == 'lead')
        self.assertIn('服务端未能确认', lead_first['messages'][1]['content'])

        # 用户答复后重新发起：答复写进首条消息，调查正常完成。
        resumed = ScriptModel(lead_script_basic(), sub_script_contribution())
        answer = {'question': question, 'answer': 'GMV'}
        second = run_hitl('最近卖得怎么样？', make_folder(self), resumed,
                          metric='order_count', period=P, base_period=B,
                          clarifications=[answer])
        self.assertEqual(second.stop_reason, 'finish')
        self.assertIsNotNone(second.report)
        resumed_first = next(body for role, body in resumed.requests if role == 'lead')
        opening = resumed_first['messages'][1]['content']
        self.assertIn('GMV', opening)
        self.assertIn('用户已答复澄清问题', opening)

    def test_ambiguous_questions_all_wait_for_confirmation(self):
        questions = ['最近卖得怎么样？', '销售额为什么变了？', '哪里出问题了？',
                     '上个月表现如何？', '这个指标怎么算？']
        for question in questions:
            with self.subTest(question=question):
                folder = make_folder(self)
                model = ScriptModel([ask_step(question, ['口径A', '口径B'])],
                                    sub_script_contribution())
                result = run_hitl(question, folder, model)
                self.assertEqual(result.stop_reason, 'waiting_confirmation')
                self.assertIsNone(result.report)
                self.assertEqual(result.pending['type'], 'clarification')
                roles = [role for role, _ in model.requests]
                self.assertNotIn('sub', roles)


class ProposeMetricTests(unittest.TestCase):
    def test_propose_metric_pending_then_confirm_builds_snapshot(self):
        folder = make_folder(self)
        service, scope = make_memory()
        memory = (service, scope)
        toolset = InvestigationToolset(folder, SNAPSHOT, memory=memory)
        self.addCleanup(toolset.close)
        model = ScriptModel(propose_steps(), sub_script_contribution())
        result = run_hitl('销售额为什么变了？', folder, model, memory=memory, toolset=toolset)
        self.assertEqual(result.stop_reason, 'waiting_confirmation')
        self.assertIsNone(result.report)
        pending = result.pending
        self.assertEqual(pending['type'], 'metric')
        self.assertIn('gmv_含取消', pending['question'])
        self.assertEqual(pending['options'], ['确认', '不确认'])
        memory_id = pending['memory_id']
        self.assertTrue(memory_id)
        # pending 口径：不出现在有效记忆检索里，也不能直接建快照。
        entry = next(item for item in service.history(scope) if item.memory_id == memory_id)
        self.assertEqual(entry.status, 'pending')
        self.assertEqual(service.retrieve(scope), [])
        with self.assertRaises(ValueError):
            snapshot_from_memory(entry)
        # 本次调查的语义层快照不被 pending 口径改变。
        self.assertIs(toolset.snapshot, SNAPSHOT)
        # 用户确认后可建快照，状态与金额来源来自提议内容。
        confirmed = service.confirm(scope, memory_id, actor='user', expected_current_id=None)
        snapshot = snapshot_from_memory(confirmed)
        self.assertIsInstance(snapshot, SemanticSnapshot)
        self.assertEqual(snapshot.included_statuses, ('delivered', 'canceled'))
        self.assertEqual(snapshot.amount_source, 'payments')
        self.assertEqual(snapshot.version, f'{memory_id}:v1')

    def test_propose_and_search_without_memory_return_errors(self):
        folder = make_folder(self)
        steps = [
            [tool_call('propose_metric', {'name': 'gmv_含取消', 'definition': '含取消订单的成交总额',
                                          'included_statuses': ['delivered', 'canceled']})],
            [tool_call('search_memory', {'query': '订单量归因'})],
            finish_basic(),
        ]
        model = ScriptModel(steps, sub_script_contribution())
        result = run_hitl('销售额为什么变了？', folder, model, metric='order_count',
                          period=P, base_period=B)
        # 未配置记忆：两个工具都报错，调查不进入等待确认，脚本走完 finish。
        self.assertNotEqual(result.stop_reason, 'waiting_confirmation')
        self.assertEqual(result.stop_reason, 'finish')
        self.assertIsNone(result.pending)
        lead_bodies = [body for role, body in model.requests if role == 'lead']
        self.assertGreaterEqual(len(lead_bodies), 3)
        tool_texts = [json.dumps(message, ensure_ascii=False)
                      for body in lead_bodies[1:] for message in body['messages']
                      if message.get('role') == 'tool']
        self.assertTrue(any('未配置业务记忆，不能提议新口径' in text for text in tool_texts),
                        tool_texts)
        self.assertTrue(any('未配置业务记忆' in text for text in tool_texts), tool_texts)


class EpisodeTests(unittest.TestCase):
    def test_valid_report_recorded_and_shown_in_next_opening(self):
        memory = make_memory()
        service, scope = memory
        question = '2017 年 5 月订单量环比变化，主要来自哪些客群？'
        lead_steps = lead_script_basic()[:2] + [finish_good()]
        model = ScriptModel(lead_steps, sub_script_contribution())
        result = run_hitl(question, make_folder(self), model, memory=memory,
                          metric='order_count', period=P, base_period=B)
        # 通过核对的报告写入情景记忆并可检索。
        self.assertTrue(result.report['valid'], result.report.get('claims_check'))
        self.assertTrue(result.episode_id)
        episodes = search_episodes(service, scope, question)
        self.assertTrue(episodes)
        self.assertEqual(episodes[0]['memory_id'], result.episode_id)
        # 第二次调查：首条消息附最相似的历史归因。
        second_model = ScriptModel(lead_script_basic(), sub_script_contribution())
        second = run_hitl(question, make_folder(self), second_model, memory=memory,
                          metric='order_count', period=P, base_period=B)
        self.assertIsNotNone(second.report)
        lead_first = next(body for role, body in second_model.requests if role == 'lead')
        self.assertIn('相似的历史归因', lead_first['messages'][1]['content'])

    def test_invalid_report_not_recorded(self):
        memory = make_memory()
        service, scope = memory
        # 基础报告取值与引用结果对不上，代码核对判为无效（同 ReviewTests 的 bad 报告）。
        model = ScriptModel(lead_script_basic(), sub_script_contribution())
        result = run_hitl('2017 年 5 月订单量环比变化？', make_folder(self), model,
                          memory=memory, metric='order_count', period=P, base_period=B)
        self.assertFalse(result.report['valid'])
        self.assertIsNone(result.episode_id)
        self.assertEqual(search_episodes(service, scope, '订单量'), [])


if __name__ == '__main__':
    unittest.main()
