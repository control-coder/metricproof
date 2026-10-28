"""A6 报告核验（agents.claims）：数字匹配、引用、因果用词与 dispatch 展开，纯离线。"""
import unittest

from datalab.agents.claims import EMPTY_SUMMARY, ClaimChecker, numbers_in, redact_report
from datalab.agents.tools import ResultStore


def store_with_results():
    store = ResultStore()
    store.add('detect_anomaly', {}, 'L1', {'current': {'value': '14629.087450'}, 'change_rate': '-0.046477',
                                          'delta': '-713.049881', 'robust_z': '-2.228365'})
    store.add('contribution', {}, 'L1', {'rows': [{'group': 'credit_card', 'change': '-680.95',
                                                   'share_of_change': '0.954990'}],
                                         'total_change': 5942824})
    store.add('dispatch_subagent', {}, None, {'dispatched': True})
    return store


class NumberExtractionTests(unittest.TestCase):
    def test_skips_dates_ids_identifiers_and_small_counts(self):
        text = ('2018 年 2 月 aov 为 14629.09（r1-detect_anomaly），payment_installments=10、价格带 100_200、'
                '卖家 3d871de0142c、53243585…，3 个维度，2018-03-01，下降 4.65%，上升 6.893 个百分点')
        self.assertEqual([item['text'] for item in numbers_in(text)], ['14629.09', '4.65%', '6.893 个百分点'])
        self.assertTrue(numbers_in('6.893 个百分点')[0]['percent'])


class ClaimCheckerTests(unittest.TestCase):
    def setUp(self):
        self.checker = ClaimChecker(store_with_results(), {'r3-dispatch_subagent': ['r2-contribution']})

    def test_correct_report_passes(self):
        report = {
            'summary': 'aov 为 14629.09，环比下降 713.05、降幅 4.65%（r1-detect_anomaly）。',
            'root_causes': [{'dimension': 'payment_type', 'value': 'credit_card',
                             'explanatory_power': '0.954990', 'result_ids': ['r3-dispatch_subagent']}],
            'findings': [{'text': '变化集中在 credit_card，解释力 95.5%，运费增量 59428.24 元', 'result_ids': ['r2-contribution']}],
            'recommendations': ['持续跟踪信用卡订单'],
        }
        check = self.checker.check_report(report)
        self.assertTrue(check['passed'], check['issues'])
        self.assertEqual(check['checked_numbers'], 5)

    def test_detects_mutation_missing_id_unknown_id_and_causal(self):
        cases = {
            'number_mismatch': {'summary': '环比下降 913.05（r1-detect_anomaly）。'},
            'missing_result_id': {'summary': '环比下降 713.05。'},
            'unknown_result_id': {'summary': '环比下降 713.05（r9-detect_anomaly）。'},
            'causal_assertion': {'summary': '由于信用卡订单减少，环比下降 713.05（r1-detect_anomaly）。'},
            'value_not_found': {'summary': '', 'root_causes': [{'dimension': 'payment_type', 'value': 'boleto',
                                                               'result_ids': ['r2-contribution']}]},
        }
        for issue, report in cases.items():
            with self.subTest(issue=issue):
                check = self.checker.check_report(report)
                self.assertFalse(check['passed'])
                self.assertIn(issue, [item['issue'] for item in check['issues']])

    def test_root_cause_without_ids_and_mutated_power(self):
        check = self.checker.check_report({'summary': '', 'root_causes': [
            {'dimension': 'payment_type', 'value': 'credit_card'},
            {'dimension': 'payment_type', 'value': 'credit_card', 'explanatory_power': '0.754990',
             'result_ids': ['r2-contribution']}]})
        self.assertEqual([item['issue'] for item in check['issues']], ['missing_result_id', 'number_mismatch'])

    def test_dispatch_reference_expands_to_branch_results(self):
        self.assertEqual(self.checker.expand('r3-dispatch_subagent'), ['r2-contribution'])
        self.assertEqual(self.checker.check_text('解释力 0.954990（r3-dispatch_subagent）'), [])
        self.assertEqual(self.checker.check_text('降幅 4.65%（r3-dispatch_subagent）')[0]['issue'], 'number_mismatch')


class ShortRefTests(unittest.TestCase):
    def setUp(self):
        self.checker = ClaimChecker(store_with_results())

    def test_branch_scoped_result_id(self):
        # 并行子 Agent 的编号 r3.1-contribution：整体按完整 result_id 解析，前缀 r3 不被当作缩写。
        store = store_with_results()
        branch_id = store.add('contribution', {}, 'L1', {'rows': [{'change': '-680.95'}]}, scope='r3')
        self.assertEqual(branch_id, 'r3.1-contribution')
        checker = ClaimChecker(store)
        self.assertEqual(checker.short_refs(f'见 {branch_id}'), [])
        self.assertEqual(checker.check_text(f'变化 -680.95（{branch_id}）'), [])
        self.assertEqual(store.ordered_ids(), ['r1-detect_anomaly', 'r2-contribution', 'r3-dispatch_subagent',
                                               'r3.1-contribution'])

    def test_unique_short_refs_resolve(self):
        self.assertEqual(self.checker.short_refs('见 r1、r2/r3，或 r1-r2'), ['r1-detect_anomaly', 'r2-contribution',
                                                                         'r3-dispatch_subagent', 'r1-detect_anomaly',
                                                                         'r2-contribution'])
        self.assertEqual(self.checker.check_text('降幅 4.65%（r1）'), [])
        self.assertEqual(self.checker.check_text('下降 713.05（r1/r2）'), [])

    def test_formal_id_not_split_and_unknown_or_ambiguous_ignored(self):
        self.assertEqual(self.checker.short_refs('r1-detect_anomaly 与 Chr2、r9'), [])
        store = store_with_results()
        store._results['r2-other'] = {'tool': 'other', 'result': {}, 'result_id': 'r2-other'}
        self.assertEqual(ClaimChecker(store).short_refs('r2 与 r1'), ['r1-detect_anomaly'])
        issues = self.checker.check_text('下降 713.05（r9）')
        self.assertEqual([item['issue'] for item in issues], ['missing_result_id'])


class DerivedNumberTests(unittest.TestCase):
    def setUp(self):
        store = ResultStore()
        store.add('contribution', {}, 'L1', {'rows': [{'group': 'SP', 'share': '0.539704'},
                                                     {'group': 'PR', 'share': '0.387049'}],
                                             'rate_a': '0.153301', 'rate_b': '0.477325', 'count': 7})
        self.checker = ClaimChecker(store)

    def test_sum_and_difference_hit_and_are_counted(self):
        report = {'summary': 'SP 与 PR 合计 0.926753，差评率变化 0.324024（r1-contribution）。'}
        check = self.checker.check_report(report)
        self.assertTrue(check['passed'], check['issues'])
        self.assertEqual(check['derived_numbers'], 2)

    def test_wrong_derived_and_small_integers_do_not_hit(self):
        issues = self.checker.check_text('合计 0.926799（r1-contribution）')
        self.assertEqual([item['issue'] for item in issues], ['number_mismatch'])
        # 两位有效数字不做派生检查：0.69 ≈ 0.539704 + 0.153301 的四舍五入也不放行
        issues = self.checker.check_text('合计 0.69（r1-contribution）')
        self.assertEqual([item['issue'] for item in issues], ['number_mismatch'])


class RedactReportTests(unittest.TestCase):
    def test_removes_items_and_sentences(self):
        checker = ClaimChecker(store_with_results())
        report = {
            'summary': 'aov 下降 713.05（r1-detect_anomaly）。另有 999.99 的增量；整体平稳。',
            'findings': [{'text': '正常 4.65%（r1-detect_anomaly）'}, {'text': '虚假 888.88（r1-detect_anomaly）'}],
            'recommendations': ['关注 777.77', '持续跟踪'],
            'root_causes': [{'dimension': 'payment_type', 'value': 'boleto', 'result_ids': ['r2-contribution']}],
        }
        check = checker.check_report(report)
        new, removed = redact_report(report, check['issues'])
        self.assertEqual(new['summary'], 'aov 下降 713.05（r1-detect_anomaly）。整体平稳。')
        self.assertEqual(len(new['findings']), 1)
        self.assertEqual(new['recommendations'], ['持续跟踪'])
        self.assertEqual(new['root_causes'], [])
        self.assertEqual({item['where'] for item in removed},
                         {'summary', 'findings[1]', 'recommendations[0]', 'root_causes[0]'})
        self.assertEqual(len(report['findings']), 2)
        self.assertTrue(checker.check_report(new)['passed'])

    def test_empty_summary_placeholder(self):
        checker = ClaimChecker(store_with_results())
        report = {'summary': '由于促销，下降 913.05（r1-detect_anomaly）。'}
        new, _ = redact_report(report, checker.check_report(report)['issues'])
        self.assertEqual(new['summary'], EMPTY_SUMMARY)


if __name__ == '__main__':
    unittest.main()
