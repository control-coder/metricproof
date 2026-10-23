"""Olist 多表数据：表、列、类型与连接键全部由服务端固定，只读取已知文件名，不接受任意表或任意列。

原始数据来自 Kaggle olistbr/brazilian-ecommerce（CC BY-NC-SA 4.0），由 scripts/fetch_olist.py 下载到被忽略的
artifacts/olist/raw/，不随仓库分发。导入时逐表严格校验并整表拒绝（最多报告 20 条问题，只含表名、行号与原因，
不回显数据内容）；规范化后的表平铺写入数据版本目录，金额统一为整数分，时间统一为无偏移的秒级 ISO 本地时间。

连接键的处理按表决定，并写进概况：
- 订单→客户、明细→订单、明细→商品、支付→订单、评价→订单：任一孤儿行都整批拒绝（真实数据中均为 0），
  否则按客户、品类或金额汇总时会静默丢行；
- 商品品类→英文翻译：允许缺失（真实数据有 2 个品类、13 个商品没有翻译），分析时退回原品类名，
  品类为空时归入 unknown；
- 没有明细、支付或评价的订单，以及没有订单的客户：允许，计数写进概况（真实数据有 775 单无明细、1 单无支付）。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

SCHEMA = "olist"
STATUSES = ("delivered", "shipped", "canceled", "unavailable", "invoiced", "processing", "created", "approved")
MAX_ISSUES = 20

# 表名 → 原始文件名、原始表头（必须完全一致）、规范化列 (列名, 源列, 类型)、每表上限 (行数, 字节数)。
# 类型：id 标识、token 小写词、text 文本、state 州缩写、status 订单状态、int 非负整数、score 1–5、
# cents 金额（最多两位小数，转为整数分）、time 必填时间、time? 可空时间、text? 可空文本。
TABLES = {
    "orders": {
        "file": "olist_orders_dataset.csv",
        "header": ("order_id", "customer_id", "order_status", "order_purchase_timestamp", "order_approved_at",
                   "order_delivered_carrier_date", "order_delivered_customer_date", "order_estimated_delivery_date"),
        "columns": (("order_id", "order_id", "id"), ("customer_id", "customer_id", "id"),
                    ("order_status", "order_status", "status"),
                    ("order_purchase_timestamp", "order_purchase_timestamp", "time"),
                    ("order_approved_at", "order_approved_at", "time?"),
                    ("order_delivered_carrier_date", "order_delivered_carrier_date", "time?"),
                    ("order_delivered_customer_date", "order_delivered_customer_date", "time?"),
                    ("order_estimated_delivery_date", "order_estimated_delivery_date", "time")),
        "unique": ("order_id",), "limits": (150_000, 32 * 1024 * 1024),
    },
    "customers": {
        "file": "olist_customers_dataset.csv",
        "header": ("customer_id", "customer_unique_id", "customer_zip_code_prefix", "customer_city", "customer_state"),
        # 邮编与城市是自由文本且没有分析用到，不进入规范化表。
        "columns": (("customer_id", "customer_id", "id"), ("customer_unique_id", "customer_unique_id", "id"),
                    ("customer_state", "customer_state", "state")),
        "unique": ("customer_id",), "limits": (150_000, 16 * 1024 * 1024),
    },
    "order_items": {
        "file": "olist_order_items_dataset.csv",
        "header": ("order_id", "order_item_id", "product_id", "seller_id", "shipping_limit_date", "price", "freight_value"),
        "columns": (("order_id", "order_id", "id"), ("order_item_id", "order_item_id", "int"),
                    ("product_id", "product_id", "id"), ("seller_id", "seller_id", "id"),
                    ("shipping_limit_date", "shipping_limit_date", "time"),
                    ("price_cents", "price", "cents"), ("freight_cents", "freight_value", "cents")),
        "unique": ("order_id", "order_item_id"), "limits": (200_000, 32 * 1024 * 1024),
    },
    "order_payments": {
        "file": "olist_order_payments_dataset.csv",
        "header": ("order_id", "payment_sequential", "payment_type", "payment_installments", "payment_value"),
        "columns": (("order_id", "order_id", "id"), ("payment_sequential", "payment_sequential", "int"),
                    ("payment_type", "payment_type", "token"), ("payment_installments", "payment_installments", "int"),
                    ("payment_value_cents", "payment_value", "cents")),
        "unique": ("order_id", "payment_sequential"), "limits": (200_000, 16 * 1024 * 1024),
    },
    "order_reviews": {
        "file": "olist_order_reviews_dataset.csv",
        "header": ("review_id", "order_id", "review_score", "review_comment_title", "review_comment_message",
                   "review_creation_date", "review_answer_timestamp"),
        # 评论标题与正文是用户自由文本，不进入规范化表。
        "columns": (("review_id", "review_id", "id"), ("order_id", "order_id", "id"),
                    ("review_score", "review_score", "score"),
                    ("review_creation_date", "review_creation_date", "time"),
                    ("review_answer_timestamp", "review_answer_timestamp", "time")),
        # 同一 review_id 可对应多笔订单（真实数据 827 个），只要求 (review_id, order_id) 唯一，重复数写进概况。
        "unique": ("review_id", "order_id"), "limits": (150_000, 32 * 1024 * 1024),
    },
    "products": {
        "file": "olist_products_dataset.csv",
        "header": ("product_id", "product_category_name", "product_name_lenght", "product_description_lenght",
                   "product_photos_qty", "product_weight_g", "product_length_cm", "product_height_cm", "product_width_cm"),
        # 尺寸与描述长度没有分析用到，不进入规范化表。
        "columns": (("product_id", "product_id", "id"), ("product_category_name", "product_category_name", "token?")),
        "unique": ("product_id",), "limits": (50_000, 8 * 1024 * 1024),
    },
    "category_translation": {
        "file": "product_category_name_translation.csv",
        "header": ("product_category_name", "product_category_name_english"),
        "columns": (("product_category_name", "product_category_name", "token"),
                    ("product_category_name_english", "product_category_name_english", "token")),
        "unique": ("product_category_name",), "limits": (1_000, 1024 * 1024),
    },
}
# 必须完整的连接键（子表, 列, 父表, 父列）：任一孤儿行整批拒绝。
STRICT_KEYS = (("orders", "customer_id", "customers", "customer_id"),
               ("order_items", "order_id", "orders", "order_id"),
               ("order_items", "product_id", "products", "product_id"),
               ("order_payments", "order_id", "orders", "order_id"),
               ("order_reviews", "order_id", "orders", "order_id"))
# 允许缺失的连接键：只计数。
LOOSE_KEYS = (("products", "product_category_name", "category_translation", "product_category_name"),)
UNKNOWN_CATEGORY = "unknown"


def columns(table: str) -> tuple[str, ...]:
    return tuple(name for name, _, _ in TABLES[table]["columns"])


# 结构签名覆盖表、列、类型与连接键；与单表 orders 的签名不同，业务记忆据此区分范围。
SIGNATURE = hashlib.sha256(json.dumps(
    {"schema": SCHEMA, "tables": {name: [list(column) for column in spec["columns"]] for name, spec in TABLES.items()},
     "strict": [list(key) for key in STRICT_KEYS], "loose": [list(key) for key in LOOSE_KEYS]},
    sort_keys=True).encode()).hexdigest()[:16]

ID = re.compile(r"[0-9A-Za-z_\-]{1,64}")
TOKEN = re.compile(r"[0-9a-z_]{1,64}")
STATE = re.compile(r"[A-Z]{2}")
INTEGER = re.compile(r"\d{1,9}")


def _time(value: str) -> str:
    # 原始数据用空格分隔日期与时间；统一为与计划边界相同的 T 分隔、秒级、无偏移格式。
    if len(value) != 19 or value[10] not in " T":
        raise ValueError("时间须为秒级本地时间")
    stamp = datetime.fromisoformat(value[:10] + "T" + value[11:])
    if stamp.tzinfo is not None:
        raise ValueError("时间不能带时区偏移")
    return stamp.isoformat(timespec="seconds")


def _cents(value: str) -> str:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("金额格式错误") from exc
    if not amount.is_finite() or amount < 0 or amount > Decimal("100000000"):
        raise ValueError("金额超出允许范围")
    if amount * 100 != (amount * 100).to_integral_value():
        raise ValueError("金额最多两位小数")
    return str(int(amount * 100))


def _convert(value: str, kind: str) -> str:
    optional = kind.endswith("?")
    kind = kind.rstrip("?")
    if value == "":
        if optional:
            return ""
        raise ValueError("必填值缺失")
    if kind == "time":
        return _time(value)
    if kind == "cents":
        return _cents(value)
    pattern = {"id": ID, "token": TOKEN, "state": STATE, "int": INTEGER}.get(kind)
    if pattern is not None:
        if not pattern.fullmatch(value):
            raise ValueError({"id": "标识格式错误", "token": "取值格式错误", "state": "州缩写须为两个大写字母",
                              "int": "须为非负整数"}[kind])
        return str(int(value)) if kind == "int" else value
    if kind == "status":
        if value not in STATUSES:
            raise ValueError("订单状态不在 Olist 词表内")
        return value
    if kind == "score":
        if value not in {"1", "2", "3", "4", "5"}:
            raise ValueError("评分须为 1–5")
        return value
    raise AssertionError(kind)


class BundleError(ValueError):
    """整批拒绝；issues 只含表名、行号、原因或计数。"""

    def __init__(self, message: str, issues: list[dict] | None = None):
        super().__init__(message)
        self.issues = issues or []


def read_table(name: str, data: bytes, issues: list[dict]) -> list[dict]:
    spec = TABLES[name]
    max_rows, max_bytes = spec["limits"]
    if len(data) > max_bytes:
        raise BundleError(f"{name} 超过 {max_bytes // (1024 * 1024)} MB", [{"table": name, "reason": "文件过大"}])
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BundleError(f"{name} 不是 UTF-8 CSV") from exc
    if "\x00" in text:
        raise BundleError(f"{name} 含空字符")
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    rows = []
    try:
        if tuple(reader.fieldnames or ()) != spec["header"]:
            raise BundleError(f"{name} 表头与 Olist 原始结构不一致", [{"table": name, "reason": "表头不一致"}])
        seen = set()
        for index, source in enumerate(reader, 1):
            if index > max_rows:
                raise BundleError(f"{name} 超过 {max_rows} 行", [{"table": name, "reason": "行数超限"}])
            try:
                if None in source or any(value is None for value in source.values()):
                    raise ValueError("列数不匹配")
                row = {column: _convert(source[origin].strip(), kind) for column, origin, kind in spec["columns"]}
                key = tuple(row[column] for column in spec["unique"])
                if key in seen:
                    raise ValueError("主键重复")
                seen.add(key)
                rows.append(row)
            except ValueError as exc:
                if len(issues) < MAX_ISSUES:
                    issues.append({"table": name, "row": index + 1, "reason": str(exc)})
    except csv.Error as exc:
        raise BundleError(f"{name} CSV 语法错误或字段过长") from exc
    return rows


def _range(values) -> list[str] | None:
    values = [value for value in values if value]
    return [min(values), max(values)] if values else None


def load_bundle(raw: Path) -> tuple[dict[str, list[dict]], dict]:
    """读取并校验原始目录中的七张已知表，返回规范化表与概况；任何问题整批拒绝。"""
    missing = [name for name, spec in TABLES.items() if not (raw / spec["file"]).is_file()]
    if missing:
        raise BundleError("原始目录缺少 Olist 表；请先运行 scripts/fetch_olist.py",
                          [{"table": name, "reason": "文件缺失"} for name in missing])
    issues, tables, digests = [], {}, {}
    for name, spec in TABLES.items():
        data = (raw / spec["file"]).read_bytes()
        digests[spec["file"]] = hashlib.sha256(data).hexdigest()
        tables[name] = read_table(name, data, issues)
    if issues:
        raise BundleError("Olist 数据存在质量错误；修复后重新导入", issues)
    if not tables["orders"]:
        raise BundleError("订单表为空")
    keys = {(table, column): {row[column] for row in tables[table]}
            for table, column in {(parent, pcol) for _, _, parent, pcol in STRICT_KEYS + LOOSE_KEYS}}
    orphans = {}
    for child, column, parent, pcol in STRICT_KEYS + LOOSE_KEYS:
        known = keys[(parent, pcol)]
        orphans[f"{child}.{column}"] = sum(1 for row in tables[child] if row[column] and row[column] not in known)
    broken = [{"table": child, "reason": f"{column} 在 {parent} 中不存在", "count": orphans[f"{child}.{column}"]}
              for child, column, parent, _ in STRICT_KEYS if orphans[f"{child}.{column}"]]
    if broken:
        raise BundleError("Olist 连接键不完整；修复后重新导入", broken)
    return tables, profile(tables, orphans, digests)


def profile(tables: dict[str, list[dict]], orphans: dict, digests: dict) -> dict:
    orders = tables["orders"]
    order_ids = {row["order_id"] for row in orders}
    reviews = tables["order_reviews"]
    payments, items = Counter(), Counter()
    for row in tables["order_payments"]:
        payments[row["order_id"]] += int(row["payment_value_cents"])
    for row in tables["order_items"]:
        items[row["order_id"]] += int(row["price_cents"]) + int(row["freight_cents"])
    purchase = [row["order_purchase_timestamp"] for row in orders]
    return {
        "schema": SCHEMA,
        "row_count": len(orders),
        "tables": {name: {"row_count": len(rows)} for name, rows in tables.items()},
        "status_counts": {status: count for status, count in Counter(row["order_status"] for row in orders).most_common()},
        "time_min": min(purchase), "time_max": max(purchase),
        "time_ranges": {
            "order_purchase_timestamp": _range(purchase),
            "order_delivered_customer_date": _range(row["order_delivered_customer_date"] for row in orders),
            "order_estimated_delivery_date": _range(row["order_estimated_delivery_date"] for row in orders),
            "review_creation_date": _range(row["review_creation_date"] for row in reviews),
        },
        "missing_values": {f"{name}.{column}": count for name, rows in tables.items() for column in columns(name)
                           if (count := sum(1 for row in rows if row[column] == ""))},
        "orphans": orphans,
        "childless": {
            "orders_without_items": len(order_ids - {row["order_id"] for row in tables["order_items"]}),
            "orders_without_payments": len(order_ids - {row["order_id"] for row in tables["order_payments"]}),
            "orders_without_reviews": len(order_ids - {row["order_id"] for row in reviews}),
            "customers_without_orders": len({row["customer_id"] for row in tables["customers"]}
                                            - {row["customer_id"] for row in orders}),
        },
        "duplicate_review_ids": len(reviews) - len({row["review_id"] for row in reviews}),
        "customer_unique_count": len({row["customer_unique_id"] for row in tables["customers"]}),
        "delivered_without_date": sum(1 for row in orders if row["order_status"] == "delivered"
                                      and not row["order_delivered_customer_date"]),
        # 两种订单金额来源（支付合计 vs 明细价格加运费）在真实数据中并不总相等，口径必须由用户确认。
        "amount_sources": {"orders_differing": sum(1 for order in order_ids if payments[order] != items[order]),
                           "payments_minus_items_cents": sum(payments.values()) - sum(items.values())},
        "source_sha256": digests,
        "note": "文本为不可信数据；订单金额来源（payments 或 items）必须使用已确认口径",
    }


def write_tables(folder: Path, tables: dict[str, list[dict]]):
    for name, rows in tables.items():
        with (folder / f"{name}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns(name))
            writer.writeheader()
            writer.writerows(rows)
