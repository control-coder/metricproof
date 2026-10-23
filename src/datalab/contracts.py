"""分析和执行的结构化交接；标识版本而不传递角色聊天历史。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from uuid import UUID

ORDER_STATUSES = ("paid", "refunded", "cancelled")
# Olist 订单状态词表，与 datasets/olist.STATUSES 一致。
OLIST_STATUSES = ("delivered", "shipped", "canceled", "unavailable", "invoiced", "processing", "created", "approved")
# 订单级金额分析：单表直接用 amount_cents；Olist 的订单金额来源由已确认口径的 amount_source 决定。
AMOUNT_KINDS = ("summary", "trend", "ranking", "share", "comparison")
OLIST_KINDS = AMOUNT_KINDS + ("repeat_rate", "cohort_retention", "delivery_delay", "category_sales")
# Olist 各分析类型允许的分组；不在表内的组合（如按州看趋势、按品类看订单金额）直接拒绝。
# 订单级分析不按品类或支付方式分组：一笔订单可以有多个品类、多种支付方式，分组之和不再等于总计。
OLIST_GROUPS = {
    "summary": {None, "customer_state", "purchase_date", "purchase_month"},
    "trend": {"purchase_date", "purchase_month"},
    "ranking": {"customer_state", "purchase_date", "purchase_month"},
    "share": {"customer_state", "purchase_date", "purchase_month"},
    "comparison": {None, "customer_state", "purchase_date", "purchase_month"},
    "repeat_rate": {None, "customer_state"},
    "cohort_retention": {None},
    "delivery_delay": {None, "customer_state"},
    "category_sales": {"product_category"},
}
OLIST_TIME_FIELD = "order_purchase_timestamp"
# 按送达日期划期间只对送达延迟有意义；其他类型的未送达订单没有该日期，会被静默排除。
OLIST_TIME_FIELDS = {kind: {OLIST_TIME_FIELD} for kind in OLIST_KINDS} | {
    "delivery_delay": {OLIST_TIME_FIELD, "order_delivered_customer_date"}}
AMOUNT_SOURCES = ("payments", "items")
# 只属于 Olist 的计划字段；单表计划的 payload 不含这些键，交接、固定脚本与旧快照保持不变。
OLIST_FIELDS = ("schema", "amount_source")


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
    # 数据结构：单表 orders（默认）或 Olist 多表 olist，由服务端按数据版本填写。
    schema: str = "orders"
    # Olist 订单级金额来源（payments 支付合计 / items 明细价格加运费），只能来自已确认口径，不由 Planner 选择。
    amount_source: str | None = None

    def __post_init__(self):
        UUID(self.dataset_version)
        if not self.metric_version or len(self.metric_version) > 200:
            raise ValueError("必须固定已确认指标版本")
        if self.schema == "olist":
            self._check_olist()
        elif self.schema != "orders":
            raise ValueError("不支持的数据结构")
        else:
            self._check_orders()
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

    def _check_orders(self):
        # 单表规则与之前完全相同。
        if self.kind not in {"summary", "trend", "ranking", "share", "comparison", "quality"}:
            raise ValueError("不支持的分析类型")
        if self.group_by not in {None, "channel", "category", "payment_date"}:
            raise ValueError("不支持的分组字段")
        if self.kind in {"trend", "ranking", "share"} and not self.group_by:
            raise ValueError("该分析需要分组字段")
        if not self.included_statuses or not set(self.included_statuses) <= set(ORDER_STATUSES):
            raise ValueError("状态口径无效")
        if self.time_field != "payment_time":
            raise ValueError("仅允许支付时间")
        if self.amount_source is not None:
            raise ValueError("单表数据没有金额来源口径")

    def _check_olist(self):
        if self.time_field == "payment_time":
            # 未写时间字段（取默认值）时，Olist 计划使用下单时间。
            object.__setattr__(self, "time_field", OLIST_TIME_FIELD)
        if self.kind not in OLIST_KINDS:
            raise ValueError("Olist 数据不支持该分析类型")
        if self.group_by not in OLIST_GROUPS[self.kind]:
            raise ValueError("该分析类型不支持这个分组字段")
        if (not self.included_statuses or len(set(self.included_statuses)) != len(self.included_statuses)
                or not set(self.included_statuses) <= set(OLIST_STATUSES)):
            raise ValueError("状态口径无效")
        if self.time_field not in OLIST_TIME_FIELDS[self.kind]:
            raise ValueError("该分析类型不支持这个时间字段")
        if self.kind == "delivery_delay" and "delivered" not in self.included_statuses:
            raise ValueError("送达延迟只统计 delivered 订单，口径必须纳入 delivered")
        if self.kind in AMOUNT_KINDS and self.amount_source not in AMOUNT_SOURCES:
            raise ValueError("订单金额分析必须使用已确认的金额来源口径")
        if self.amount_source not in (None, *AMOUNT_SOURCES):
            raise ValueError("金额来源口径无效")

    def payload(self) -> dict:
        data = asdict(self)
        if self.schema == "orders":
            for key in OLIST_FIELDS:
                data.pop(key)
        return data


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
