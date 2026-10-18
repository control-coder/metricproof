"""确定性模拟角色验收；不调用真实模型、不在宿主执行角色代码。"""
from copy import deepcopy
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from datalab.contracts import AnalysisPlan, ExecutionManifest, VerificationReport
from datalab.roles.runtime import (
    PERMISSIONS, BoundTools, BudgetExceeded, CallBudget, ModelCallError, ModelReply, Role, RoleRuntime)
from datalab.roles.model_config import ModelConfig
from datalab.orchestration.workflow import LONGEST_PATH_CALLS, MetricSnapshot, State, Task, Workflow, check_feedback


class FakeBackend:
    """返回固定契约，只用于测试控制流而非模型质量。"""

    def __init__(self):
        self.contexts = []
        self.confirm = False
        self.wrong_version = False
        self.reviewer_accepts = True
        # 依次返回的计划字段；用完后重复最后一个，用于模拟 Reviewer 驳回后的复核。
        self.plans = [{}]
        # 依次对 Reviewer/复核 Planner 抛出的异常或返回的原始内容，用于模拟截断、超时与契约错误。
        self.reviewer_reply = None
        self.replan_error = None

    def invoke(self, role, context, *, timeout, token_limit):
        self.contexts.append(deepcopy(context))
        handoff = context["handoff"]
        if role == Role.PLANNER:
            if self.replan_error and "review" in handoff:
                raise self.replan_error
            if self.confirm:
                return ModelReply({"confirmation": "请明确需要比较的期间"}, 10)
            metric = handoff["metric"]
            fields = self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]
            plan = AnalysisPlan(handoff["dataset_version"], "wrong-version" if self.wrong_version else metric["version"],
                                included_statuses=tuple(metric["included_statuses"]), **fields)
            return ModelReply({"plan": plan.payload()}, 10)
        if role == Role.ANALYST:
            # 只生成标记，测试服务不会执行此代码。
            return ModelReply({"code": "# 模拟分析角色代码"}, 20)
        if isinstance(self.reviewer_reply, Exception):
            raise self.reviewer_reply
        if self.reviewer_reply is not None:
            return ModelReply(self.reviewer_reply, 10)
        accepts = self.reviewer_accepts
        if isinstance(accepts, list):
            accepts = accepts.pop(0) if len(accepts) > 1 else accepts[0]
        return ModelReply({"accepted": accepts, "feedback": "按问题检查计划"}, 10)


class FakeServices:
    """只模拟清单和核验结果，不构造容器验证假象。"""

    def __init__(self):
        self.execution_statuses = ["COMPLETED"]
        self.checks = [True]
        self.executions = 0
        self.verifies = 0

    def profile(self, task):
        return {"dataset_family": "orders", "schema_signature": "schema-v1", "quality": {"row_count": 6}}

    def execute(self, task, code, timeout, cancelled):
        index = min(self.executions, len(self.execution_statuses) - 1)
        self.executions += 1
        return ExecutionManifest(str(uuid4()), task.dataset_version, task.plan.metric_version, "test-only",
            "input/analysis.py", ["output/result.json"], 0, self.execution_statuses[index])

    def verify(self, task, manifest):
        index = min(self.verifies, len(self.checks) - 1)
        self.verifies += 1
        check = {"name": "test_reference", "passed": self.checks[index]}
        if not self.checks[index]:
            # expected 模拟核验端误带的数值，工作流必须过滤掉。
            check["issues"] = [{"path": "comparison.growth", "issue": "value", "expected": "0.123456"}]
        return VerificationReport(self.checks[index], [check], getattr(self, "reference", {}))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.services = FakeServices()
        self.workflow = Workflow(RoleRuntime(self.backend), self.services)
        self.metric = MetricSnapshot("confirmed-v1", ("paid",), "trusted-test-fixture", "demo", "orders", "schema-v1")
        self.task = Task("demo", str(uuid4()), "汇总订单金额", metric=self.metric)

    def test_success_three_independent_contexts(self):
        task = self.workflow.run(self.task)
        self.assertEqual(task.state, State.SUCCEEDED)
        self.assertEqual([item["role"] for item in self.backend.contexts], ["Planner", "Analyst", "Reviewer"])
        self.assertNotIn("profile", self.backend.contexts[1]["handoff"])
        self.assertNotIn("code", self.backend.contexts[2]["handoff"])
        self.assertEqual(task.budget.calls, 3)
        self.assertEqual(task.budget.tokens, 40)

    def test_reference_failure_requires_feedback_even_if_reviewer_accepts(self):
        self.services.checks = [False, True]
        task = self.workflow.run(self.task)
        self.assertEqual(task.state, State.SUCCEEDED)
        self.assertEqual(task.review_feedbacks, 1)
        self.assertEqual(self.services.executions, 2)
        # 核验失败直接回传 Analyst，不调用 Reviewer：Planner、Analyst×2、Reviewer。
        self.assertEqual([item["role"] for item in self.backend.contexts], ["Planner", "Analyst", "Analyst", "Reviewer"])
        self.assertNotIn("review", self.backend.contexts[2]["handoff"]["feedback"])

    def test_reviewer_sees_question_but_not_reference_numbers(self):
        self.services.reference = {"total": {"amount_cents": 12345}}
        task = self.workflow.run(self.task)
        self.assertEqual(task.state, State.SUCCEEDED)
        review = self.backend.contexts[2]["handoff"]
        self.assertEqual(review["question"], "汇总订单金额")
        # 数据语义家族、结构签名属于服务端范围校验，不进入模型可见的概况。
        self.assertNotIn("dataset_family", review["profile"])
        self.assertEqual(review["profile"]["quality"]["row_count"], 6)
        self.assertNotIn("12345", str(review))
        self.assertNotIn("checks", review["plan"])
        self.assertNotIn("checks", self.backend.contexts[1]["handoff"]["plan"])

    def test_rejection_goes_to_planner_and_unchanged_plan_keeps_verified_result(self):
        self.backend.reviewer_accepts = [False]
        task = self.workflow.run(self.task)
        self.assertEqual(task.state, State.SUCCEEDED)
        self.assertEqual([item["role"] for item in self.backend.contexts], ["Planner", "Analyst", "Reviewer", "Planner"])
        self.assertEqual(self.backend.contexts[3]["handoff"]["review"]["feedback"], "按问题检查计划")
        self.assertEqual((task.replans, self.services.executions), (1, 1))
        self.assertIn("维持原计划", task.timeline[-1]["note"])

    def test_revised_plan_is_executed_and_reviewed_again(self):
        self.backend.reviewer_accepts = [False, True]
        self.backend.plans = [{}, {"kind": "ranking", "group_by": "channel"}]
        task = self.workflow.run(self.task)
        self.assertEqual(task.state, State.SUCCEEDED)
        self.assertEqual((task.plan.kind, task.plan.group_by, self.services.executions), ("ranking", "channel", 2))
        self.assertEqual(task.budget.calls, 6)

    def test_longest_fallback_path_fits_default_budget(self):
        # 代码修正、核验反馈、驳回后重规划全部触发：Planner×2、Analyst×4、Reviewer×2。
        self.services.execution_statuses = ["FAILED", "COMPLETED"]
        self.services.checks = [False, True]
        self.backend.reviewer_accepts = [False, True]
        self.backend.plans = [{}, {"kind": "ranking", "group_by": "channel"}]
        task = self.workflow.run(self.task)
        self.assertEqual(task.state, State.SUCCEEDED)
        self.assertEqual([item["role"] for item in self.backend.contexts],
                         ["Planner", "Analyst", "Analyst", "Analyst", "Reviewer", "Planner", "Analyst", "Reviewer"])
        self.assertEqual(task.budget.calls, LONGEST_PATH_CALLS)
        self.assertEqual(task.budget.max_calls, LONGEST_PATH_CALLS)
        self.assertEqual(ModelConfig('mimo', {}).max_calls, LONGEST_PATH_CALLS)

    def test_replan_not_started_without_enough_calls_keeps_verified_result(self):
        # 预算在驳回时只剩 2 次，走不完重规划、重新执行和再审查：不开始，结果以独立核验为准。
        self.services.execution_statuses = ["FAILED", "COMPLETED"]
        self.services.checks = [False, True]
        self.backend.reviewer_accepts = [False]
        self.task.budget = CallBudget(max_calls=LONGEST_PATH_CALLS - 1)
        task = self.workflow.run(self.task)
        self.assertEqual((task.state, task.failure, task.replans), (State.SUCCEEDED, None, 0))
        self.assertEqual(len(self.backend.contexts), 5)
        self.assertIn("剩余调用不足以完成重规划", task.timeline[-1]["note"])
        self.assertIn("按问题检查计划", task.timeline[-1]["note"])

    def test_second_rejection_after_revision_fails(self):
        self.backend.reviewer_accepts = [False]
        self.backend.plans = [{}, {"kind": "ranking", "group_by": "channel"}]
        task = self.workflow.run(self.task)
        self.assertEqual((task.state, task.failure), (State.FAILED, "REVIEW_REJECTED"))

    def test_verification_feedback_locates_error_without_values(self):
        self.services.checks = [False, True]
        task = self.workflow.run(self.task)
        self.assertEqual(task.state, State.SUCCEEDED)
        checks = self.backend.contexts[2]["handoff"]["feedback"]["checks"]
        self.assertEqual(checks[0]["issues"], [{"path": "comparison.growth", "issue": "value"}])
        self.assertNotIn("0.123456", str(self.backend.contexts[2]))

    def test_check_feedback_whitelists_issue_keys(self):
        report = VerificationReport(False, [{"name": "download_table", "passed": False, "detail": "x",
            "issues": [{"issue": "row_content", "row": 1, "columns": ["share"], "value": "0.5"}]}], {"total": 1})
        self.assertEqual(check_feedback(report), [{"name": "download_table", "passed": False,
            "issues": [{"issue": "row_content", "row": 1, "columns": ["share"]}]}])

    def test_reviewer_failures_fall_back_to_verified_result(self):
        cases = [ModelCallError("模型输出达到长度上限被截断"), ModelCallError("模型请求超时"),
                 {"accepted": "yes", "feedback": 1}]
        for reply in cases:
            with self.subTest(reply=str(reply)):
                self.setUp()
                self.backend.reviewer_reply = reply
                task = self.workflow.run(self.task)
                self.assertEqual((task.state, task.failure, task.replans), (State.SUCCEEDED, None, 0))
                self.assertIn("跳过咨询性审查", task.timeline[-1]["note"])
                self.assertIn("Reviewer 调用失败", task.review_skipped)

    def test_replan_failure_keeps_verified_result_and_feedback(self):
        self.backend.reviewer_accepts = [False]
        self.backend.replan_error = ModelCallError("模型请求超时")
        task = self.workflow.run(self.task)
        self.assertEqual((task.state, task.replans), (State.SUCCEEDED, 1))
        self.assertIn("Planner 复核调用失败", task.timeline[-1]["note"])
        self.assertIn("按问题检查计划", task.timeline[-1]["note"])

    def test_reference_failure_never_self_approved(self):
        self.services.checks = [False]
        task = self.workflow.run(self.task)
        self.assertEqual(task.state, State.FAILED)
        self.assertEqual(task.failure, "VERIFICATION_FAILED")
        self.assertEqual(self.services.executions, 2)

    def test_execution_has_only_one_correction(self):
        self.services.execution_statuses = ["FAILED"]
        task = self.workflow.run(self.task)
        self.assertEqual(task.state, State.FAILED)
        self.assertEqual(task.code_corrections, 1)
        self.assertEqual(self.services.executions, 2)

    def test_one_execution_correction_can_succeed(self):
        self.services.execution_statuses = ["FAILED", "COMPLETED"]
        task = self.workflow.run(self.task)
        self.assertEqual(task.state, State.SUCCEEDED)
        self.assertEqual(task.code_corrections, 1)

    def test_budget_exhaustion_stops_before_extra_role(self):
        self.task.budget = CallBudget(max_calls=1)
        task = self.workflow.run(self.task)
        self.assertEqual(task.state, State.FAILED)
        self.assertEqual(task.failure, "BUDGET_EXHAUSTED")
        self.assertEqual((len(self.backend.contexts), self.services.executions), (1, 0))

    def test_budget_exhausted_at_review_keeps_verified_result(self):
        # 预算只够 Planner 与 Analyst：Reviewer 不再调用，结果仍以独立核验为准。
        self.task.budget = CallBudget(max_calls=2)
        task = self.workflow.run(self.task)
        self.assertEqual((task.state, len(self.backend.contexts)), (State.SUCCEEDED, 2))
        self.assertIn("BudgetExceeded", task.review_skipped)

    def test_missing_metric_waits_without_worker_calls(self):
        self.task.metric = None
        task = self.workflow.run(self.task)
        self.assertEqual(task.state, State.WAITING_CONFIRMATION)
        self.assertEqual(self.backend.contexts, [])
        self.assertEqual(self.services.executions, 0)
        self.assertEqual(self.workflow.resume(task, self.metric).state, State.SUCCEEDED)

    def test_planner_can_request_confirmation_and_resume(self):
        self.backend.confirm = True
        self.assertEqual(self.workflow.run(self.task).state, State.WAITING_CONFIRMATION)
        self.backend.confirm = False
        self.assertEqual(self.workflow.resume(self.task, self.metric).state, State.SUCCEEDED)
        self.assertEqual(self.task.budget.calls, 4)

    def test_cross_project_rule_rejected(self):
        self.task.metric = MetricSnapshot("v1", ("paid",), "trusted", "other", "orders", "schema-v1")
        self.assertEqual(self.workflow.run(self.task).state, State.FAILED)
        self.assertEqual(self.services.executions, 0)

    def test_planner_cannot_replace_confirmed_metric(self):
        self.backend.wrong_version = True
        self.assertEqual(self.workflow.run(self.task).state, State.FAILED)
        self.assertEqual(self.services.executions, 0)

    def test_cancel_and_terminal_delivery_do_not_execute_again(self):
        event = threading.Event()
        event.set()
        self.assertEqual(self.workflow.run(self.task, cancelled=event).state, State.CANCELLED)
        self.assertEqual(self.workflow.run(self.task).state, State.CANCELLED)
        self.assertEqual(self.services.executions, 0)

    def test_role_permissions_checked_at_dispatch(self):
        handlers = {"execute_python": lambda: "执行", "verify": lambda: "核验"}
        with self.assertRaises(PermissionError):
            BoundTools(Role.PLANNER, handlers).call("execute_python")
        with self.assertRaises(PermissionError):
            BoundTools(Role.REVIEWER, handlers).call("execute_python")
        with self.assertRaises(PermissionError):
            BoundTools(Role.ANALYST, handlers).call("verify")
        with self.assertRaises(PermissionError):
            BoundTools(Role.ANALYST, handlers).call("bash")
        self.assertEqual(BoundTools(Role.ANALYST, handlers).call("execute_python"), "执行")

    def test_each_role_exposes_exactly_the_tool_it_is_bound_to(self):
        self.assertEqual({role: set(tools) for role, tools in PERMISSIONS.items()},
                         {Role.PLANNER: {"profile"}, Role.ANALYST: {"execute_python"}, Role.REVIEWER: {"verify"}})
        self.workflow.run(self.task)
        self.assertEqual([item["allowed_tools"] for item in self.backend.contexts],
                         [["profile"], ["execute_python"], ["verify"]])

    def test_token_and_time_budget(self):
        budget = CallBudget(max_tokens=1)
        budget.begin_call()
        with self.assertRaises(BudgetExceeded):
            budget.finish_call(2)
        budget = CallBudget(seconds=1, started=0)
        with self.assertRaises(BudgetExceeded):
            budget.begin_call()

    def test_success_cannot_be_overwritten_by_another_delivery(self):
        self.workflow.run(self.task)
        self.assertEqual(self.workflow.run(self.task).state, State.SUCCEEDED)
        self.assertEqual(self.services.executions, 1)
        with self.assertRaises(ValueError):
            self.task.move(State.EXECUTING, "非法重入")

class BudgetPauseTests(unittest.TestCase):
    def test_waiting_does_not_reset_active_time_consumption(self):
        with patch('datalab.roles.runtime.time.monotonic', return_value=100) as clock:
            budget = CallBudget(seconds=5)
            budget.begin_call()
            clock.return_value = 102
            budget.pause()
            self.assertEqual(budget.spent_seconds, 2)
            self.assertEqual(budget.remaining_time(), 3)
            # 后续调用再次启动计时但仍只有三秒有效预算。
            clock.return_value = 200
            timeout, _ = budget.begin_call()
            self.assertEqual(timeout, 3)
