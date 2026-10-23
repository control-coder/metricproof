"""将已确认的分析计划装配为可审计的固定脚本。"""
from dataclasses import replace
import json
from pathlib import Path
from datalab.contracts import AnalysisPlan


def fixed_code(plan: AnalysisPlan) -> str:
    payload = repr(json.dumps(plan.payload(), ensure_ascii=False))
    if plan.schema == "olist":
        # 多表固定分析读取服务端投影到 /input 的输入表。
        source = Path(__file__).with_name("fixed_olist.py").read_text(encoding="utf-8")
        return source + f'\nPLAN = json.loads({payload})\nexport_result(analyze("/input", PLAN), "/output")\n'
    source = Path(__file__).with_name("fixed_analysis.py").read_text(encoding="utf-8")
    return source + f'\nPLAN = json.loads({payload})\nexport_result(analyze("/input/orders.csv", PLAN), "/output")\n'


def rebind(script: str, plan: AnalysisPlan, changes: dict) -> str | None:
    """脚本正是该计划的固定脚本时，按改动后的计划重新生成；否则返回 None。供服务端蜕变核验使用。"""
    if script != fixed_code(plan):
        return None
    return fixed_code(replace(plan, **changes))
