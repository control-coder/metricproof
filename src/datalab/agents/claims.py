"""报告核验（A6）：代码逐条核对报告里的数字、result_id 引用与因果断言，不依赖模型。

计划第四节：报告中的每个数字都必须引用 result_id；代码核对文字里的数字与所引用的结果
是否一致，不一致即判为输出无效。审查 Agent 通过 verify_claims 工具调用同一套检查，
编排层在审查前后也各跑一次，结论不以模型自我审批为准。

核对单元：summary、每条 finding、每条 recommendation、每条 root_cause。单元引用的
result_id = 文字中括注的 rN-tool ∪ 结构字段 result_ids。dispatch_subagent 的引用展开为该
分支子 Agent 产生的工具结果（子 Agent 的发现本身是模型写的，不能当作数字来源）。

问题类型（issue）：
- unknown_result_id：引用的 result_id 不存在；
- missing_result_id：root_cause 没有 result_ids，或单元含数字却没有任何引用；
- number_mismatch：数字在所引用结果的数值里找不到；
- value_not_found：root_cause 的维度取值不在所引用结果里；
- causal_assertion：出现因果断言用词。

数字匹配规则（容忍正常的书写差异，不放过改动）：
- 先去掉 result_id、日期（2018 年 3 月、2018-03-01 等）与含字母或下划线的标识
  （payment_installments=10、100_200、卖家 ID）；不带小数点与百分号、绝对值 ≤ 31 的整数
  视为序号或计数（「3 个维度」「第 1 步」），不核对。
- 后面紧跟省略号的数字是截断的标识（「卖家 53243585…」），不核对。
- 按绝对值比较（「下降 713.05」对应 −713.05）；带 % 或「个百分点」的数先除以 100；金额允许分↔元
  （结果值除以 100）。
- 容差为报告数字最后一位的一个单位（如 14629.09 对 14629.087450），兼容四舍五入与截断。
- 找不到时，有效数字 ≥ 3 位的数字还可等于所引结果中两个数之和或之差（模型自行求和、求差），
  计入 derived_numbers，不算问题。
- result_id 允许缩写（r5、r5/r17、r5-r13），仅当 store 中以该序号开头的结果恰有一个时才采纳。
"""
from __future__ import annotations

import copy
import json
import re
from bisect import bisect_left
from decimal import Decimal, InvalidOperation

CAUSAL_WORDS = ("导致", "因为", "由于", "使得")
# 末尾禁止紧跟字母数字，避免把「r5-r13」误读成 result_id「r5-r」
RESULT_ID = re.compile(r"r\d+(?:\.\d+)?-[a-z_]+(?![A-Za-z0-9])")
# 缩写引用：r5、r5/r17、r5-r13；不跟「-工具名」，前面不能是字母数字（排除型号、单词内部）
_SHORT_REF = re.compile(r"(?<![A-Za-z0-9_])r(\d+)(?![A-Za-z0-9_])(?!\.\d)(?!-[a-z_]+(?![A-Za-z0-9_]))")
_SENTENCE = re.compile(r"[^。；;]+[。；;]?|[。；;]")
EMPTY_SUMMARY = "（结论摘要中的数字均未通过核对，已剔除）"
_DATE = re.compile(r"\d{4}-\d{1,2}(?:-\d{1,2})?(?:T[\d:]+)?|\d{4}\s*年(?:\s*\d{1,2}\s*月)?(?:\s*\d{1,2}\s*日)?"
                   r"|\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\s*=\s*[A-Za-z0-9_.\-]+")
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])([-+−]?)(\d[\d,]*(?:\.\d+)?)(\s*(?:%|个百分点))?(?![A-Za-z0-9_…]|\.\.)")


def numbers_in(text: str) -> list[dict]:
    """抽取文字中需要核对的数字：{text, value(Decimal, 绝对值), percent, decimals}。"""
    cleaned = RESULT_ID.sub(" ", text or "")
    cleaned = _DATE.sub(" ", cleaned)
    cleaned = _IDENT.sub(" ", cleaned)
    found = []
    for match in _NUMBER.finditer(cleaned):
        digits = match.group(2).replace(",", "")
        try:
            value = Decimal(digits)
        except InvalidOperation:
            continue
        percent = bool(match.group(3))
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        if not percent and decimals == 0 and value <= 31:
            continue
        found.append({"text": match.group(0).strip(), "value": value, "percent": percent,
                      "decimals": decimals})
    return found


def _leaves(node, numbers: set, strings: set, depth: int = 0):
    if depth > 12:
        return
    if isinstance(node, dict):
        for value in node.values():
            _leaves(value, numbers, strings, depth + 1)
    elif isinstance(node, list):
        for value in node:
            _leaves(value, numbers, strings, depth + 1)
    elif isinstance(node, bool) or node is None:
        return
    elif isinstance(node, (int, float)):
        numbers.add(abs(Decimal(str(node))))
    elif isinstance(node, str):
        strings.add(node)
        try:
            numbers.add(abs(Decimal(node.strip())))
        except InvalidOperation:
            pass


def _matches(number: dict, pool: set) -> bool:
    target = number["value"] / 100 if number["percent"] else number["value"]
    unit = Decimal(1).scaleb(-number["decimals"])
    tolerance = unit / 100 if number["percent"] else unit
    for value in pool:
        if abs(value - target) <= tolerance or abs(value / 100 - target) <= tolerance:
            return True
    return False


def _significant_digits(number: dict) -> int:
    return len(re.sub(r"\D", "", number["text"]).lstrip("0"))


class _DerivedIndex:
    """数值池的有序副本，用于判断数字是否为池中两数之和或之差，单个数字 O(n log n)。"""

    def __init__(self, pool: set):
        base = sorted(pool)
        self.scales = [base, [value / 100 for value in base]]

    def matches(self, number: dict) -> bool:
        target = number["value"] / 100 if number["percent"] else number["value"]
        unit = Decimal(1).scaleb(-number["decimals"])
        tolerance = unit / 100 if number["percent"] else unit
        if target <= 0:
            return False
        for values in self.scales:
            for index, a in enumerate(values):
                # 和：a + b = target，b 取排序位置在 a 之后的值，避免同一个数加自身
                if self._has(values, target - a, tolerance, index + 1):
                    return True
                # 差：a - b = target 或 b - a = target，目标为正则 a 与 b 必然不同位
                if self._has(values, a - target, tolerance, 0) or self._has(values, a + target, tolerance, 0):
                    return True
        return False

    @staticmethod
    def _has(values: list, goal: Decimal, tolerance: Decimal, start: int) -> bool:
        position = bisect_left(values, goal - tolerance, start)
        return position < len(values) and values[position] <= goal + tolerance


class ClaimChecker:
    """对一个 ResultStore 做报告核对；branch_results 给出 dispatch 引用到子 Agent 结果的展开。"""

    def __init__(self, store, branch_results: dict[str, list[str]] | None = None):
        self.store = store
        self.branch_results = branch_results or {}
        self._pools: dict[str, tuple[set, set]] = {}

    def expand(self, result_id: str) -> list[str]:
        """dispatch_subagent 的引用展开为子 Agent 产生的结果；其余原样。"""
        if result_id in self.branch_results:
            return list(self.branch_results[result_id])
        return [result_id]

    def _pool(self, result_ids: list[str]) -> tuple[set, set]:
        numbers, strings = set(), set()
        for result_id in result_ids:
            for real in self.expand(result_id):
                if real not in self._pools:
                    entry = self.store.get(real)
                    pool = (set(), set())
                    if entry is not None:
                        _leaves(entry.get("result"), *pool)
                    self._pools[real] = pool
                numbers |= self._pools[real][0]
                strings |= self._pools[real][1]
        return numbers, strings

    def short_refs(self, text: str) -> list[str]:
        """把「r5」「r5/r17」「r5-r13」这类缩写解析为 result_id；序号在 store 中不唯一或不存在则忽略。"""
        results = getattr(self.store, "_results", None) or {}
        found = []
        for match in _SHORT_REF.finditer(text or ""):
            prefix = f"r{match.group(1)}-"
            candidates = [key for key in results if key.startswith(prefix)]
            if len(candidates) == 1:
                found.append(candidates[0])
        return found

    def check_text(self, text: str, result_ids: list[str] | None = None, *, where: str = "claim") -> list[dict]:
        """核对一段文字：引用存在、数字可在引用结果中找到、无因果断言。"""
        return self._check_text(text, result_ids, where)[0]

    def _check_text(self, text: str, result_ids: list[str] | None, where: str) -> tuple[list[dict], int]:
        """返回 (issues, 派生数字个数)；派生数字指等于所引结果中两个数之和或之差的数字。"""
        issues = []
        derived = 0
        cited = list(dict.fromkeys(RESULT_ID.findall(text or "") + self.short_refs(text)
                                   + list(result_ids or [])))
        known = []
        for result_id in cited:
            if self.store.get(result_id) is None:
                issues.append({"where": where, "issue": "unknown_result_id", "result_id": result_id})
            else:
                known.append(result_id)
        numbers = numbers_in(text)
        if numbers and not cited:
            issues.append({"where": where, "issue": "missing_result_id",
                           "numbers": [item["text"] for item in numbers[:5]]})
        elif numbers and known:
            pool, _ = self._pool(known)
            index = None
            for number in numbers:
                if _matches(number, pool):
                    continue
                if _significant_digits(number) >= 3:
                    index = index or _DerivedIndex(pool)
                    if index.matches(number):
                        derived += 1
                        continue
                issues.append({"where": where, "issue": "number_mismatch", "number": number["text"],
                               "result_ids": known})
        for word in CAUSAL_WORDS:
            if word in (text or ""):
                issues.append({"where": where, "issue": "causal_assertion", "word": word})
        return issues, derived

    def check_root_cause(self, item, index: int) -> list[dict]:
        where = f"root_causes[{index}]"
        if not isinstance(item, dict):
            return self.check_text(str(item), where=where)
        result_ids = [str(value) for value in item.get("result_ids") or []]
        if not result_ids:
            return [{"where": where, "issue": "missing_result_id"}]
        issues = []
        known = []
        for result_id in result_ids:
            if self.store.get(result_id) is None:
                issues.append({"where": where, "issue": "unknown_result_id", "result_id": result_id})
            else:
                known.append(result_id)
        if not known:
            return issues
        pool, strings = self._pool(known)
        if item.get("value") is not None and str(item["value"]) not in strings:
            issues.append({"where": where, "issue": "value_not_found", "value": str(item["value"])})
        for key in ("explanatory_power", "contribution"):
            raw = item.get(key)
            if raw in (None, ""):
                continue
            for number in numbers_in(str(raw)) or []:
                if not _matches(number, pool):
                    issues.append({"where": where, "issue": "number_mismatch", "field": key,
                                   "number": number["text"], "result_ids": known})
        return issues

    def check_report(self, report: dict | None) -> dict:
        """整份报告核对：返回 {passed, issues, checked_numbers, derived_numbers}；passed 为 False 即输出无效。"""
        if not report:
            return {"passed": False, "issues": [{"where": "report", "issue": "missing_report"}],
                    "checked_numbers": 0, "derived_numbers": 0}
        issues = []
        texts = [("summary", report.get("summary") or "", [])]
        for index, item in enumerate(report.get("findings") or []):
            if isinstance(item, dict):
                texts.append((f"findings[{index}]", item.get("text") or "", item.get("result_ids") or []))
            else:
                texts.append((f"findings[{index}]", str(item), []))
        for index, item in enumerate(report.get("recommendations") or []):
            texts.append((f"recommendations[{index}]", str(item), []))
        checked = 0
        derived = 0
        for where, text, ids in texts:
            checked += len(numbers_in(text))
            found, count = self._check_text(text, ids, where)
            issues.extend(found)
            derived += count
        for index, item in enumerate(report.get("root_causes") or []):
            issues.extend(self.check_root_cause(item, index))
        return {"passed": not issues, "issues": issues, "checked_numbers": checked,
                "derived_numbers": derived}


def _unit_text(report: dict, where: str) -> str:
    match = re.fullmatch(r"(\w+)\[(\d+)\]", where)
    if not match:
        return str(report.get(where) or "")
    item = (report.get(match.group(1)) or [])[int(match.group(2))]
    if isinstance(item, dict):
        return str(item.get("text")) if match.group(1) == "findings" and item.get("text") else \
            json.dumps(item, ensure_ascii=False)
    return str(item)


def _sentence_flagged(sentence: str, issues: list[dict]) -> str | None:
    """句子含问题数字、问题引用或因果词时，返回问题类型。"""
    sentence_numbers = {item["text"] for item in numbers_in(sentence)}
    for issue in issues:
        kind = issue.get("issue")
        if kind == "number_mismatch" and issue.get("number") in sentence_numbers:
            return kind
        if kind == "missing_result_id" and sentence_numbers:
            return kind
        if kind == "unknown_result_id" and issue.get("result_id") and issue["result_id"] in sentence:
            return kind
        if kind == "causal_assertion" and issue.get("word") and issue["word"] in sentence:
            return kind
    return None


def redact_report(report: dict, issues: list[dict]) -> tuple[dict, list[dict]]:
    """按 issues 剔除有问题的内容：返回（新报告, 被剔除内容列表）；不修改原对象，也不判定有效性。

    findings、recommendations、root_causes 按 where 下标整条删除；summary 按「。；;」切句，
    只删含问题数字、问题引用或因果词的句子。调用方剔除后需再跑一次 check_report。
    """
    result = copy.deepcopy(report or {})
    removed = []
    by_where: dict[str, list[dict]] = {}
    for issue in issues or []:
        by_where.setdefault(str(issue.get("where")), []).append(issue)
    drop: dict[str, set[int]] = {}
    for where, items in by_where.items():
        match = re.fullmatch(r"(findings|recommendations|root_causes)\[(\d+)\]", where)
        if not match or not isinstance(result.get(match.group(1)), list):
            continue
        index = int(match.group(2))
        if index >= len(result[match.group(1)]):
            continue
        drop.setdefault(match.group(1), set()).add(index)
        removed.append({"where": where, "text": _unit_text(report, where),
                        "issues": list(dict.fromkeys(str(item.get("issue")) for item in items))})
    for key, indexes in drop.items():
        result[key] = [item for index, item in enumerate(result[key]) if index not in indexes]
    summary_issues = by_where.get("summary")
    if summary_issues and result.get("summary"):
        kept, dropped, kinds = [], [], []
        for sentence in _SENTENCE.findall(str(result["summary"])):
            kind = _sentence_flagged(sentence, summary_issues)
            if kind:
                dropped.append(sentence)
                kinds.append(kind)
            else:
                kept.append(sentence)
        if dropped:
            removed.append({"where": "summary", "text": "".join(dropped).strip(),
                            "issues": list(dict.fromkeys(kinds))})
            result["summary"] = "".join(kept).strip() or EMPTY_SUMMARY
    return result, removed


def review_view(report: dict) -> str:
    """审查 Agent 的输入视图：报告 JSON（不含编排层附加的核验字段）。"""
    hidden = {"verification", "causal_assertions", "review", "claims_check", "valid"}

    def strip(node):
        if isinstance(node, dict):
            return {key: strip(value) for key, value in node.items() if key not in hidden}
        if isinstance(node, list):
            return [strip(value) for value in node]
        return node

    return json.dumps(strip(report), ensure_ascii=False)
