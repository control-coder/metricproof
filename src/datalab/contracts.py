"""分析和执行的结构化交接；标识版本而不传递角色聊天历史。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class AnalysisPlan:
    dataset_version: str
    metric_version: str
    kind: str = "summary"
    group_by: str | None = None
    included_statuses: tuple[str, ...] = ("paid",)
    start: str | None = None
    end: str | None = None
    previous_start: str | None = None
    previous_end: str | None = None
    # 与服务端每次都执行的核验项一致；状态过滤已由独立参考覆盖，不声明不存在的检查。
    # 蜕变关系（metamorphic）由运行配置开启，不在默认声明中。
    checks: tuple[str, ...] = ("independent_reference", "group_total", "download_table", "invariants", "reference_agreement")
    time_field: str = "payment_time"

    def __post_init__(self):
        UUID(self.dataset_version)
        if not self.metric_version or len(self.metric_version) > 200:
            raise ValueError("必须固定已确认指标版本")
        if self.kind not in {"summary", "trend", "ranking", "share", "comparison", "quality"}:
            raise ValueError("不支持的分析类型")
        if self.group_by not in {None, "channel", "category", "payment_date"}:
            raise ValueError("不支持的分组字段")
        if self.kind in {"trend", "ranking", "share"} and not self.group_by:
            raise ValueError("该分析需要分组字段")
        if not self.included_statuses or not set(self.included_statuses) <= {"paid", "refunded", "cancelled"}:
            raise ValueError("状态口径无效")
        if self.time_field != "payment_time":
            raise ValueError("仅允许支付时间")
        if "independent_reference" not in self.checks:
            raise ValueError("不能关闭独立数值核验")
        for key in ("start", "end", "previous_start", "previous_end"):
            value = getattr(self, key)
            if value and (len(value) != 19 or datetime.fromisoformat(value).tzinfo is not None or "T" not in value):
                raise ValueError("期间边界须为无偏移的秒级 ISO 时间")
        for begin, finish in ((self.start, self.end), (self.previous_start, self.previous_end)):
            if begin and finish and begin >= finish:
                raise ValueError("期间起点必须早于终点")
        if self.kind == "comparison" and not all((self.start, self.end, self.previous_start, self.previous_end)):
            raise ValueError("期间对比必须明确两个左闭右开区间")

    def payload(self) -> dict:
        return asdict(self)


@dataclass
class ExecutionManifest:
    run_id: str
    dataset_version: str
    metric_version: str
    image_id: str
    code_path: str
    output_paths: list[str]
    elapsed_seconds: float
    status: str
    reason: str | None = None


@dataclass
class VerificationReport:
    passed: bool
    checks: list[dict] = field(default_factory=list)
    reference: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ReportClaim:
    text: str
    artifact_id: str
    result_pointer: str
