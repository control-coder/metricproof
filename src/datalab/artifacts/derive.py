"""核验通过后派生图表与说明；固定演示与模型两条路径都走这里，输入只有 result.json 与服务端元数据。"""
from __future__ import annotations

import json
from pathlib import Path

from datalab.artifacts.charts import write_charts
from datalab.artifacts.narrative import describe

# 派生文件放在容器输出目录之外，避免与 Analyst 产物混淆；文件名固定。
DERIVED_DIR = "derived"
NARRATIVE_FILE = "narrative.json"


# 报告结论：每类分析一条，指向已核验参考结果中的一个总计字段。订单级金额分析（单表与 Olist）沿用原句式。
# (识别该类结果的总计键, 引用的总计键, 句式, 是否为分)
CLAIMS = (("amount_cents", "amount_cents", "确认口径下订单金额合计 {}", True),
          ("repeat_rate", "repeat_rate", "确认口径下复购率 {}", False),
          ("late_rate", "late_rate", "确认口径下送达延迟率 {}", False),
          ("price_cents", "price_cents", "确认口径下纳入订单的商品金额合计 {}", True),
          ("last_complete_month", "customer_count", "确认口径下进入留存 cohort 的客户共 {} 位", False))


def claims(reference: dict, artifact_id: str) -> list[dict]:
    """由独立参考结果生成报告结论；比值为 null 时写明不计算。"""
    total = reference.get("total") or {}
    for marker, key, template, cents in CLAIMS:
        if marker in total:
            value = total[key]
            text = f"{value / 100:.2f}" if cents else ("不计算（分母为 0）" if value is None else str(value))
            return [{"text": template.format(text), "artifact_id": artifact_id, "result_pointer": f"/total/{key}"}]
    return []


def derive(run_folder: Path, plan: dict, *, metric_version: str, included_statuses, currency: str,
           data_range: tuple[str, str] | None) -> dict:
    """读取本次尝试的 result.json，写入 derived/ 下的图表与说明，返回索引信息。

    只应在独立核验通过后调用：此时 result.json 与独立参考逐项一致，派生内容不会展示未经核验的数。
    """
    result = json.loads((run_folder / "output/result.json").read_text(encoding="utf-8"))
    folder = run_folder / DERIVED_DIR
    charts = write_charts(result, plan, folder, currency=currency)
    narrative = describe(result, plan, metric_version=metric_version, included_statuses=included_statuses,
                         currency=currency, data_range=data_range)
    narrative["charts"] = [item["title"] for item in charts]
    (folder / NARRATIVE_FILE).write_text(json.dumps(narrative, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"narrative": narrative, "charts": charts,
            "files": {"分析说明.json": f"{DERIVED_DIR}/{NARRATIVE_FILE}",
                      **{f"图表-{index}.png": f"{DERIVED_DIR}/{item['file']}" for index, item in enumerate(charts, 1)}}}
