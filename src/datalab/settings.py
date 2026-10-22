"""只在显式启动应用时加载本机配置，不把连接凭据放入日志。"""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    broker_url: str = field(repr=False)
    artifact_root: Path
    executor_image: str = 'datalab-executor:py312-v1'
    # 蜕变关系核验：每条关系多一次沙箱运行。当前六类都有独立参考，默认关闭；runtime.json 中 "metamorphic_checks": true 开启。
    metamorphic_checks: bool = False

    @classmethod
    def load(cls):
        file = Path(os.environ.get('DATALAB_CONFIG', 'artifacts/local/runtime.json'))
        if not file.is_file():
            raise RuntimeError('缺少本地运行配置，请先执行 scripts/local_services.py start')
        payload = json.loads(file.read_text(encoding='utf-8'))
        return cls(payload['database_url'], payload['broker_url'], Path(payload['artifact_root']).resolve(),
                   payload.get('executor_image', 'datalab-executor:py312-v1'),
                   payload.get('metamorphic_checks', False) is True)
