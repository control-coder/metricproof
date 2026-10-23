"""评测统计与留出题集的离线检查；不调用模型，不需要 Docker 或数据库。"""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'evals'))

import heldout  # noqa: E402
import report  # noqa: E402
import stats  # noqa: E402

from datalab.contracts import AnalysisPlan  # noqa: E402


class StatsTests(unittest.TestCase):
    def test_wilson_matches_known_values(self):
        low, high = stats.wilson(20, 20)
        self.assertAlmostEqual(low, 0.8389, places=4)
        self.assertEqual(high, 1.0)
        low, high = stats.wilson(10, 20)
        self.assertAlmostEqual(low, 0.2993, places=4)
        self.assertAlmostEqual(high, 0.7007, places=4)
        self.assertEqual(stats.wilson(0, 0), (None, None))

    def test_bootstrap_is_reproducible_and_contains_point(self):
        groups = {index: [1, 1, 0] if index % 3 else [0, 0, 0] for index in range(12)}
        first = stats.cluster_bootstrap(groups, lambda values: sum(values) / len(values))
        second = stats.cluster_bootstrap(groups, lambda values: sum(values) / len(values))
        self.assertEqual(first, second)
        point, low, high = first
        self.assertLessEqual(low, point)
        self.assertLessEqual(point, high)

    def test_identical_arms_have_zero_paired_difference(self):
        arm = {index: [1, 0, 1] for index in range(10)}
        point, low, high = stats.paired_difference(arm, dict(arm))
        self.assertEqual((point, low, high), (0.0, 0.0, 0.0))

    def test_consistent_gap_has_interval_above_zero(self):
        better = {index: [1, 1] for index in range(15)}
        worse = {index: [1, 0] for index in range(15)}
        point, low, high = stats.paired_difference(better, worse)
        self.assertAlmostEqual(point, 0.5)
        self.assertGreater(low, 0)
        self.assertLessEqual(high, 0.5 + 1e-9)

    def test_median_ratio(self):
        self.assertEqual(stats.median_ratio([2, 4, 6], [1, 2, 3]), 2.0)
        self.assertIsNone(stats.median_ratio([], [1]))
        self.assertIsNone(stats.median_ratio([1], [0]))


class HeldoutTests(unittest.TestCase):
    def test_frozen_fingerprint_unchanged(self):
        # 题目或真值计划被改动时失败：留出集冻结后不能按结果调整。
        self.assertEqual(heldout.fingerprint(), heldout.HELDOUT_SHA256)

    def test_six_kinds_four_each_and_valid_plans(self):
        kinds = [fields['kind'] for _, fields in heldout.HELDOUT]
        for kind in ('summary', 'trend', 'ranking', 'share', 'comparison', 'quality'):
            self.assertEqual(kinds.count(kind), 4, kind)
        for _, fields in heldout.HELDOUT:
            AnalysisPlan(str(uuid4()), 'truth', included_statuses=('paid',), **fields)

    def test_no_overlap_with_dev_questions(self):
        sys.path.insert(0, str(ROOT / 'evals'))
        import model_compare
        dev = {question for question, _ in model_compare.QUESTIONS}
        self.assertFalse(dev & {question for question, _ in heldout.HELDOUT})


class OlistQuestionTests(unittest.TestCase):
    def setUp(self):
        import olist_questions
        self.questions = olist_questions

    def test_frozen_fingerprint_unchanged(self):
        self.assertEqual(self.questions.fingerprint(), self.questions.OLIST_HELDOUT_SHA256)

    def test_kinds_counts_metrics_and_valid_plans(self):
        from datalab.contracts import AMOUNT_KINDS, OLIST_KINDS
        kinds = ['clarify' if 'clarify' in fields else fields['kind'] for _, fields, _ in self.questions.HELDOUT]
        for kind in OLIST_KINDS + ('clarify',):
            self.assertEqual(kinds.count(kind), 4, kind)
        for split in (self.questions.DEV, self.questions.HELDOUT):
            for _, fields, source in split:
                self.assertIn(source, self.questions.METRICS)
                if 'clarify' in fields:
                    continue
                extra = {'amount_source': source} if fields['kind'] in AMOUNT_KINDS else {}
                AnalysisPlan(str(uuid4()), 'truth', schema='olist', included_statuses=self.questions.STATUSES, **fields, **extra)
        # 两种金额来源都在留出集中出现，非金额类型只用 payments 口径。
        sources = {source for _, fields, source in self.questions.HELDOUT if fields.get('kind') in AMOUNT_KINDS}
        self.assertEqual(sources, {'payments', 'items'})

    def test_no_overlap_with_dev(self):
        dev = {question for question, _, _ in self.questions.DEV}
        self.assertFalse(dev & {question for question, _, _ in self.questions.HELDOUT})


class AnalystReplayTests(unittest.TestCase):
    def setUp(self):
        import analyst_replay
        self.replay = analyst_replay
        self.plan = AnalysisPlan(str(uuid4()), 'rule-v1', kind='comparison', start='2026-09-01T00:00:00',
                                 end='2026-10-01T00:00:00', previous_start='2026-08-01T00:00:00',
                                 previous_end='2026-09-01T00:00:00')

    class FakeTask:
        task_id = 'task'
        question = '9月成交额相比8月增长了多少？'

    def test_variant_names_are_validated(self):
        self.assertEqual(self.replay.split_variant('func'), ('func', 'base'))
        self.assertEqual(self.replay.split_variant('func+slim_question'), ('func', 'slim_question'))
        with self.assertRaises(SystemExit):
            self.replay.split_variant('func+unknown')

    def test_slim_handoff_keeps_only_fields_analyst_uses(self):
        base = self.replay.handoff_for('base', self.FakeTask, self.plan)
        slim = self.replay.handoff_for('slim', self.FakeTask, self.plan)
        self.assertIn('dataset_version', base['plan'])
        self.assertEqual(set(slim['plan']), set(self.replay.SLIM_KEYS))
        self.assertEqual(slim['plan']['start'], self.plan.start)
        self.assertNotIn('question', slim)

    def test_question_variants_add_only_the_question(self):
        with_question = self.replay.handoff_for('question', self.FakeTask, self.plan)
        self.assertEqual(with_question['question'], self.FakeTask.question)
        self.assertEqual({key: value for key, value in with_question.items() if key != 'question'},
                         self.replay.handoff_for('base', self.FakeTask, self.plan))

    def test_summary_reports_ratio_against_baseline(self):
        rows = []
        for variant, tokens in (('func', 2000), ('func+slim', 1000)):
            for case in range(1, 9):
                rows.append({'variant': variant, 'case': case, 'repeat': 0, 'passed': True, 'execution': 'COMPLETED',
                             'usage': [{'prompt_tokens': 900, 'completion_tokens': tokens}], 'cost_yuan': tokens / 1e5,
                             'model_seconds': tokens / 100})
        summary = self.replay.summarize(rows, ['func', 'func+slim'], 'func')
        self.assertEqual(summary[1]['tokens_ratio_vs_func'][0], 0.5)
        self.assertNotIn('tokens_ratio_vs_func', summary[0])


class ReportTests(unittest.TestCase):
    def record(self, arm, case, batch, correct, seconds, cost):
        return {'experiment': 'agents', 'provider': 'mimo', 'arm': arm, 'case': case, 'batch': batch,
                'correct': correct, 'verified_but_wrong': False, 'attempts': 1, 'seconds': seconds, 'cost_yuan': cost}

    def test_report_runs_on_synthetic_results(self):
        cases = []
        for batch in (1, 2, 3):
            for case in range(1, 9):
                cases.append(self.record('single_agent', case, batch, True, 10.0, 0.004))
                cases.append(self.record('three_roles', case, batch, case != 3 or batch != 1, 20.0, 0.008))
        payload = {'split': 'heldout', 'git_commit': 'abc', 'prompts_sha256': 'p', 'heldout_sha256': 'h', 'cases': cases}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'result.json'
            path.write_text(json.dumps(payload), encoding='utf-8')
            output = Path(folder) / 'out.md'
            with patch.object(sys, "argv", ["report.py", str(path), "--markdown", str(output)]), contextlib.redirect_stdout(io.StringIO()):
                report.main()
            text = output.read_text(encoding='utf-8')
        self.assertIn('三角色减单 Agent 的正确率差', text)
        self.assertIn('2.00×', text)
        self.assertNotIn('警告', text)

    def test_mismatched_prompts_are_flagged(self):
        meta = [{'split': 'heldout', 'prompts_sha256': 'a', 'heldout_sha256': 'h'},
                {'split': 'heldout', 'prompts_sha256': 'b', 'heldout_sha256': 'h'}]
        self.assertTrue(any('prompts_sha256' in note for note in report.warnings(meta)))


if __name__ == '__main__':
    unittest.main()
