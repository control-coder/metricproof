"""A5 dev 评测：10 个 dev 案例各跑一次串行、一次并行，计算 AC@1/AC@3 与过程指标。

用法（仓库根目录，datalab 环境）：

    python -B evals/investigate.py                # 完整：10 案例 × 串行+并行，真实 MiMo
    python -B evals/investigate.py --cases inj-price_cut-05 raw-02   # 小规模试跑（调提示词）
    python -B evals/investigate.py --replay       # 离线回放（cassette 已录制，不发网络请求）

流程与口径：
- 案例：evals/anomaly/anomaly_cases.json 的 dev 10 个（7 注入 + 3 原始）；注入案例的数据目录为
  artifacts/anomaly/<case_id>/（缺失时调用 evals/anomaly/inject.materialize 重建），原始数据问题
  用基础数据版本（cases.json meta.base_version，.tmp/a3/datasets 下；缺失时按 build.py 导入）。
- 口径快照：cases.json meta.snapshot（anomaly-v1）。
- 模型：真实调用只用 MiMo（model_config 读取 .env）；真实调用时用 cassette 录制到
  artifacts/cassettes/a5/（被忽略，不提交）；--replay 用录制内容离线回放。
- 每个案例 run_investigation 一次串行、一次并行；单个失败记录原因继续下一个。
- 指标：AC@1/AC@3（evals/anomaly/score.py，注入与原始分开统计；候选取报告 root_causes），
  平均步数、模型调用数、token、费用（model_config 无单价则只记 token）、p50/p90 时延、
  工具调用准确率、越权次数、重复占比（ToolStats）、停止原因、L1 核验通过率。
- 结果写 artifacts/evals/a5-dev.json（与 OrderProof 旧结果分开）。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals" / "anomaly"))

from datalab.agents.cassette import CassetteTransport  # noqa: E402
from datalab.agents.investigation import InvestigationBudget, run_investigation  # noqa: E402
from datalab.execution.runner import DockerRunner  # noqa: E402
from datalab.roles.llm import ChatClient  # noqa: E402
from datalab.roles.model_config import load_model_config  # noqa: E402
from datalab.semantic.snapshot import SemanticSnapshot  # noqa: E402

import cases as cases_module  # noqa: E402
import inject as injector  # noqa: E402
import score as score_module  # noqa: E402

TARGET_ROOT = ROOT / "artifacts" / "anomaly"
BASE_ROOT = ROOT / ".tmp" / "a3" / "datasets"
CASSETTE_DIR = ROOT / "artifacts" / "cassettes" / "a5"
OUTPUT = ROOT / "artifacts" / "evals" / "a5-dev.json"

# 每个调查的预算（调用数、token、墙钟秒）：按计划第八节约 15 次调用留余量。
BUDGET = {"max_calls": 30, "max_tokens": 300_000, "seconds": 600.0}
REVIEW = True  # A6 起默认开启审查 Agent；--no-review 关闭
SANDBOX = None  # main() 中按 --no-sandbox 决定是否接 DockerRunner
# 主 Agent 可排查的候选维度提示（由案例指标允许的维度给出，注入到首条消息）。
RAW_KIND = "raw"


def log(started: float, message: str):
    print(f"[{time.time() - started:7.1f}s] {message}", flush=True)


def load_cases() -> dict:
    return cases_module.load()


def snapshot_of(data: dict) -> SemanticSnapshot:
    meta = data["meta"]["snapshot"]
    return SemanticSnapshot(meta["version"], tuple(meta["included_statuses"]), meta["amount_source"])


def base_folder_of(data: dict) -> Path:
    """基础数据版本目录：.tmp/a3/datasets 下匹配 meta.base_version；缺失时按 build.py 导入。"""
    version = data["meta"]["base_version"]
    folder = BASE_ROOT / version
    if (folder / "metadata.json").is_file():
        return folder
    import build as build_module  # evals/anomaly/build.py 的导入逻辑（复用，不重复实现）

    return build_module.base_folder()


def folder_of(case: dict, base: Path) -> Path:
    """案例数据目录：注入案例为物化目录（缺失时重建），原始问题为基础版本目录。"""
    if case["kind"] == RAW_KIND:
        return base
    target = TARGET_ROOT / case["id"]
    if not (target / "metadata.json").is_file():
        injector.materialize(case, base, TARGET_ROOT)
    return target


def profile_of(folder: Path) -> dict:
    metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    return metadata.get("profile") or {}


def dimensions_hint(case: dict) -> list[str]:
    """案例指标允许的维度（首条消息的候选维度提示，粒度不匹配的由语义层拒绝）。"""
    from datalab.agents.subagent import allowed_dimensions

    return allowed_dimensions(case["metric"])


def make_client(replay: bool, cassette_path: Path) -> tuple[ChatClient, CassetteTransport | None]:
    """真实 MiMo 客户端；replay 用 cassette 回放（不发请求），否则录制到 cassette。"""
    config = load_model_config()
    profile = config.profile("mimo")
    if not profile.configured and not replay:
        raise RuntimeError("MiMo 未配置 API Key（.env 的 DATALAB_MIMO_API_KEY）")
    if replay:
        return ChatClient(profile, transport=CassetteTransport(cassette_path, "replay")), None
    transport = CassetteTransport(cassette_path, "record")
    return ChatClient(profile, transport=transport), transport


def candidates_of(report: dict | None) -> list[dict]:
    """报告 root_causes 转评分候选（dimension, value, contribution）。"""
    if not report:
        return []
    candidates = []
    for item in report.get("root_causes") or []:
        if item.get("dimension") and item.get("value") is not None:
            candidates.append({"dimension": item["dimension"], "value": str(item["value"]),
                               "contribution": item.get("contribution")
                               or item.get("explanatory_power"),
                               "effect": item.get("effect")})
    return candidates


def verify_pass_rate(verification: dict) -> float | None:
    """L1 核验通过率：已核验（非 skipped）结果中 passed 的比例；无已核验结果为 None。"""
    checked = [item for item in verification.values() if not item.get("skipped")]
    if not checked:
        return None
    return sum(1 for item in checked if item.get("passed")) / len(checked)


def run_case(case: dict, folder: Path, snapshot, client, *, parallel: bool) -> dict:
    """单个案例的一次调查；失败时返回带 error 的记录，不抛出。"""
    started = time.perf_counter()
    try:
        result = run_investigation(
            case["question"], folder, snapshot, client, parallel=parallel,
            budget=InvestigationBudget(**BUDGET), metric=case["metric"],
            period=case["period"], base_period=case["base_period"],
            profile=profile_of(folder), dimensions_hint=dimensions_hint(case), review=REVIEW,
            sandbox=SANDBOX)
    except Exception as error:
        return {"case": case["id"], "parallel": parallel, "error": type(error).__name__ + ": " + str(error)[:200],
                "seconds": round(time.perf_counter() - started, 3)}
    scored = score_module.score(case, candidates_of(result.report))
    return {"case": case["id"], "parallel": parallel,
            "stop_reason": result.stop_reason,
            "report": result.report,
            "ac1": scored["ac1"], "ac3": scored["ac3"],
            "contribution_error": scored["contribution_error"],
            "steps": result.steps, "model_calls": result.model_calls, "tokens": result.tokens,
            "seconds": result.seconds,
            "tool_stats": result.tool_stats,
            "verify_pass_rate": verify_pass_rate(result.verification),
            "verified": sum(1 for item in result.verification.values() if not item.get("skipped")),
            "referenced": len(result.verification),
            "branches": len(result.branches),
            # 时延拆分：主 Agent 与子 Agent 的模型请求累计秒数（并行时子 Agent 的累计值大于墙钟）。
            "model_seconds": {
                "lead": round(sum(item.get("seconds") or 0 for item in result.trace
                                  if item["kind"] == "model_call" and item["agent"] == "lead"), 1),
                "subagents": round(sum(item.get("seconds") or 0 for item in result.trace
                                       if item["kind"] == "model_call" and item["agent"] != "lead"), 1)},
            "causal_assertions": (result.report or {}).get("causal_assertions", []),
            # A6：审查 Agent 裁决、修订前的代码核对问题数、修订与否、最终报告是否有效。
            "review": {key: ((result.report or {}).get("review") or {}).get(key)
                       for key in ("verdict", "revised", "steps")}
                      | {"code_issues_before": len(((result.report or {}).get("review") or {})
                                                   .get("code_issues_before") or []),
                         "reviewer_issues": len(((result.report or {}).get("review") or {}).get("issues") or [])},
            "valid": (result.report or {}).get("valid"),
            "final_issues": [item["issue"] for item in
                             ((result.report or {}).get("claims_check") or {}).get("issues") or []],
            "degraded": bool((result.report or {}).get("degraded"))}


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def summarize(rows: list[dict], kind: str) -> dict:
    """按 kind（injected/raw）与模式汇总；失败案例计入 failures，不进均值。"""
    picked = [row for row in rows if row.get("kind") == kind and "error" not in row]
    failed = [row for row in rows if row.get("kind") == kind and "error" in row]
    if not picked:
        return {"cases": 0, "failures": len(failed)}
    seconds = [row["seconds"] for row in picked]
    verify = [row["verify_pass_rate"] for row in picked if row["verify_pass_rate"] is not None]
    accuracy = [row["tool_stats"]["accuracy"] for row in picked]
    duplicate = [row["tool_stats"]["duplicate_ratio"] for row in picked]
    denied = sum(row["tool_stats"]["denied"] for row in picked)
    lead_steps = [row["steps"]["lead"] for row in picked]
    sub_steps = [step for row in picked for step in row["steps"]["subagents"]]
    causal = sum(len(row["causal_assertions"]) for row in picked)
    return {"cases": len(picked), "failures": len(failed),
            "ac1": round(sum(row["ac1"] for row in picked) / len(picked), 4),
            "ac3": round(sum(row["ac3"] for row in picked) / len(picked), 4),
            "avg_lead_steps": round(statistics.mean(lead_steps), 2),
            "avg_sub_steps": round(statistics.mean(sub_steps), 2) if sub_steps else None,
            "avg_model_calls": round(statistics.mean(row["model_calls"] for row in picked), 2),
            "total_tokens": sum(row["tokens"] for row in picked),
            "p50_seconds": round(percentile(seconds, 0.5), 2),
            "p90_seconds": round(percentile(seconds, 0.9), 2),
            "tool_accuracy": round(statistics.mean(accuracy), 4),
            "denied_calls": denied,
            "duplicate_ratio": round(statistics.mean(duplicate), 4),
            "verify_pass_rate": round(statistics.mean(verify), 4) if verify else None,
            "causal_assertion_cases": causal}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", nargs="*", default=None, help="只跑指定案例 ID（调提示词用）")
    parser.add_argument("--replay", action="store_true", help="用 cassette 离线回放，不发网络请求")
    parser.add_argument("--parallel-only", action="store_true", help="只跑并行（串行结果已存在时）")
    parser.add_argument("--serial-only", action="store_true", help="只跑串行")
    parser.add_argument("--workers", type=int, default=4, help="同时运行的调查数（案例间并发）")
    parser.add_argument("--output", type=Path, default=OUTPUT, help="结果文件")
    parser.add_argument("--cassette-dir", type=Path, default=CASSETTE_DIR, help="cassette 目录（各轮分开）")
    parser.add_argument("--no-review", action="store_true", help="关闭审查 Agent（回放 A5 录制时用）")
    parser.add_argument("--no-sandbox", action="store_true", help="不接执行沙箱（run_sql 返回未配置）")
    args = parser.parse_args()
    global REVIEW, SANDBOX
    REVIEW = not args.no_review
    if not args.no_sandbox:
        # A8 起子 Agent 可用 run_sql：本机 Docker + datalab-executor:py312-v2；容器工作目录在忽略的 artifacts 下。
        SANDBOX = DockerRunner(None, ROOT / "artifacts" / "sandbox")
    started = time.time()

    data = load_cases()
    snapshot = snapshot_of(data)
    base = base_folder_of(data)
    dev = cases_module.by_split(data, "dev")
    if args.cases:
        wanted = set(args.cases)
        dev = [case for case in dev if case["id"] in wanted]
        missing = wanted - {case["id"] for case in dev}
        if missing:
            raise SystemExit(f"案例不在 dev 划分内：{sorted(missing)}")
    log(started, f"dev 案例 {len(dev)} 个；基础版本 {base.name}")

    modes = [True] if args.parallel_only else [False] if args.serial_only else [False, True]
    folders = {case["id"]: folder_of(case, base) for case in dev}

    def task(case, parallel):
        mode = "parallel" if parallel else "serial"
        cassette_path = args.cassette_dir / f"{case['id']}-{mode}.json"
        client, recorder = make_client(args.replay, cassette_path)
        outcome = run_case(case, folders[case["id"]], snapshot, client, parallel=parallel)
        if recorder is not None:
            recorder.save()
        outcome["kind"] = "raw" if case["kind"] == RAW_KIND else "injected"
        outcome["mode"] = mode
        log(started, f"{case['id']} [{mode}] stop={outcome.get('stop_reason')} "
                     f"ac1={outcome.get('ac1')} ac3={outcome.get('ac3')} "
                     f"calls={outcome.get('model_calls')} tokens={outcome.get('tokens')} "
                     f"{outcome.get('seconds')}s requests={len(client.usage)}"
                     + (f" ERROR={outcome['error']}" if "error" in outcome else ""))
        return outcome

    # 案例之间互不依赖，按 --workers 并发跑（每个调查独立的客户端、cassette 与语义层引擎）；
    # 单个调查内的串行/并行语义不受影响，时延指标仍按单个调查的墙钟计。
    jobs = [(case, parallel) for case in dev for parallel in modes]
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        rows = list(pool.map(lambda job: task(*job), jobs))

    result = {"meta": {"date": time.strftime("%Y-%m-%d %H:%M"),
                       "cases": [case["id"] for case in dev],
                       "budget": BUDGET, "replay": args.replay,
                       "note": "A5 dev：10 案例 × 串行+并行；注入与原始分开统计；"
                               "model_config 无单价，费用只记 token"},
              "rows": rows,
              "summary": {mode: {kind: summarize([row for row in rows if row["mode"] == mode], kind)
                                 for kind in ("injected", "raw")}
                          for mode in ("serial", "parallel")}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log(started, f"结果写入 {args.output}")
    total_calls = sum(row.get("model_calls", 0) for row in rows)
    log(started, f"模型调用总数 {total_calls}")


if __name__ == "__main__":
    main()
