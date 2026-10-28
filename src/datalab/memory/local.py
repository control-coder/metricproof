"""进程内记忆仓库：供离线评测和测试使用，不依赖 PostgreSQL，不作为生产后端。"""
from contextlib import contextmanager
from copy import deepcopy
from threading import RLock

from datalab.memory.service import MemoryEntry, Scope


class InMemoryMemoryTransaction:
    def __init__(self, entries: dict, scope: Scope):
        self.entries = entries
        self.scope = scope

    def list_entries(self) -> list[MemoryEntry]:
        return [deepcopy(entry) for entry in self.entries.values() if entry.scope == self.scope]

    def insert(self, entry: MemoryEntry):
        if entry.scope != self.scope:
            raise PermissionError("不能写入其他记忆范围")
        self.entries[entry.memory_id] = deepcopy(entry)

    def update_status(self, entry: MemoryEntry):
        if entry.scope != self.scope:
            raise PermissionError("不能写入其他记忆范围")
        self.entries[entry.memory_id] = deepcopy(entry)


class InMemoryMemoryRepository:
    """RLock 覆盖整个事务，暂存副本保证失败回滚；读写均深拷贝，调用方不能改动已存条目。"""

    def __init__(self):
        self._entries: dict[str, MemoryEntry] = {}
        self._lock = RLock()

    @contextmanager
    def transaction(self, scope: Scope):
        with self._lock:
            staged = deepcopy(self._entries)
            yield InMemoryMemoryTransaction(staged, scope)
            self._entries = staged
