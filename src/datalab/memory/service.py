"""先按语义范围过滤，再检索；历史摘要永远不能自动升级成规则。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from typing import ContextManager, Protocol
from uuid import uuid4

from datalab.contracts import AMOUNT_SOURCES, OLIST_STATUSES, ORDER_STATUSES
from datalab.datasets.service import schema_of
from datalab.orchestration.workflow import MetricSnapshot


# 标准字段的默认语义，作为导入时的待确认候选；未经用户确认不作为业务规则使用。
FIELD_MEANINGS = {
    "order_id": "订单编号，一行一笔订单，在同一数据版本内唯一",
    "payment_time": "支付时间，数据声明时区下的秒级本地时间，期间过滤与按日分组使用此字段",
    "amount": "订单金额，单币种、最多两位小数，导入后按分存储为 amount_cents",
    "status": "订单状态，取值 paid/refunded/cancelled，由已确认口径决定纳入哪些状态",
    "channel": "成交渠道，按原文分组，不做合并或推断",
    "category": "商品品类，按原文分组，不做合并或推断",
}


class MemoryConflict(ValueError):
    """当前版本已变化，必须向用户展示冲突并重新确认。"""


@dataclass(frozen=True)
class Scope:
    project_id: str
    dataset_family: str
    schema_signature: str

    def __post_init__(self):
        if any(not isinstance(value, str) or not value.strip() or len(value) > 200
               for value in (self.project_id, self.dataset_family, self.schema_signature)):
            raise ValueError("记忆必须有明确项目、数据语义家族和结构签名")


@dataclass(frozen=True)
class MemoryEntry:
    memory_id: str
    project_id: str
    dataset_family: str
    schema_signature: str
    kind: str
    name: str
    content: dict
    status: str
    version: int
    source_run_id: str
    confirmed_by: str | None = None
    effective_from: str | None = None
    supersedes_id: str | None = None
    invalidated_by: str | None = None
    invalidated_at: str | None = None

    @property
    def scope(self) -> Scope:
        return Scope(self.project_id, self.dataset_family, self.schema_signature)


class MemoryTransaction(Protocol):
    def list_entries(self) -> list[MemoryEntry]: ...
    def insert(self, entry: MemoryEntry): ...
    def update_status(self, entry: MemoryEntry): ...


class MemoryRepository(Protocol):
    def transaction(self, scope: Scope) -> ContextManager[MemoryTransaction]: ...


class MemoryService:
    def __init__(self, repository: MemoryRepository):
        self.repository = repository

    def propose(self, scope: Scope, kind: str, name: str, content: dict, source_run_id: str) -> MemoryEntry:
        if kind not in {"metric", "field_semantics", "display_preference", "summary"}:
            raise ValueError("不支持的记忆类型")
        if not name.strip() or len(name) > 120 or not source_run_id.strip() or len(source_run_id) > 200:
            raise ValueError("记忆名称或来源无效")
        if not isinstance(content, dict) or len(json.dumps(content, ensure_ascii=False).encode()) > 8192:
            raise ValueError("记忆内容必须是限长结构化对象")
        if kind == "metric":
            statuses = content.get("included_statuses")
            definition = content.get("definition")
            olist = schema_of(scope.schema_signature) == "olist"
            allowed = OLIST_STATUSES if olist else ORDER_STATUSES
            if (not isinstance(statuses, list) or not statuses or any(not isinstance(item, str) for item in statuses)
                    or len(statuses) != len(set(statuses)) or not set(statuses) <= set(allowed)
                    or not isinstance(definition, str) or not definition.strip()):
                raise ValueError("指标必须明确文字定义和订单状态")
            # Olist 口径可以同时确认订单金额来源；单表口径没有这个字段。
            if "amount_source" in content and (not olist or content["amount_source"] not in AMOUNT_SOURCES):
                raise ValueError("金额来源只能是 payments 或 items，且只适用于 Olist 数据")
        if kind == "field_semantics":
            if (content.get("field") not in FIELD_MEANINGS or content.get("field") != name
                    or not isinstance(content.get("meaning"), str) or not content["meaning"].strip()
                    or not isinstance(content.get("source_column"), str) or not content["source_column"].strip()):
                raise ValueError("字段语义必须对应标准字段并写明含义与源列")
        with self.repository.transaction(scope) as txn:
            entries = txn.list_entries()
            version = 1 + max((entry.version for entry in entries if entry.kind == kind and entry.name == name), default=0)
            entry = MemoryEntry(str(uuid4()), scope.project_id, scope.dataset_family, scope.schema_signature,
                                kind, name, deepcopy(content), "recorded" if kind == "summary" else "pending",
                                version, source_run_id)
            txn.insert(entry)
            return deepcopy(entry)

    def suggest_field_semantics(self, scope: Scope, mapping: dict[str, str], source_run_id: str) -> list[MemoryEntry]:
        """导入或字段映射时为六个标准字段写入待确认候选；与当前有效条目内容相同时不重复写版本。"""
        created = []
        for field, meaning in FIELD_MEANINGS.items():
            content = {"field": field, "source_column": mapping[field], "meaning": meaning}
            with self.repository.transaction(scope) as txn:
                live = [entry for entry in txn.list_entries() if entry.kind == "field_semantics" and entry.name == field
                        and entry.status in {"pending", "confirmed"}]
            if any(entry.content == content for entry in live):
                continue
            created.append(self.propose(scope, "field_semantics", field, content, source_run_id))
        return created

    def confirm(self, scope: Scope, memory_id: str, *, actor: str, expected_current_id: str | None) -> MemoryEntry:
        if not actor.strip() or len(actor) > 200:
            raise ValueError("必须记录明确确认人或可信预配置标识")
        with self.repository.transaction(scope) as txn:
            entries = txn.list_entries()
            entry = next((item for item in entries if item.memory_id == memory_id), None)
            if entry is None:
                raise KeyError("该范围不存在这条记忆")
            if entry.kind == "summary" or entry.status != "pending":
                raise ValueError("仅待确认业务规则或展示偏好可以确认")
            current = next((item for item in entries if item.name == entry.name and item.kind == entry.kind
                            and item.status == "confirmed"), None)
            if (current.memory_id if current else None) != expected_current_id:
                raise MemoryConflict("当前规则与确认时看到的版本不同")
            if current and current.version >= entry.version:
                raise MemoryConflict("不能把较旧的候选重新确认为最新版本")
            if current:
                txn.update_status(replace(current, status="superseded"))
            confirmed = replace(entry, status="confirmed", confirmed_by=actor,
                                effective_from=datetime.now(timezone.utc).isoformat(),
                                supersedes_id=current.memory_id if current else None)
            txn.update_status(confirmed)
            return deepcopy(confirmed)

    def invalidate(self, scope: Scope, memory_id: str, *, actor: str) -> MemoryEntry:
        if not actor.strip():
            raise ValueError("失效操作必须具有操作人")
        with self.repository.transaction(scope) as txn:
            entry = next((item for item in txn.list_entries() if item.memory_id == memory_id), None)
            if entry is None:
                raise KeyError("该范围不存在这条记忆")
            if entry.status != "confirmed":
                raise ValueError("只能使当前已确认规则失效")
            invalid = replace(entry, status="invalidated", invalidated_by=actor,
                              invalidated_at=datetime.now(timezone.utc).isoformat())
            txn.update_status(invalid)
            return deepcopy(invalid)

    def history(self, scope: Scope) -> list[MemoryEntry]:
        with self.repository.transaction(scope) as txn:
            return deepcopy(sorted(txn.list_entries(), key=lambda entry: (entry.name, entry.version)))

    def retrieve(self, scope: Scope, *, keyword: str = "", kind: str | None = None) -> list[MemoryEntry]:
        # 数据家族不是 schema 的别名：相同字段不同语义不得跨范围默认复用。
        entries = self.history(scope)
        now = datetime.now(timezone.utc)
        return [entry for entry in entries if entry.status == "confirmed" and entry.kind != "summary"
                and (kind is None or entry.kind == kind)
                and (not keyword or keyword.casefold() in (entry.name + json.dumps(entry.content, ensure_ascii=False)).casefold())
                and entry.effective_from and datetime.fromisoformat(entry.effective_from) <= now]

    @staticmethod
    def metric_snapshot(entry: MemoryEntry) -> MetricSnapshot:
        if entry.kind != "metric" or entry.status != "confirmed" or not entry.confirmed_by:
            raise ValueError("默认计算只能使用已确认指标")
        return MetricSnapshot(f"{entry.memory_id}:v{entry.version}", tuple(entry.content["included_statuses"]),
                              f"{entry.source_run_id};confirmed_by={entry.confirmed_by}", entry.project_id,
                              entry.dataset_family, entry.schema_signature, entry.content.get("amount_source"))
