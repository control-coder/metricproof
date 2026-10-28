"""只检查本机依赖可用性；不读取凭据、不修改服务、不下载镜像。"""
import json
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    result = {
        'python_environment': Path(sys.prefix).name,
        'python_version': '.'.join(map(str, sys.version_info[:3])),
        'python_valid': Path(sys.prefix).name.lower() == 'datalab' and sys.version_info[:2] == (3, 12),
        'docker_engine': False,
        'executor_image': False,
        'local_config_present': Path('artifacts/local/runtime.json').is_file(),
        'service_ports': '由本机专用配置管理随机回环端口；预检不读取凭据，完整连接以真实集成为准',
        'model_credentials_read': False,
    }
    docker = shutil.which('docker')
    if docker:
        try:
            check = subprocess.run([docker, 'version', '--format', '{{.Server.Version}}'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=False)
            result['docker_engine'] = check.returncode == 0 and bool(check.stdout.strip())
            if result['docker_engine']:
                image = subprocess.run([docker, 'image', 'inspect', '--format', '{{.Id}}', 'datalab-executor:py312-v2'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
                result['executor_image'] = image.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            result['docker_check'] = '控制服务不可用或超时'
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # 配置存在不代表数据库认证成功；真实集成由专门用例验证。
    return 0 if result['python_valid'] and result['docker_engine'] and result['executor_image'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
