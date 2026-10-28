"""可信宿主管理无网络容器；不提供任意宿主命令或降级执行。"""
from __future__ import annotations

import base64
import binascii
import json
import math
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID, uuid4

from datalab.contracts import ExecutionManifest
from datalab.datasets import olist
from datalab.datasets.service import DatasetStore


# 服务端变换输入的上限：数据集本身不超过 20 MB，变换只追加少量行。
MAX_INPUT_BYTES = 32 * 1024 * 1024
# 多表输入的文件名：小写表名加 .csv，不能与 analysis.py、runner.py 冲突。
INPUT_NAME = re.compile(r"[a-z][a-z_]{0,31}\.csv")


class ExecutionError(RuntimeError):
    """执行失败或控制服务不可用；不能视为成功。"""


@dataclass(frozen=True)
class OutputSpec:
    files: tuple[str, ...] = ("result.json", "table.csv")
    max_bytes: int = 8 * 1024 * 1024

    def __post_init__(self):
        if not self.files or len(self.files) > 5 or len(set(self.files)) != len(self.files):
            raise ValueError("输出文件数量无效")
        if any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}\.(json|csv|png)", name) for name in self.files):
            raise ValueError("输出必须是受限的平面文件名")
        if sum(name.endswith(".png") for name in self.files) > 3:
            raise ValueError("最多输出三张图")
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= 8 * 1024 * 1024:
            raise ValueError("输出大小限额无效")


def sandbox_inputs(store: DatasetStore, plan) -> dict:
    """execute_python 的输入参数：单表数据为空（复制规范化订单），Olist 多表数据为服务端按计划投影的输入表。"""
    if plan.schema != "olist":
        return {}
    return {"input_files": olist.dataset_inputs(store.directory(plan.dataset_version), plan)}


class DockerRunner:
    """只向容器传入本次规范化订单、代码和收集器，不挂载宿主工作区。"""

    def __init__(self, store: DatasetStore | None, root: Path, *, image: str = "datalab-executor:py312-v2"):
        self.store = store
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.image = image
        self.docker = shutil.which("docker")
        if not self.docker:
            raise ExecutionError("未安装 Docker；禁止降级为宿主执行")

    def _control(self, *args: str) -> str:
        try:
            result = subprocess.run([self.docker, *args], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=20, check=False, encoding="utf-8")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutionError("Docker 控制命令不可用或超时") from exc
        if result.returncode:
            raise ExecutionError("Docker 控制命令失败；请检查服务与本地镜像")
        return result.stdout.strip()

    def image_id(self) -> str:
        identifier = self._control("image", "inspect", "--format", "{{.Id}}", self.image)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", identifier):
            raise ExecutionError("镜像标识无效")
        return identifier

    def command(self, name: str, input_dir: Path, image_id: str, spec: OutputSpec) -> list[str]:
        mount = str(input_dir.resolve())
        if "," in mount:
            raise ValueError("容器输入路径不支持逗号")
        return [self.docker, "create", "--pull", "never", "--name", name,
                "--label", "datalab.executor=true", "--network", "none", "--read-only",
                "--user", "65532:65532", "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
                "--cpus", "1", "--memory", "256m", "--memory-swap", "256m", "--pids-limit", "32",
                "--ulimit", "nofile=64:64", "--ulimit", "fsize=16777216:16777216",
                "--log-driver", "none", "--ipc", "none",
                "--mount", f"type=bind,source={mount},target=/input,readonly",
                "--tmpfs", "/output:rw,noexec,nosuid,nodev,size=16m,mode=0700,uid=65532,gid=65532",
                "--workdir", "/output", "--entrypoint", "python", image_id,
                "-I", "-B", "/input/runner.py", json.dumps(asdict(spec), separators=(",", ":"))]

    @staticmethod
    def decode_output(raw: bytes, spec: OutputSpec) -> dict[str, bytes]:
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) != {"outputs"}:
                raise ValueError
            outputs = data["outputs"]
            if not isinstance(outputs, dict) or set(outputs) != set(spec.files):
                raise ValueError
            result = {name: base64.b64decode(value, validate=True) for name, value in outputs.items()}
            if sum(len(value) for value in result.values()) > spec.max_bytes:
                raise ValueError
            return result
        except (ValueError, TypeError, KeyError, binascii.Error) as exc:
            raise ExecutionError("容器返回了非法或超限产物") from exc

    def execute_python(self, run_id: str, dataset_version: str, code: str, timeout: float,
                       output_spec: OutputSpec, *, project_id: str, metric_version: str,
                       cancelled: threading.Event | None = None, orders_csv: bytes | None = None,
                       input_files: dict[str, bytes] | None = None) -> ExecutionManifest:
        """orders_csv 只供服务端核验传入本任务数据的变换版本（如蜕变关系），仍写入同一只读输入挂载，
        容器参数与默认路径完全相同；不接受模型或请求方提供的内容。

        input_files 是多表数据（Olist）由服务端按计划投影出的输入表（文件名 → CSV 字节），写入同一只读挂载；
        多表数据版本必须提供，单表数据版本不接受。"""
        self._validate(run_id, code, timeout, orders_csv, input_files)
        version = self.store.get(dataset_version, project_id)
        if (version.schema == "orders") != (input_files is None):
            raise ValueError("单表数据使用规范化订单，多表数据必须由服务端按计划投影输入")
        return self._run(run_id, dataset_version, code, timeout, output_spec,
                         metric_version=metric_version, cancelled=cancelled,
                         orders_csv=orders_csv, input_files=input_files)

    def execute_inputs(self, run_id: str, input_files: dict[str, bytes], code: str, timeout: float,
                       output_spec: OutputSpec, *, dataset_version: str, metric_version: str,
                       cancelled: threading.Event | None = None) -> ExecutionManifest:
        """不查 DatasetStore 的固定输入执行：输入表已由服务端投影为文件名 → CSV 字节（如 run_sql），
        校验、容器参数、清理与执行清单和 execute_python 完全一致。"""
        if input_files is None:
            raise ValueError("固定输入执行必须提供服务端投影的输入表")
        self._validate(run_id, code, timeout, None, input_files)
        return self._run(run_id, dataset_version, code, timeout, output_spec,
                         metric_version=metric_version, cancelled=cancelled, input_files=input_files)

    @staticmethod
    def _validate(run_id: str, code: str, timeout: float, orders_csv: bytes | None,
                  input_files: dict[str, bytes] | None):
        if str(UUID(run_id)) != run_id:
            raise ValueError("任务标识必须为规范 UUID")
        if not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError("执行时限必须位于 0 到 120 秒之间")
        if len(code.encode("utf-8")) > 256 * 1024:
            raise ValueError("代码超过限额")
        if orders_csv is not None and (type(orders_csv) is not bytes or len(orders_csv) > MAX_INPUT_BYTES):
            raise ValueError("服务端输入超过限额")
        if input_files is not None and (
                orders_csv is not None or not isinstance(input_files, dict) or not 1 <= len(input_files) <= 8
                or any(not isinstance(name, str) or not INPUT_NAME.fullmatch(name) or type(data) is not bytes
                       for name, data in input_files.items())
                or sum(len(data) for data in input_files.values()) > MAX_INPUT_BYTES):
            raise ValueError("服务端输入表无效或超过限额")

    def _run(self, run_id: str, dataset_version: str, code: str, timeout: float,
             output_spec: OutputSpec, *, metric_version: str,
             cancelled: threading.Event | None = None, orders_csv: bytes | None = None,
             input_files: dict[str, bytes] | None = None) -> ExecutionManifest:
        """execute_python 与 execute_inputs 共用的容器生命周期、产物收集与清单落盘；入参已由调用方校验。"""
        folder = (self.root / run_id).resolve()
        if folder.parent != self.root:
            raise ValueError("任务目录越界")
        folder.mkdir(exist_ok=False)
        inputs = folder / "input"
        inputs.mkdir()
        if input_files is not None:
            for filename, data in input_files.items():
                (inputs / filename).write_bytes(data)
        elif orders_csv is None:
            shutil.copyfile(self.store.directory(dataset_version) / "orders.csv", inputs / "orders.csv")
        else:
            (inputs / "orders.csv").write_bytes(orders_csv)
        (inputs / "analysis.py").write_text(code, encoding="utf-8")
        shutil.copyfile(Path(__file__).with_name("container_entry.py"), inputs / "runner.py")
        manifest = ExecutionManifest(run_id, dataset_version, metric_version, "", "input/analysis.py", [], 0, "FAILED")
        name = "datalab-exec-" + uuid4().hex
        started = time.monotonic()
        process = None
        creation_attempted = False
        raw = bytearray()
        overflow = threading.Event()
        reader = None
        try:
            if cancelled is not None and cancelled.is_set():
                manifest.status = "CANCELLED"
                return manifest
            manifest.image_id = self.image_id()
            creation_attempted = True
            self._control(*self.command(name, inputs, manifest.image_id, output_spec)[1:])
            process = subprocess.Popen([self.docker, "start", "--attach", name], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            wire_limit = output_spec.max_bytes * 2 + 4096

            def drain():
                while chunk := process.stdout.read(65536):
                    if len(raw) + len(chunk) > wire_limit:
                        overflow.set()
                        return
                    raw.extend(chunk)

            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if cancelled is not None and cancelled.is_set():
                    manifest.status = "CANCELLED"
                    raise ExecutionError("任务已取消")
                if overflow.is_set():
                    raise ExecutionError("容器输出超过限额")
                if time.monotonic() >= deadline:
                    manifest.status = "TIMED_OUT"
                    raise ExecutionError("容器执行超时")
                time.sleep(0.02)
            reader.join(timeout=2)
            if reader.is_alive() or overflow.is_set():
                raise ExecutionError("容器输出未完整结束或超限")
            state = json.loads(self._control("inspect", "--format", "{{json .State}}", name))
            if process.returncode or state.get("ExitCode") != 0 or state.get("OOMKilled") or state.get("Running"):
                raise ExecutionError("容器异常退出或超出内存限额")
            if cancelled is not None and cancelled.is_set():
                manifest.status = "CANCELLED"
                raise ExecutionError("任务已取消")
            decoded = self.decode_output(bytes(raw), output_spec)
            outputs = folder / "output"
            outputs.mkdir()
            for filename, content in decoded.items():
                (outputs / filename).write_bytes(content)
                manifest.output_paths.append("output/" + filename)
            manifest.status = "COMPLETED"
        except (ExecutionError, OSError, ValueError, subprocess.SubprocessError) as exc:
            manifest.reason = str(exc) if isinstance(exc, ExecutionError) else "执行控制或产物处理失败"
        finally:
            if creation_attempted:
                try:
                    self._control("rm", "--force", name)
                except ExecutionError:
                    manifest.status = "CLEANUP_PENDING"
                    manifest.reason = "容器清理未确认；需按记录检查残留容器"
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                if reader is not None:
                    reader.join(timeout=2)
                if process.stdout is not None:
                    process.stdout.close()
            manifest.elapsed_seconds = round(time.monotonic() - started, 3)
            record = {**asdict(manifest), "container_name": name, "limits": {
                "timeout_seconds": timeout, "memory_mb": 256, "cpu": 1, "pids": 32,
                "output_bytes": output_spec.max_bytes, "output_tmpfs_mb": 16,
            }}
            (folder / "manifest.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest
