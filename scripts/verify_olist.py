"""用真实 Olist 数据在真实受限容器里测量多表输入的耗时与峰值内存，不调用模型。

先运行 scripts/fetch_olist.py；数据导入到被忽略的 artifacts/olist/scale/，结果写入同目录的 olist-result.json。
每类分析一个代表性计划：框架脚本读入服务端投影的输入表后，在 analyze 里读取本进程峰值 RSS（ru_maxrss）
与容器 cgroup 的 memory.peak（内核不提供时为 null）。容器限额与生产路径相同（256 MB、1 核、无网络）。
"""
import argparse
import json
from pathlib import Path
import time
from uuid import uuid4

from datalab.contracts import AnalysisPlan
from datalab.datasets.service import DatasetStore
from datalab.execution.analysis_frame import assemble
from datalab.execution.runner import DockerRunner, OutputSpec, sandbox_inputs
from datalab.orchestration.workflow import role_plan

ROOT = Path(__file__).resolve().parents[1]
STATUSES = ('delivered', 'shipped', 'invoiced', 'processing', 'approved', 'created')
PLANS = {
    'summary-payments': {'kind': 'summary', 'amount_source': 'payments'},
    'ranking-items-state': {'kind': 'ranking', 'group_by': 'customer_state', 'amount_source': 'items'},
    'trend-month': {'kind': 'trend', 'group_by': 'purchase_month', 'amount_source': 'payments'},
    'repeat_rate-state': {'kind': 'repeat_rate', 'group_by': 'customer_state'},
    'cohort_retention': {'kind': 'cohort_retention', 'start': '2017-01-01T00:00:00', 'end': '2018-09-01T00:00:00'},
    'delivery_delay-state': {'kind': 'delivery_delay', 'group_by': 'customer_state'},
    'category_sales': {'kind': 'category_sales', 'group_by': 'product_category'},
}
PROBE = '''
def analyze(data, plan):
    import resource
    try:
        with open("/sys/fs/cgroup/memory.peak") as handle:
            peak = int(handle.read())
    except (OSError, ValueError):
        peak = None
    return {"kind": plan["kind"], "rows": [],
            "total": {"rows": sum(len(rows) for rows in data.values()), "cells": sum(len(rows) * len(rows[0]) for rows in data.values() if rows),
                      "maxrss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, "cgroup_peak_bytes": peak}}
'''


def main():
    parser = argparse.ArgumentParser(description='真实 Olist 数据的容器耗时与内存测量')
    parser.add_argument('--raw', type=Path, default=ROOT / 'artifacts/olist/raw')
    args = parser.parse_args()
    scratch = ROOT / 'artifacts/olist/scale'
    store = DatasetStore(scratch / 'datasets')
    started = time.perf_counter()
    version = store.import_olist(args.raw, project_id='scale', dataset_family='olist-real')
    # 每次都重新导入以测导入耗时；只保留本次的数据版本，删除本脚本此前在该忽略目录里留下的版本。
    for folder in (scratch / 'datasets').iterdir():
        if folder.name != version.dataset_version and (folder / 'metadata.json').is_file():
            for path in folder.iterdir():
                path.unlink()
            folder.rmdir()
    output = {'import_seconds': round(time.perf_counter() - started, 3), 'dataset_version': version.dataset_version,
              'limits': {'memory_mb': 256, 'cpu': 1, 'timeout_seconds': 30}, 'load': {}}
    runner = DockerRunner(store, scratch / 'runs')
    for name, fields in PLANS.items():
        plan = AnalysisPlan(version.dataset_version, 'scale-v1', schema='olist', included_statuses=STATUSES, **fields)
        prepared = time.perf_counter()
        extra = sandbox_inputs(store, plan)
        projection = time.perf_counter() - prepared
        manifest = runner.execute_python(str(uuid4()), version.dataset_version, assemble(role_plan(plan), PROBE), 30,
                                         OutputSpec(), project_id='scale', metric_version='scale-v1', **extra)
        record = {'status': manifest.status, 'elapsed_seconds': manifest.elapsed_seconds,
                  'projection_seconds': round(projection, 3),
                  'input_bytes': sum(len(data) for data in extra['input_files'].values())}
        if manifest.status == 'COMPLETED':
            result = json.loads((runner.root / manifest.run_id / 'output/result.json').read_text(encoding='utf-8'))
            record.update(result['total'])
        output['load'][name] = record
        print(name, json.dumps(record, ensure_ascii=False))
    (scratch / 'olist-result.json').write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    if any(item['status'] != 'COMPLETED' for item in output['load'].values()):
        raise SystemExit('有测量运行未完成，查看 artifacts/olist/scale/runs/ 下的执行清单')


if __name__ == '__main__':
    main()
