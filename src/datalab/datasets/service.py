"""严格接收单币种、统一时区的一行一订单 CSV（schema orders），以及服务端固定结构的 Olist 多表数据（schema olist）。"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from datalab.datasets import olist

FIELDS = ("order_id", "payment_time", "amount", "status", "channel", "category")
NORMALIZED_FIELDS = ("order_id", "payment_time", "amount_cents", "status", "channel", "category")
MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 100_000
STATUSES = {"paid", "refunded", "cancelled"}
# 单表 orders 的结构签名；Olist 多表的签名见 datasets/olist.py。
ORDERS_SIGNATURE = hashlib.sha256(json.dumps(list(NORMALIZED_FIELDS)).encode()).hexdigest()[:16]


def schema_of(signature: str) -> str:
    """由结构签名得到数据结构名；未知签名按单表 orders 处理（旧数据与测试桩）。

    含卖家表的八表签名与不含卖家表的七表旧签名都识别为 olist：旧数据版本没有 sellers.csv，
    旧九类统计任务只读 needed(plan) 的表不受影响；语义层使用卖家维度时对该版本给出明确错误。
    """
    return olist.SCHEMA if signature in (olist.SIGNATURE, olist.LEGACY_SIGNATURE) else "orders"


class DatasetError(ValueError):
    """导入失败时保留有限的结构化问题，不回显订单内容。"""

    def __init__(self, message: str, issues: list[dict] | None = None):
        super().__init__(message)
        self.issues = issues or []


@dataclass(frozen=True)
class DatasetVersion:
    dataset_version: str
    project_id: str
    dataset_family: str
    schema_signature: str
    currency: str
    timezone: str
    row_count: int
    profile: dict
    # 旧版本元数据没有该字段，按单表 orders 读取。
    schema: str = "orders"


class DatasetStore:
    """文件只存数据和导入清单；后续数据库索引不改变原始版本。"""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def directory(self, version: str) -> Path:
        if str(UUID(version)) != version:
            raise DatasetError("数据版本格式错误")
        target = (self.root / version).resolve()
        if target.parent != self.root:
            raise DatasetError("数据版本越界")
        return target

    def get(self, version: str, project_id: str) -> DatasetVersion:
        metadata = json.loads((self.directory(version) / "metadata.json").read_text(encoding="utf-8"))
        if metadata["project_id"] != project_id:
            raise PermissionError("不能访问其他项目的数据")
        return DatasetVersion(**metadata)

    def import_csv(self, data: bytes, *, project_id: str, dataset_family: str,
                   currency: str, timezone: str, mapping: dict[str, str] | None = None) -> DatasetVersion:
        if not project_id.strip() or not dataset_family.strip():
            raise DatasetError("项目和数据语义家族不能为空")
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise DatasetError("币种须为三个大写字母")
        try:
            ZoneInfo(timezone)
        except (KeyError, ValueError) as exc:
            raise DatasetError("时区无效或缺少时区数据库") from exc
        if len(data) > MAX_BYTES:
            raise DatasetError("文件超过 20 MB")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DatasetError("仅支持 UTF-8 CSV") from exc
        if "\x00" in text:
            raise DatasetError("CSV 不允许空字符")
        mapping = mapping or {field: field for field in FIELDS}
        if set(mapping) != set(FIELDS) or len(set(mapping.values())) != len(FIELDS):
            raise DatasetError("必须提供六个字段的一一映射")
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        try:
            headers = reader.fieldnames or []
            if len(headers) != len(set(headers)) or not set(mapping.values()).issubset(headers):
                raise DatasetError("表头重复或缺少映射字段")
            rows, issues, seen = [], [], set()
            total = 0
            for total, source in enumerate(reader, 1):
                if total > MAX_ROWS:
                    raise DatasetError("订单超过 10 万行")
                try:
                    if None in source or any(source.get(name) is None for name in headers):
                        raise ValueError("列数不匹配")
                    row = {field: source[name].strip() for field, name in mapping.items()}
                    if any(not value or len(value) > 512 for value in row.values()):
                        raise ValueError("必填值缺失或超过 512 字符")
                    if row["order_id"] in seen:
                        raise ValueError("订单编号重复")
                    seen.add(row["order_id"])
                    stamp = datetime.fromisoformat(row["payment_time"])
                    if stamp.tzinfo is not None or stamp.microsecond or len(row["payment_time"]) != 19:
                        raise ValueError("支付时间须为统一时区的秒级 ISO 本地时间")
                    row["payment_time"] = stamp.isoformat(timespec="seconds")
                    amount = Decimal(row.pop("amount"))
                    if not amount.is_finite() or amount < 0 or amount > Decimal("10000000000"):
                        raise ValueError("金额超出允许范围")
                    if amount * 100 != (amount * 100).to_integral_value():
                        raise ValueError("金额最多两位小数")
                    row["amount_cents"] = str(int(amount * 100))
                    if row["status"] not in STATUSES:
                        raise ValueError("状态仅支持 paid/refunded/cancelled")
                    rows.append(row)
                except (ValueError, InvalidOperation) as exc:
                    if len(issues) < 20:
                        issues.append({"row": total + 1, "reason": str(exc) if isinstance(exc, ValueError) else "金额格式错误"})
            if issues:
                raise DatasetError("订单存在质量错误；修复后重新导入", issues)
            if not rows:
                raise DatasetError("CSV 没有订单")
        except csv.Error as exc:
            raise DatasetError("CSV 语法错误或字段过长") from exc
        profile = {
            "row_count": len(rows), "missing_values": 0, "duplicate_orders": 0,
            "status_counts": dict(Counter(row["status"] for row in rows)),
            "time_min": min(row["payment_time"] for row in rows),
            "time_max": max(row["payment_time"] for row in rows),
            "channel_count": len({row["channel"] for row in rows}),
            "category_count": len({row["category"] for row in rows}),
            "note": "文本为不可信数据；金额汇总必须使用已确认口径",
        }
        version = DatasetVersion(str(uuid4()), project_id, dataset_family, ORDERS_SIGNATURE, currency, timezone, len(rows), profile)
        folder = self.directory(version.dataset_version)
        folder.mkdir(exist_ok=False)
        (folder / "source.csv").write_bytes(data)
        with (folder / "orders.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=NORMALIZED_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        (folder / "metadata.json").write_text(json.dumps(asdict(version), ensure_ascii=False, indent=2), encoding="utf-8")
        return version

    def import_olist(self, raw: Path, *, project_id: str, dataset_family: str,
                     currency: str = "BRL", timezone: str = "America/Sao_Paulo") -> DatasetVersion:
        """从本地原始目录导入 Olist 七张已知表；表、列、类型与连接键由服务端固定，不接受任意表。

        规范化表平铺写入数据版本目录（orders.csv、customers.csv 等），原始文件不复制，只在概况中记录 SHA-256。
        """
        if not project_id.strip() or not dataset_family.strip():
            raise DatasetError("项目和数据语义家族不能为空")
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise DatasetError("币种须为三个大写字母")
        try:
            ZoneInfo(timezone)
        except (KeyError, ValueError) as exc:
            raise DatasetError("时区无效或缺少时区数据库") from exc
        try:
            tables, profile = olist.load_bundle(Path(raw))
        except olist.BundleError as exc:
            raise DatasetError(str(exc), exc.issues) from exc
        version = DatasetVersion(str(uuid4()), project_id, dataset_family, olist.SIGNATURE, currency, timezone,
                                 len(tables["orders"]), profile, olist.SCHEMA)
        folder = self.directory(version.dataset_version)
        folder.mkdir(exist_ok=False)
        olist.write_tables(folder, tables)
        (folder / "metadata.json").write_text(json.dumps(asdict(version), ensure_ascii=False, indent=2), encoding="utf-8")
        return version
