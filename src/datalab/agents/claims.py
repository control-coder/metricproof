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
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation

CAUSAL_WORDS = ("导致", "因为", "由于", "使得")
RESULT_ID = re.compile(r"r\d+-[a-z_]+")
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

    def check_text(self, text: str, result_ids: list[str] | None = None, *, where: str = "claim") -> list[dict]:
        """核对一段文字：引用存在、数字可在引用结果中找到、无因果断言。"""
        issues = []
        cited = list(dict.fromkeys(RESULT_ID.findall(text or "") + list(result_ids or [])))
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
            for number in numbers:
                if not _matches(number, pool):
                    issues.append({"where": where, "issue": "number_mismatch", "number": number["text"],
                                   "result_ids": known})
        for word in CAUSAL_WORDS:
            if word in (text or ""):
                issues.append({"where": where, "issue": "causal_assertion", "word": word})
        return issues

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
        """整份报告核对：返回 {passed, issues, checked_numbers}；passed 为 False 即输出无效。"""
        if not report:
            return {"passed": False, "issues": [{"where": "report", "issue": "missing_report"}],
                    "checked_numbers": 0}
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
        for where, text, ids in texts:
            checked += len(numbers_in(text))
            issues.extend(self.check_text(text, ids, where=where))
        for index, item in enumerate(report.get("root_causes") or []):
            issues.extend(self.check_root_cause(item, index))
        return {"passed": not issues, "issues": issues, "checked_numbers": checked}


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
