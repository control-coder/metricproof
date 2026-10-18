"""模型供应商配置：读取本机 .env，只暴露密钥是否存在，不在日志或接口中输出密钥。"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import tempfile

GUIDED = 'guided'
PROVIDERS = ('mimo', 'deepseek')
CHOICES = (*PROVIDERS, GUIDED)
ACTIVE_KEY = 'DATALAB_MODEL_PROVIDER'


class ModelConfigError(ValueError):
    """配置缺失或不合法；信息中不包含密钥值。"""


@dataclass(frozen=True)
class ModelProfile:
    provider: str
    base_url: str
    model: str
    api_key: str = field(default='', repr=False)
    request_timeout: float = 180
    max_output_tokens: int = 16384
    temperature: float = 0.2
    json_mode: bool = True
    # Reviewer 只输出一个短判断（MiMo 实测最多约 500 token、13 秒），单独收紧上限，失控时尽早降级。
    reviewer_max_output_tokens: int = 2048
    reviewer_timeout: float = 60

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def public(self) -> dict:
        return {'provider': self.provider, 'model': self.model, 'base_url': self.base_url, 'configured': self.configured}


@dataclass(frozen=True)
class ModelConfig:
    active: str
    profiles: dict[str, ModelProfile]
    max_calls: int = 8
    max_tokens: int = 150000
    seconds: float = 600

    def profile(self, provider: str | None = None) -> ModelProfile:
        name = provider or self.active
        if name not in self.profiles:
            raise ModelConfigError('当前为固定演示模式或供应商未知')
        return self.profiles[name]


def env_path() -> Path:
    # 默认使用启动目录（serve.py 固定为项目根目录）；测试与部署可显式指定。
    return Path(os.environ.get('DATALAB_ENV_FILE', '.env'))


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _number(values, key, default, kind, low, high):
    raw = values.get(key, '')
    try:
        value = kind(raw) if raw else default
    except ValueError as error:
        raise ModelConfigError(f'{key} 不是有效数字') from error
    if not low <= value <= high:
        raise ModelConfigError(f'{key} 超出允许范围')
    return value


def load_model_config(path: Path | None = None) -> ModelConfig:
    path = path or env_path()
    if not path.is_file():
        raise ModelConfigError('缺少模型配置文件 .env，可参考 .env.example 创建')
    values = read_env(path)
    active = values.get(ACTIVE_KEY, 'mimo').strip().lower()
    if active not in CHOICES:
        raise ModelConfigError(f'{ACTIVE_KEY} 只能是 {"、".join(CHOICES)}')
    profiles = {}
    for provider in PROVIDERS:
        prefix = f'DATALAB_{provider.upper()}_'
        base_url = values.get(prefix + 'BASE_URL', '').rstrip('/')
        model = values.get(prefix + 'MODEL', '')
        if not base_url or not model:
            raise ModelConfigError(f'{prefix}BASE_URL 与 {prefix}MODEL 必须配置')
        # 密钥只经 HTTPS 发送，避免配置失误时明文外发。
        if not base_url.startswith('https://'):
            raise ModelConfigError(f'{prefix}BASE_URL 必须使用 HTTPS')
        # 进程环境变量优先，便于用安全渠道注入而不写入文件。
        key = os.environ.get(prefix + 'API_KEY') or values.get(prefix + 'API_KEY', '')
        profiles[provider] = ModelProfile(provider, base_url, model, key.strip(),
            _number(values, prefix + 'TIMEOUT', 180.0, float, 1, 300),
            _number(values, prefix + 'MAX_OUTPUT_TOKENS', 16384, int, 256, 32768),
            _number(values, prefix + 'TEMPERATURE', 0.2, float, 0, 2),
            values.get(prefix + 'JSON_MODE', 'true').strip().lower() not in {'0', 'false', 'no', 'off'},
            _number(values, prefix + 'REVIEWER_MAX_OUTPUT_TOKENS', 2048, int, 256, 32768),
            _number(values, prefix + 'REVIEWER_TIMEOUT', 60.0, float, 1, 300))
    return ModelConfig(active, profiles,
        _number(values, 'DATALAB_MODEL_MAX_CALLS', 8, int, 3, 12),
        _number(values, 'DATALAB_MODEL_MAX_TOKENS', 150000, int, 1000, 500000),
        _number(values, 'DATALAB_MODEL_TIME_SECONDS', 600.0, float, 30, 600))


def switch_provider(provider: str, path: Path | None = None) -> str:
    """只改写当前供应商一行，保留其他配置与注释；原子替换避免写坏文件。"""
    provider = provider.strip().lower()
    if provider not in CHOICES:
        raise ModelConfigError(f'只能切换到 {"、".join(CHOICES)}')
    path = path or env_path()
    if not path.is_file():
        raise ModelConfigError('缺少模型配置文件 .env，可参考 .env.example 创建')
    text = path.read_text(encoding='utf-8')
    line = f'{ACTIVE_KEY}={provider}'
    pattern = re.compile(rf'^\s*{ACTIVE_KEY}\s*=.*$', re.MULTILINE)
    text = pattern.sub(line, text, count=1) if pattern.search(text) else line + '\n' + text
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix='.env.', suffix='.tmp')
    try:
        with os.fdopen(handle, 'w', encoding='utf-8', newline='') as output:
            output.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    load_model_config(path)
    return provider
