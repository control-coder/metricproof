"""A7 真实模型冒烟：含糊问题与需要新口径的问题是否都进入等待确认。

用法（仓库根目录，datalab 环境）：

    python -B evals/clarify_smoke.py            # 真实 MiMo，录制到 artifacts/cassettes/a7/
    python -B evals/clarify_smoke.py --replay   # 离线回放

口径：服务端不给指标、本期与对比期（模拟无法从问题确定的情形），记忆为进程内仓库
（memory.local.InMemoryMemoryRepository，Olist 范围）；每题预算 6 次调用。判定：stop_reason 为
waiting_confirmation 记为通过；需要新口径的题另记是否调用了 propose_metric（不强制，ask_user
同样是合理的澄清）。题目是固定的 7 道，不做调优；结果写 artifacts/evals/a7-clarify.json。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

import investigate  # noqa: E402

from datalab.agents.investigation import InvestigationBudget, run_investigation  # noqa: E402
from datalab.datasets import olist  # noqa: E402
from datalab.memory.local import InMemoryMemoryRepository  # noqa: E402
from datalab.memory.service import MemoryService, Scope  # noqa: E402

QUESTIONS = [
    ("vague-01", "最近卖得怎么样？", "clarification"),
    ("vague-02", "销售额为什么变了？", "clarification"),
    ("vague-03", "哪里出问题了？", "clarification"),
    ("vague-04", "上个月表现如何，主要是什么原因？", "clarification"),
    ("vague-05", "这个指标最近波动大吗？", "clarification"),
    ("metric-01", "把已取消的订单也算进 GMV，2018 年 3 月环比为什么变化？", "metric"),
    ("metric-02", "按商品金额而不是支付金额统计 GMV，2017 年 11 月主要增长来自哪里？", "metric"),
]
CASSETTES = ROOT / "artifacts" / "cassettes" / "a7"
OUTPUT = ROOT / "artifacts" / "evals" / "a7-clarify.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", action="store_true")
    args = parser.parse_args()
    started = time.time()
    data = investigate.load_cases()
    snapshot = investigate.snapshot_of(data)
    folder = investigate.base_folder_of(data)
    profile = investigate.profile_of(folder)

    def task(item):
        case_id, question, kind = item
        client, recorder = investigate.make_client(args.replay, CASSETTES / f"{case_id}.json")
        service = MemoryService(InMemoryMemoryRepository())
        scope = Scope("a7-smoke", "olist", olist.SIGNATURE)
        result = run_investigation(question, folder, snapshot, client, parallel=True,
                                   budget=InvestigationBudget(6, 120_000, 300), profile=profile,
                                   memory=(service, scope))
        if recorder is not None:
            recorder.save()
        tools = [entry.get("tool") for entry in result.trace if entry["kind"] == "tool_call"]
        row = {"case": case_id, "question": question, "expected": kind, "stop_reason": result.stop_reason,
               "waiting": result.stop_reason == "waiting_confirmation", "pending": result.pending,
               "tools": tools, "model_calls": result.model_calls, "seconds": result.seconds,
               "pending_metrics": [entry.name for entry in service.history(scope) if entry.status == "pending"]}
        print(f"[{time.time() - started:6.1f}s] {case_id} stop={result.stop_reason} "
              f"pending={(result.pending or {}).get('type')} tools={tools}", flush=True)
        return row

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(task, QUESTIONS))
    summary = {"waiting": sum(row["waiting"] for row in rows), "total": len(rows),
               "metric_proposed": sum(1 for row in rows if row["expected"] == "metric"
                                      and (row["pending"] or {}).get("type") == "metric"),
               "metric_questions": sum(1 for row in rows if row["expected"] == "metric"),
               "model_calls": sum(row["model_calls"] for row in rows)}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps({"meta": {"date": time.strftime("%Y-%m-%d %H:%M"), "replay": args.replay,
                                           "note": "A7 澄清冒烟：服务端不给指标与期间，7 道固定题"},
                                  "summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
