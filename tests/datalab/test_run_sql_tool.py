"""A8 run_sql 工具验收：L2 检查单元、注册表路径（白名单/沙箱/核验）与真实容器集成。

离线部分用假沙箱或 patch 掉的 run_query（不启动容器）；集成部分需
DATALAB_DOCKER_TESTS=1 且本机有 datalab-executor:py312-v2 镜像。
"""
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from datalab.agents.claims import ClaimChecker
from datalab.agents.investigation import Investigation
from datalab.agents.toolset import InvestigationToolset
from datalab.verification.sql_result import check_sql_result, reconcile_check
from test_investigation import P, SNAPSHOT, make_folder, tool_call

DOCKER = os.environ.get('DATALAB_DOCKER_TESTS') == '1'
STATUSES_SQL = '(' + ', '.join(f"'{status}'" for status in SNAPSHOT.included_statuses) + ')'
PERIOD_SQL = (f"o.order_purchase_timestamp >= '{P['start']}' "
              f"AND o.order_purchase_timestamp < '{P['end']}'")
# 与 semantic.compiler 的 gmv_payments 同口径：纳入状态 + 期间 + 支付合计，按客户州分组。
GMV_SQL = ("SELECT c.customer_state AS state, COALESCE(SUM(COALESCE(pay.a, 0)), 0) AS amount "
           "FROM orders o JOIN customers c ON c.customer_id = o.customer_id "
           "LEFT JOIN (SELECT order_id, SUM(payment_value_cents) AS a FROM order_payments "
           "GROUP BY order_id) pay ON pay.order_id = o.order_id "
           f"WHERE o.order_status IN {STATUSES_SQL} AND {PERIOD_SQL} "
           "GROUP BY c.customer_state ORDER BY c.customer_state LIMIT 100")
# 同口径但不做状态过滤：合成数据 P 期间含 canceled/unavailable 订单，金额必然不同。
GMV_SQL_NO_STATUS = GMV_SQL.replace(f"o.order_status IN {STATUSES_SQL} AND ", '')


def make_toolset(test, sandbox):
    """合成数据 + 假/真沙箱的任务级工具集；退出时关闭引擎。"""
    folder = make_folder(test)
    toolset = InvestigationToolset(folder, SNAPSHOT, sandbox=sandbox)
    test.addCleanup(toolset.close)
    return toolset


def call_run_sql(toolset, arguments, agent='subagent'):
    return toolset.registry.call(agent, tool_call('run_sql', arguments))


class ExplodingSandbox:
    """占位沙箱：白名单拒绝的 SQL 不允许到达沙箱，一旦到达立即失败。"""

    def execute_inputs(self, *args, **kwargs):
        raise AssertionError('沙箱不应被调用')


def check_passed(check):
    return check['passed'], check['checks']


class CheckSqlResultTests(unittest.TestCase):
    """check_sql_result / reconcile_check 的纯函数单元测试。"""

    @staticmethod
    def output(**overrides):
        base = {'columns': ['a', 'b'], 'rows': [['x', 1], ['y', 2]],
                'permuted_rows': [['y', 2], ['x', 1]],
                'table_counts': {'orders': 3}, 'host_counts': {'orders': 3}}
        base.update(overrides)
        return base

    def test_pass_case(self):
        passed, checks = check_passed(check_sql_result(self.output(), 10))
        self.assertTrue(passed, checks)
        self.assertEqual(set(checks), {'invariants', 'permutation', 'load'})

    def test_duplicate_column_names(self):
        passed, checks = check_passed(check_sql_result(self.output(columns=['a', 'a']), 10))
        self.assertFalse(passed)
        self.assertFalse(checks['invariants']['passed'])
        self.assertIn('列名重复', checks['invariants']['detail'])

    def test_nan_cell(self):
        passed, checks = check_passed(check_sql_result(self.output(rows=[['x', 'NaN']]), 10))
        self.assertFalse(passed)
        self.assertIn('含非有限数值', checks['invariants']['detail'])

    def test_permuted_rows_differing(self):
        passed, checks = check_passed(check_sql_result(
            self.output(permuted_rows=[['y', 2], ['z', 9]]), 10))
        self.assertFalse(passed)
        self.assertFalse(checks['permutation']['passed'])

    def test_float_last_bits_tolerance(self):
        # 置换后浮点求和顺序不同，末位不同的浮点仍判一致（按 10 位有效数字比较）。
        rows = [['s', 0.1 + 0.2 + 0.3]]
        permuted = [['s', 0.3 + 0.2 + 0.1]]
        passed, checks = check_passed(check_sql_result(
            self.output(columns=['g', 'v'], rows=rows, permuted_rows=permuted), 10))
        self.assertTrue(passed, checks)

    def test_load_count_mismatch(self):
        passed, checks = check_passed(check_sql_result(
            self.output(table_counts={'orders': 2}), 10))
        self.assertFalse(passed)
        self.assertFalse(checks['load']['passed'])


class ReconcileCheckTests(unittest.TestCase):
    """reconcile_check：列之和 vs 语义层加法型指标值，允许分↔元书写差异。"""

    @staticmethod
    def output(rows, columns=('state', 'amount')):
        return {'columns': list(columns), 'rows': rows}

    def test_pass_in_cents(self):
        check = reconcile_check(self.output([['SP', 100], ['RJ', 200]]), 10, 'amount', 300, 'cents')
        self.assertTrue(check['passed'], check)
        self.assertEqual(check['values']['column_sum'], '300')

    def test_pass_in_yuan_times_100(self):
        # 结果列以元书写：sum*100 与语义层整数值（分）一致仍判通过。
        check = reconcile_check(self.output([['SP', 1.5], ['RJ', 1.5]]), 10, 'amount', 300, 'cents')
        self.assertTrue(check['passed'], check)

    def test_fail_on_mismatch(self):
        check = reconcile_check(self.output([['SP', 100]]), 10, 'amount', 300, 'cents')
        self.assertFalse(check['passed'])
        self.assertIn('列之和与语义层值不一致', check['detail'])

    def test_fail_when_rows_reach_limit(self):
        check = reconcile_check(self.output([['SP', 100], ['RJ', 200]]), 2, 'amount', 300, 'cents')
        self.assertFalse(check['passed'])
        self.assertIn('LIMIT', check['detail'][0])

    def test_fail_on_missing_column(self):
        check = reconcile_check(self.output([['SP', 100]]), 10, 'missing', 100, 'cents')
        self.assertFalse(check['passed'])
        self.assertIn('missing', check['detail'][0])


class RunSqlRegistryTests(unittest.TestCase):
    """经 registry 的 subagent 调用路径：授权、白名单拒绝、沙箱结果与 L2 核验。"""

    def test_no_sandbox_configured(self):
        toolset = make_toolset(self, None)
        observation = call_run_sql(toolset, {'sql': 'SELECT order_id FROM orders LIMIT 1'})
        self.assertFalse(observation.ok)
        self.assertIn('未配置执行沙箱', observation.error)

    def test_guard_rejections_never_touch_sandbox(self):
        toolset = make_toolset(self, ExplodingSandbox())
        cases = [
            ('DELETE FROM orders', 'not_select'),
            # information_schema.tables 带库名前缀，先命中 qualified_table；不带前缀的元数据表
            # 才走到 unknown_table（两者都被拒绝）。
            ('SELECT * FROM information_schema.tables LIMIT 1', 'qualified_table'),
            ('SELECT * FROM duckdb_tables LIMIT 1', 'unknown_table'),
            ('SELECT order_id FROM orders', 'missing_limit'),
            ('SELECT 1 FROM orders LIMIT 1; DROP TABLE orders', 'multiple_statements'),
        ]
        for sql, code in cases:
            with self.subTest(code=code):
                observation = call_run_sql(toolset, {'sql': sql})
                self.assertFalse(observation.ok, observation.result_id)
                self.assertIn(code, observation.error)
                self.assertIn('SQL 被拒绝', observation.error)
        # 拒绝发生在 Schema 校验之后、handler 执行之前：ResultStore 不应有条目。
        self.assertEqual(len(toolset.registry.store), 0)

    def test_unknown_table_hint_lists_whitelist(self):
        toolset = make_toolset(self, ExplodingSandbox())
        observation = call_run_sql(toolset, {'sql': 'SELECT * FROM duckdb_tables LIMIT 1'})
        self.assertIn('unknown_table', observation.error)
        # 观察 error 有 512 字符上限：未知表只列表名，八张表都要完整保留。
        self.assertIn('可用表：', observation.error)
        for name in ('orders', 'customers', 'order_items', 'order_payments', 'sellers', 'category_translation'):
            self.assertIn(name, observation.error)

    def test_unknown_column_hint_lists_referenced_table_columns(self):
        toolset = make_toolset(self, ExplodingSandbox())
        observation = call_run_sql(toolset, {'sql': 'SELECT customer_city FROM customers LIMIT 5'})
        self.assertIn('unknown_column', observation.error)
        self.assertIn('customers(customer_id,customer_unique_id,customer_state)', observation.error)
        self.assertNotIn('order_items(', observation.error)

    @staticmethod
    def good_output(folder, rows=None):
        return {'columns': ['order_id'], 'rows': rows if rows is not None else [['o1'], ['o2']],
                'permuted_rows': [['o2'], ['o1']],
                'table_counts': {'orders': 60}, 'host_counts': {'orders': 60}}

    def test_accepted_sql_stores_l2_and_verifies(self):
        toolset = make_toolset(self, object())  # run_query 被 patch，沙箱只需非 None
        with patch('datalab.execution.sql_sandbox.run_query',
                   side_effect=lambda sandbox, folder, sql, tables, limit, **kw: self.good_output(folder)):
            observation = call_run_sql(toolset, {'sql': 'SELECT order_id FROM orders LIMIT 2'})
        self.assertTrue(observation.ok, observation.error)
        result_id = observation.result_id
        entry = toolset.registry.store.get(result_id)
        self.assertEqual(entry['level'], 'L2')
        self.assertEqual(entry['tool'], 'run_sql')
        self.assertTrue(entry['result']['l2']['passed'], entry['result']['l2'])
        self.assertEqual(entry['result']['tables'], {'orders': ['order_id']})
        verified = toolset.verify_results([result_id])[result_id]
        self.assertEqual(verified['level'], 'L2')
        self.assertTrue(verified['passed'], verified)
        self.assertFalse(verified['skipped'])

    def test_permuted_rows_differing_fails_verification(self):
        toolset = make_toolset(self, object())
        output = self.good_output(None)
        output['permuted_rows'] = [['o2'], ['o3']]  # 与 rows 作为多重集合不同
        with patch('datalab.execution.sql_sandbox.run_query', return_value=output):
            observation = call_run_sql(toolset, {'sql': 'SELECT order_id FROM orders LIMIT 2'})
        self.assertTrue(observation.ok, observation.error)
        verified = toolset.verify_results([observation.result_id])[observation.result_id]
        self.assertFalse(verified['passed'])
        self.assertIn('permutation', [item['path'] for item in verified['differences']])

    def test_output_error_becomes_tool_error(self):
        toolset = make_toolset(self, object())
        with patch('datalab.execution.sql_sandbox.run_query',
                   return_value={'error': 'Binder Error: x'}):
            observation = call_run_sql(toolset, {'sql': 'SELECT order_id FROM orders LIMIT 2'})
        self.assertFalse(observation.ok)
        self.assertIn('SQL 执行出错', observation.error)

    def test_reconcile_ratio_metric_rejected(self):
        toolset = make_toolset(self, ExplodingSandbox())
        observation = call_run_sql(toolset, {
            'sql': 'SELECT order_id FROM orders LIMIT 1',
            'reconcile': {'metric': 'aov', 'period': P, 'column': 'amount'}})
        self.assertFalse(observation.ok)
        self.assertIn('reconcile 只支持加法型指标', observation.error)

    def test_lead_agent_denied(self):
        toolset = make_toolset(self, ExplodingSandbox())
        observation = call_run_sql(toolset, {'sql': 'SELECT order_id FROM orders LIMIT 1'},
                                   agent='lead')
        self.assertFalse(observation.ok)
        self.assertIn('无权调用', observation.error)


class GradeTests(unittest.TestCase):
    """investigation._grade：L2 引用全部通过 → L2；混合 L1+L2 → L2；未通过 → L3。"""

    @staticmethod
    def grade(verification):
        investigation = Investigation.__new__(Investigation)  # 只测 _grade 的纯映射，不跑编排
        report = {'summary': '结论', 'findings': [{'text': '见引用', 'result_ids': list(verification)}]}
        return investigation._grade(report, verification, ClaimChecker(None))['findings'][0]['verification']

    def test_l2_only_passing_is_l2(self):
        self.assertEqual(self.grade({'r1-run_sql': {'passed': True, 'level': 'L2'}}), 'L2')

    def test_mixed_l1_l2_passing_is_l2(self):
        verification = {'r1-query_metric': {'passed': True},
                        'r2-run_sql': {'passed': True, 'level': 'L2'}}
        self.assertEqual(self.grade(verification), 'L2')

    def test_failing_l2_is_l3(self):
        self.assertEqual(self.grade({'r1-run_sql': {'passed': False, 'level': 'L2'}}), 'L3')


@unittest.skipUnless(DOCKER, '需显式启用真实 Docker 集成')
class DockerIntegrationTests(unittest.TestCase):
    """真实容器 + 合成数据：正确 SQL 的 L2（含 reconcile）通过，缺状态过滤的 SQL 不通过。"""

    def setUp(self):
        from datalab.execution.runner import DockerRunner

        self.started = time.perf_counter()
        self.toolset = make_toolset(self, None)
        scratch = Path(self.toolset.folder).parent
        self.runner = DockerRunner(None, scratch / 'sandbox-runs')
        self.toolset.sandbox = self.runner

    def tearDown(self):
        print(f'\n[docker] {self._testMethodName}: {time.perf_counter() - self.started:.1f}s')

    def run_sql(self, sql, reconcile=None):
        arguments = {'sql': sql}
        if reconcile:
            arguments['reconcile'] = reconcile
        observation = call_run_sql(self.toolset, arguments)
        self.assertTrue(observation.ok, observation.error)
        result = json.loads(observation.content)['result']
        return result, self.toolset.verify_results([observation.result_id])[observation.result_id]

    def test_gmv_sql_matching_semantic_layer_passes_reconcile(self):
        result, verified = self.run_sql(
            GMV_SQL, reconcile={'metric': 'gmv', 'period': P, 'column': 'amount'})
        self.assertEqual(result['tables'], {'customers': ['customer_id', 'customer_state'],
                                            'order_payments': ['order_id', 'payment_value_cents'],
                                            'orders': ['customer_id', 'order_id',
                                                       'order_purchase_timestamp', 'order_status']})
        states = sorted(row[0] for row in result['rows'])
        self.assertEqual(states, ['MG', 'RJ', 'SP'])
        self.assertTrue(result['l2']['passed'], result['l2'])
        self.assertTrue(result['l2']['checks']['reconcile']['passed'], result['l2'])
        self.assertTrue(verified['passed'], verified)

    def test_gmv_sql_without_status_filter_fails_reconcile(self):
        result, verified = self.run_sql(
            GMV_SQL_NO_STATUS, reconcile={'metric': 'gmv', 'period': P, 'column': 'amount'})
        reconcile = result['l2']['checks']['reconcile']
        self.assertFalse(reconcile['passed'], reconcile)
        # 与语义层值不一致即失败；金额来源行数未到 LIMIT，不是截断导致的失败。
        self.assertIn('列之和与语义层值不一致', reconcile['detail'])
        self.assertFalse(verified['passed'])

    def test_limit_without_order_by_permutation_observation(self):
        # 无 ORDER BY 的 LIMIT：行序置换后截取到的行可能不同，此时判不通过是预期行为；
        # 只记录实际结论不断言（60 行取 3 行，两种结果都可能出现）。
        result, _ = self.run_sql('SELECT order_id FROM orders LIMIT 3')
        permutation = result['l2']['checks']['permutation']['passed']
        print(f'\n[docker] LIMIT 无 ORDER BY 的置换检查 passed={permutation}（两种结果都合理）')


if __name__ == '__main__':
    unittest.main()
