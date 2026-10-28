"""情景记忆的分词、BM25 检索与范围隔离测试，全程离线。"""
import json
import unittest

from datalab.memory.episodes import BM25, record_episode, search_episodes, tokenize
from datalab.memory.local import InMemoryMemoryRepository
from datalab.memory.service import MemoryService, Scope


class TokenizeTests(unittest.TestCase):
    def test_mixed_text(self):
        tokens = tokenize("2017 年 11 月 GMV 环比上升，customer_state=SP")
        for expected in ("gmv", "customer_state", "sp", "环比", "比上", "2017", "11"):
            self.assertIn(expected, tokens)

    def test_single_cjk_char_kept(self):
        self.assertEqual(tokenize("月"), ["月"])


class BM25Tests(unittest.TestCase):
    def setUp(self):
        self.corpus = [["gmv", "上升", "运费"], ["订单", "下降"], ["客户", "流失", "gmv"]]

    def test_matching_doc_first(self):
        scores = BM25(self.corpus).scores(["gmv", "运费"])
        self.assertEqual(scores[0], max(scores))
        self.assertGreater(scores[0], 0)

    def test_no_overlap_gives_zero(self):
        self.assertEqual(BM25(self.corpus).scores(["不存在"]), [0.0, 0.0, 0.0])

    def test_empty_corpus_and_docs(self):
        self.assertEqual(BM25([]).scores(["gmv"]), [])
        self.assertEqual(BM25([[]]).scores(["gmv"]), [0.0])


class EpisodeTests(unittest.TestCase):
    def setUp(self):
        self.service = MemoryService(InMemoryMemoryRepository())
        self.scope = Scope('demo', 'olist', 'schema-v1')
        report_freight = {"summary": "运费上涨导致 GMV 环比上升", "root_causes": [
            {"dimension": " freight_type", "value": "express", "explanatory_power": 0.8, "effect": "推高金额", "extra": "丢弃"}]}
        report_channel = {"summary": "新渠道带来订单增长", "root_causes": [
            {"dimension": "channel", "value": "ads", "explanatory_power": 0.6}]}
        self.freight = record_episode(self.service, self.scope, question="2017年11月 GMV 为什么上升？",
            metric="gmv", period={"start": "2017-11-01", "end": "2017-11-30"}, base_period=None,
            report=report_freight, run_id="run-1")
        self.channel = record_episode(self.service, self.scope, question="订单量为什么增长？",
            metric="orders", period={"start": "2017-12-01", "end": "2017-12-31"}, base_period={"start": "2017-11-01"},
            report=report_channel, run_id="run-2")

    def test_record_format_and_status(self):
        self.assertEqual(self.freight.kind, "summary")
        self.assertEqual(self.freight.status, "recorded")
        self.assertEqual(self.freight.name, "episode:gmv:2017-11")
        content = self.freight.content
        self.assertEqual(content["type"], "episode")
        self.assertEqual(content["root_causes"], [
            {"dimension": " freight_type", "value": "express", "explanatory_power": 0.8, "effect": "推高金额"}])

    def test_search_returns_freight_first(self):
        results = search_episodes(self.service, self.scope, "运费 上升")
        self.assertTrue(results)
        self.assertEqual(results[0]["memory_id"], self.freight.memory_id)
        self.assertEqual(results[0]["metric"], "gmv")
        self.assertEqual(results[0]["score"], round(results[0]["score"], 4))
        self.assertTrue(all(result["score"] > 0 for result in results))

    def test_episode_never_retrievable_or_confirmable(self):
        self.assertEqual(self.service.retrieve(self.scope), [])
        with self.assertRaises(ValueError):
            self.service.confirm(self.scope, self.freight.memory_id, actor='user:test', expected_current_id=None)

    def test_scope_isolated(self):
        other = Scope('demo', 'olist', 'schema-v2')
        self.assertEqual(search_episodes(self.service, other, "运费"), [])

    def test_huge_summary_stays_within_limit(self):
        entry = record_episode(self.service, self.scope, question="q", metric="gmv",
            period={"start": "2018-01-01"}, base_period=None,
            report={"summary": "长" * 20000, "root_causes": []}, run_id="run-3")
        self.assertLessEqual(len(json.dumps(entry.content, ensure_ascii=False).encode()), 8192)


if __name__ == "__main__":
    unittest.main()
