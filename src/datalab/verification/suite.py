"""服务端核验的统一入口：工作流与固定演示路径共用同一组检查，避免两处胶水代码各自漂移。

顺序与成本：独立 SQL 参考与分组合计、下载表、结果不变量、两份服务端参考一致性都是宿主内的确定性计算，
每次都运行；蜕变关系每条需要额外的沙箱运行，只在显式开启且前面的检查全部通过时运行
（前面已失败时结论不变，再花沙箱时间没有意义）。
"""
from pathlib import Path

from datalab.contracts import AnalysisPlan, VerificationReport
from datalab.verification import invariants, metamorphic
from datalab.verification.python_reference import agreement
from datalab.verification.reference import verify
from datalab.verification.table import table_issue


def run_checks(orders: Path, plan: AnalysisPlan, actual: dict, table: Path, *,
               sandbox: metamorphic.Sandbox | None = None) -> VerificationReport:
    report = verify(orders, plan, actual)
    # 下载表问题只报告类型与位置（行号、列名），不含期望内容。
    issue = table_issue(table, report.reference)
    download = {"name": "download_table", "passed": issue is None}
    if issue:
        download["issues"] = [issue]
    report.checks.append(download)
    report.checks.append(invariants.check(plan, actual))
    report.checks.append(agreement(orders, plan, report.reference))
    if sandbox is not None and all(item["passed"] for item in report.checks):
        report.checks.append(metamorphic.check(plan, orders, actual, sandbox))
    report.passed = all(item["passed"] for item in report.checks)
    return report
