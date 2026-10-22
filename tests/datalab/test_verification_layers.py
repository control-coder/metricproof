"""开放分析核验层的离线测试：结果不变量、第二份 Python 参考与 SQL 参考的性质测试、蜕变关系与运行时接线。

蜕变关系的离线用例把脚本改指向临时目录后用本机解释器运行，只用于测试固定代码与测试自带的错误代码；
真实沙箱上的同一检查见 test_execution.py 的 Docker 集成用例。
"""
import copy
import csv
import io
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from datalab.contracts import AnalysisPlan, ExecutionManifest, VerificationReport
from datalab.datasets.service import DatasetStore
from datalab.execution.analysis_frame import assemble, model_part, rebind as frame_rebind
from datalab.execution.codegen import fixed_code
from datalab.execution.fixed_analysis import analyze
from datalab.orchestration.workflow import MetricSnapshot, State, Task, Workflow, role_plan
from datalab.roles.runtime import ModelReply, Role, RoleRuntime
from datalab.verification import invariants, metamorphic
from datalab.verification.python_reference import agreement, python_reference, ratio
from datalab.verification.reference import _fraction, reference_result
from datalab.verification.suite import run_checks

ROOT = Path(__file__).resolve().parents[2]
CHANNELS = ['广告', '直播', '=SUM(A1)', 'a b', 'Z', 'a']
CATEGORIES = ['服饰', '食品', 'x']
STAMPS = ['2026-07-31T00:00:00', '2026-07-31T23:59:59', '2026-08-01T00:00:00', '2026-08-01T12:00:00',
          '2026-08-01T23:59:59', '2026-08-02T00:00:00', '2026-08-02T08:30:00', '2026-08-03T00:00:00']
BOUNDS = [None, '2026-07-31T00:00:00', '2026-08-01T00:00:00', '2026-08-01T12:00:00', '2026-08-02T00:00:00',
          '2026-08-03T00:00:00', '2026-08-04T00:00:00']

# 测试用的框架代码：与提示契约一致的正确实现。
GOOD = '''
def analyze(data, plan):
    if plan["kind"] == "quality":
        return {"kind": "quality", "row_count": len(data),
                "duplicate_orders": len(data) - len({row["order_id"] for row in data}),
                "missing_values": sum(row[key] == "" for row in data for key in ("order_id", "payment_time", "status", "channel", "category")),
                "status_counts": {name: sum(row["status"] == name for row in data) for name in ("paid", "refunded", "cancelled")}}
    rows = selected(data, plan, plan["start"], plan["end"])
    total = {"order_count": len(rows), "amount_cents": sum(int(row["amount_cents"]) for row in rows)}
    groups = defaultdict(lambda: [0, 0])
    if plan["group_by"]:
        for row in rows:
            entry = groups[group_key(row, plan["group_by"])]
            entry[0] += 1
            entry[1] += int(row["amount_cents"])
    out = [{"group": key, "order_count": value[0], "amount_cents": value[1]} for key, value in sorted(groups.items())]
    if plan["kind"] == "ranking":
        out.sort(key=lambda item: (-item["amount_cents"], item["group"]))
    if plan["kind"] == "share":
        for item in out:
            item["share"] = ratio(item["amount_cents"], total["amount_cents"])
    result = {"kind": plan["kind"], "total": total, "rows": out}
    if plan["kind"] == "comparison":
        previous = selected(data, plan, plan["previous_start"], plan["previous_end"])
        amount = sum(int(row["amount_cents"]) for row in previous)
        delta = total["amount_cents"] - amount
        result["comparison"] = {"previous_order_count": len(previous), "previous_amount_cents": amount,
                                "delta_cents": delta, "growth": ratio(delta, amount)}
    return result
'''


def orders_bytes(rng, count):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow(['order_id', 'payment_time', 'amount', 'status', 'channel', 'category'])
    for index in range(count):
        stamp = rng.choice(STAMPS) if rng.random() < 0.5 else \
            f'2026-{rng.choice(["07-31", "08-01", "08-02", "08-03"])}T{rng.randrange(24):02d}:{rng.randrange(60):02d}:{rng.randrange(60):02d}'
        cents = rng.choice([0, 1, 99, rng.randrange(100, 500000), 999999999999])
        writer.writerow([f'P{index:04d}', stamp, f'{cents // 100}.{cents % 100:02d}',
                         rng.choice(['paid', 'paid', 'refunded', 'cancelled']), rng.choice(CHANNELS), rng.choice(CATEGORIES)])
    return out.getvalue().encode('utf-8')


def period(rng):
    while True:
        start, end = rng.choice(BOUNDS), rng.choice(BOUNDS)
        if not (start and end and start >= end):
            return start, end


def random_plan(rng, version):
    kind = rng.choice(['summary', 'trend', 'ranking', 'share', 'comparison', 'quality'])
    group = {'trend': 'payment_date', 'ranking': rng.choice(['channel', 'category']),
             'share': rng.choice(['channel', 'category', 'payment_date'])}.get(kind, rng.choice([None, None, 'channel', 'payment_date']))
    statuses = tuple(name for name in ('paid', 'refunded', 'cancelled') if rng.random() < 0.6) or ('paid',)
    fields = {}
    if kind == 'comparison':
        while not all(fields.values()) or len(fields) < 4:
            (start, end), (previous_start, previous_end) = period(rng), period(rng)
            fields = {'start': start, 'end': end, 'previous_start': previous_start, 'previous_end': previous_end}
    elif kind != 'quality':
        fields['start'], fields['end'] = period(rng)
    return AnalysisPlan(version, 'prop-v1', kind=kind, group_by=None if kind == 'quality' else group,
                        included_statuses=statuses, **fields)


class Fixture(unittest.TestCase):
    def setUp(self):
        scratch = (ROOT / '.tmp').resolve()
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch, prefix='layers-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = DatasetStore(self.root / 'datasets')

    def dataset(self, seed, count=60):
        version = self.store.import_csv(orders_bytes(random.Random(seed), count), project_id='demo',
                                        dataset_family='orders', currency='CNY', timezone='Asia/Shanghai')
        return version.dataset_version, self.store.directory(version.dataset_version) / 'orders.csv'


class ReferenceAgreementTests(Fixture):
    def test_python_reference_equals_sql_reference_on_random_cases(self):
        # 40 份种子数据 × 6 个随机计划 = 240 组；同时确认两份参考都满足全部不变量，固定分析也与之一致。
        combos = 0
        for seed in range(40):
            rng = random.Random(1000 + seed)
            version, path = self.dataset(seed, rng.choice([0, 1, 5, 40, 120]) or 1)
            for _ in range(6):
                plan = random_plan(rng, version)
                with self.subTest(seed=seed, plan=plan.payload()):
                    sql = reference_result(path, plan)
                    self.assertEqual(python_reference(path, plan), sql)
                    self.assertEqual(invariants.violations(plan, sql), [])
                    self.assertEqual(json.loads(json.dumps(analyze(path, plan.payload()))), sql)
                    self.assertTrue(agreement(path, plan, sql)['passed'])
                combos += 1
        self.assertGreaterEqual(combos, 200)

    def test_integer_ratio_matches_decimal_formatting(self):
        rng = random.Random(7)
        cases = [(0, 5), (-1, 10 ** 9), (1, 3), (2, 3), (1, 2000000), (3, 2000000), (-5, 2), (10 ** 14, 7)]
        cases += [(rng.randrange(-10 ** 12, 10 ** 12), rng.randrange(1, 10 ** 12)) for _ in range(3000)]
        for numerator, denominator in cases:
            self.assertEqual(ratio(numerator, denominator), _fraction(numerator, denominator), (numerator, denominator))
        self.assertIsNone(ratio(5, 0))

    def test_disagreement_fails_closed_without_values(self):
        version, path = self.dataset(3)
        plan = AnalysisPlan(version, 'prop-v1', kind='share', group_by='channel')
        sql = reference_result(path, plan)
        broken = copy.deepcopy(sql)
        broken['total']['amount_cents'] += 1
        with patch('datalab.verification.python_reference.python_reference', return_value=broken):
            item = agreement(path, plan, sql)
        self.assertFalse(item['passed'])
        self.assertEqual(item['issues'], [{'path': 'total.amount_cents', 'issue': 'disagreement'}])
        self.assertNotIn(str(sql['total']['amount_cents']), json.dumps(item))


class InvariantTests(Fixture):
    def setUp(self):
        super().setUp()
        self.version, self.path = self.dataset(11, 200)

    def result(self, **fields):
        plan = AnalysisPlan(self.version, 'prop-v1', **fields)
        return plan, json.loads(json.dumps(analyze(self.path, plan.payload())))

    def issues(self, plan, result):
        return {(item.get('path'), item['issue']) for item in invariants.violations(plan, result)}

    def test_correct_results_pass_for_every_kind(self):
        cases = [{'kind': 'summary'}, {'kind': 'summary', 'group_by': 'category', 'start': '2026-08-01T00:00:00'},
                 {'kind': 'trend', 'group_by': 'payment_date', 'start': '2026-08-01T00:00:00', 'end': '2026-08-03T00:00:00'},
                 {'kind': 'ranking', 'group_by': 'channel'}, {'kind': 'share', 'group_by': 'payment_date'},
                 {'kind': 'share', 'group_by': 'channel', 'start': '2026-09-01T00:00:00'},
                 {'kind': 'comparison', 'start': '2026-08-02T00:00:00', 'end': '2026-08-04T00:00:00',
                  'previous_start': '2026-07-01T00:00:00', 'previous_end': '2026-07-02T00:00:00'},
                 {'kind': 'quality'}]
        for fields in cases:
            with self.subTest(fields=fields):
                plan, result = self.result(**fields)
                self.assertTrue(invariants.check(plan, result)['passed'], invariants.violations(plan, result))

    def test_targeted_wrong_results_fail(self):
        def mutate(fields, change, expected):
            plan, result = self.result(**fields)
            change(result)
            with self.subTest(expected=expected):
                self.assertIn(expected, self.issues(plan, result))

        ranking = {'kind': 'ranking', 'group_by': 'channel'}
        mutate(ranking, lambda r: r['total'].update(amount_cents=r['total']['amount_cents'] + 1), ('total', 'sum_mismatch'))
        mutate(ranking, lambda r: r['rows'].reverse(), ('rows[1]', 'unsorted'))
        mutate(ranking, lambda r: r['rows'].append(dict(r['rows'][0])), ('rows[6].group', 'duplicate_group'))
        mutate(ranking, lambda r: r['rows'][0].update(order_count=True), ('rows[0].order_count', 'type'))
        mutate(ranking, lambda r: r['rows'][0].update(amount_cents=-5), ('rows[0].amount_cents', 'negative'))
        mutate(ranking, lambda r: r['rows'].append({'group': '~', 'order_count': 0, 'amount_cents': 0}),
               ('rows[6].order_count', 'empty_group'))
        mutate(ranking, lambda r: r.update(kind='summary'), ('kind', 'kind_mismatch'))
        mutate({'kind': 'summary'}, lambda r: r['rows'].append({'group': 'a', 'order_count': 1, 'amount_cents': 1}),
               ('rows', 'rows_not_empty'))
        mutate({'kind': 'summary'}, lambda r: r['total'].update(order_count=0), ('total.amount_cents', 'zero_count_amount'))
        trend = {'kind': 'trend', 'group_by': 'payment_date', 'start': '2026-08-01T00:00:00', 'end': '2026-08-03T00:00:00'}
        mutate(trend, lambda r: r['rows'][-1].update(group='2026-08-03'), ('rows[1].group', 'group_out_of_range'))
        mutate(trend, lambda r: r['rows'][0].update(group='2026-08'), ('rows[0].group', 'invalid_group'))
        share = {'kind': 'share', 'group_by': 'channel'}
        mutate(share, lambda r: r['rows'][0].update(share='0.000001'), ('rows[0].share', 'ratio_mismatch'))
        mutate(share, lambda r: r['rows'][0].update(share=0.5), ('rows[0].share', 'type'))
        mutate(share, lambda r: [row.update(share='0.500000') for row in r['rows']], ('rows', 'share_sum'))
        comparison = {'kind': 'comparison', 'start': '2026-08-02T00:00:00', 'end': '2026-08-04T00:00:00',
                      'previous_start': '2026-07-31T00:00:00', 'previous_end': '2026-08-02T00:00:00'}
        mutate(comparison, lambda r: r['comparison'].update(delta_cents=-r['comparison']['delta_cents'] or 1),
               ('comparison.delta_cents', 'delta_mismatch'))
        mutate(comparison, lambda r: r['comparison'].update(growth='9.000000'), ('comparison.growth', 'ratio_mismatch'))
        mutate(comparison, lambda r: r['comparison'].update(growth=None), ('comparison.growth', 'type'))
        mutate(comparison, lambda r: r.pop('comparison'), ('comparison', 'missing'))
        quality = {'kind': 'quality'}
        mutate(quality, lambda r: r['status_counts'].update(paid=r['status_counts']['paid'] + 1), ('status_counts', 'sum_mismatch'))
        mutate(quality, lambda r: r['status_counts'].update(gift=0), ('status_counts.gift', 'unexpected'))
        mutate(quality, lambda r: r.update(duplicate_orders=r['row_count']), ('duplicate_orders', 'bound'))

    def test_issue_vocabulary_is_closed_and_valueless(self):
        plan, result = self.result(kind='share', group_by='channel')
        for row in result['rows']:
            row['share'] = '0.123456'
        result['total']['amount_cents'] = 777777777
        found = invariants.violations(plan, result)
        self.assertTrue(found)
        for item in found:
            self.assertLessEqual(set(item), {'path', 'issue', 'expected_type'})
            self.assertIn(item['issue'], invariants.ISSUES)
        self.assertNotIn('777777777', json.dumps(found))

    def test_malformed_results_do_not_raise(self):
        plan = AnalysisPlan(self.version, 'prop-v1', kind='comparison', start='2026-08-02T00:00:00', end='2026-08-04T00:00:00',
                            previous_start='2026-07-31T00:00:00', previous_end='2026-08-02T00:00:00', group_by='channel')
        for value in ([], {'kind': 'comparison', 'total': [], 'rows': {}, 'comparison': 3},
                      {'kind': 'comparison', 'total': {'order_count': 'x'}, 'rows': [1, {'group': 5}], 'comparison': {}}):
            self.assertFalse(invariants.check(plan, value)['passed'])


def local_execute(folder: Path):
    """测试用执行器：把容器路径改到临时目录后在本机运行；只运行测试自带或服务端固定的代码。"""

    def execute(script, data, timeout):
        run = folder / uuid4().hex
        (run / 'output').mkdir(parents=True)
        (run / 'orders.csv').write_bytes(data)
        text = script.replace('/input/orders.csv', (run / 'orders.csv').as_posix()).replace('"/output"', repr((run / 'output').as_posix())) \
            .replace('/output/', (run / 'output').as_posix() + '/')
        (run / 'run.py').write_text(text, encoding='utf-8')
        done = subprocess.run([sys.executable, '-I', str(run / 'run.py')], capture_output=True, timeout=timeout)
        if done.returncode:
            return None
        return json.loads((run / 'output/result.json').read_text(encoding='utf-8'))

    return execute


class MetamorphicTests(Fixture):
    def setUp(self):
        super().setUp()
        self.version, self.path = self.dataset(21, 150)

    def run_relations(self, plan, code, *, framework=True, remaining=None):
        script = assemble(role_plan(plan), code) if framework else fixed_code(plan)
        execute = local_execute(self.root / 'runs')
        baseline = execute(script, self.path.read_bytes(), 30)
        sandbox = metamorphic.Sandbox(script, execute, remaining or (lambda: 120.0))
        return metamorphic.check(plan, self.path, baseline, sandbox)

    def statuses(self, item):
        return {entry['relation']: entry['status'] for entry in item['relations']}

    def plans(self):
        return [AnalysisPlan(self.version, 'm-v1', kind='summary', start='2026-08-01T00:00:00', end='2026-08-03T00:00:00'),
                AnalysisPlan(self.version, 'm-v1', kind='trend', group_by='payment_date'),
                AnalysisPlan(self.version, 'm-v1', kind='ranking', group_by='channel', included_statuses=('paid', 'refunded'),
                             end='2026-08-02T00:00:00'),
                AnalysisPlan(self.version, 'm-v1', kind='share', group_by='category', start='2026-08-01T00:00:00'),
                AnalysisPlan(self.version, 'm-v1', kind='comparison', start='2026-08-02T00:00:00', end='2026-08-03T00:00:00',
                             previous_start='2026-08-01T00:00:00', previous_end='2026-08-02T00:00:00'),
                AnalysisPlan(self.version, 'm-v1', kind='quality')]

    def test_correct_framework_and_fixed_code_pass(self):
        expected = {'summary': 'passed', 'trend': 'passed', 'ranking': 'passed', 'share': 'not_applicable',
                    'comparison': 'not_applicable', 'quality': 'not_applicable'}
        for plan in self.plans():
            for framework in (True, False):
                with self.subTest(kind=plan.kind, framework=framework):
                    item = self.run_relations(plan, GOOD, framework=framework)
                    self.assertTrue(item['passed'], item)
                    statuses = self.statuses(item)
                    self.assertEqual(statuses['permutation'], 'passed')
                    self.assertEqual(statuses['irrelevant_rows'], 'not_applicable' if plan.kind == 'quality' else 'passed')
                    self.assertEqual(statuses['window_split'], expected[plan.kind])

    def test_inclusive_end_is_caught_by_irrelevant_rows(self):
        code = GOOD.replace('rows = selected(data, plan, plan["start"], plan["end"])',
                            'rows = [row for row in data if row["status"] in plan["included_statuses"] and '
                            '(plan["start"] is None or row["payment_time"] >= plan["start"]) and '
                            '(plan["end"] is None or row["payment_time"] <= plan["end"])]')
        self.assertNotEqual(code, GOOD)
        item = self.run_relations(self.plans()[0], code)
        self.assertFalse(item['passed'])
        self.assertIn({'relation': 'irrelevant_rows', 'issue': 'result_changed'}, item['issues'])
        for issue in item['issues']:
            self.assertEqual(set(issue), {'relation', 'issue'})

    def test_order_dependent_code_is_caught_by_permutation(self):
        code = GOOD.replace('for key, value in sorted(groups.items())]', 'for key, value in groups.items()]')
        self.assertNotEqual(code, GOOD)
        item = self.run_relations(AnalysisPlan(self.version, 'm-v1', kind='summary', group_by='channel'), code)
        self.assertIn({'relation': 'permutation', 'issue': 'result_changed'}, item['issues'])

    def test_non_additive_code_is_caught_by_window_split(self):
        code = GOOD.replace('total = {"order_count": len(rows),', 'total = {"order_count": len(rows) + 1,')
        self.assertNotEqual(code, GOOD)
        item = self.run_relations(self.plans()[0], code)
        self.assertIn({'relation': 'window_split', 'issue': 'not_additive'}, item['issues'])

    def test_crash_on_transformed_input_is_reported(self):
        code = GOOD.replace('    rows = selected(', '    assert len(data) == %d\n    rows = selected(' % len(metamorphic.read_orders(self.path)))
        item = self.run_relations(self.plans()[0], code)
        self.assertIn({'relation': 'irrelevant_rows', 'issue': 'execution_failed'}, item['issues'])

    def test_no_time_left_skips_every_relation(self):
        item = self.run_relations(self.plans()[0], GOOD, remaining=lambda: 1.0)
        self.assertTrue(item['passed'])
        self.assertEqual(set(self.statuses(item).values()), {'skipped'})

    def test_irrelevant_rows_avoid_both_comparison_periods(self):
        plan = self.plans()[4]
        rows = metamorphic.read_orders(self.path)
        extra = metamorphic.irrelevant_rows(rows, plan)[len(rows):]
        stamps = {row['payment_time'] for row in extra if row['status'] in plan.included_statuses}
        # 本期终点 08-03 与上期起点前一秒在两期之外；本期起点前一秒落在上期内，必须不用。
        self.assertEqual(stamps, {'2026-08-03T00:00:00', '2026-07-31T23:59:59'})
        self.assertTrue(all(row['status'] not in plan.included_statuses for row in extra
                            if row['payment_time'] not in stamps))

    def test_rebind_changes_only_the_plan_constant(self):
        plan = self.plans()[0]
        script = assemble(role_plan(plan), GOOD)
        moved = frame_rebind(script, {'end': '2026-08-02T00:00:00'})
        self.assertEqual(model_part(moved), GOOD)
        self.assertEqual(moved, assemble({**role_plan(plan), 'end': '2026-08-02T00:00:00'}, GOOD))
        self.assertIsNone(frame_rebind('print(1)', {'end': None}))
        self.assertIsNone(metamorphic.rebind('print(1)', plan, {'end': '2026-08-02T00:00:00'}))


class SuiteTests(Fixture):
    def test_run_checks_includes_every_layer_and_passes_correct_result(self):
        version, path = self.dataset(5, 80)
        plan = AnalysisPlan(version, 'm-v1', kind='share', group_by='channel')
        output = self.root / 'out'
        script = fixed_code(plan).replace('/input/orders.csv', path.as_posix()).replace('"/output"', repr(output.as_posix()))
        (self.root / 'run.py').write_text(script, encoding='utf-8')
        subprocess.run([sys.executable, '-I', str(self.root / 'run.py')], check=True, timeout=30)
        actual = json.loads((output / 'result.json').read_text(encoding='utf-8'))
        report = run_checks(path, plan, actual, output / 'table.csv')
        self.assertEqual([item['name'] for item in report.checks],
                         ['independent_reference', 'group_total', 'download_table', 'invariants', 'reference_agreement'])
        self.assertTrue(report.passed, report.checks)
        sandbox = metamorphic.Sandbox(fixed_code(plan), local_execute(self.root / 'runs'))
        report = run_checks(path, plan, actual, output / 'table.csv', sandbox=sandbox)
        self.assertEqual(report.checks[-1]['name'], 'metamorphic')
        self.assertTrue(report.passed, report.checks)
        actual['rows'][0]['share'] = '1.000001'
        report = run_checks(path, plan, actual, output / 'table.csv', sandbox=sandbox)
        self.assertFalse(report.passed)
        # 前面已失败时不再花沙箱时间跑蜕变关系。
        self.assertNotIn('metamorphic', [item['name'] for item in report.checks])
        self.assertIn('ratio_mismatch', [issue['issue'] for item in report.checks if item['name'] == 'invariants'
                                         for issue in item['issues']])


class DisagreementWorkflowTests(unittest.TestCase):
    class Backend:
        def __init__(self):
            self.roles = []

        def invoke(self, role, context, *, timeout, token_limit):
            self.roles.append(role)
            handoff = context['handoff']
            if role == Role.PLANNER:
                metric = handoff['metric']
                return ModelReply({'plan': AnalysisPlan(handoff['dataset_version'], metric['version'],
                                                        included_statuses=tuple(metric['included_statuses'])).payload()}, 1)
            if role == Role.ANALYST:
                return ModelReply({'code': '# 模拟'}, 1)
            return ModelReply({'accepted': True, 'feedback': '相符'}, 1)

    class Services:
        def profile(self, task):
            return {'dataset_family': 'orders', 'schema_signature': 's', 'quality': {}}

        def execute(self, task, code, timeout, cancelled):
            return ExecutionManifest(str(uuid4()), task.dataset_version, task.plan.metric_version, 'x', 'input/analysis.py', [], 0, 'COMPLETED')

        def verify(self, task, manifest):
            return VerificationReport(False, [{'name': 'independent_reference', 'passed': True},
                                              {'name': 'reference_agreement', 'passed': False,
                                               'issues': [{'path': 'total.amount_cents', 'issue': 'disagreement'}]}])

        def code(self, manifest):
            return ''

    def test_reference_disagreement_fails_without_retry_or_review(self):
        backend = self.Backend()
        metric = MetricSnapshot('v1', ('paid',), 'trusted', 'demo', 'orders', 's')
        task = Workflow(RoleRuntime(backend), self.Services()).run(Task('demo', str(uuid4()), '汇总', metric=metric),
                                                                   cancelled=threading.Event())
        self.assertEqual((task.state, task.failure), (State.FAILED, 'REFERENCE_DISAGREEMENT'))
        self.assertEqual(backend.roles, [Role.PLANNER, Role.ANALYST])


if __name__ == '__main__':
    unittest.main()
