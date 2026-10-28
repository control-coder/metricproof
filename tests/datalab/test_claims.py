"""A6 报告核验（agents.claims）：数字匹配、引用、因果用词与 dispatch 展开，纯离线。"""
import unittest

from datalab.agents.claims import ClaimChecker, numbers_in
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
                '卖家 3d871de0142c，3 个维度，2018-03-01，下降 4.65%')
        self.assertEqual([item['text'] for item in numbers_in(text)], ['14629.09', '4.65%'])


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


if __name__ == '__main__':
    unittest.main()
