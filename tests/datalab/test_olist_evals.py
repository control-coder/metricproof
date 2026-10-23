"""Olist 评测脚本的离线检查：变异目录锚点、框架形态的基准代码与参考一致、评分规则；不调用模型、不需要 Docker。"""
import random
import sys
import tempfile
import unittest
from pathlib import Path

from olist_bundle import bundle, write
from test_olist_verification import local_execute
from datalab.contracts import AnalysisPlan
from datalab.datasets import olist
from datalab.datasets.service import DatasetStore
from datalab.execution.analysis_frame import assemble
from datalab.orchestration.workflow import role_plan
from datalab.verification.olist_sql import reference_result

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'evals'))
import olist_compare  # noqa: E402
import olist_injection  # noqa: E402


class OlistInjectionCatalogTests(unittest.TestCase):
    def setUp(self):
        scratch = (ROOT / '.tmp').resolve()
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch, prefix='olist-inj-test-')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = DatasetStore(root / 'datasets')
        self.version = self.store.import_olist(write(root / 'raw', bundle(random.Random(41), 120)), project_id='p',
                                               dataset_family='olist').dataset_version
        self.folder = self.store.directory(self.version)
        self.execute = local_execute(root / 'runs')

    def run_code(self, plan, code):
        return self.execute(assemble(role_plan(plan), code), olist.dataset_inputs(self.folder, plan), 30)

    def plans(self):
        statuses = ('delivered', 'shipped', 'invoiced')
        return {kind: AnalysisPlan(self.version, 'v', kind=kind, schema='olist', included_statuses=statuses, **fields)
                for kind, fields in (('ranking', {'group_by': 'customer_state', 'amount_source': 'payments',
                                                  'start': '2017-02-01T00:00:00'}),
                                     ('repeat_rate', {'group_by': 'customer_state'}), ('cohort_retention', {'start': '2017-02-01T00:00:00'}),
                                     ('delivery_delay', {}), ('category_sales', {'group_by': 'product_category'}))}

    def test_every_operator_applies_once_and_compiles(self):
        names = [operator.name for operator in olist_injection.OPERATORS]
        self.assertEqual(len(names), len(set(names)))
        for operator in olist_injection.OPERATORS:
            compile(assemble({}, operator.apply()), operator.name, 'exec')
        self.assertEqual(len(olist_injection.catalog_fingerprint()), 64)

    def test_base_matches_reference_and_olist_mutants_change_results(self):
        plans = self.plans()
        for kind, plan in plans.items():
            with self.subTest(kind=kind):
                self.assertEqual(self.run_code(plan, olist_injection.BASE), reference_result(self.folder, plan))
        changed = {name: olist_injection.OPERATORS[[op.name for op in olist_injection.OPERATORS].index(name)]
                   for name in ('repeat_customer_id', 'cohort_last_order', 'late_timestamp_level', 'category_drops_unknown',
                                'amount_source_swapped')}
        for name, operator in changed.items():
            plan = plans[{'repeat_customer_id': 'repeat_rate', 'cohort_last_order': 'cohort_retention',
                          'late_timestamp_level': 'delivery_delay', 'category_drops_unknown': 'category_sales',
                          'amount_source_swapped': 'ranking'}[name]]
            with self.subTest(operator=name):
                self.assertNotEqual(self.run_code(plan, operator.apply()), reference_result(self.folder, plan))


class OlistCompareScoreTests(unittest.TestCase):
    def record(self, **fields):
        return {'state': 'SUCCEEDED', 'passed': True, 'reference': {'total': 1}, 'attempts': 1, 'replans': 0, 'failure': None,
                'usage': [{'prompt_tokens': 1000, 'cached_tokens': 0, 'completion_tokens': 500}], **fields}

    def test_correct_silent_error_and_clarification(self):
        self.assertTrue(olist_compare.score(self.record(), {'total': 1})['correct'])
        wrong = olist_compare.score(self.record(), {'total': 2})
        self.assertEqual((wrong['correct'], wrong['verified_but_wrong'], wrong['outcome']), (False, True, 'VERIFIED_BUT_WRONG'))
        # 应澄清却直接给出结果，记为静默错误；应澄清且停下等待，记为正确。
        self.assertTrue(olist_compare.score(self.record(), None)['verified_but_wrong'])
        waited = olist_compare.score(self.record(state='WAITING_CONFIRMATION', passed=False, reference=None), None)
        self.assertTrue(waited['correct'] and waited['first_pass'])
        extra = olist_compare.score(self.record(state='WAITING_CONFIRMATION', passed=False, reference=None), {'total': 1})
        self.assertEqual(extra['outcome'], 'UNNEEDED_CLARIFICATION')
        self.assertAlmostEqual(extra['cost_yuan'], 0.002)

    def test_only_mimo_is_allowed(self):
        self.assertEqual(olist_compare.PROVIDER, 'mimo')
        self.assertIn('角色：单 Agent', olist_compare.SINGLE)
        self.assertIn('cohort_retention', olist_compare.SINGLE)


if __name__ == '__main__':
    unittest.main()
