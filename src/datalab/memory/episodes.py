"""情景记忆：每次完成的归因保存问题、计划要点、关键发现，供 BM25 检索作少样本示例；情景记忆不能升级为业务规则。"""
from __future__ import annotations

import json
from math import log
import re

from datalab.memory.service import MemoryEntry, MemoryService, Scope

# ASCII/拉丁字母数字（下划线保留在标识符内）或 CJK 连续段。
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[一-鿿぀-ヿ가-힯]+")

_CONTENT_LIMIT = 8192
_SUMMARY_LIMIT = 600
_QUESTION_LIMIT = 300


def tokenize(text: str) -> list[str]:
    """小写后切分：ASCII/拉丁词与数字成词，CJK 连续段取字二元组（单字段保留单字）。"""
    tokens = []
    for match in _TOKEN_PATTERN.finditer(text.lower()):
        chunk = match.group()
        if chunk.isascii():
            tokens.append(chunk)
        elif len(chunk) == 1:
            tokens.append(chunk)
        else:
            tokens.extend(chunk[index:index + 2] for index in range(len(chunk) - 1))
    return tokens


class BM25:
    """标准 Okapi BM25，idf = ln((N - df + 0.5)/(df + 0.5) + 1)。"""

    def __init__(self, documents: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.documents = documents
        self.doc_freq: dict[str, int] = {}
        for document in documents:
            for token in set(document):
                self.doc_freq[token] = self.doc_freq.get(token, 0) + 1
        self.avg_len = sum(len(document) for document in documents) / len(documents) if documents else 0.0

    def scores(self, query: list[str]) -> list[float]:
        total = len(self.documents)
        results = []
        for document in self.documents:
            term_freq: dict[str, int] = {}
            for token in document:
                term_freq[token] = term_freq.get(token, 0) + 1
            score = 0.0
            for token in query:
                df = self.doc_freq.get(token, 0)
                tf = term_freq.get(token, 0)
                if not df or not tf:
                    continue
                idf = log((total - df + 0.5) / (df + 0.5) + 1)
                length_norm = 1 - self.b + self.b * len(document) / self.avg_len if self.avg_len else 1.0
                score += idf * tf * (self.k1 + 1) / (tf + self.k1 * length_norm)
            results.append(score)
        return results


def _fits(content: dict) -> bool:
    return len(json.dumps(content, ensure_ascii=False).encode()) <= _CONTENT_LIMIT


def record_episode(service: MemoryService, scope: Scope, *, question: str, metric: str, period: dict,
                   base_period: dict | None, report: dict, run_id: str) -> MemoryEntry:
    """把一次完成的归因写入情景记忆（kind=summary，状态 recorded，永不升级为业务规则）。"""
    root_causes = []
    for cause in report.get("root_causes") or []:
        kept = {key: cause[key] for key in ("dimension", "value", "explanatory_power") if key in cause}
        if "effect" in cause:
            kept["effect"] = cause["effect"]
        root_causes.append(kept)
        if len(root_causes) == 3:
            break
    content = {"type": "episode", "question": question[:_QUESTION_LIMIT], "metric": metric,
               "period": period, "base_period": base_period, "summary": str(report.get("summary", ""))[:_SUMMARY_LIMIT],
               "root_causes": root_causes}
    while not _fits(content) and content["summary"]:
        content["summary"] = content["summary"][:len(content["summary"]) // 2]
    name = f"episode:{metric}:{period['start'][:7]}"[:120]
    return service.propose(scope, "summary", name, content, run_id)


def _index_text(content: dict) -> str:
    parts = [content["question"], content["summary"], content["metric"]]
    parts.extend(f"{cause.get('dimension', '')} {cause.get('value', '')}" for cause in content["root_causes"])
    return " ".join(parts)


def search_episodes(service: MemoryService, scope: Scope, query: str, k: int = 3) -> list[dict]:
    """在当前范围内按 BM25 检索情景记忆，按得分降序返回至多 k 条正得分结果。"""
    episodes = [entry for entry in service.history(scope)
                if entry.kind == "summary" and entry.content.get("type") == "episode"]
    if not episodes:
        return []
    ranked = BM25([tokenize(_index_text(entry.content)) for entry in episodes]).scores(tokenize(query))
    order = sorted(range(len(episodes)), key=lambda index: (-ranked[index], -episodes[index].version, -index))
    results = []
    for index in order:
        if ranked[index] <= 0 or len(results) >= k:
            break
        content = episodes[index].content
        results.append({"memory_id": episodes[index].memory_id, "score": round(ranked[index], 4),
                        "question": content["question"], "metric": content["metric"], "period": content["period"],
                        "root_causes": content["root_causes"], "summary": content["summary"]})
    return results
