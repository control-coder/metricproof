"""控制层故障注入，仅验证处理分支，不替代真实容器安全验证。"""
import base64
import io
import json
from unittest.mock import Mock, patch
from uuid import uuid4

from test_execution import RunnerFixture
from datalab.execution.runner import ExecutionError, OutputSpec


class LifecycleTests(RunnerFixture):
    def fake_process(self, payload, *, running=False):
        process = Mock()
        process.stdout = io.BytesIO(payload)
        process.poll.return_value = None if running else 0
        process.returncode = None if running else 0
        return process

    def invoke(self):
        return self.runner.execute_python(str(uuid4()), self.version.dataset_version, "", 0.05,
            OutputSpec(files=("result.json",)), project_id="demo", metric_version="trusted-v1")

    def control(self, *args):
        if args[0] == "image":
            return "sha256:" + "a" * 64
        if args[0] == "inspect":
            return json.dumps({"ExitCode": 0, "OOMKilled": False, "Running": False})
        return ""

    def test_completed_lifecycle_cleans_container(self):
        wire = json.dumps({"outputs": {"result.json": base64.b64encode(b'{}').decode()}}).encode()
        with patch.object(self.runner, "_control", side_effect=self.control) as control, \
             patch("datalab.execution.runner.subprocess.Popen", return_value=self.fake_process(wire)):
            manifest = self.invoke()
            self.assertEqual(manifest.status, "COMPLETED")
            self.assertEqual(control.call_args.args[:2], ("rm", "--force"))
            self.assertEqual((self.runner.root / manifest.run_id / "output/result.json").read_bytes(), b"{}")

    def test_timeout_always_cleans_and_writes_manifest(self):
        with patch.object(self.runner, "_control", side_effect=self.control) as control, \
             patch("datalab.execution.runner.subprocess.Popen", return_value=self.fake_process(b"", running=True)):
            manifest = self.invoke()
            self.assertEqual(manifest.status, "TIMED_OUT")
            self.assertEqual(control.call_args.args[:2], ("rm", "--force"))
            self.assertTrue((self.runner.root / manifest.run_id / "manifest.json").exists())

    def test_cleanup_failure_cannot_report_complete(self):
        def control(*args):
            if args[0] == "rm":
                raise ExecutionError("模拟清理失败")
            return self.control(*args)
        wire = b'{"outputs":{"result.json":"e30="}}'
        with patch.object(self.runner, "_control", side_effect=control), \
             patch("datalab.execution.runner.subprocess.Popen", return_value=self.fake_process(wire)):
            self.assertEqual(self.invoke().status, "CLEANUP_PENDING")

    def test_server_input_uses_same_mount_and_flags(self):
        # 蜕变核验传入的变换数据写进同一个只读输入目录，容器参数与默认路径逐项相同。
        wire = json.dumps({"outputs": {"result.json": base64.b64encode(b'{}').decode()}}).encode()
        commands = []

        def control(*args):
            if args[0] == "create":
                commands.append(args)
            return self.control(*args)
        data = b"order_id,payment_time,amount_cents,status,channel,category\r\nX,2026-08-01T00:00:00,1,paid,a,b\r\n"
        with patch.object(self.runner, "_control", side_effect=control), \
             patch("datalab.execution.runner.subprocess.Popen", side_effect=lambda *a, **k: self.fake_process(wire)):
            default = self.invoke()
            override = self.runner.execute_python(str(uuid4()), self.version.dataset_version, "", 0.05,
                OutputSpec(files=("result.json",)), project_id="demo", metric_version="trusted-v1", orders_csv=data)
        self.assertEqual((default.status, override.status), ("COMPLETED", "COMPLETED"))
        self.assertEqual((self.runner.root / override.run_id / "input/orders.csv").read_bytes(), data)
        original = self.store.directory(self.version.dataset_version) / "orders.csv"
        self.assertEqual((self.runner.root / default.run_id / "input/orders.csv").read_bytes(), original.read_bytes())
        normalize = lambda args: [item for item in args if default.run_id not in item and override.run_id not in item  # noqa: E731
                                  and not item.startswith("datalab-exec-")]
        self.assertEqual(normalize(commands[0]), normalize(commands[1]))

    def test_server_input_limits_checked_before_docker(self):
        with patch.object(self.runner, "_control") as control:
            for data in ("text", b"x" * (32 * 1024 * 1024 + 1)):
                with self.assertRaises(ValueError):
                    self.runner.execute_python(str(uuid4()), self.version.dataset_version, "", 1, OutputSpec(),
                        project_id="demo", metric_version="trusted-v1", orders_csv=data)
            with self.assertRaises(PermissionError):
                self.runner.execute_python(str(uuid4()), self.version.dataset_version, "", 1, OutputSpec(),
                    project_id="other", metric_version="trusted-v1", orders_csv=b"x")
            control.assert_not_called()

    def test_invalid_output_is_not_written(self):
        with patch.object(self.runner, "_control", side_effect=self.control), \
             patch("datalab.execution.runner.subprocess.Popen", return_value=self.fake_process(b"not json")):
            manifest = self.invoke()
            self.assertEqual(manifest.status, "FAILED")
            self.assertFalse((self.runner.root / manifest.run_id / "output").exists())
