"""合成异动注入器：在规范化 Olist 表（datasets/olist.read_tables 的结构）上施加已知异动。

六种注入类型，全部为纯函数 ``inject(tables, spec, rng) -> tables``：
随机性只来自调用方以案例 ID 为种子创建的 ``random.Random``，相同输入必然得到相同输出。
物化 ``materialize(case, base_folder, target_root)`` 复制基础数据版本目录并只重写受影响的表，
写好 metadata.json，DatasetStore 与 SemanticEngine 都能直接读取。

注入类型与受影响表：
- order_drop：某品类 × 某客户州 × 某月，删掉含该品类商品的订单的一部分（同时删除其明细、支付、评价子行）。
- freight_up：某卖家州的卖家在某月的明细运费按比例上调。
- delivery_delay：某客户州某月一部分已送达订单的送达日期推迟到预计送达日之后，并把其评价分数改为 1–2。
- mix_shift：某支付方式（或某客户州）在某月的全部订单原样复制一份（新 order_id/customer_id/customer_unique_id，
  子行同步复制），组内客单价不变、权重上升。
- price_cut：某品类某月明细价格按比例下调。
- seller_drop：某卖家在某月的订单全部消失（订单只剩该卖家时整单删除及子行）。
"""
from __future__ import annotations

import copy
import random
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from datalab.datasets import olist
from datalab.datasets.service import DatasetStore


def month_range(month: str) -> tuple[str, str]:
    """'2018-03' → 该月的左闭右开期间（秒级 ISO）。"""
    start = datetime.fromisoformat(month + "-01T00:00:00")
    following = datetime(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return start.isoformat(timespec="seconds"), following.isoformat(timespec="seconds")


def _in_month(order: dict, month: str) -> bool:
    start, end = month_range(month)
    return start <= order["order_purchase_timestamp"] < end


def _orders_by_id(tables: dict) -> dict[str, dict]:
    return {row["order_id"]: row for row in tables["orders"]}


def _customers_by_id(tables: dict) -> dict[str, dict]:
    return {row["customer_id"]: row for row in tables["customers"]}


def _category_maps(tables: dict):
    """返回 (category_of, english)：category_of(product_id) 为英文品类名（缺翻译用原名，空为 unknown）。"""
    english = {row["product_category_name"]: row["product_category_name_english"]
               for row in tables["category_translation"]}
    category = {row["product_id"]: row["product_category_name"] for row in tables["products"]}

    def of(product_id: str) -> str:
        name = category.get(product_id, "")
        return english.get(name, name) if name else olist.UNKNOWN_CATEGORY

    return of, english


def _drop_orders(tables: dict, order_ids: set[str]):
    """整单删除：同时删除明细、支付、评价子行；客户行保留（允许没有订单的客户）。"""
    tables["orders"] = [row for row in tables["orders"] if row["order_id"] not in order_ids]
    tables["order_items"] = [row for row in tables["order_items"] if row["order_id"] not in order_ids]
    tables["order_payments"] = [row for row in tables["order_payments"] if row["order_id"] not in order_ids]
    tables["order_reviews"] = [row for row in tables["order_reviews"] if row["order_id"] not in order_ids]


def inject_order_drop(tables: dict, spec: dict, rng: random.Random) -> dict:
    """删除目标月含 (品类, 客户州) 组合明细的订单的 share 比例；返回注入后的表。"""
    month, category, state, share = spec["month"], spec["product_category"], spec["customer_state"], spec["share"]
    category_of, _ = _category_maps(tables)
    orders, customers = _orders_by_id(tables), _customers_by_id(tables)
    candidates = []
    for item in tables["order_items"]:
        order = orders[item["order_id"]]
        if _in_month(order, month) and category_of(item["product_id"]) == category \
                and customers[order["customer_id"]]["customer_state"] == state:
            candidates.append(item["order_id"])
    # 排序后迭代：RNG 按固定顺序消费，保证跨进程可复现（set 迭代顺序受哈希种子影响）。
    drop = {order_id for order_id in sorted(set(candidates)) if rng.random() < share}
    tables = copy.deepcopy(tables)
    _drop_orders(tables, drop)
    return tables


def inject_freight_up(tables: dict, spec: dict, rng: random.Random) -> dict:
    """目标月、卖家州为 seller_state 的明细运费乘 factor（四舍五入到整数分，至少加 1 分）。"""
    month, state, factor = spec["month"], spec["seller_state"], spec["factor"]
    orders = _orders_by_id(tables)
    states = {row["seller_id"]: row["seller_state"] for row in tables["sellers"]}
    tables = copy.deepcopy(tables)
    for item in tables["order_items"]:
        order = orders[item["order_id"]]
        if _in_month(order, month) and states.get(item["seller_id"]) == state:
            freight = int(item["freight_cents"])
            raised = int(freight * factor + 0.5)
            item["freight_cents"] = str(max(raised, freight + 1))
    return tables


def inject_delivery_delay(tables: dict, spec: dict, rng: random.Random) -> dict:
    """目标州目标月已送达订单的 share 比例：送达日期推迟到预计送达日之后 1–7 天，评价分数改为 1–2。"""
    month, state, share = spec["month"], spec["customer_state"], spec["share"]
    customers = _customers_by_id(tables)
    tables = copy.deepcopy(tables)
    delayed = []
    for order in tables["orders"]:
        if order["order_status"] != "delivered" or not order["order_delivered_customer_date"]:
            continue
        if not _in_month(order, month) or customers[order["customer_id"]]["customer_state"] != state:
            continue
        if rng.random() >= share:
            continue
        estimated = datetime.fromisoformat(order["order_estimated_delivery_date"])
        current = datetime.fromisoformat(order["order_delivered_customer_date"])
        pushed = estimated + timedelta(days=rng.randrange(1, 8), seconds=rng.randrange(86400))
        order["order_delivered_customer_date"] = max(pushed, current + timedelta(seconds=1)).isoformat(timespec="seconds")
        delayed.append(order["order_id"])
    for review in tables["order_reviews"]:
        if review["order_id"] in set(delayed):
            review["review_score"] = str(rng.randrange(1, 3))
    return tables


def inject_mix_shift(tables: dict, spec: dict, rng: random.Random) -> dict:
    """目标月内 dimension == value 的全部订单原样复制一份：新 order_id/customer_id/customer_unique_id，
    子行同步复制；该组订单数与金额翻倍，组内客单价精确不变。"""
    month, dimension, value = spec["month"], spec["dimension"], spec["value"]
    if dimension not in ("payment_type", "customer_state"):
        raise ValueError("mix_shift 只支持 payment_type 或 customer_state")
    orders, customers = _orders_by_id(tables), _customers_by_id(tables)
    first_payment = {}
    for row in tables["order_payments"]:
        key = row["order_id"]
        if key not in first_payment or int(row["payment_sequential"]) < int(first_payment[key]["payment_sequential"]):
            first_payment[key] = row
    targets = []
    for order in tables["orders"]:
        if not _in_month(order, month):
            continue
        if dimension == "payment_type":
            payment = first_payment.get(order["order_id"])
            if (payment["payment_type"] if payment else "unknown") != value:
                continue
        elif customers[order["customer_id"]]["customer_state"] != value:
            continue
        targets.append(order["order_id"])
    tables = copy.deepcopy(tables)
    suffix = spec.get("suffix", "zz")
    order_map = {order_id: f"{order_id}{suffix}" for order_id in targets}
    new_customers = []
    for order_id in targets:
        customer = next(row for row in tables["customers"] if row["customer_id"] == orders[order_id]["customer_id"])
        clone = dict(customer)
        clone["customer_id"] = f"{customer['customer_id']}{suffix}"
        # customer_unique_id 也新建且按克隆客户行唯一：同一人在目标月有多单时各克隆互不相同，
        # 避免克隆之间被误判为复购（改变新老客结构）。
        clone["customer_unique_id"] = f"{clone['customer_id']}{suffix}"
        new_customers.append(clone)
    tables["customers"].extend(new_customers)
    customer_by_order = {order_id: new_customers[index]["customer_id"] for index, order_id in enumerate(targets)}
    for order_id in targets:
        order = orders[order_id]
        clone = dict(order)
        clone["order_id"] = order_map[order_id]
        clone["customer_id"] = customer_by_order[order_id]
        tables["orders"].append(clone)
    for name in ("order_items", "order_payments", "order_reviews"):
        clones = []
        for row in tables[name]:
            if row["order_id"] in order_map:
                clone = dict(row)
                clone["order_id"] = order_map[row["order_id"]]
                if name == "order_reviews":
                    clone["review_id"] = f"{row['review_id']}{suffix}"
                clones.append(clone)
        tables[name].extend(clones)
    return tables


def inject_price_cut(tables: dict, spec: dict, rng: random.Random) -> dict:
    """目标品类目标月的明细价格乘 factor（四舍五入到整数分，至少减 1 分；价格为 0 的不动）。"""
    month, category, factor = spec["month"], spec["product_category"], spec["factor"]
    category_of, _ = _category_maps(tables)
    orders = _orders_by_id(tables)
    tables = copy.deepcopy(tables)
    for item in tables["order_items"]:
        order = orders[item["order_id"]]
        if not _in_month(order, month) or category_of(item["product_id"]) != category:
            continue
        price = int(item["price_cents"])
        if price <= 1:
            continue
        item["price_cents"] = str(min(max(int(price * factor + 0.5), 0), price - 1))
    return tables


def inject_seller_drop(tables: dict, spec: dict, rng: random.Random) -> dict:
    """目标月内该卖家的全部明细删除；订单不含其他明细时整单删除（及支付、评价子行）。"""
    month, seller_id = spec["month"], spec["seller_id"]
    orders = _orders_by_id(tables)
    tables = copy.deepcopy(tables)
    removed_orders = set()
    kept = []
    for item in tables["order_items"]:
        order = orders[item["order_id"]]
        if _in_month(order, month) and item["seller_id"] == seller_id:
            removed_orders.add(item["order_id"])
            continue
        kept.append(item)
    tables["order_items"] = kept
    surviving = {item["order_id"] for item in kept}
    empty = {order_id for order_id in removed_orders if order_id not in surviving}
    _drop_orders(tables, empty)
    return tables


INJECTORS = {
    "order_drop": inject_order_drop,
    "freight_up": inject_freight_up,
    "delivery_delay": inject_delivery_delay,
    "mix_shift": inject_mix_shift,
    "price_cut": inject_price_cut,
    "seller_drop": inject_seller_drop,
}


def inject(tables: dict, spec: dict, rng: random.Random) -> dict:
    """按 spec['type'] 分派到对应注入器；spec 其余字段为注入参数。"""
    kind = spec["type"]
    if kind not in INJECTORS:
        raise ValueError(f"未知注入类型 {kind!r}")
    return INJECTORS[kind](tables, spec, rng)


def rng_for(case_id: str) -> random.Random:
    """以案例 ID 为种子的 RNG：注入的随机性唯一来源，保证可复现。"""
    return random.Random(f"anomaly:{case_id}")


def materialize(case: dict, base_folder: Path, target_root: Path) -> Path:
    """物化一个注入案例：复制基础数据版本目录，重写受影响表，写 metadata.json。

    目标目录为 ``target_root / case['id']``；已存在时先删除（属于 artifacts 下的可重建产物）。
    metadata 以基础版本为准，补上注入说明（案例 ID、类型与参数）；数据集 UUID 沿用基础版本的，
    物化目录只供评测读取，不注册进 DatasetStore 的项目索引。
    """
    import json

    base_folder = Path(base_folder)
    target = Path(target_root) / case["id"]
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(base_folder, target)
    tables = olist.read_tables(base_folder)
    injected = inject(tables, case["injection"], rng_for(case["id"]))
    olist.write_tables(target, injected)
    metadata = json.loads((base_folder / "metadata.json").read_text(encoding="utf-8"))
    metadata["profile"] = {**metadata.get("profile", {}),
                           "anomaly_injection": {"case": case["id"], "injection": case["injection"]}}
    (target / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def base_version_of(store: DatasetStore, project_id: str) -> tuple[str, Path]:
    """取项目下的基础数据版本（评测用单一基础版本；多个时取目录中最新的一个）。"""
    versions = []
    for child in Path(store.root).iterdir():
        metadata = child / "metadata.json"
        if not metadata.is_file():
            continue
        import json
        record = json.loads(metadata.read_text(encoding="utf-8"))
        if record.get("project_id") == project_id and record.get("schema") == olist.SCHEMA \
                and "anomaly_injection" not in record.get("profile", {}):
            versions.append((child.stat().st_mtime, record["dataset_version"], child))
    if not versions:
        raise FileNotFoundError(f"项目 {project_id!r} 下没有 Olist 基础数据版本；请先运行 build.py --import-base")
    versions.sort()
    _, version, folder = versions[-1]
    return version, folder
