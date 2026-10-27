"""合成异动题集的加载与完整性校验。

冻结文件为同目录 ``anomaly_cases.json``：规格 + gold + dev/test 划分，含基础数据原始文件的 SHA-256
（取自数据版本 profile 的 source_sha256）。冻结日期见文件中的 frozen_at 字段：题集先于 Agent 开发冻结，
冻结后不再按 Agent 表现修改；任何修改都会改变文件指纹并需要重新走质量检查。

加载时校验：案例计数（40 = 30 注入 + 10 原始）、划分（dev 10 / test 30，注入案例约 7/23、原始 3/7）、
每条案例的字段完整性（id、split、kind、injection_type、question、metric、period、base_period、gold 等）。
"""
from __future__ import annotations

import json
from pathlib import Path

FROZEN_FILE = Path(__file__).with_name("anomaly_cases.json")

REQUIRED_FIELDS = ("id", "split", "kind", "injection_type", "question", "metric", "period", "base_period", "gold")
GOLD_FIELDS = ("dimensions",)
INJECTION_TYPES = ("order_drop", "freight_up", "delivery_delay", "mix_shift", "price_cut", "seller_drop")


class CasesError(ValueError):
    """题集文件不完整或与冻结约定不一致。"""


def load(path: Path | None = None) -> dict:
    """读取冻结题集并做完整性校验；返回 {"meta": ..., "cases": [...]}。"""
    path = Path(path or FROZEN_FILE)
    if not path.is_file():
        raise CasesError(f"题集文件不存在：{path}（先运行 python -B evals/anomaly/build.py 冻结）")
    data = json.loads(path.read_text(encoding="utf-8"))
    validate(data)
    return data


def validate(data: dict) -> None:
    meta = data.get("meta")
    if not isinstance(meta, dict):
        raise CasesError("缺少 meta 段")
    for field in ("frozen_at", "base_version", "source_sha256"):
        if field not in meta:
            raise CasesError(f"meta 缺少字段 {field}")
    cases = data.get("cases")
    if not isinstance(cases, list):
        raise CasesError("缺少 cases 列表")
    injected = [case for case in cases if case["kind"] == "injected"]
    raw = [case for case in cases if case["kind"] == "raw"]
    if len(cases) != 40 or len(injected) != 30 or len(raw) != 10:
        raise CasesError(f"案例计数不符：总数 {len(cases)}、注入 {len(injected)}、原始 {len(raw)}（应为 40/30/10）")
    for split, want_total, want_injected in (("dev", 10, 7), ("test", 30, 23)):
        picked = [case for case in cases if case["split"] == split]
        if len(picked) != want_total or sum(case["kind"] == "injected" for case in picked) != want_injected:
            raise CasesError(f"{split} 划分不符：{len(picked)} 个、其中注入 "
                             f"{sum(case['kind'] == 'injected' for case in picked)} 个")
    ids = set()
    for case in cases:
        missing = [field for field in REQUIRED_FIELDS if field not in case]
        if missing:
            raise CasesError(f"案例 {case.get('id', '?')} 缺少字段 {missing}")
        if case["id"] in ids:
            raise CasesError(f"案例 ID 重复：{case['id']}")
        ids.add(case["id"])
        if case["kind"] == "injected":
            if case["injection_type"] not in INJECTION_TYPES:
                raise CasesError(f"案例 {case['id']} 注入类型未知：{case['injection_type']}")
            if "injection" not in case:
                raise CasesError(f"案例 {case['id']} 缺少注入参数")
        gold = case["gold"]
        if not isinstance(gold, dict) or not gold.get("dimensions"):
            raise CasesError(f"案例 {case['id']} 的 gold 缺少 dimensions")
        for item in gold["dimensions"]:
            if "dimension" not in item or "value" not in item:
                raise CasesError(f"案例 {case['id']} 的 gold 维度项缺少 dimension/value")


def by_split(data: dict, split: str) -> list[dict]:
    return [case for case in data["cases"] if case["split"] == split]
