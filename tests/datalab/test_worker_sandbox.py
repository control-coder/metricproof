"""沙箱不可用时 worker 的行为：任务必须以明确原因失败并释放租约，不能留在已领取状态被心跳续租。

不依赖 PostgreSQL / Redis / Docker：用内存仓库替换数据库访问，并让 Docker 查找返回空。
"""
import importlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from datalab.orchestration.workflow import State, Task
from datalab.storage.repository import encode_task


class MemoryRepository:
    """只实现 worker 用到的接口；记录每次保存与是否释放租约。"""

    def __init__(self, task):
        self.row = {'snapshot': encode_task(task), 'cancel_requested': False, 'fence': 1,
                    'request': {'options': {'kind': 'summary'}, 'mode': 'guided-demo'}}
        self.saves = []
        self.heartbeats = 0
        self.lock = threading.Lock()

    def __call__(self, _url):
        return self

    def claim(self, _task_id):
        return 'lease-token', self.row

    def heartbeat(self, _task_id, _token):
        with self.lock:
            self.heartbeats += 1
        return False

    def save(self, task, token, *, release=False):
        self.saves.append((task.state, task.failure, token, release))


class SandboxUnavailableTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        config = Path(cls.temp.name) / 'runtime.json'
        config.write_text(json.dumps({'database_url': 'postgresql://unused', 'broker_url': 'memory://',
                                      'artifact_root': str(Path(cls.temp.name) / 'artifacts')}), encoding='utf-8')
        with patch.dict(os.environ, {'DATALAB_CONFIG': str(config)}):
            cls.worker = importlib.import_module('datalab.orchestration.worker')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_missing_docker_fails_task_and_releases_lease(self):
        repository = MemoryRepository(Task('p1', 'v1', '各渠道成交额'))
        with patch.object(self.worker, 'Repository', repository), \
                patch('datalab.execution.runner.shutil.which', return_value=None):
            self.worker.process_task('task-1')
        # 最后一次保存是终态失败，原因可区分，并且释放了租约。
        self.assertEqual(repository.saves[-1], (State.FAILED, 'SANDBOX_UNAVAILABLE', 'lease-token', True))
        # 心跳线程已停止：之后不再续租。
        count = repository.heartbeats
        threading.Event().wait(1.5)
        self.assertEqual(repository.heartbeats, count)


if __name__ == '__main__':
    unittest.main()
