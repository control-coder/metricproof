"""run_sql 结果的 L2 部分核验（A8）：不变量、蜕变关系与汇总比对。

自由查询没有独立参考实现可复算，只能检查与查询语义无关的必要条件：
- 不变量：列名唯一、每行列数一致、数值有限、行数不超过 LIMIT；
- 蜕变关系（行序置换不变）：同一容器内在行序打乱的表上再执行一次，结果作为多重集合必须相同。
  没有 ORDER BY 或排序键有并列时 LIMIT 截取到的行依赖行序，此时判为不通过是预期行为；
- 载入比对：沙箱 DuckDB 载入的每表行数等于宿主投影的行数；
- 汇总比对（模型给出 reconcile 时）：结果某列之和等于语义层加法型指标在同一期间的值，
  允许分↔元的书写差异。结果行数达到 LIMIT 时可能被截断，不做比对，判为不通过。
"""
from __future__ import annotations

import math
from collections import Counter
from decimal import Decimal, InvalidOperation

NON_FINITE = ('NaN', 'Infinity', '-Infinity')


def _cell_key(value):
    # 置换后浮点求和的顺序不同，末位可能不同：浮点按 10 位有效数字比较。
    if isinstance(value, float):
        return f'{value:.10g}'
    return repr(value)


def _rows_key(rows: list) -> Counter:
    return Counter(tuple(_cell_key(cell) for cell in row) for row in rows)


def _decimal(value) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def check_sql_result(output: dict, limit: int) -> dict:
    """返回 {passed, checks: {name: {passed, detail}}}；reconcile 由 reconcile_check 另行补充。"""
    columns = list(output.get('columns') or [])
    rows = list(output.get('rows') or [])
    checks = {}
    problems = []
    if len(set(columns)) != len(columns):
        problems.append('列名重复')
    if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
        problems.append('行的列数与列名不一致')
    if any(cell in NON_FINITE or (isinstance(cell, float) and not math.isfinite(cell))
           for row in rows if isinstance(row, list) for cell in row):
        problems.append('含非有限数值')
    if len(rows) > limit:
        problems.append('行数超过 LIMIT')
    checks['invariants'] = {'passed': not problems, 'detail': problems}
    permuted = list(output.get('permuted_rows') or [])
    same = _rows_key(rows) == _rows_key(permuted)
    checks['permutation'] = {'passed': same, 'detail': [] if same else
                             ['行序打乱后结果不同（缺少确定的 ORDER BY 或排序键并列时 LIMIT 结果依赖行序）']}
    loaded, host = output.get('table_counts') or {}, output.get('host_counts') or {}
    same_counts = bool(host) and {key: int(value) for key, value in loaded.items()} == dict(host)
    checks['load'] = {'passed': same_counts, 'detail': [] if same_counts else
                      [{'sandbox': loaded, 'host': host}]}
    return {'passed': all(item['passed'] for item in checks.values()), 'checks': checks}


def reconcile_check(output: dict, limit: int, column: str, expected: int, unit: str) -> dict:
    """结果列 column 之和 vs 语义层加法型指标值 expected（整数，单位 unit）。"""
    columns = list(output.get('columns') or [])
    rows = list(output.get('rows') or [])
    if column not in columns:
        return {'passed': False, 'detail': [f'结果中没有列 {column}']}
    if len(rows) >= limit:
        return {'passed': False, 'detail': ['结果行数达到 LIMIT，可能被截断，不能比对汇总值']}
    index = columns.index(column)
    total = Decimal(0)
    for row in rows:
        if row[index] is None:
            continue
        number = _decimal(row[index])
        if number is None:
            return {'passed': False, 'detail': [f'列 {column} 含非数值']}
        total += number
    candidates = [total]
    if unit == 'cents':
        candidates.append(total * 100)
    matched = any(abs(candidate - expected) <= Decimal('0.5') for candidate in candidates)
    detail = {'column_sum': str(total), 'semantic_value': expected, 'unit': unit}
    return {'passed': matched, 'detail': [] if matched else ['列之和与语义层值不一致'], 'values': detail}
