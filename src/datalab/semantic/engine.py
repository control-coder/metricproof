"""语义层引擎：把规范化表一次性载入内存 SQLite 并缓存，按编译器生成的 SQL 执行查询。

同一任务多次查询复用同一连接；数据版本目录没有 sellers.csv（旧签名导入的版本）时，
使用卖家维度给出清楚错误「该数据版本导入时没有卖家表，请重新导入」。
比值为 6 位小数字符串（与 verification._fraction 一致，分母 0 为 null），金额为整数分。
"""
from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path
import sqlite3

from datalab.datasets import olist
from datalab.semantic.compiler import compile_query
from datalab.semantic.errors import SemanticError
from datalab.semantic.metrics import ADDITIVE, components, get_metric
from datalab.semantic.snapshot import SemanticSnapshot

INTEGER_COLUMNS = {"price_cents", "freight_cents", "payment_value_cents", "payment_installments"}
# 各粒度需要的规范化表；订单级按金额来源用 payments 或 items 子表，两张都载入，与编译器 LEFT JOIN 一致。
GRAIN_TABLES = {
    "order": ("orders", "customers", "order_payments", "order_items"),
    "item": ("order_items", "orders", "customers", "products", "category_translation", "sellers"),
    "review": ("order_reviews", "orders", "customers"),
    "customer": ("orders", "customers"),
}
# 载入后建索引的连接键（列固定，见 datasets/olist.STRICT_KEYS）。
_INDEX_KEYS = {
    "orders": (("orders", "customer_id"),),
    "customers": (("customers", "customer_id"), ("customers", "customer_unique_id")),
    "order_items": (("order_items", "order_id"), ("order_items", "product_id"), ("order_items", "seller_id")),
    "order_payments": (("order_payments", "order_id"),),
    "order_reviews": (("order_reviews", "order_id"),),
    "products": (("products", "product_id"),),
    "sellers": (("sellers", "seller_id"),),
}


def _fraction(numerator: int, denominator: int) -> str | None:
    return None if not denominator else format(Decimal(numerator) / Decimal(denominator), ".6f")


class SemanticEngine:
    """folder 为数据版本目录；snapshot 为已确认口径快照。"""

    def __init__(self, folder: Path, snapshot: SemanticSnapshot):
        self.folder = Path(folder)
        self.snapshot = snapshot
        self._db: sqlite3.Connection | None = None
        self._loaded: set[str] = set()

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _load(self, name: str):
        if name in self._loaded:
            return
        path = self.folder / f"{name}.csv"
        if not path.is_file():
            if name == "sellers":
                raise SemanticError("该数据版本导入时没有卖家表，请重新导入（含 sellers.csv）后再使用卖家维度")
            raise SemanticError(f"数据版本缺少规范化表 {name}.csv")
        db = self._connection()
        columns = olist.columns(name)
        definition = ", ".join(f'"{column}" {"INTEGER" if column in INTEGER_COLUMNS else "TEXT"}' for column in columns)
        db.execute(f'CREATE TABLE "{name}" ({definition})')
        with path.open(encoding="utf-8", newline="") as handle:
            db.executemany(f'INSERT INTO "{name}" VALUES ({", ".join("?" for _ in columns)})',
                           ([int(row[column]) if column in INTEGER_COLUMNS and row[column] != "" else row[column]
                             for column in columns] for row in csv.DictReader(handle)))
        # 内存 SQLite 没有自动索引；给固定连接键建索引，否则真实数据上嵌套循环不可接受。
        for index, (table, column) in enumerate(_INDEX_KEYS.get(name, ())):
            db.execute(f'CREATE INDEX "ix_{name}_{index}" ON "{name}" ("{column}")')
        self._loaded.add(name)

    def _connection(self) -> sqlite3.Connection:
        if self._db is None:
            self._db = sqlite3.connect(":memory:")
        return self._db

    def _prepare(self, metric_name: str):
        metric = get_metric(metric_name)
        for name in sorted(GRAIN_TABLES[metric.grain]):
            self._load(name)
        return metric

    def query(self, metric_name: str, period: tuple[str | None, str | None],
              compare_period: tuple[str | None, str | None] | None = None,
              filters: dict | None = None, group_by: str | None = None) -> dict:
        """执行语义层查询，返回结构化结果；compare_period 给出时附 compare 与 delta。"""
        metric = self._prepare(metric_name)
        result = self._run(metric, period, filters, group_by)
        if compare_period is not None:
            compare = self._run(metric, compare_period, filters, group_by)
            result["compare"] = compare
            result["delta"] = _delta(result["total"], compare["total"])
        return result

    def _run(self, metric, period, filters, group_by) -> dict:
        db = self._connection()
        # 总计与分组分别编译执行：分组行加总不一定等于总计（如不同订单数按组去重）。
        total_parts = self._parts(db, compile_query(metric.name, period, filters, None, self.snapshot))
        grouped: dict[str, dict[str, int]] = {}
        if group_by:
            for component, sql, params in compile_query(metric.name, period, filters, group_by, self.snapshot):
                for grp, value in db.execute(sql, params).fetchall():
                    grouped.setdefault(str(grp), {})[component] = int(value or 0)
        numerator, denominator = components(metric)
        total = _assemble(metric, total_parts, numerator, denominator)
        rows = [{"group": grp, **_assemble(metric, parts, numerator, denominator)}
                for grp, parts in sorted(grouped.items())]
        return {"metric": metric.name, "grain": metric.grain, "type": metric.kind, "unit": metric.unit,
                "period": {"start": period[0], "end": period[1]}, "total": total, "rows": rows}

    @staticmethod
    def _parts(db, queries) -> dict[str, int]:
        parts: dict[str, int] = {}
        for component, sql, params in queries:
            parts[component] = int(db.execute(sql, params).fetchone()[0] or 0)
        return parts


def _assemble(metric, parts: dict[str, int], numerator: str, denominator: str | None) -> dict:
    """由成分整数组装总计或一行：加法型 value 即分子；比率型给出分子、分母与比值。"""
    if metric.kind == ADDITIVE:
        return {"numerator": parts.get(metric.name, 0), "denominator": None, "value": parts.get(metric.name, 0)}
    top, bottom = parts.get(numerator, 0), parts.get(denominator, 0)
    return {"numerator": top, "denominator": bottom, "value": _fraction(top, bottom)}


def _delta(total: dict, compare: dict) -> dict:
    """本期与对比期总计之差；加法型为整数差，比率型为两个 6 位小数字符串之差。"""
    if total["denominator"] is None:
        return {"numerator": total["numerator"] - compare["numerator"], "denominator": None,
                "value": total["value"] - compare["value"]}
    value = None
    if total["value"] is not None and compare["value"] is not None:
        value = format(Decimal(total["value"]) - Decimal(compare["value"]), ".6f")
    return {"numerator": total["numerator"] - compare["numerator"],
            "denominator": total["denominator"] - compare["denominator"], "value": value}
