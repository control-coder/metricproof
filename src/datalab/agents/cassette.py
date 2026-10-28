"""离线录制回放：cassette 包装 httpx transport，录制的请求摘要不包含密钥。

- 录制：把请求体（去掉头信息，只用规范化 body）的 SHA-256 作为键，记录响应 JSON 到文件。
- 回放：同一键返回录制的响应；未命中直接报错，不发网络请求。
- cassette 文件是纯 JSON，不含 Authorization 或其他头；可以提交到仓库供离线测试与 CI 回放。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx


class CassetteMiss(RuntimeError):
    """回放时请求未命中；直接报错而不是发网络请求，保证离线测试确定性。"""


def _canonical(body: bytes) -> str:
    """请求体规范化：JSON 排序键后序列化；非 JSON 按原字节。"""
    try:
        value = json.loads(body.decode('utf-8'))
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (ValueError, UnicodeDecodeError):
        return body.decode('utf-8', errors='replace')


def key_of(body: bytes) -> str:
    """cassette 键：只由请求体决定，与方法、URL、头无关；Authorization 等敏感头不参与。"""
    return hashlib.sha256(_canonical(body).encode('utf-8')).hexdigest()


class CassetteTransport(httpx.BaseTransport):
    """httpx 传输层包装：record 模式录制，replay 模式回放。"""

    def __init__(self, path: str | Path, mode: str, inner: httpx.BaseTransport | None = None):
        if mode not in ('record', 'replay'):
            raise ValueError("mode 只能是 'record' 或 'replay'")
        self.path = Path(path)
        self.mode = mode
        self._inner = inner or httpx.HTTPTransport()
        if mode == 'replay':
            if not self.path.is_file():
                raise CassetteMiss(f'cassette 文件不存在：{self.path}')
            self._entries = json.loads(self.path.read_text(encoding='utf-8'))
        else:
            self._entries = {}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        key = key_of(request.content)
        if self.mode == 'replay':
            if key not in self._entries:
                raise CassetteMiss(f'cassette 未命中请求 {key[:12]}…（共 {len(self._entries)} 条）')
            entry = self._entries[key]
            return httpx.Response(entry['status'], json=entry['json'], request=request)
        response = self._inner.handle_request(request)
        # 自定义 transport 返回的响应默认是流式的：不 read() 时访问 content/json 会抛
        # ResponseNotRead。录制前先把响应体读进内存，随后照常返回给调用方。
        response.read()
        if response.status_code == 200:
            try:
                self._entries[key] = {'status': response.status_code, 'json': response.json()}
            except ValueError:
                # 非 JSON 响应不录制（本项目模型接口恒为 JSON）。
                pass
        return response

    def save(self):
        """录制模式结束时写盘；只写规范化后的响应 JSON，不含任何请求头或密钥。"""
        if self.mode != 'record':
            raise RuntimeError('只有 record 模式可以 save')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._entries, ensure_ascii=False, indent=2, sort_keys=True),
                             encoding='utf-8')

    def __len__(self):
        return len(self._entries)


def record_client(path: str | Path, **kwargs) -> tuple[httpx.Client, CassetteTransport]:
    """便捷构造：录制用 client 与 transport，用 ``with`` 或手动 save。"""
    transport = CassetteTransport(path, 'record')
    return httpx.Client(transport=transport, **kwargs), transport


def replay_client(path: str | Path, **kwargs) -> httpx.Client:
    """便捷构造：回放用 client；未命中抛 CassetteMiss。"""
    return httpx.Client(transport=CassetteTransport(path, 'replay'), **kwargs)
