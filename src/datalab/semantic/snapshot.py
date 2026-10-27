"""语义层口径快照：只取计算需要的字段，与 orchestration.workflow.MetricSnapshot 解耦。

snapshot 只能来自已确认口径或可信预配置（服务端在调用语义层前建立）；
语义层不验证来源，只要求字段齐全、取值在词表内。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from datalab.contracts import OLIST_STATUSES
from datalab.semantic.errors import SemanticError

_VERSION = re.compile(r"[A-Za-z0-9:._\-]{1,200}")


@dataclass(frozen=True)
class SemanticSnapshot:
    version: str
    included_statuses: tuple[str, ...]
    # 订单级金额来源（payments / items）；gmv 必须使用，其他指标可为 None。
    amount_source: str | None = None

    def __post_init__(self):
        if not _VERSION.fullmatch(self.version):
            raise SemanticError("口径版本格式不合法")
        if not self.included_statuses or not set(self.included_statuses) <= set(OLIST_STATUSES):
            raise SemanticError("纳入状态不在 Olist 词表内")
        if self.amount_source not in (None, "payments", "items"):
            raise SemanticError("金额来源口径无效")

    def require_amount_source(self) -> str:
        if self.amount_source is None:
            raise SemanticError("gmv 需要已确认口径给出订单金额来源（payments 或 items）")
        return self.amount_source
