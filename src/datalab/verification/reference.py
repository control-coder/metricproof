"""使用 SQL 聚合独立复算，不导入分析脚本或执行其代码。"""
import csv
import sqlite3
from decimal import Decimal
from pathlib import Path

from datalab.contracts import AnalysisPlan, VerificationReport
from datalab.verification.invariants import group_total


def _fraction(numerator: int, denominator: int) -> str | None:
    return None if not denominator else format(Decimal(numerator) / Decimal(denominator), ".6f")


def reference_result(path: Path, plan: AnalysisPlan) -> dict:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        db.execute("CREATE TABLE orders (order_id TEXT, payment_time TEXT, amount_cents INTEGER, status TEXT, channel TEXT, category TEXT)")
        with path.open(encoding="utf-8", newline="") as handle:
            db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
                           ((row["order_id"], row["payment_time"], int(row["amount_cents"]), row["status"], row["channel"], row["category"])
                            for row in csv.DictReader(handle)))
        if plan.kind == "quality":
            row = db.execute("SELECT COUNT(*) AS n, COUNT(*) - COUNT(DISTINCT order_id) AS duplicates FROM orders").fetchone()
            counts = dict(db.execute("SELECT status, COUNT(*) FROM orders GROUP BY status").fetchall())
            missing = db.execute("SELECT COALESCE(SUM((order_id='')+(payment_time='')+(status='')+(channel='')+(category='')),0) FROM orders").fetchone()[0]
            return {"kind": "quality", "row_count": row["n"], "duplicate_orders": row["duplicates"],
                    "missing_values": missing, "status_counts": {status: counts.get(status, 0) for status in ("paid", "refunded", "cancelled")}}

        def where(start, end):
            clause = "status IN (" + ",".join("?" for _ in plan.included_statuses) + ")"
            args = list(plan.included_statuses)
            if start:
                clause += " AND payment_time >= ?"
                args.append(start)
            if end:
                clause += " AND payment_time < ?"
                args.append(end)
            return clause, args

        clause, args = where(plan.start, plan.end)
        total = dict(db.execute(f"SELECT COUNT(*) AS order_count, COALESCE(SUM(amount_cents),0) AS amount_cents FROM orders WHERE {clause}", args).fetchone())
        rows = []
        if plan.group_by:
            group = {"payment_date": "substr(payment_time,1,10)", "channel": "channel", "category": "category"}[plan.group_by]
            sort = 'amount_cents DESC, "group"' if plan.kind == "ranking" else '"group"'
            query = f'SELECT {group} AS "group", COUNT(*) AS order_count, SUM(amount_cents) AS amount_cents FROM orders WHERE {clause} GROUP BY {group} ORDER BY {sort}'
            rows = [dict(row) for row in db.execute(query, args)]
            if plan.kind == "share":
                for row in rows:
                    row["share"] = _fraction(row["amount_cents"], total["amount_cents"])
        result = {"kind": plan.kind, "total": total, "rows": rows}
        if plan.kind == "comparison":
            previous_clause, previous_args = where(plan.previous_start, plan.previous_end)
            previous = db.execute(f"SELECT COUNT(*), COALESCE(SUM(amount_cents),0) FROM orders WHERE {previous_clause}", previous_args).fetchone()
            delta = total["amount_cents"] - previous[1]
            result["comparison"] = {"previous_order_count": previous[0], "previous_amount_cents": previous[1],
                                    "delta_cents": delta, "growth": _fraction(delta, previous[1])}
        return result
    finally:
        db.close()


MAX_DIFFS = 12


def _kind(value) -> str:
    return {bool: "bool", int: "int", str: "str", type(None): "null", list: "list", dict: "object"}.get(type(value), "other")


def differences(expected, actual, path: str = "") -> list[dict]:
    """列出不一致的字段路径与问题类型，只描述位置和类型，不含期望值或实际值。"""
    if _kind(expected) != _kind(actual):
        # 期望类型来自输出契约本身（提示中已写明），不泄露数值。
        return [{"path": path or "$", "issue": "type", "expected_type": _kind(expected)}]
    if isinstance(expected, dict):
        found = [{"path": f"{path}.{key}".lstrip("."), "issue": "missing"} for key in expected if key not in actual]
        found += [{"path": f"{path}.{key}".lstrip("."), "issue": "unexpected"} for key in actual if key not in expected]
        for key in expected:
            if key in actual:
                found += differences(expected[key], actual[key], f"{path}.{key}".lstrip("."))
        return found
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return [{"path": path, "issue": "length"}]
        return [item for index, (want, got) in enumerate(zip(expected, actual))
                for item in differences(want, got, f"{path}[{index}]")]
    return [] if expected == actual else [{"path": path or "$", "issue": "value"}]


def verify(path: Path, plan: AnalysisPlan, actual: dict) -> VerificationReport:
    expected = reference_result(path, plan)
    # JSON 值类型也参与核验，拒绝 bool==int 之类的宽松等价。
    diffs = differences(expected, actual)
    matched = not diffs
    checks = [{"name": "independent_reference", "passed": matched,
               "detail": "独立 SQL 参考与结果逐项一致" if matched else "结果与独立参考不一致"}]
    if diffs:
        checks[0]["issues"] = diffs[:MAX_DIFFS]
        checks[0]["issue_count"] = len(diffs)
    if plan.group_by and plan.kind != "quality":
        checks.append({"name": "group_total", "passed": group_total(actual)})
    return VerificationReport(all(check["passed"] for check in checks), checks, expected)
