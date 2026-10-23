"""Analyst 代码的服务端框架：计划常量、CSV 读写、编码与公式注入防护由服务端固定，模型只写 analyze 函数。

组装后的脚本仍在同一受限容器中执行并接受独立核验，框架不改变安全边界，只减少模型需要生成的确定性代码。
"""
import ast
import json

HEAD = '''import csv
import json
from collections import defaultdict
from decimal import Decimal

PLAN = json.loads(__PLAN__)


def selected(data, plan, start, end):
    return [row for row in data if row["status"] in plan["included_statuses"]
            and (start is None or row["payment_time"] >= start) and (end is None or row["payment_time"] < end)]


def group_key(row, group_by):
    return row["payment_time"][:10] if group_by == "payment_date" else row[group_by]


def ratio(numerator, denominator):
    return None if denominator == 0 else format(Decimal(numerator) / Decimal(denominator), ".6f")

# --- 模型代码开始 ---
'''

TAIL = '''
# --- 模型代码结束 ---


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\\t", "\\r")):
        return "'" + value
    return value


def _shape(result, kind):
    # 只删除当前分析类型不应出现的键（非 share 的行级 share、行内 comparison、非 comparison 的顶层 comparison），
    # 不改任何值、不补缺失的键；数值仍由独立参考逐项核验。
    if not isinstance(result, dict):
        return result
    if kind != "comparison":
        result.pop("comparison", None)
    rows = result.get("rows")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                row.pop("comparison", None)
                if kind != "share":
                    row.pop("share", None)
    return result


def _load():
    # 单表：订单行列表；Olist 多表：表名 → 行列表，只含服务端按计划投影的表与列。值都是字符串。
    if PLAN.get("schema", "orders") != "olist":
        with open("/input/orders.csv", encoding="utf-8", newline="") as _handle:
            return list(csv.DictReader(_handle))
    import os
    _tables = {}
    for _name in sorted(os.listdir("/input")):
        if _name.endswith(".csv"):
            with open("/input/" + _name, encoding="utf-8", newline="") as _handle:
                _tables[_name[:-4]] = list(csv.DictReader(_handle))
    return _tables


_data = _load()
_result = _shape(analyze(_data, dict(PLAN)), PLAN["kind"])
with open("/output/result.json", "w", encoding="utf-8") as _handle:
    json.dump(_result, _handle, ensure_ascii=False)
if PLAN["kind"] == "quality":
    _table = [{"row_count": _result.get("row_count")}]
else:
    _table = _result.get("rows") or [_result.get("total")]
with open("/output/table.csv", "w", encoding="utf-8-sig", newline="") as _handle:
    _writer = csv.DictWriter(_handle, fieldnames=list(_table[0]))
    _writer.writeheader()
    _writer.writerows({key: _cell(value) for key, value in row.items()} for row in _table)
'''


def assemble(plan: dict, code: str) -> str:
    # 计划以 JSON 字符串字面量写入，模型输出不能改变计划常量。
    return HEAD.replace('__PLAN__', repr(json.dumps(plan, ensure_ascii=False))) + code + TAIL


def rebind(script: str, changes: dict) -> str | None:
    """把框架脚本中的计划常量替换部分字段后重新组装，模型代码原样保留；非框架脚本返回 None。

    只供服务端蜕变核验改写期间边界；计划常量按字面量解析，不执行脚本内容。
    """
    prefix, marker = HEAD.split('__PLAN__')
    if not (script.startswith(prefix) and script.endswith(TAIL) and marker in script):
        return None
    try:
        plan = json.loads(ast.literal_eval(script[len(prefix):script.index(marker)]))
    except (ValueError, SyntaxError):
        return None
    if not isinstance(plan, dict):
        return None
    return assemble({**plan, **changes}, model_part(script))


def model_part(script: str) -> str:
    """从组装后的脚本取回模型自己写的部分，用于修正反馈；非框架脚本原样返回。"""
    prefix, marker = HEAD.split('__PLAN__')
    if script.startswith(prefix) and script.endswith(TAIL) and marker in script:
        return script[script.index(marker) + len(marker):-len(TAIL)]
    return script
