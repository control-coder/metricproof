"""错误注入基准的离线测试：变异算子目录、正确代码通过全部核验层、计划变异与汇总；不需要 Docker 或模型。

执行用测试内的本机执行器，只运行服务端固定代码及其确定性变异体。
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'evals'))

import heldout  # noqa: E402
import injection  # noqa: E402

from datalab.contracts import AnalysisPlan  # noqa: E402
from datalab.datasets.service import DatasetStore  # noqa: E402
from datalab.execution.analysis_frame import assemble  # noqa: E402


class LocalLab:
    """与 injection.DockerLab 接口相同，把容器路径改到临时目录后在本机运行。"""

    def __init__(self, folder: Path):
        self.folder = folder
        self.store = DatasetStore(folder / 'datasets')

    def _run(self, script, data):
        run = self.folder / 'runs' / uuid4().hex
        (run / 'output').mkdir(parents=True)
        (run / 'orders.csv').write_bytes(data)
        text = script.replace('/input/orders.csv', (run / 'orders.csv').as_posix()).replace('/output/', (run / 'output').as_posix() + '/')
        (run / 'run.py').write_text(text, encoding='utf-8')
        done = subprocess.run([sys.executable, '-I', str(run / 'run.py')], capture_output=True, timeout=60)
        if done.returncode:
            return None, None
        return json.loads((run / 'output/result.json').read_text(encoding='utf-8')), run / 'output/table.csv'

    def run(self, plan, script):
        return self._run(script, (self.store.directory(plan.dataset_version) / 'orders.csv').read_bytes())

    def execute(self, plan):
        return lambda script, data, timeout: self._run(script, data)[0]


class CatalogTests(unittest.TestCase):
    def test_every_operator_applies_and_changes_the_code(self):
        self.assertTrue(12 <= len(injection.OPERATORS) <= 20)
        self.assertEqual(len({item.name for item in injection.OPERATORS}), len(injection.OPERATORS))
        for operator in injection.OPERATORS:
            with self.subTest(operator=operator.name):
                mutated = operator.apply()
                self.assertNotEqual(mutated, injection.BASE)
                self.assertTrue(operator.kinds)
                compile(assemble({}, mutated), operator.name, 'exec')

    def test_missing_anchor_fails_loudly(self):
        with self.assertRaises(injection.MutationError):
            injection.Operator('x', 'f', ('summary',), (('不存在的锚点', 'y'),), '').apply()
        operator = injection.OPERATORS[0]
        with self.assertRaises(injection.MutationError):
            operator.apply(operator.apply())

    def test_base_code_is_the_fixed_analysis_function(self):
        self.assertIn('def analyze(data, plan):\n    source = data\n', injection.BASE)
        self.assertNotIn('open(', injection.BASE)
        self.assertEqual(len(injection.catalog_fingerprint()), 64)

    def test_plan_mutants_are_valid_and_differ_from_truth(self):
        items = injection.plan_mutants()
        self.assertEqual(sum(item['operator'] == 'statuses_changed' for item in items), len(heldout.HELDOUT))
        for item in items:
            with self.subTest(operator=item['operator'], case=item['case']):
                AnalysisPlan(str(uuid4()), 'v1', **item['fields'])
                if item['operator'] != 'statuses_changed':
                    self.assertNotEqual(item['fields'], item['truth'])
        jobs = injection.reviewer_jobs([{**item, 'case': item['case'] + 1, **({'metric_lock': 'rejected'} if 'statuses' in item else {})}
                                        for item in items])
        self.assertLessEqual(len(jobs) + 2, injection.MAX_REVIEWER_CALLS)


class LayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = ROOT / '.tmp'
        scratch.mkdir(exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(dir=scratch, prefix='injection-test-')
        cls.lab = LocalLab(Path(cls.temp.name))
        datasets = (('adversarial-20261024', 20261024, True, ('paid', 'refunded')),)
        cases = injection.prepare(cls.lab, datasets)
        # 每类取一题，控制离线测试时间。
        cls.cases = [next(case for case in cases if case['kind'] == kind)
                     for kind in ('summary', 'trend', 'ranking', 'share', 'comparison', 'quality')]
        cls.baseline = [injection.run_correct(cls.lab, case) for case in cls.cases]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_correct_code_passes_every_layer(self):
        for record in self.baseline:
            with self.subTest(kind=record['kind']):
                self.assertFalse([name for name, value in record['layers'].items() if value is False], record)

    def test_boundary_mutant_is_exposed_and_caught(self):
        case = self.cases[0]
        operator = next(item for item in injection.OPERATORS if item.name == 'end_inclusive')
        record = injection.run_mutant(self.lab, operator, case)
        self.assertEqual(record['status'], 'exposed')
        self.assertFalse(record['layers']['independent_reference'])
        self.assertFalse(record['layers']['metamorphic'])
        self.assertIn('irrelevant_rows:result_changed', record['metamorphic_issues'])
        self.assertTrue(record['layers']['reference_agreement'])

    def test_ratio_mutant_is_caught_without_reference(self):
        case = self.cases[3]
        operator = next(item for item in injection.OPERATORS if item.name == 'share_unfiltered_total')
        record = injection.run_mutant(self.lab, operator, case)
        self.assertEqual(record['status'], 'exposed')
        self.assertFalse(record['layers']['invariants'])
        self.assertIn('ratio_mismatch', record['invariant_issues'])

    def test_irrelevant_mutant_on_this_plan_is_equivalent(self):
        # 计划没有按日分组时，按月截断不改变输出：记为未暴露，不进检出率分母。
        case = self.cases[2]
        operator = next(item for item in injection.OPERATORS if item.name == 'date_key_month')
        self.assertEqual(injection.run_mutant(self.lab, operator, case)['status'], 'equivalent')

    def test_correct_code_under_wrong_plan_passes_numeric_layers(self):
        version = self.cases[0]['plan'].dataset_version
        items = [item for item in injection.plan_mutants() if item['case'] == 0]
        versions = {'seed-20260928': version}
        for item in items:
            with self.subTest(operator=item['operator']):
                record = injection.run_plan_mutant(self.lab, item, versions, ('paid', 'refunded'))
                if item['operator'] == 'statuses_changed':
                    self.assertEqual(record['metric_lock'], 'rejected')
                else:
                    self.assertTrue(record['result_differs'])
                    self.assertFalse([name for name, value in record['layers'].items() if value is False])


class SummaryTests(unittest.TestCase):
    def record(self, operator, status, **layers):
        values = dict.fromkeys(injection.LAYERS, True)
        values.update(layers)
        return {'operator': operator, 'family': 'f-' + operator, 'status': status, 'layers': values,
                'invariant_issues': [], 'metamorphic_issues': ['permutation:result_changed'] if layers.get('metamorphic') is False else []}

    def test_equivalent_and_crashed_are_excluded_from_denominators(self):
        records = [self.record('a', 'exposed', independent_reference=False, invariants=False),
                   self.record('a', 'exposed', independent_reference=False, metamorphic=False),
                   self.record('b', 'exposed', independent_reference=False),
                   self.record('b', 'equivalent'), {'operator': 'b', 'family': 'f-b', 'status': 'crashed'}]
        summary = injection.summarize_code(records)
        self.assertEqual((summary['exposed'], summary['equivalent'], summary['crashed']), (3, 1, 1))
        self.assertEqual(summary['layers']['independent_reference']['caught'], 3)
        self.assertEqual(summary['layers']['reference_free_union']['caught'], 2)
        self.assertEqual(summary['reference_free_residual'], 1)
        self.assertEqual(summary['verified_but_wrong'], 0)
        self.assertEqual(summary['metamorphic_relations']['permutation'], 1)
        low, high = summary['layers']['invariants']['wilson95']
        self.assertLess(low, 1 / 3)
        self.assertGreater(high, 1 / 3)

    def test_markdown_renders(self):
        records = [self.record('a', 'exposed', independent_reference=False, invariants=False)]
        plans = injection.summarize_plans([{'operator': 'statuses_changed', 'metric_lock': 'rejected'},
                                           {'operator': 'kind_changed', 'result_differs': True, 'layers': dict.fromkeys(injection.LAYERS, True)}],
                                          [{'label': 'mutant', 'operator': 'kind_changed', 'accepted': False, 'usage': []},
                                           {'label': 'control', 'operator': 'truth', 'accepted': True, 'usage': []}])
        payload = {'git_commit': 'abc', 'date': '2026-10-22', 'catalog_sha256': 'c' * 64, 'heldout_sha256': heldout.HELDOUT_SHA256,
                   'operators': [1], 'datasets': [1], 'baseline': {'cases': 1, 'false_alarms': 0},
                   'code_summary': injection.summarize_code(records), 'plan_summary': plans}
        text = injection.markdown(payload)
        self.assertIn('Reviewer（MiMo）检出率 1/1', text)
        self.assertEqual(plans['numeric_blind_spot']['caught'], 1)


if __name__ == '__main__':
    unittest.main()
