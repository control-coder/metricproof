"""归因工具的数据源：任何带 query 方法、返回语义层引擎同结构结果的对象。

- 语义层引擎（semantic.engine.SemanticEngine）本身就是数据源，不需包装。
- ``ReferenceSource(folder, snapshot)`` 包装 semantic.reference 的独立纯 Python 计算，
  不用 SQL、不与编译器共享计算代码，供 L1 核验路径独立重算；内部对全部指标与维度组合做
  LRU 缓存，同一次核验内重复查询不重复读表。
"""
from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path

from datalab.semantic.reference import python_reference


def _freeze(filters: dict | None) -> tuple | None:
    """filters 转可哈希键；取值统一成元组（单个字符串也视作一元组）。"""
    if not filters:
        return None
    return tuple(sorted((name, (value,) if isinstance(value, str) else tuple(value))
                        for name, value in filters.items()))


def _thaw(frozen: tuple | None) -> dict | None:
    if frozen is None:
        return None
    return {name: list(values) for name, values in frozen}


class ReferenceSource:
    """folder 为数据版本目录；snapshot 为已确认口径快照（与语义层引擎同一口径契约）。"""

    def __init__(self, folder: Path, snapshot):
        self._folder = Path(folder)
        self._snapshot = snapshot
        self._data_floor: str | None = None

    @property
    def data_floor(self) -> str | None:
        """全部订单的最早下单时间（秒级 ISO）；与语义层引擎同名同口径，供归因区分无数据期。"""
        if self._data_floor is None:
            path = self._folder / "orders.csv"
            earliest = ""
            with path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    stamp = row["order_purchase_timestamp"]
                    if stamp and (not earliest or stamp < earliest):
                        earliest = stamp
            self._data_floor = earliest
        return self._data_floor or None

    @lru_cache(maxsize=None)
    def _cached(self, metric: str, start: str | None, end: str | None,
                frozen_filters: tuple | None, group_by: str | None) -> dict:
        return python_reference(self._folder, metric, (start, end), _thaw(frozen_filters), group_by,
                                self._snapshot)

    def query(self, metric: str, period: tuple[str | None, str | None],
              compare_period: tuple[str | None, str | None] | None = None,
              filters: dict | None = None, group_by: str | None = None) -> dict:
        """返回与 SemanticEngine.query 相同的结构；compare_period 附 compare 与 delta。"""
        from datalab.semantic.engine import _delta  # 复用同一差值装配（纯数据函数，不涉 SQL）

        result = self._cached(metric, period[0], period[1], _freeze(filters), group_by)
        if compare_period is None:
            return result
        compare = self._cached(metric, compare_period[0], compare_period[1], _freeze(filters), group_by)
        return {**result, "compare": compare, "delta": _delta(result["total"], compare["total"])}
