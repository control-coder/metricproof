"""三角色对接真实执行器，核验端只读访问本任务尝试。"""
import json
import threading
from uuid import uuid4

from datalab.contracts import ExecutionManifest, VerificationReport
from datalab.execution.runner import DockerRunner, OutputSpec
from datalab.orchestration.workflow import Task
from datalab.verification.metamorphic import Sandbox, runner_execute
from datalab.verification.suite import run_checks


class ExecutionServices:
    def __init__(self, runner: DockerRunner, *, metamorphic: bool = False):
        self.runner = runner
        # 蜕变关系每条多一次沙箱运行；当前六类都有独立参考，默认关闭，见 docs/architecture.md。
        self.metamorphic = metamorphic

    def profile(self, task: Task) -> dict:
        version = self.runner.store.get(task.dataset_version, task.project_id)
        return {"dataset_family": version.dataset_family, "schema_signature": version.schema_signature,
                "currency": version.currency, "timezone": version.timezone, "quality": version.profile}

    def execute(self, task: Task, code: str, timeout: float, cancelled: threading.Event) -> ExecutionManifest:
        return self.runner.execute_python(str(uuid4()), task.dataset_version, code, timeout, OutputSpec(),
            project_id=task.project_id, metric_version=task.plan.metric_version, cancelled=cancelled)

    def code(self, manifest: ExecutionManifest) -> str:
        """读回某次执行尝试实际运行的代码，恢复核验检查点时用于下一次反馈。"""
        try:
            return (self.runner.root / manifest.run_id / "input/analysis.py").read_text(encoding="utf-8")
        except OSError:
            return ""

    def verify(self, task: Task, manifest: ExecutionManifest) -> VerificationReport:
        if not any(item is manifest for item in task.attempts):
            raise PermissionError("不能核验其他任务产物")
        self.runner.store.get(task.dataset_version, task.project_id)
        folder = self.runner.root / manifest.run_id
        try:
            try:
                actual = json.loads((folder / "output/result.json").read_text(encoding="utf-8"))
            except FileNotFoundError:
                return VerificationReport(False, [{"name": "artifact_format", "passed": False, "issues": [
                    {"path": "result.json", "issue": "file_missing"}]}])
            except (UnicodeError, ValueError):
                return VerificationReport(False, [{"name": "artifact_format", "passed": False, "issues": [
                    {"path": "result.json", "issue": "invalid_json"}]}])
            if not isinstance(actual, dict):
                return VerificationReport(False, [{"name": "artifact_format", "passed": False, "issues": [
                    {"path": "result.json", "issue": "type", "expected_type": "object"}]}])
            sandbox = None
            if self.metamorphic:
                # 重跑的是本次尝试实际执行的同一份代码，剩余时间取任务预算。
                sandbox = Sandbox(self.code(manifest), runner_execute(self.runner, task.plan, task.project_id),
                                  task.budget.remaining_time)
            return run_checks(self.runner.store.directory(task.dataset_version) / "orders.csv", task.plan, actual,
                              folder / "output/table.csv", sandbox=sandbox)
        except (OSError, ValueError, TypeError, AttributeError, KeyError, RecursionError):
            return VerificationReport(False, [{"name": "artifact_format", "passed": False}])
