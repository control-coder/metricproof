"""Olist 错误注入基准（离线，不调用模型）：度量各核验层对真实 Olist 数据上“已知是错的”代码与计划各能拦下多少。

代码变异：从 Olist 固定分析（execution/fixed_olist.py）的 analyze 出发，改成框架形态（data 由框架读好），
按精确锚点替换生成带错误家族标签的变异体，其中一部分沿用单表基准（evals/injection.py）的算子思路（期间边界、口径状态、
比值舍入、排序、两期对比、分组行、金额换算），另一部分是 Olist 特有的口径错误：用 customer_id 代替 customer_unique_id、
cohort 取错首单、start 截断首单、留存右删失差一个月、送达延迟按时间戳比较或不看状态、品类丢弃 unknown 或未翻译品类、
品类金额混入运费、订单金额来源用反、复购客户按最后一单归州。
每个变异体 × 适用的 Olist 留出集真值计划（evals/olist_questions.py，不改题，指纹校验），在真实 Olist 数据与真实受限沙箱中运行，
各层互不短路：独立 SQL 参考、分组合计、下载表、结果不变量、两份参考一致性、蜕变关系（本基准中开启）。
与正确代码在同一用例上的 result.json 与下载表都相同记为“未暴露”，不计入检出率分母。

计划变异只测口径锁：纳入状态或订单金额来源与已确认口径不同的计划，由工作流的计划接收校验拒绝。

结果写入 artifacts/evals/olist-injection.json 与 olist-injection-report.md。
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
import olist_questions  # noqa: E402
from injection import git_commit, rate  # noqa: E402
from model_compare import ROOT  # noqa: E402

from datalab.contracts import AMOUNT_KINDS, AnalysisPlan  # noqa: E402
from datalab.datasets import olist  # noqa: E402
from datalab.datasets.service import DatasetStore  # noqa: E402
from datalab.execution import fixed_olist  # noqa: E402
from datalab.execution.analysis_frame import assemble  # noqa: E402
from datalab.execution.runner import DockerRunner, OutputSpec  # noqa: E402
from datalab.orchestration.workflow import MetricSnapshot, Task, Workflow, role_plan  # noqa: E402
from datalab.verification import invariants, metamorphic  # noqa: E402
from datalab.verification.python_reference import agreement  # noqa: E402
from datalab.verification.reference import differences, reference_result  # noqa: E402
from datalab.verification.table import table_issue  # noqa: E402

PROJECT = 'olist-injection'
METRIC = 'olist-injection-v1'
LAYERS = ('independent_reference', 'group_total', 'download_table', 'invariants', 'reference_agreement', 'metamorphic')
REFERENCE_FREE = ('invariants', 'metamorphic')
AMOUNT = AMOUNT_KINDS
GROUPED_AMOUNT = ('summary', 'trend', 'ranking', 'share', 'comparison')
PERIOD_KINDS = AMOUNT + ('repeat_rate', 'delivery_delay', 'category_sales')


class MutationError(ValueError):
    """锚点缺失或不唯一：固定分析变了而目录没跟上，必须显式失败。"""


def base_code() -> str:
    source = Path(fixed_olist.__file__).read_text(encoding='utf-8').replace('\r\n', '\n')
    # 从常量开始截取（框架已导入 csv、json、defaultdict、Decimal），到导出函数之前为止。
    body = source[source.index('AMOUNT_KINDS = '):source.index('def export_result(')].rstrip() + '\n'
    anchor = 'def analyze(folder, plan):\n    data = load(folder)\n'
    if body.count(anchor) != 1:
        raise MutationError('固定分析的读入段落已变化')
    return body.replace(anchor, 'def analyze(data, plan):\n')


BASE = base_code()


@dataclass(frozen=True)
class Operator:
    name: str
    family: str
    kinds: tuple
    edits: tuple
    note: str

    def apply(self, code: str = BASE) -> str:
        mutated = code
        for anchor, replacement in self.edits:
            if mutated.count(anchor) != 1:
                raise MutationError(f'{self.name}：锚点缺失或不唯一')
            mutated = mutated.replace(anchor, replacement)
        if mutated == code:
            raise MutationError(f'{self.name}：替换后代码没有变化')
        return mutated


QUANTIZE = 'return str((Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.000001")))'
GROUP_ROWS = 'result_rows = [{"group": key, **value} for key, value in sorted(groups.items())]'
RATIO_KINDS = ('share', 'comparison', 'repeat_rate', 'cohort_retention', 'delivery_delay')

OPERATORS = (
    # 沿用单表基准的错误家族
    Operator('end_inclusive', 'boundary', PERIOD_KINDS,
             (('(not end or order[field] < end)', '(not end or order[field] <= end)'),), '期间终点按闭区间处理'),
    Operator('start_exclusive', 'boundary', PERIOD_KINDS,
             (('(not start or order[field] >= start)', '(not start or order[field] > start)'),), '期间起点按开区间处理'),
    Operator('status_filter_dropped', 'status', PERIOD_KINDS,
             (('order["order_status"] in statuses and order[field]\n', 'order[field]\n'),), '不按口径状态过滤'),
    Operator('status_hardcoded_delivered', 'status', PERIOD_KINDS,
             (('order["order_status"] in statuses and order[field]\n', 'order["order_status"] == "delivered" and order[field]\n'),),
             '口径状态写死为 delivered'),
    Operator('ratio_float', 'rounding', RATIO_KINDS, ((QUANTIZE, 'return format(numerator / denominator, ".6f")'),), '比值用浮点除法'),
    Operator('ratio_five_places', 'rounding', RATIO_KINDS,
             ((QUANTIZE, 'return str((Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.00001")))'),), '比值只保留 5 位小数'),
    Operator('ranking_ascending', 'ordering', ('ranking',),
             (('result_rows.sort(key=lambda row: (-row["amount_cents"], row["group"]))',
               'result_rows.sort(key=lambda row: (row["amount_cents"], row["group"]))'),), '排行按金额升序'),
    Operator('delta_sign_flipped', 'comparison', ('comparison',),
             (('delta = total["amount_cents"] - previous_amount', 'delta = previous_amount - total["amount_cents"]'),), '差额符号取反'),
    Operator('growth_wrong_base', 'comparison', ('comparison',),
             (('"growth": ratio(delta, previous_amount)', '"growth": ratio(delta, total["amount_cents"])'),), '增长率以本期为分母'),
    Operator('date_key_month', 'grouping', ('trend', 'ranking', 'share'),
             (('row[plan["time_field"]][:10] if group_by == "purchase_date"', 'row[plan["time_field"]][:7] if group_by == "purchase_date"'),),
             '按日分组截成了月份'),
    Operator('total_in_reais', 'scale', AMOUNT,
             (('"amount_cents": sum(amounts[row["order_id"]] for row in rows)}', '"amount_cents": sum(amounts[row["order_id"]] for row in rows) // 100}'),),
             '总计金额误换算为雷亚尔'),
    Operator('spurious_group', 'rows', GROUPED_AMOUNT,
             ((GROUP_ROWS, GROUP_ROWS + '\n    if group_by:\n        result_rows.append({"group": "ZZ", "order_count": 0, "amount_cents": 0})'),),
             '多出一行空分组'),
    Operator('last_group_dropped', 'rows', GROUPED_AMOUNT, ((GROUP_ROWS, GROUP_ROWS + '\n    result_rows = result_rows[:-1]'),),
             '分组行少了最后一行'),
    # Olist 特有的口径错误
    Operator('amount_source_swapped', 'olist_amount', AMOUNT,
             (('if plan["amount_source"] == "payments":', 'if plan["amount_source"] != "payments":'),), '订单金额来源用反（payments 与 items 互换）'),
    Operator('repeat_customer_id', 'olist_customer', ('repeat_rate',),
             (('person = customer["customer_unique_id"]', 'person = customer["customer_id"]'),), '复购按 customer_id 而不是 customer_unique_id 计人'),
    Operator('repeat_threshold_three', 'olist_customer', ('repeat_rate',),
             (('repeat = sum(1 for count in counts.values() if count >= 2)', 'repeat = sum(1 for count in counts.values() if count > 2)'),),
             '复购总数要求至少 3 单（分组行仍为 2 单）'),
    Operator('repeat_state_last_order', 'olist_customer', ('repeat_rate',),
             (('states.setdefault(person, customer.get("customer_state"))', 'states[person] = customer.get("customer_state")'),),
             '客户归入期间内最后一笔订单的州'),
    Operator('cohort_customer_id', 'olist_cohort', ('cohort_retention',),
             (('activity[customers[order["customer_id"]]["customer_unique_id"]].append', 'activity[order["customer_id"]].append'),),
             '留存按 customer_id 计人'),
    Operator('cohort_last_order', 'olist_cohort', ('cohort_retention',),
             (('first = min(stamps)', 'first = max(stamps)'),), 'cohort 取最后一单（而不是首单）所在月'),
    Operator('cohort_start_truncates_first', 'olist_cohort', ('cohort_retention',),
             (('(horizon is None or order[field] < horizon):', '(horizon is None or order[field] < horizon) and (not plan["start"] or order[field] >= plan["start"]):'),),
             'start 截断了活动，start 之前已下过单的客户被当成新客'),
    Operator('cohort_censoring_off_by_one', 'olist_cohort', ('cohort_retention',),
             (('if cohort + offset >= limit:', 'if cohort + offset > limit:'),), '右删失差一个月，输出了不完整的月份'),
    Operator('late_timestamp_level', 'olist_delivery', ('delivery_delay',),
             (('row["order_delivered_customer_date"][:10] > row["order_estimated_delivery_date"][:10]',
               'row["order_delivered_customer_date"] > row["order_estimated_delivery_date"]'),), '延迟按时间戳比较，同一天送达也算延迟'),
    Operator('delivery_ignores_status', 'olist_delivery', ('delivery_delay',),
             (('if row["order_status"] != "delivered" or not row["order_delivered_customer_date"]', 'if not row["order_delivered_customer_date"]'),),
             '不限定 delivered 状态'),
    Operator('category_drops_unknown', 'olist_category', ('category_sales',),
             (('name = names.get(item["product_id"]) or ""\n', 'name = names.get(item["product_id"]) or ""\n        if not name:\n            continue\n'),),
             '丢弃没有品类的明细（unknown）'),
    Operator('category_untranslated_unknown', 'olist_category', ('category_sales',),
             (('key = english.get(name, name) if name else UNKNOWN', 'key = english.get(name, UNKNOWN) if name else UNKNOWN'),),
             '未翻译品类并入 unknown'),
    Operator('category_freight_in_price', 'olist_category', ('category_sales',),
             (('groups[key]["price_cents"] += int(item["price_cents"])', 'groups[key]["price_cents"] += int(item["price_cents"]) + int(item["freight_cents"])'),),
             '品类商品金额混入运费'),
    Operator('category_total_orders_summed', 'olist_category', ('category_sales',),
             (('"order_count": len(set().union(*(value["orders"] for value in groups.values()))),',
               '"order_count": sum(len(value["orders"]) for value in groups.values()),'),), '总订单数按品类相加，跨品类订单重复计数'),
)


def catalog_fingerprint() -> str:
    payload = {'base_sha256': hashlib.sha256(BASE.encode('utf-8')).hexdigest(), 'operators': [asdict(item) for item in OPERATORS]}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


class DockerLab:
    def __init__(self, folder: Path, image: str):
        self.store = DatasetStore(folder / 'datasets')
        self.runner = DockerRunner(self.store, folder / 'runs', image=image)

    def run(self, plan: AnalysisPlan, script: str, inputs: dict):
        run_id = str(uuid4())
        manifest = self.runner.execute_python(run_id, plan.dataset_version, script, 30, OutputSpec(),
                                              project_id=PROJECT, metric_version=plan.metric_version, input_files=inputs)
        folder = self.runner.root / run_id
        try:
            if manifest.status != 'COMPLETED':
                return None, None
            return (json.loads((folder / 'output/result.json').read_text(encoding='utf-8')),
                    (folder / 'output/table.csv').read_bytes())
        except (OSError, ValueError):
            return None, None
        finally:
            # 运行目录含投影输入（每次 10–18 MB），读出结果后即删除。
            shutil.rmtree(folder, ignore_errors=True)

    def execute(self, plan: AnalysisPlan):
        """蜕变重跑：与工作流相同的 runner_execute（同一隔离参数），每次运行后删除运行目录。"""
        def run(script, data, timeout):
            # 与 metamorphic.runner_execute 相同的调用（多表输入走 input_files），只多了运行后清理本次目录。
            run_id = str(uuid4())
            manifest = self.runner.execute_python(run_id, plan.dataset_version, script, timeout, OutputSpec(),
                                                  project_id=PROJECT, metric_version=plan.metric_version, input_files=data)
            folder = self.runner.root / run_id
            try:
                if manifest.status != 'COMPLETED':
                    return None
                return json.loads((folder / 'output/result.json').read_text(encoding='utf-8'))
            except (OSError, UnicodeError, ValueError):
                return None
            finally:
                shutil.rmtree(folder, ignore_errors=True)
        return run


def evaluate(lab, case: dict, script: str, result, table: bytes) -> dict:
    """各层互不短路；参考值与两份参考的一致性每个用例只算一次。True 为通过，None 为不适用。"""
    plan, reference = case['plan'], case['reference']
    handle, name = tempfile.mkstemp(suffix='.csv', dir=lab.runner.root.parent)
    os.close(handle)
    scratch = Path(name)
    try:
        scratch.write_bytes(table)
        download = table_issue(scratch, reference) is None
    finally:
        scratch.unlink(missing_ok=True)
    keys = invariants.ADDITIVE.get(plan.kind, ('amount_cents', 'order_count'))
    found = invariants.violations(plan, result)
    meta = metamorphic.check(plan, case['orders'], result, metamorphic.Sandbox(script, lab.execute(plan)))
    layers = {'independent_reference': not differences(reference, result),
              'group_total': invariants.group_total(result, keys) if plan.group_by and isinstance(result, dict) else None,
              'download_table': download, 'invariants': not found, 'reference_agreement': case['agreement'],
              'metamorphic': meta['passed']}
    return {'layers': layers, 'invariant_issues': sorted({item['issue'] for item in found}),
            'relations': {item['relation']: item['status'] for item in meta['relations']},
            'metamorphic_issues': sorted({f"{item['relation']}:{item['issue']}" for item in meta.get('issues', [])})}


def prepare(lab, raw: Path) -> list[dict]:
    version = lab.store.import_olist(raw, project_id=PROJECT, dataset_family='olist-injection')
    folder = lab.store.directory(version.dataset_version)
    cases = []
    for index, (question, fields, source) in enumerate(olist_questions.HELDOUT):
        if fields.get('clarify'):
            continue
        extra = {'amount_source': source} if fields['kind'] in AMOUNT_KINDS else {}
        plan = AnalysisPlan(version.dataset_version, METRIC, schema='olist', included_statuses=olist_questions.STATUSES,
                            **fields, **extra)
        reference = reference_result(folder / 'orders.csv', plan)
        cases.append({'case': index + 1, 'kind': plan.kind, 'plan': plan, 'orders': folder / 'orders.csv', 'reference': reference,
                      'agreement': agreement(folder / 'orders.csv', plan, reference)['passed'],
                      'inputs': olist.dataset_inputs(folder, plan)})
    return cases


def run_correct(lab, case: dict) -> dict:
    script = assemble(role_plan(case['plan']), BASE)
    result, table = lab.run(case['plan'], script, case['inputs'])
    if result is None:
        raise RuntimeError('正确代码执行失败；检查 Docker 与执行镜像')
    case['correct'], case['table'] = result, table
    return {'case': case['case'], 'kind': case['kind'], **evaluate(lab, case, script, result, table)}


def run_mutant(lab, operator: Operator, case: dict) -> dict:
    script = assemble(role_plan(case['plan']), operator.apply())
    record = {'operator': operator.name, 'family': operator.family, 'case': case['case'], 'kind': case['kind']}
    result, table = lab.run(case['plan'], script, case['inputs'])
    if result is None:
        return {**record, 'status': 'crashed'}
    if not differences(case['correct'], result) and table == case['table']:
        return {**record, 'status': 'equivalent'}
    return {**record, 'status': 'exposed', **evaluate(lab, case, script, result, table)}


def metric_lock(cases: list[dict]) -> dict:
    """纳入状态或订单金额来源与已确认口径不同的计划，工作流在执行前拒绝。"""
    rejected = total = 0
    records = []
    for case in cases:
        plan = case['plan']
        metric = MetricSnapshot(METRIC, plan.included_statuses, 'trusted', PROJECT, 'olist-injection', olist.SIGNATURE,
                                plan.amount_source or 'payments')
        changes = [('statuses_changed', {'included_statuses': plan.included_statuses + ('canceled',)})]
        if plan.kind in AMOUNT_KINDS:
            changes.append(('amount_source_changed', {'amount_source': 'items' if plan.amount_source == 'payments' else 'payments'}))
        for name, change in changes:
            proposal = {'plan': {**plan.payload(), **change}}
            try:
                Workflow._accept_plan(Task(PROJECT, plan.dataset_version, 'q'), proposal, metric)
                outcome = 'accepted'
            except PermissionError:
                outcome = 'rejected'
            total += 1
            rejected += outcome == 'rejected'
            records.append({'case': case['case'], 'operator': name, 'outcome': outcome})
    return {'rate': rate(rejected, total), 'records': records}


def layer_rates(exposed: list[dict]) -> dict:
    rates = {}
    for layer in LAYERS:
        applicable = [row for row in exposed if row['layers'][layer] is not None]
        rates[layer] = rate(sum(row['layers'][layer] is False for row in applicable), len(applicable))
    rates['reference_free_union'] = rate(sum(any(row['layers'][layer] is False for layer in REFERENCE_FREE) for row in exposed),
                                         len(exposed))
    return rates


def summarize(records: list[dict]) -> dict:
    exposed = [row for row in records if row['status'] == 'exposed']
    groups = {}
    for key in ('family', 'operator'):
        groups[key] = {}
        for name in sorted({row[key] for row in records}):
            items = [row for row in records if row[key] == name]
            hit = [row for row in items if row['status'] == 'exposed']
            groups[key][name] = {'cases': len(items), 'exposed': len(hit),
                                 'equivalent': sum(row['status'] == 'equivalent' for row in items),
                                 'crashed': sum(row['status'] == 'crashed' for row in items),
                                 'layers': {layer: value['caught'] for layer, value in layer_rates(hit).items()} if hit else {}}
    return {'mutant_cases': len(records), 'exposed': len(exposed),
            'equivalent': sum(row['status'] == 'equivalent' for row in records),
            'crashed': sum(row['status'] == 'crashed' for row in records),
            'layers': layer_rates(exposed),
            'metamorphic_relations': {name: sum(any(issue.startswith(name + ':') for issue in row['metamorphic_issues']) for row in exposed)
                                      for name in metamorphic.RELATIONS},
            'verified_but_wrong': sum(all(value is not False for value in row['layers'].values()) for row in exposed),
            'reference_free_residual': sum(all(row['layers'][layer] is not False for layer in REFERENCE_FREE) for row in exposed),
            **groups}


def percent(value: dict) -> str:
    if not value['total']:
        return '—'
    low, high = value['wilson95']
    return f"{value['caught']}/{value['total']}（{value['rate'] * 100:.1f}%，[{low * 100:.1f}, {high * 100:.1f}]）"


def markdown(payload: dict) -> str:
    code = payload['code_summary']
    lines = ['# Olist 错误注入基准结果', '',
             f"提交 `{payload['git_commit']}`，日期 {payload['date']}，变异目录指纹 `{payload['catalog_sha256'][:12]}…`，"
             f"Olist 留出集指纹 `{payload['heldout_sha256'][:12]}…`，墙钟 {payload['wall_seconds']} 秒。数据为真实 Olist 全量（一份）。"
             '区间为 95% Wilson，同一算子在不同用例上不是独立样本，区间只作参考。', '',
             f"{len(payload['operators'])} 个算子 × 适用的 {payload['baseline']['cases']} 个留出集计划，共 {code['mutant_cases']} 个变异用例："
             f"暴露 {code['exposed']}，未暴露 {code['equivalent']}，执行失败 {code['crashed']}。"
             f"正确代码在 {payload['baseline']['cases']} 个用例上被任一层误判 {payload['baseline']['false_alarms']} 次。", '',
             '| 核验层 | 检出 / 暴露（比例，95% 区间） |', '| --- | --- |']
    lines += [f'| {layer} | {percent(value)} |' for layer, value in code['layers'].items()]
    lines += ['', f"通过全部层却是错的：{code['verified_but_wrong']}；只看不依赖参考的两层会漏过 {code['reference_free_residual']} 个。"
              '蜕变关系各自检出：' + '、'.join(f'{name} {count}' for name, count in code['metamorphic_relations'].items()) + '。', '',
              '| 算子 | 用例 | 暴露 | 未暴露 | 执行失败 | 参考 | 不变量 | 蜕变 | 不依赖参考的并集 |',
              '| --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for name, value in code['operator'].items():
        layer = value['layers']
        lines.append(f"| {name} | {value['cases']} | {value['exposed']} | {value['equivalent']} | {value['crashed']} | "
                     f"{layer.get('independent_reference', 0)} | {layer.get('invariants', 0)} | {layer.get('metamorphic', 0)} | "
                     f"{layer.get('reference_free_union', 0)} |")
    lock = payload['metric_lock']['rate']
    lines += ['', f"口径锁：纳入状态或订单金额来源与已确认口径不同的计划被拒绝 {lock['caught']}/{lock['total']}。"]
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description='Olist 错误注入基准（不调用模型）')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--image', default='datalab-executor:py312-v2')
    parser.add_argument('--raw', default=str(ROOT / 'artifacts/olist/raw'))
    parser.add_argument('--output', default='olist-injection.json')
    parser.add_argument('--markdown', default='olist-injection-report.md')
    args = parser.parse_args()
    if olist_questions.fingerprint() != olist_questions.OLIST_HELDOUT_SHA256:
        raise SystemExit('Olist 留出题集与冻结指纹不符')
    for operator in OPERATORS:
        compile(assemble({}, operator.apply()), operator.name, 'exec')
    scratch = ROOT / '.tmp'
    scratch.mkdir(exist_ok=True)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(dir=scratch, prefix='olist-injection-') as folder:
        lab = DockerLab(Path(folder), args.image)
        cases = prepare(lab, Path(args.raw))
        print(f'用例 {len(cases)} 个，先跑正确代码', flush=True)
        with ThreadPoolExecutor(args.workers) as pool:
            baseline = list(pool.map(lambda case: run_correct(lab, case), cases))
        false_alarms = sum(any(value is False for value in row['layers'].values()) for row in baseline)
        jobs = [(operator, case) for operator in OPERATORS for case in cases if case['kind'] in operator.kinds]
        print(f'正确代码误判 {false_alarms}；代码变异用例 {len(jobs)} 个', flush=True)
        done, lock = [0], threading.Lock()

        def mutant_job(job):
            record = run_mutant(lab, *job)
            with lock:
                done[0] += 1
                if done[0] % 20 == 0:
                    print(f'{done[0]}/{len(jobs)}', round(time.perf_counter() - started), flush=True)
            return record

        with ThreadPoolExecutor(args.workers) as pool:
            records = list(pool.map(mutant_job, jobs))
        locks = metric_lock(cases)
    payload = {'date': time.strftime('%Y-%m-%d'), 'git_commit': git_commit(), 'wall_seconds': round(time.perf_counter() - started, 1),
               'config': {'workers': args.workers, 'image': args.image, 'metric_version': METRIC, 'metamorphic_seed': metamorphic.SEED,
                          'included_statuses': list(olist_questions.STATUSES), 'model_calls': 0},
               'heldout_sha256': olist_questions.OLIST_HELDOUT_SHA256, 'catalog_sha256': catalog_fingerprint(),
               'base_code_sha256': hashlib.sha256(BASE.encode('utf-8')).hexdigest(),
               'operators': [{'name': item.name, 'family': item.family, 'kinds': list(item.kinds), 'note': item.note} for item in OPERATORS],
               'baseline': {'cases': len(baseline), 'false_alarms': false_alarms, 'records': baseline},
               'code_summary': summarize(records), 'metric_lock': locks, 'code_records': records}
    output = ROOT / 'artifacts/evals'
    output.mkdir(parents=True, exist_ok=True)
    (output / args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
    (output / args.markdown).write_text(markdown(payload), encoding='utf-8')
    print(markdown(payload))


if __name__ == '__main__':
    main()
