"""受限执行控制与真实容器检查；未开启集成时明确跳过。"""
import base64
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from datalab.datasets.service import DatasetStore
from datalab.contracts import AnalysisPlan
from datalab.execution.runner import DockerRunner, ExecutionError, OutputSpec
from datalab.execution.pipeline import run_fixed

ROOT = Path(__file__).resolve().parents[2]


class RunnerFixture(unittest.TestCase):
    def setUp(self):
        scratch = (ROOT / ".tmp").resolve()
        self.assertTrue(scratch.is_relative_to(ROOT))
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch, prefix="executor-")
        self.addCleanup(self.temp.cleanup)
        self.store = DatasetStore(Path(self.temp.name) / "datasets")
        self.version = self.store.import_csv((ROOT / "examples/orders.csv").read_bytes(),
            project_id="demo", dataset_family="orders", currency="CNY", timezone="Asia/Shanghai")
        with patch("datalab.execution.runner.shutil.which", return_value="docker"):
            self.runner = DockerRunner(self.store, Path(self.temp.name) / "runs")


class RunnerTests(RunnerFixture):
    def test_command_has_required_isolation(self):
        command = self.runner.command("test", Path(self.temp.name), "sha256:" + "a" * 64, OutputSpec())
        for flag, value in {"--network": "none", "--user": "65532:65532", "--cap-drop": "ALL",
                            "--security-opt": "no-new-privileges=true", "--memory": "256m",
                            "--memory-swap": "256m", "--pids-limit": "32", "--cpus": "1"}.items():
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertIn("--read-only", command)
        self.assertTrue(command[command.index("--mount") + 1].endswith("target=/input,readonly"))
        self.assertNotIn("unconfined", " ".join(command))
        self.assertNotIn("docker.sock", " ".join(command))
        self.assertNotIn("--env", command)

    def test_output_spec_rejects_escape(self):
        for name in ("../x.json", "/a.json", "a/b.csv", "a\\b.csv", "CON", "x.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                OutputSpec(files=(name,))

    def test_output_protocol_rejects_malformed_and_large(self):
        spec = OutputSpec(files=("x.json",), max_bytes=3)
        payload = json.dumps({"outputs": {"x.json": base64.b64encode(b"123").decode()}}).encode()
        self.assertEqual(DockerRunner.decode_output(payload, spec), {"x.json": b"123"})
        for value in (b"garbage", b'{"outputs":{"../x.json":"eA=="}}',
                      b'{"outputs":{"x.json":"%%%%"}}', b'{"outputs":{"x.json":"MTIzNA=="}}'):
            with self.assertRaises(ExecutionError):
                DockerRunner.decode_output(value, spec)

    def test_cross_project_denied_before_docker(self):
        with patch.object(self.runner, "_control") as control:
            with self.assertRaises(PermissionError):
                self.runner.execute_python(str(uuid4()), self.version.dataset_version, "", 1, OutputSpec(),
                    project_id="other", metric_version="trusted-v1")
            control.assert_not_called()

    def test_cancelled_before_launch(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch.object(self.runner, "_control") as control:
            result = self.runner.execute_python(str(uuid4()), self.version.dataset_version, "", 1, OutputSpec(),
                project_id="demo", metric_version="trusted-v1", cancelled=cancelled)
            self.assertEqual(result.status, "CANCELLED")
            control.assert_not_called()
            self.assertTrue((self.runner.root / result.run_id / "manifest.json").exists())

    def test_docker_failure_is_recorded_no_host_fallback(self):
        with patch.object(self.runner, "_control", side_effect=ExecutionError("不可用")):
            result = self.runner.execute_python(str(uuid4()), self.version.dataset_version,
                "raise RuntimeError('禁止宿主运行')", 1, OutputSpec(), project_id="demo", metric_version="trusted-v1")
            self.assertEqual(result.status, "FAILED")
            self.assertEqual(result.output_paths, [])


@unittest.skipUnless(os.environ.get("DATALAB_DOCKER_TESTS") == "1", "需显式启用真实 Docker 集成")
class DockerIntegrationTests(RunnerFixture):
    """只在已构建专用镜像时运行；不自动拉取镜像。"""

    def test_real_csv_to_verified_report(self):
        report = run_fixed(self.runner, AnalysisPlan(self.version.dataset_version, "trusted-v1", group_by="channel"), "demo")
        self.assertEqual(report["status"], "SUCCEEDED", report)
        self.assertTrue(report["verification"]["passed"])
        self.assert_no_container(report["run_id"])

    def test_real_metamorphic_reruns_in_same_sandbox(self):
        # 固定代码在真实容器中满足三条蜕变关系；每次重跑都是独立容器且被清理。
        plan = AnalysisPlan(self.version.dataset_version, "trusted-v1", kind="trend", group_by="payment_date",
                            included_statuses=("paid",))
        before = {path.name for path in self.runner.root.iterdir()} if self.runner.root.exists() else set()
        report = run_fixed(self.runner, plan, "demo", metamorphic=True)
        self.assertEqual(report["status"], "SUCCEEDED", report)
        final = report["verification"]["checks"][-1]
        self.assertEqual((final["name"], final["passed"]), ("metamorphic", True), final)
        self.assertEqual({item["status"] for item in final["relations"]}, {"passed"})
        runs = {path.name for path in self.runner.root.iterdir()} - before
        self.assertEqual(len(runs), 5)
        for run_id in runs:
            self.assert_no_container(run_id)

    def assert_no_container(self, run_id):
        metadata = json.loads((self.runner.root / run_id / "manifest.json").read_text(encoding="utf-8"))
        remaining = self.runner._control("ps", "-aq", "--filter", "name=^/" + metadata["container_name"] + "$")
        self.assertEqual(remaining, "")

    def run_code(self, code, timeout=10, cancelled=None):
        return self.runner.execute_python(str(uuid4()), self.version.dataset_version, code, timeout,
            OutputSpec(files=("result.json",)), project_id="demo", metric_version="trusted-v1", cancelled=cancelled)

    def test_real_permissions_network_and_file_isolation(self):
        code = """
import os, socket, json
from pathlib import Path
assert os.getuid() == 65532
assert set(p.name for p in Path('/input').iterdir()) == {'orders.csv', 'analysis.py', 'runner.py'}
assert not Path('/var/run/docker.sock').exists()
assert not Path('/workspace').exists()
assert not any('KEY' in k or 'TOKEN' in k for k in os.environ)
status = Path('/proc/self/status').read_text()
assert 'Seccomp:\\t2' in status
assert 'NoNewPrivs:\\t1' in status
assert 'CapEff:\\t0000000000000000' in status
for path in ['/input/orders.csv', '/escape.txt', '/tmp/escape.txt']:
    try:
        open(path, 'w')
    except OSError:
        pass
    else:
        raise AssertionError('越界可写')
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
except OSError:
    pass
else:
    raise AssertionError('网络未隔离')
Path('/output/result.json').write_text(json.dumps({'isolated': True}))
"""
        result = self.run_code(code)
        self.assertEqual(result.status, "COMPLETED", result)
        self.assert_no_container(result.run_id)

    def test_real_timeout_cleanup(self):
        result = self.run_code("while True: pass", timeout=0.5)
        self.assertEqual(result.status, "TIMED_OUT", result)
        self.assert_no_container(result.run_id)

    def test_real_cancel_cleanup(self):
        event = threading.Event()
        timer = threading.Timer(2, event.set)
        timer.start()
        try:
            result = self.run_code("while True: pass", cancelled=event)
            self.assertEqual(result.status, "CANCELLED", result)
            self.assert_no_container(result.run_id)
        finally:
            timer.cancel()

    def test_real_symlink_rejected(self):
        result = self.run_code("import os; os.symlink('/input/orders.csv', '/output/result.json')")
        self.assertEqual(result.status, "FAILED", result)
        self.assert_no_container(result.run_id)

    def test_real_output_disk_limit(self):
        result = self.run_code("with open('/output/result.json','wb') as f:\n while True: f.write(b'x'*1048576)")
        self.assertEqual(result.status, "FAILED", result)
        self.assert_no_container(result.run_id)

    def test_real_memory_limit(self):
        result = self.run_code("blocks=[]\nwhile True: blocks.append(bytearray(16*1024*1024))")
        self.assertEqual(result.status, "FAILED", result)
        self.assert_no_container(result.run_id)
