"""A6 离线错误注入基准（审查/报告核对器）：回放 A5 串行录制，对报告注入确定性错误，
检验 claims.ClaimChecker 的检出率与原报告误报率。

用法（仓库根目录，datalab 环境）：

    python -B evals/review_inject.py

口径：
- 案例：evals/anomaly/anomaly_cases.json 的 dev 10 个；逐案回放 artifacts/cassettes/a5/<id>-serial.json
  （parallel=False, review=False，其余参数与 evals/investigate.py run_case 相同）。
- 每个案例直接构造 Investigation 并调用 .run(...)，在 close() 前取 ResultStore 与
  branch_results，构造 ClaimChecker 做核对。
- 原报告先去掉编排层附加字段（verification、causal_assertions、review、claims_check、valid），
  再跑 check_report 作为误报检查。
- 每个案例用 random.Random(20261028) 确定性生成变异报告，每类至多 3 个、每个只注入一个错误：
  number（改写一个当前能匹配的数字为 ×1.37）、delete_id（删除一条 finding 或 root_cause 的
  全部 result_id 引用）、causal（插入因果断言句式）。原报告已失败时，仅当对应问题类型计数
  相比原报告增加才算检出。
- 结果写 artifacts/evals/a6-inject.json。
"""
from __future__ import annotations

import copy
import json
import random
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals" / "anomaly"))

from datalab.agents.cassette import CassetteMiss  # noqa: E402
from datalab.agents.claims import RESULT_ID, ClaimChecker, _matches, numbers_in  # noqa: E402
from datalab.agents.investigation import Investigation, InvestigationBudget  # noqa: E402

import investigate  # noqa: E402

CASSETTE_DIR = ROOT / "artifacts" / "cassettes" / "a5"
OUTPUT = ROOT / "artifacts" / "evals" / "a6-inject.json"
SEED = 20261028
MAX_PER_TYPE = 3
NUMBER_FACTOR = 1.37
# 编排层附加字段（claims.review_view 的隐藏集），核对前去掉
HIDDEN_FIELDS = ("verification", "causal_assertions", "review", "claims_check", "valid")
# 注入类型 → 期望的问题类型
EXPECTED_ISSUE = {"number": "number_mismatch", "delete_id": "missing_result_id",
                  "causal": "causal_assertion"}


def log(started: float, message: str):
    print(f"[{time.time() - started:7.1f}s] {message}", flush=True)


def strip_orchestration(report: dict) -> dict:
    """去掉编排层附加的核验字段（只作用于顶层；finding 内的 verification 一并去掉）。"""
    cleaned = {key: value for key, value in report.items() if key not in HIDDEN_FIELDS}
    for key in ("findings", "root_causes"):
        items = cleaned.get(key)
        if isinstance(items, list):
            cleaned[key] = [
                {k: v for k, v in item.items() if k not in HIDDEN_FIELDS}
                if isinstance(item, dict) else item
                for item in items
            ]
    return cleaned


def replay_case(case: dict, folder: Path, snapshot, started: float) -> dict:
    """回放一个案例的 A5 串行录制；返回 {store, branch_results, report} 或 {skipped: 原因}。"""
    cassette_path = CASSETTE_DIR / f"{case['id']}-serial.json"
    if not cassette_path.is_file():
        return {"skipped": f"cassette 不存在：{cassette_path.name}"}
    try:
        client, _ = investigate.make_client(True, cassette_path)
    except Exception as error:  # 含 CassetteMiss（文件缺失）与配置错误
        return {"skipped": f"客户端构造失败：{type(error).__name__}: {str(error)[:120]}"}
    investigation = Investigation(
        case["question"], folder, snapshot, client, parallel=False,
        budget=InvestigationBudget(**investigate.BUDGET), profile=investigate.profile_of(folder),
        review=False)
    try:
        result = investigation.run(metric=case["metric"], period=case["period"],
                                   base_period=case["base_period"],
                                   dimensions_hint=investigate.dimensions_hint(case))
    except CassetteMiss as error:
        return {"skipped": f"CassetteMiss：{str(error)[:120]}"}
    except Exception as error:
        return {"skipped": f"回放失败：{type(error).__name__}: {str(error)[:120]}"}
    if result.report is None:
        investigation.close()
        return {"skipped": f"无报告（stop={result.stop_reason}）"}
    if "CassetteMiss" in result.stop_reason:
        # 回放中途请求未命中：报告不完整（降级报告），不作为基准样本。
        investigation.close()
        return {"skipped": f"回放中途 CassetteMiss（stop={result.stop_reason}）"}
    # 在 close() 前取出 store 与 dispatch→分支结果映射（close 只关闭引擎，store 仍可读，这里
    # 明确按任务要求在 close 前抓取）。
    store = investigation.toolset.registry.store
    branch_results = dict(investigation.branch_results)
    report = copy.deepcopy(result.report)
    investigation.close()
    log(started, f"{case['id']} 回放完成 stop={result.stop_reason} "
                 f"branches={len(branch_results)} 报告字段={sorted(report)[:6]}…")
    return {"store": store, "branch_results": branch_results, "report": report}


def _issue_types(check: dict) -> list[str]:
    return [item["issue"] for item in check["issues"]]


def _finding_texts(report: dict) -> list[tuple[str, dict, str]]:
    """(where, item, text) 列表：summary 与每条 dict 型 finding。"""
    texts = [("summary", None, report.get("summary") or "")]
    for index, item in enumerate(report.get("findings") or []):
        if isinstance(item, dict):
            texts.append((f"findings[{index}]", item, item.get("text") or ""))
    return texts


def make_number_mutants(report: dict, checker: ClaimChecker, rng: random.Random) -> list[dict]:
    """number 变异：把当前能匹配的数字改写为 ×1.37（同小数位，保留 %）；至多 3 个。
    另加至多 1 个 root_cause explanatory_power 变异（×1.37，6 位小数）。"""
    mutants = []
    candidates = []
    for where, item, text in _finding_texts(report):
        # 与 check_text 相同的引用解析：文字内 rN-tool ∪ 结构字段 result_ids
        cited = list(dict.fromkeys(RESULT_ID.findall(text) + list((item or {}).get("result_ids") or [])))
        known = [rid for rid in cited if checker.store.get(rid) is not None]
        if not known:
            continue
        pool, _ = checker._pool(known)
        for number in numbers_in(text):
            if _matches(number, pool):
                candidates.append((where, item, number))
    rng.shuffle(candidates)
    for where, item, number in candidates[:MAX_PER_TYPE]:
        mutated = copy.deepcopy(report)
        old = number["text"]
        decimals = number["decimals"]
        quantum = Decimal(1).scaleb(-decimals)
        new_text = str((number["value"] * Decimal(str(NUMBER_FACTOR))).quantize(quantum)) \
            + ("%" if number["percent"] else "")
        if item is None:
            if old not in mutated["summary"]:
                continue
            mutated["summary"] = mutated["summary"].replace(old, new_text, 1)
        else:
            index = int(where[len("findings["):-1])
            if old not in mutated["findings"][index]["text"]:
                continue
            mutated["findings"][index]["text"] = \
                mutated["findings"][index]["text"].replace(old, new_text, 1)
        mutants.append({"type": "number", "report": mutated,
                        "description": f"{where}：{old} → {new_text}"})
    # root_cause explanatory_power 变异（至多 1 个）
    for index, rc in enumerate(report.get("root_causes") or []):
        if not isinstance(rc, dict) or rc.get("explanatory_power") in (None, ""):
            continue
        result_ids = [str(v) for v in rc.get("result_ids") or []]
        known = [rid for rid in result_ids if checker.store.get(rid) is not None]
        if not known:
            continue
        pool, _ = checker._pool(known)
        matched = [n for n in numbers_in(str(rc["explanatory_power"])) if _matches(n, pool)]
        if not matched:
            continue
        mutated = copy.deepcopy(report)
        new_value = float(rc["explanatory_power"]) * NUMBER_FACTOR
        mutated["root_causes"][index]["explanatory_power"] = round(new_value, 6)
        mutants.append({"type": "number", "report": mutated,
                        "description": f"root_causes[{index}].explanatory_power："
                                       f"{rc['explanatory_power']} → {mutated['root_causes'][index]['explanatory_power']}"})
        break
    return mutants


INLINE_ID = re.compile(r"r\d+-[a-z_]+")


def make_delete_id_mutants(report: dict, rng: random.Random) -> list[dict]:
    """delete_id 变异：删除一条 finding 的全部 result_id 引用（结构字段与行内 rN-tool），
    只选文字含数字的 finding；另加至多 1 个删除 root_cause.result_ids 的变异。"""
    mutants = []
    candidates = []
    for index, item in enumerate(report.get("findings") or []):
        if not isinstance(item, dict):
            continue
        text = item.get("text") or ""
        if not numbers_in(text):
            continue
        if not (item.get("result_ids") or INLINE_ID.search(text)):
            continue
        candidates.append(index)
    rng.shuffle(candidates)
    for index in candidates[:MAX_PER_TYPE]:
        mutated = copy.deepcopy(report)
        item = mutated["findings"][index]
        removed = list(item.get("result_ids") or [])
        item["result_ids"] = []
        item["text"] = INLINE_ID.sub("", item.get("text") or "")
        mutants.append({"type": "delete_id", "report": mutated,
                        "description": f"findings[{index}]：删除 result_ids={removed} 及行内引用"})
    rc_candidates = [index for index, rc in enumerate(report.get("root_causes") or [])
                     if isinstance(rc, dict) and rc.get("result_ids")]
    rng.shuffle(rc_candidates)
    for index in rc_candidates[:1]:
        mutated = copy.deepcopy(report)
        removed = mutated["root_causes"][index]["result_ids"]
        mutated["root_causes"][index]["result_ids"] = []
        mutants.append({"type": "delete_id", "report": mutated,
                        "description": f"root_causes[{index}]：删除 result_ids={removed}"})
    return mutants


CAUSAL_PATTERNS = [
    ("主要来自", "由于主要贡献来自"),
    ("集中在", "由于集中在"),
]


def make_causal_mutants(report: dict, rng: random.Random) -> list[dict]:
    """causal 变异：把首个「主要来自/集中在」改为「由于…」句式，或在一条 finding 末尾
    追加「这导致了指标下降。」；至多 3 个，每个只注入一处。"""
    mutants = []
    targets = []  # (kind, 定位信息)
    summary = report.get("summary") or ""
    for old, new in CAUSAL_PATTERNS:
        if old in summary:
            targets.append(("replace", "summary", old, new))
            break
    for index, item in enumerate(report.get("findings") or []):
        if not isinstance(item, dict):
            continue
        text = item.get("text") or ""
        for old, new in CAUSAL_PATTERNS:
            if old in text:
                targets.append(("replace", index, old, new))
                break
        else:
            if text:
                targets.append(("append", index, None, None))
    rng.shuffle(targets)
    for target in targets[:MAX_PER_TYPE]:
        mutated = copy.deepcopy(report)
        kind, where, old, new = target
        if kind == "replace" and where == "summary":
            mutated["summary"] = mutated["summary"].replace(old, new, 1)
            description = f"summary：「{old}」→「{new}」"
        elif kind == "replace":
            text = mutated["findings"][where]["text"]
            mutated["findings"][where]["text"] = text.replace(old, new, 1)
            description = f"findings[{where}]：「{old}」→「{new}」"
        else:
            mutated["findings"][where]["text"] = \
                (mutated["findings"][where]["text"] or "") + "这导致了指标下降。"
            description = f"findings[{where}]：末尾追加「这导致了指标下降。」"
        mutants.append({"type": "causal", "report": mutated, "description": description})
    return mutants


def check_mutant(checker: ClaimChecker, mutant: dict, original_counts: Counter) -> dict:
    """核对一个变异报告；原报告已失败时，要求对应问题类型计数相比原报告增加才算检出。"""
    check = checker.check_report(mutant["report"])
    counts = Counter(_issue_types(check))
    expected = EXPECTED_ISSUE[mutant["type"]]
    detected = (not check["passed"]) and counts[expected] > original_counts[expected]
    return {"type": mutant["type"], "description": mutant["description"],
            "passed": check["passed"], "issues": _issue_types(check), "detected": detected}


def run_case(case: dict, folder: Path, snapshot, started: float) -> dict:
    outcome = replay_case(case, folder, snapshot, started)
    if "skipped" in outcome:
        log(started, f"{case['id']} 跳过：{outcome['skipped']}")
        return {"case": case["id"], "skipped": outcome["skipped"]}
    checker = ClaimChecker(outcome["store"], outcome["branch_results"])
    report = strip_orchestration(outcome["report"])
    original = checker.check_report(report)
    original_counts = Counter(_issue_types(original))
    rng = random.Random(SEED)
    mutants = (make_number_mutants(report, checker, rng)
               + make_delete_id_mutants(report, rng)
               + make_causal_mutants(report, rng))
    records = [check_mutant(checker, mutant, original_counts) for mutant in mutants]
    for record in records:
        record["case"] = case["id"]
    log(started, f"{case['id']} 原报告 passed={original['passed']} issues={dict(original_counts)} "
                 f"变异 {len(records)} 个 检出 {sum(1 for r in records if r['detected'])} 个")
    return {"case": case["id"],
            "original": {"passed": original["passed"], "issues": original["issues"],
                         "issue_types": _issue_types(original),
                         "checked_numbers": original["checked_numbers"]},
            "mutants": records}


def summarize(rows: list[dict]) -> dict:
    played = [row for row in rows if "skipped" not in row]
    mutants = [record for row in played for record in row["mutants"]]
    by_type = {}
    for inject_type in ("number", "delete_id", "causal"):
        picked = [record for record in mutants if record["type"] == inject_type]
        by_type[inject_type] = {
            "total": len(picked),
            "detected": sum(1 for record in picked if record["detected"]),
            "rate": round(sum(1 for record in picked if record["detected"]) / len(picked), 4)
            if picked else None,
        }
    total = len(mutants)
    detected = sum(1 for record in mutants if record["detected"])
    flagged = [row for row in played if row["original"]["issue_types"]]
    type_counts = Counter(issue for row in played for issue in row["original"]["issue_types"])
    return {"cases_replayed": len(played), "cases_skipped": len(rows) - len(played),
            "detection": {"overall": {"total": total, "detected": detected,
                                      "rate": round(detected / total, 4) if total else None},
                          "by_type": by_type},
            "original_reports": {"flagged": len(flagged), "total": len(played),
                                 "flagged_rate": round(len(flagged) / len(played), 4) if played else None,
                                 "issue_type_counts": dict(type_counts)}}


def main():
    started = time.time()
    data = investigate.load_cases()
    snapshot = investigate.snapshot_of(data)
    base = investigate.base_folder_of(data)
    dev = [case for case in data["cases"] if case.get("split") == "dev"]
    log(started, f"dev 案例 {len(dev)} 个；基础版本 {base.name}")
    folders = {case["id"]: investigate.folder_of(case, base) for case in dev}

    with ThreadPoolExecutor(max_workers=5) as pool:
        rows = list(pool.map(lambda case: run_case(case, folders[case["id"]], snapshot, started), dev))
    rows.sort(key=lambda row: row["case"])

    result = {"meta": {"date": time.strftime("%Y-%m-%d %H:%M"),
                       "note": "离线，回放 A5 串行录制，不调用模型",
                       "seed": SEED, "factor": NUMBER_FACTOR, "max_per_type": MAX_PER_TYPE,
                       "cases": [case["id"] for case in dev]},
              "rows": rows,
              "summary": summarize(rows)}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log(started, f"结果写入 {OUTPUT}")

    summary = result["summary"]
    log(started, f"回放 {summary['cases_replayed']} 个，跳过 {summary['cases_skipped']} 个")
    detection = summary["detection"]
    log(started, f"检出率 overall {detection['overall']['detected']}/{detection['overall']['total']}"
                 f" = {detection['overall']['rate']}")
    for inject_type, item in detection["by_type"].items():
        log(started, f"  {inject_type}: {item['detected']}/{item['total']} = {item['rate']}")
    original = summary["original_reports"]
    log(started, f"原报告被标记 {original['flagged']}/{original['total']}"
                 f" = {original['flagged_rate']}，问题类型分布 {original['issue_type_counts']}")


if __name__ == "__main__":
    main()
