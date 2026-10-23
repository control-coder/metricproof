"""下载并校验 Olist 巴西电商公开数据集（多表），只写入被忽略的本地目录，不随仓库分发。

来源：Kaggle olistbr/brazilian-ecommerce（CC BY-NC-SA 4.0）。Kaggle 需要账号凭据，这里使用固定提交的
GitHub 镜像 mara/mara-olist-ecommerce-data（镜像仓库代码为 MIT，数据仍遵循原始许可）。字节数与 SHA-256
固定在本文件中，任何一项不符都会删除下载的临时文件并失败；行数与 Kaggle 公布的数量对照。
数据为署名-非商业-相同方式共享许可，禁止提交到仓库；地理位置表与卖家表不参与分析，不下载。

用法：python -B scripts/fetch_olist.py [--dest artifacts/olist/raw]
"""
import argparse
import csv
import hashlib
import http.client
import io
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMIT = '663660edff1b4cd711a172027081915771628b9f'
URL = 'https://raw.githubusercontent.com/mara/mara-olist-ecommerce-data/{commit}/data/olist-ecommerce/{name}'
DEFAULT_DEST = ROOT / 'artifacts/olist/raw'
# 文件名 → (字节数, SHA-256, 数据行数)。行数按 CSV 记录计（评论表含跨行文本，不能按换行计）。
FILES = {
    'olist_orders_dataset.csv': (17654914, '8df58ef3d2d7e9944010f7beecd9b75367f5588ec6e3c91cec19ae3345ef9ecf', 99441),
    'olist_order_items_dataset.csv': (15438671, '0bc4d068c4fe38cbb01bd90e8746e3c613fe7b4baef75fab7b0e329701c3e279', 112650),
    'olist_order_payments_dataset.csv': (5777138, '4f713964f2815dbbaa40b9488268c55aac3627bfce5aa96cf58d1f3616de3cc0', 103886),
    'olist_order_reviews_dataset.csv': (14409007, '0dff69f6fed33a13648020198ea94d7ae12afbdd4904186c6cd904e27a3e1ccd', 100000),
    'olist_customers_dataset.csv': (9033957, '983a422239e1712ded753b3bf9ecf47dc73f144d306029dcfa99e70a226883d2', 99441),
    'olist_products_dataset.csv': (2379446, '3e6569628a17fbc75fd206ee357b59e20364b9afa90f5b6cd5b4d624c58aa9cc', 32951),
    'product_category_name_translation.csv': (2613, 'a81f0d1f27b27e7293f761bc79e3ce8f348ee39c4b3ed3e49bde38f478586278', 71),
}
RETRIES = 6


def download(name: str) -> bytes:
    url = URL.format(commit=COMMIT, name=name)
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return response.read()
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError) as error:
            if attempt == RETRIES:
                raise SystemExit(f'{name} 下载失败：{type(error).__name__}') from error
            time.sleep(2 * attempt)
    raise AssertionError


def records(data: bytes) -> int:
    return sum(1 for _ in csv.reader(io.StringIO(data.decode('utf-8-sig'), newline=''))) - 1


def verify(name: str, data: bytes) -> list[str]:
    size, digest, rows = FILES[name]
    problems = []
    if len(data) != size:
        problems.append(f'字节数 {len(data)} ≠ {size}')
    actual = hashlib.sha256(data).hexdigest()
    if digest and actual != digest:
        problems.append('SHA-256 不符')
    if rows is not None and records(data) != rows:
        problems.append(f'行数 {records(data)} ≠ {rows}')
    return problems


def main():
    parser = argparse.ArgumentParser(description='下载并校验 Olist 多表数据（不提交到仓库）')
    parser.add_argument('--dest', type=Path, default=DEFAULT_DEST)
    args = parser.parse_args()
    dest = args.dest.resolve()
    if not dest.is_relative_to(ROOT / 'artifacts'):
        raise SystemExit('目标目录必须位于被忽略的 artifacts/ 下')
    dest.mkdir(parents=True, exist_ok=True)
    failed = False
    for name in FILES:
        target = dest / name
        data = target.read_bytes() if target.is_file() else download(name)
        problems = verify(name, data)
        if problems:
            failed = True
            target.unlink(missing_ok=True)
            print(name, '校验失败：', '；'.join(problems), file=sys.stderr)
            continue
        if not target.is_file():
            temporary = target.with_suffix('.part')
            temporary.write_bytes(data)
            temporary.replace(target)
        print(name, len(data), hashlib.sha256(data).hexdigest(), records(data))
    if failed:
        raise SystemExit('有文件未通过校验')


if __name__ == '__main__':
    main()
