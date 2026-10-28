"""错误注入基准：度量每一层核验对“已知是错的”代码与计划各能拦下多少。

代码变异：从服务端固定分析（execution/fixed_analysis.py）的 analyze 出发，改成框架形式（与模型写的函数同一形态），
按变异算子目录做精确锚点替换，生成带错误家族标签的变异体；每个变异体 × 留出集真值计划 × 若干种子数据，
在真实受限沙箱中运行，并对其结果分别跑独立参考、分组合计、下载表、结果不变量、两份参考一致性与蜕变关系。
变异体在某个用例上的输出与正确代码完全相同时记为“未暴露”（在该输入上等价），不计入检出率分母，只报告数量。

计划变异：扰动留出集真值计划（期间、分组、类型、两期、口径状态）。数值层的参考跟着计划走，按设计看不到这类错误，
这里明确测出这个盲区；口径状态的改动由服务端计划校验（与已确认口径比对）拒绝；其余交给 Reviewer，
用真实 MiMo 调用测其检出率，并用真值计划作对照组测误拒率。

默认不调用模型，只需要 Docker 与执行镜像；--reviewer 才会发出 MiMo 付费请求（只用 MiMo）。
结果写入 artifacts/evals/injection.json 与 injection-report.md。
"""
import argparse
import calendar
from concurrent.futures import ThreadPoolExecutor
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import hashlib
import io
import json
from pathlib import Path
import random
import sys
import tempfile
import threading
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
import heldout  # noqa: E402
from model_compare import FAMILY, PRICES, ROOT, cost, dataset_bytes  # noqa: E402
from stats import wilson  # noqa: E402

from datalab.contracts import AnalysisPlan, VerificationReport  # noqa: E402
from datalab.datasets.service import DatasetStore  # noqa: E402
from datalab.execution import fixed_analysis  # noqa: E402
from datalab.execution.analysis_frame import assemble  # noqa: E402
from datalab.execution.runner import DockerRunner, OutputSpec  # noqa: E402
from datalab.orchestration.workflow import MetricSnapshot, Task, Workflow, model_profile, role_plan  # noqa: E402
from datalab.verification import invariants, metamorphic  # noqa: E402
from datalab.verification.python_reference import agreement  # noqa: E402
from datalab.verification.reference import differences, reference_result, verify  # noqa: E402
from datalab.verification.table import table_issue  # noqa: E402

PROJECT = 'injection'
METRIC = 'injection-v1'
BOUND_KEYS = ('start', 'end', 'previous_start', 'previous_end')
NON_QUALITY = ('summary', 'trend', 'ranking', 'share', 'comparison')
GROUPED = ('trend', 'ranking', 'share')
LAYERS = ('independent_reference', 'group_total', 'download_table', 'invariants', 'reference_agreement', 'metamorphic')
REFERENCE_FREE = ('invariants', 'metamorphic')
# 种子数据沿用 model_compare 的生成器；对抗数据在其上追加恰在留出集各期间边界（及前后一秒）的订单。
DATASETS = (('seed-20260928', 20260928, False, ('paid',)), ('seed-20261023', 20261023, False, ('paid', 'refunded')),
            ('adversarial-20261024', 20261024, True, ('paid',)), ('adversarial-20261025', 20261025, True, ('paid', 'refunded')))
# 仅为防止误用的硬上限；本基准默认约 125 次 Reviewer 调用。
MAX_REVIEWER_CALLS = 150
MAX_COST_YUAN = 10.0


class MutationError(ValueError):
    """锚点缺失或不唯一：基准代码变了而目录没跟上，必须显式失败。"""


def base_code() -> str:
    """固定分析的 analyze 改成框架形态：输入改为框架读好的 data，其余逻辑原样保留。"""
    source = Path(fixed_analysis.__file__).read_text(encoding='utf-8')
    body = source[source.index('def ratio('):source.index('def export_result(')].rstrip() + '\n'
    anchor = ('def analyze(csv_path, plan):\n    with open(csv_path, encoding="utf-8", newline="") as handle:\n'
              '        source = list(csv.DictReader(handle))\n')
    if body.count(anchor) != 1:
        raise MutationError('固定分析的读入段落已变化')
    return body.replace(anchor, 'def analyze(data, plan):\n    source = data\n')


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


SELECT_END = 'and (not end or row["payment_time"] < end)'
SELECT_START = 'and (not start or row["payment_time"] >= start)'
STATUS_FILTER = 'row for row in source if row["status"] in plan["included_statuses"]'
CURRENT = 'rows = selected(plan["start"], plan["end"])'
PREVIOUS = 'previous = selected(plan["previous_start"], plan["previous_end"])'
TOTAL_AMOUNT = '"amount_cents": sum(int(row["amount_cents"]) for row in rows)}'
GROUPS = 'result_rows = [{"group": key, **value} for key, value in sorted(groups.items())]'
RANK_KEY = 'result_rows.sort(key=lambda row: (-row["amount_cents"], row["group"]))'
SHARE = 'row["share"] = ratio(row["amount_cents"], total["amount_cents"])'
QUANTIZE = 'return str((Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.000001")))'

OPERATORS = (
    Operator('end_inclusive', 'boundary', NON_QUALITY, ((SELECT_END, 'and (not end or row["payment_time"] <= end)'),),
             '期间终点按闭区间处理'),
    Operator('start_exclusive', 'boundary', NON_QUALITY, ((SELECT_START, 'and (not start or row["payment_time"] > start)'),),
             '期间起点按开区间处理'),
    Operator('start_next_day', 'boundary', NON_QUALITY,
             ((SELECT_START, 'and (not start or row["payment_time"][:10] > start[:10])'),), '起点当天整天被排除（差一天）'),
    Operator('status_filter_dropped', 'status', NON_QUALITY, ((STATUS_FILTER, 'row for row in source if row["status"]'),),
             '不按口径状态过滤'),
    Operator('status_hardcoded_paid', 'status', NON_QUALITY, ((STATUS_FILTER, 'row for row in source if row["status"] == "paid"'),),
             '口径状态写死为 paid'),
    Operator('share_unfiltered_total', 'ratio', ('share',),
             ((SHARE, 'row["share"] = ratio(row["amount_cents"], sum(int(item["amount_cents"]) for item in source))'),),
             '占比分母用未过滤的全部订单金额'),
    Operator('ratio_float', 'rounding', ('share', 'comparison'),
             ((QUANTIZE, 'return format(numerator / denominator, ".6f")'),), '比值用浮点除法再格式化'),
    Operator('ratio_truncated', 'rounding', ('share', 'comparison'),
             ((QUANTIZE, 'return str((Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.000001"), rounding="ROUND_DOWN"))'),),
             '比值截断而不是四舍五入'),
    Operator('ratio_five_places', 'rounding', ('share', 'comparison'),
             ((QUANTIZE, 'return str((Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.00001")))'),), '比值只保留 5 位小数'),
    Operator('ranking_ascending', 'ordering', ('ranking',),
             ((RANK_KEY, 'result_rows.sort(key=lambda row: (row["amount_cents"], row["group"]))'),), '排行按金额升序'),
    Operator('ranking_tie_reversed', 'ordering', ('ranking',),
             ((RANK_KEY, 'result_rows.sort(key=lambda row: (-row["amount_cents"], [-ord(char) for char in row["group"]]))'),),
             '金额相同时组名降序'),
    Operator('comparison_periods_swapped', 'comparison', ('comparison',),
             ((CURRENT, 'rows = selected(plan["previous_start"], plan["previous_end"]) if plan["kind"] == "comparison" else ' + CURRENT[7:]),
              (PREVIOUS, 'previous = selected(plan["start"], plan["end"])')), '本期与上期取反'),
    Operator('delta_sign_flipped', 'comparison', ('comparison',),
             (('delta = total["amount_cents"] - previous_amount', 'delta = previous_amount - total["amount_cents"]'),), '差额符号取反'),
    Operator('growth_wrong_base', 'comparison', ('comparison',),
             (('"growth": ratio(delta, previous_amount)', '"growth": ratio(delta, total["amount_cents"])'),), '增长率以本期为分母'),
    Operator('date_key_month', 'grouping', GROUPED,
             (('key = row["payment_time"][:10] if', 'key = row["payment_time"][:7] if'),), '按日分组截成了月份'),
    Operator('boundary_double_count', 'aggregation', NON_QUALITY,
             ((CURRENT, CURRENT + ' + [row for row in source if plan["start"] and row["payment_time"] == plan["start"]'
                         ' and row["status"] in plan["included_statuses"]]'),), '起点上的订单被两段窗口重复计入'),
    Operator('total_in_yuan', 'scale', NON_QUALITY,
             ((TOTAL_AMOUNT, '"amount_cents": sum(int(row["amount_cents"]) for row in rows) // 100}'),), '总计金额误换算为元'),
    Operator('spurious_group', 'rows', GROUPED,
             ((GROUPS, GROUPS + '\n    if group_by:\n        result_rows.append({"group": "其他", "order_count": 0, "amount_cents": 0})'),),
             '多出一行空的“其他”分组'),
    Operator('last_group_dropped', 'rows', GROUPED,
             ((GROUPS, GROUPS + '\n    result_rows = result_rows[:-1]'),), '分组行少了最后一行（行数上限差一）'),
    Operator('quality_status_filtered', 'quality', ('quality',),
             (('"kind": "quality", "row_count": len(source),',
               '"kind": "quality", "row_count": len([row for row in source if row["status"] in plan["included_statuses"]]),'),),
             '质量行数错误地按口径过滤'),
)


def catalog_fingerprint() -> str:
    payload = {'base_sha256': hashlib.sha256(BASE.encode('utf-8')).hexdigest(),
               'operators': [asdict(item) for item in OPERATORS]}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def _shift(stamp, **delta):
    return (datetime.fromisoformat(stamp) + timedelta(**delta)).isoformat(timespec='seconds')


def _next_month(stamp):
    moment = datetime.fromisoformat(stamp)
    year, month = (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
    return moment.replace(year=year, month=month, day=min(moment.day, calendar.monthrange(year, month)[1])).isoformat(timespec='seconds')


def adversarial_bytes(seed: int) -> bytes:
    """同分布的种子数据，另在留出集出现过的每个期间边界上、前一秒与后一秒各放三笔不同状态的订单。"""
    rng = random.Random(seed)
    bounds = sorted({value for _, fields in heldout.HELDOUT for key, value in fields.items() if key in BOUND_KEYS and value})
    out = io.StringIO()
    writer = csv.writer(out, lineterminator='\n')
    index = 0
    for bound in bounds:
        for stamp in (_shift(bound, seconds=-1), bound, _shift(bound, seconds=1)):
            for status in ('paid', 'refunded', 'cancelled'):
                cents = rng.randrange(500, 80000)
                writer.writerow([f'B{index:05d}', stamp, f'{cents // 100}.{cents % 100:02d}', status,
                                 rng.choice(['自然流量', '广告', '直播', '社群']), rng.choice(['服饰', '食品', '数码', '家居'])])
                index += 1
    return dataset_bytes(seed) + out.getvalue().encode('utf-8')


# ---- 计划变异 ----

def _plan_mutants(fields: dict) -> list[tuple[str, dict]]:
    kind = fields['kind']
    found = []
    if fields.get('end'):
        found.append(('end_plus_day', {**fields, 'end': _shift(fields['end'], days=1)}))
    if fields.get('start') or fields.get('end'):
        found.append(('period_next_month', {**fields, **{key: _next_month(fields[key]) for key in ('start', 'end') if fields.get(key)}}))
    if fields.get('group_by'):
        found.append(('group_by_swapped', {**fields, 'group_by': {'channel': 'category', 'category': 'channel',
                                                                   'payment_date': 'channel'}[fields['group_by']]}))
    changed = {'summary': {'kind': 'trend', 'group_by': 'payment_date'}, 'trend': {'kind': 'summary', 'group_by': None},
               'ranking': {'kind': 'share'}, 'share': {'kind': 'ranking'}, 'quality': {'kind': 'summary'},
               'comparison': {'kind': 'summary', 'previous_start': None, 'previous_end': None}}[kind]
    found.append(('kind_changed', {**fields, **changed}))
    if kind == 'comparison':
        found.append(('periods_swapped', {**fields, 'start': fields['previous_start'], 'end': fields['previous_end'],
                                          'previous_start': fields['start'], 'previous_end': fields['end']}))
    return found


def plan_mutants() -> list[dict]:
    items = []
    for index, (question, fields) in enumerate(heldout.HELDOUT):
        for name, mutated in _plan_mutants(fields):
            items.append({'operator': name, 'case': index, 'question': question, 'truth': fields, 'fields': mutated})
        # 口径状态改动：由服务端与已确认口径比对拒绝，不交给 Reviewer。
        items.append({'operator': 'statuses_changed', 'case': index, 'question': question, 'truth': fields,
                      'fields': fields, 'statuses': 'changed'})
    return items


# ---- 执行与核验 ----

class DockerLab:
    """真实受限沙箱：同一个 DockerRunner 运行主结果与蜕变重跑。"""

    def __init__(self, folder: Path, image: str):
        self.store = DatasetStore(folder / 'datasets')
        self.runner = DockerRunner(self.store, folder / 'runs', image=image)

    def run(self, plan: AnalysisPlan, script: str):
        run_id = str(uuid4())
        manifest = self.runner.execute_python(run_id, plan.dataset_version, script, 30, OutputSpec(),
                                              project_id=PROJECT, metric_version=plan.metric_version)
        if manifest.status != 'COMPLETED':
            return None, None
        folder = self.runner.root / run_id / 'output'
        try:
            return json.loads((folder / 'result.json').read_text(encoding='utf-8')), folder / 'table.csv'
        except (OSError, ValueError):
            return None, None

    def execute(self, plan: AnalysisPlan):
        return metamorphic.runner_execute(self.runner, plan, PROJECT)


def evaluate(lab, plan: AnalysisPlan, script: str, result, table: Path) -> dict:
    """对一个结果跑全部核验层，各层互不短路；True 为通过，None 为该层不适用。"""
    orders = lab.store.directory(plan.dataset_version) / 'orders.csv'
    report = verify(orders, plan, result) if isinstance(result, dict) else VerificationReport(False, [
        {'name': 'independent_reference', 'passed': False}])
    passed = {item['name']: item['passed'] for item in report.checks}
    reference = reference_result(orders, plan)
    found = invariants.violations(plan, result)
    meta = metamorphic.check(plan, orders, result, metamorphic.Sandbox(script, lab.execute(plan)))
    layers = {'independent_reference': passed['independent_reference'], 'group_total': passed.get('group_total'),
              'download_table': table_issue(table, reference) is None,
              'invariants': not found, 'reference_agreement': agreement(orders, plan, reference)['passed'],
              'metamorphic': meta['passed']}
    return {'layers': layers, 'invariant_issues': sorted({item['issue'] for item in found}),
            'relations': {item['relation']: item['status'] for item in meta['relations']},
            'metamorphic_issues': sorted({f"{item['relation']}:{item['issue']}" for item in meta.get('issues', [])})}


def prepare(lab, datasets=DATASETS) -> list[dict]:
    cases = []
    for name, seed, adversarial, statuses in datasets:
        data = adversarial_bytes(seed) if adversarial else dataset_bytes(seed)
        version = lab.store.import_csv(data, project_id=PROJECT, dataset_family=FAMILY, currency='CNY', timezone='Asia/Shanghai')
        for index, (question, fields) in enumerate(heldout.HELDOUT):
            plan = AnalysisPlan(version.dataset_version, METRIC, included_statuses=statuses, **fields)
            cases.append({'dataset': name, 'seed': seed, 'case': index, 'kind': fields['kind'], 'plan': plan})
    return cases


def run_correct(lab, case: dict) -> dict:
    plan = case['plan']
    script = assemble(role_plan(plan), BASE)
    result, table = lab.run(plan, script)
    if result is None:
        raise RuntimeError('正确代码执行失败；检查 Docker 与执行镜像')
    case['correct'], case['table'] = result, table.read_bytes()
    record = evaluate(lab, plan, script, result, table)
    return {'dataset': case['dataset'], 'case': case['case'] + 1, 'kind': case['kind'], **record}


def run_mutant(lab, operator: Operator, case: dict) -> dict:
    plan = case['plan']
    script = assemble(role_plan(plan), operator.apply())
    record = {'operator': operator.name, 'family': operator.family, 'dataset': case['dataset'], 'case': case['case'] + 1,
              'kind': case['kind']}
    result, table = lab.run(plan, script)
    if result is None:
        return {**record, 'status': 'crashed'}
    if not differences(case['correct'], result) and table.read_bytes() == case['table']:
        return {**record, 'status': 'equivalent'}
    return {**record, 'status': 'exposed', **evaluate(lab, plan, script, result, table)}


def run_plan_mutant(lab, item: dict, versions: dict, statuses: tuple) -> dict:
    """计划变异只在第一份种子数据上执行：正确代码按变异计划运行，再跑全部数值层。"""
    version = versions['seed-20260928']
    truth = AnalysisPlan(version, METRIC, included_statuses=statuses, **item['truth'])
    record = {key: item[key] for key in ('operator', 'case', 'question')}
    record['case'] += 1
    record['fields'] = item['fields']
    if 'statuses' in item:
        # 与工作流相同的计划接收校验：纳入状态必须等于已确认口径。
        task = Task(PROJECT, version, item['question'])
        metric = MetricSnapshot(METRIC, statuses, 'injection-trusted', PROJECT, FAMILY, 'schema')
        # 在已确认口径上多纳入或少纳入一种状态。
        changed = statuses + ('cancelled',) if 'cancelled' not in statuses else tuple(name for name in statuses if name != 'cancelled')
        proposal = {'plan': AnalysisPlan(version, METRIC, included_statuses=changed, **item['fields']).payload()}
        record['statuses'] = list(changed)
        try:
            Workflow._accept_plan(task, proposal, metric)
            record['metric_lock'] = 'accepted'
        except PermissionError:
            record['metric_lock'] = 'rejected'
        return record
    plan = AnalysisPlan(version, METRIC, included_statuses=statuses, **item['fields'])
    orders = lab.store.directory(version) / 'orders.csv'
    record['result_differs'] = reference_result(orders, plan) != reference_result(orders, truth)
    script = assemble(role_plan(plan), BASE)
    result, table = lab.run(plan, script)
    if result is None:
        raise RuntimeError('正确代码执行失败；检查 Docker 与执行镜像')
    record.update(evaluate(lab, plan, script, result, table))
    return record


# ---- Reviewer（真实 MiMo） ----

def reviewer_jobs(plan_records: list[dict], control_repeats: int = 2) -> list[dict]:
    jobs = [{'label': 'mutant', 'operator': item['operator'], 'case': item['case'], 'fields': item['fields']}
            for item in plan_records if 'metric_lock' not in item]
    jobs += [{'label': 'control', 'operator': 'truth', 'case': index + 1, 'fields': fields, 'repeat': repeat}
             for index, (_, fields) in enumerate(heldout.HELDOUT) for repeat in range(control_repeats)]
    return jobs


def review(jobs: list[dict], version, workers: int = 6) -> list[dict]:
    """按工作流实际交接格式调用 Reviewer；只用 MiMo。核验结论按数值层的实际结果标为通过。"""
    from datalab.roles.llm import ModelBackend
    from datalab.roles.model_config import load_model_config
    from datalab.roles.runtime import CallBudget, ModelCallError, Role, RoleRuntime
    if len(jobs) > MAX_REVIEWER_CALLS:
        raise SystemExit(f'计划 {len(jobs)} 次 Reviewer 调用，超过上限 {MAX_REVIEWER_CALLS}')
    config = load_model_config(ROOT / '.env').profile('mimo')
    profile = model_profile({'dataset_family': FAMILY, 'schema_signature': version.schema_signature, 'currency': 'CNY',
                             'timezone': 'Asia/Shanghai', 'quality': version.profile})
    checks = [{'name': name, 'passed': True} for name in AnalysisPlan(version.dataset_version, METRIC).checks]
    spent, lock = [0.0], threading.Lock()

    def call(job):
        with lock:
            if spent[0] > MAX_COST_YUAN:
                return {**job, 'accepted': None, 'feedback': '费用超过上限，未调用', 'usage': []}
        plan = AnalysisPlan(version.dataset_version, METRIC, included_statuses=('paid',), **job['fields'])
        handoff = Workflow.review_handoff(str(uuid4()), heldout.HELDOUT[job['case'] - 1][0], profile, plan, [str(uuid4())],
                                          VerificationReport(True, checks))
        backend = ModelBackend(config)
        started = time.perf_counter()
        try:
            reply = RoleRuntime(backend).invoke(Role.REVIEWER, handoff, CallBudget(1, 150000, 600))
        except ModelCallError as error:
            reply = {'accepted': None, 'feedback': str(error)[:200]}
        usage = backend.client.usage
        with lock:
            spent[0] += cost('mimo', usage)
        return {**job, 'accepted': reply.get('accepted'), 'feedback': str(reply.get('feedback'))[:300],
                'seconds': round(time.perf_counter() - started, 2), 'usage': usage}

    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(call, jobs))


def usage_totals(rows: list[dict]) -> dict:
    usage = [item for row in rows for item in row.get('usage', [])]
    return {'calls': len(rows), 'prompt_tokens': sum(item.get('prompt_tokens') or 0 for item in usage),
            'cached_tokens': sum(item.get('cached_tokens') or 0 for item in usage),
            'completion_tokens': sum(item.get('completion_tokens') or 0 for item in usage),
            'cost_yuan': round(cost('mimo', usage), 6)}


# ---- 汇总 ----

def rate(caught: int, total: int) -> dict:
    low, high = wilson(caught, total)
    return {'caught': caught, 'total': total, 'rate': round(caught / total, 4) if total else None,
            'wilson95': [round(low, 4), round(high, 4)] if total else None}


def layer_rates(exposed: list[dict]) -> dict:
    rates = {}
    for layer in LAYERS:
        applicable = [row for row in exposed if row['layers'][layer] is not None]
        rates[layer] = rate(sum(row['layers'][layer] is False for row in applicable), len(applicable))
    rates['reference_free_union'] = rate(sum(any(row['layers'][layer] is False for layer in REFERENCE_FREE) for row in exposed),
                                         len(exposed))
    return rates


def summarize_code(records: list[dict]) -> dict:
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
    relations = {name: sum(any(issue.startswith(name + ':') for issue in row['metamorphic_issues']) for row in exposed)
                 for name in metamorphic.RELATIONS}
    return {'mutant_cases': len(records), 'exposed': len(exposed),
            'equivalent': sum(row['status'] == 'equivalent' for row in records),
            'crashed': sum(row['status'] == 'crashed' for row in records),
            'layers': layer_rates(exposed), 'metamorphic_relations': relations,
            # 通过全部层却是错的：参考按定义能检出所有暴露的变异，这一项应为 0。
            'verified_but_wrong': sum(all(value is not False for value in row['layers'].values()) for row in exposed),
            # 只有不依赖参考的两层时会漏过的错误。
            'reference_free_residual': sum(all(row['layers'][layer] is not False for layer in REFERENCE_FREE) for row in exposed),
            **groups}


def summarize_plans(records: list[dict], reviews: list[dict]) -> dict:
    numeric = [row for row in records if 'layers' in row]
    locked = [row for row in records if 'metric_lock' in row]
    summary = {'plan_mutants': len(records), 'numeric_evaluated': len(numeric),
               'numeric_result_differs': sum(row['result_differs'] for row in numeric),
               # 盲区：结果与真值不同，但全部数值层（含参考）都通过。
               'numeric_blind_spot': rate(sum(row['result_differs'] and all(value is not False for value in row['layers'].values())
                                              for row in numeric), sum(row['result_differs'] for row in numeric)),
               'numeric_layers_any_fail': sum(any(value is False for value in row['layers'].values()) for row in numeric),
               'metric_lock': rate(sum(row['metric_lock'] == 'rejected' for row in locked), len(locked))}
    if reviews:
        mutants = [row for row in reviews if row['label'] == 'mutant']
        controls = [row for row in reviews if row['label'] == 'control']
        summary['reviewer'] = {
            'catch': rate(sum(row['accepted'] is False for row in mutants), sum(row['accepted'] is not None for row in mutants)),
            'false_rejection': rate(sum(row['accepted'] is False for row in controls), sum(row['accepted'] is not None for row in controls)),
            'errors': sum(row['accepted'] is None for row in reviews),
            'by_operator': {name: rate(sum(row['accepted'] is False for row in mutants if row['operator'] == name),
                                       sum(row['accepted'] is not None for row in mutants if row['operator'] == name))
                            for name in sorted({row['operator'] for row in mutants})},
            'usage': usage_totals(reviews)}
    return summary


def percent(value: dict) -> str:
    if not value['total']:
        return '—'
    low, high = value['wilson95']
    return f"{value['caught']}/{value['total']}（{value['rate'] * 100:.1f}%，[{low * 100:.1f}, {high * 100:.1f}]）"


def markdown(payload: dict) -> str:
    code, plans = payload['code_summary'], payload['plan_summary']
    lines = ['# 错误注入基准结果', '',
             f"提交 `{payload['git_commit']}`，日期 {payload['date']}，变异目录指纹 `{payload['catalog_sha256'][:12]}…`，"
             f"留出集指纹 `{payload['heldout_sha256'][:12]}…`。区间为 95% Wilson，同一算子在不同用例上不是独立样本，区间只作参考。", '',
             '## 代码变异', '',
             f"{len(payload['operators'])} 个算子 × 适用的留出集计划 × {len(payload['datasets'])} 份数据，共 {code['mutant_cases']} 个变异用例："
             f"暴露 {code['exposed']}，未暴露（该输入上与正确代码输出相同）{code['equivalent']}，执行失败 {code['crashed']}。"
             f"正确代码在 {payload['baseline']['cases']} 个用例上被任一层误判 {payload['baseline']['false_alarms']} 次。", '',
             '| 核验层 | 检出 / 暴露（比例，95% 区间） |', '| --- | --- |']
    lines += [f'| {layer} | {percent(value)} |' for layer, value in code['layers'].items()]
    lines += ['', f"通过全部层却是错的：{code['verified_but_wrong']}；只看不依赖参考的两层（invariants ∪ metamorphic）会漏过 "
              f"{code['reference_free_residual']} 个。蜕变关系各自检出：" +
              '、'.join(f'{name} {count}' for name, count in code['metamorphic_relations'].items()) + '。', '',
              '| 错误家族 | 用例 | 暴露 | 未暴露 | 参考 | 不变量 | 蜕变 | 不依赖参考的并集 |', '| --- | --- | --- | --- | --- | --- | --- | --- |']
    for name, value in code['family'].items():
        layer = value['layers']
        lines.append(f"| {name} | {value['cases']} | {value['exposed']} | {value['equivalent']} | {layer.get('independent_reference', 0)} | "
                     f"{layer.get('invariants', 0)} | {layer.get('metamorphic', 0)} | {layer.get('reference_free_union', 0)} |")
    lines += ['', '| 算子 | 暴露 | 未暴露 | 不变量 | 蜕变 | 不依赖参考的并集 |', '| --- | --- | --- | --- | --- | --- |']
    for name, value in code['operator'].items():
        layer = value['layers']
        lines.append(f"| {name} | {value['exposed']} | {value['equivalent']} | {layer.get('invariants', 0)} | "
                     f"{layer.get('metamorphic', 0)} | {layer.get('reference_free_union', 0)} |")
    lines += ['', '## 计划变异', '',
              f"共 {plans['plan_mutants']} 个计划变异。口径状态改动由计划接收校验拒绝：{percent(plans['metric_lock'])}。"
              f"其余 {plans['numeric_evaluated']} 个用正确代码按变异计划执行，其中 {plans['numeric_result_differs']} 个的结果与真值不同；"
              f"这些结果全部数值层都通过的比例（盲区）：{percent(plans['numeric_blind_spot'])}；任一数值层失败 {plans['numeric_layers_any_fail']} 个。"]
    if 'reviewer' in plans:
        reviewer = plans['reviewer']
        usage = reviewer['usage']
        lines += ['', f"Reviewer（MiMo）检出率 {percent(reviewer['catch'])}，真值计划误拒率 {percent(reviewer['false_rejection'])}，"
                  f"调用错误 {reviewer['errors']}。共 {usage['calls']} 次调用，输入 {usage['prompt_tokens']} token（缓存 {usage['cached_tokens']}），"
                  f"输出 {usage['completion_tokens']} token，费用 {usage['cost_yuan']:.4f} 元。", '',
                  '| 计划变异 | Reviewer 拒绝 / 有效调用 |', '| --- | --- |']
        lines += [f'| {name} | {percent(value)} |' for name, value in reviewer['by_operator'].items()]
    return '\n'.join(lines) + '\n'


def git_commit():
    import subprocess
    try:
        commit = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT, text=True, timeout=10).strip()
        dirty = bool(subprocess.check_output(['git', 'status', '--porcelain', '--', 'src', 'evals'], cwd=ROOT, text=True, timeout=10).strip())
    except (OSError, subprocess.SubprocessError):
        return None
    return commit + ('+dirty' if dirty else '')


def main():
    parser = argparse.ArgumentParser(description='错误注入基准（默认不调用模型；--reviewer 才发出 MiMo 付费请求）')
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--image', default='datalab-executor:py312-v2')
    parser.add_argument('--reviewer', action='store_true', help='用真实 MiMo 测 Reviewer 对计划变异的检出率与误拒率（付费）')
    parser.add_argument('--reviewer-smoke', action='store_true', help='只做 2 次 MiMo 调用的冒烟（1 个变异、1 个真值），不跑沙箱')
    parser.add_argument('--control-repeats', type=int, default=2)
    parser.add_argument('--output', default='injection.json')
    parser.add_argument('--markdown', default='injection-report.md')
    args = parser.parse_args()
    if heldout.fingerprint() != heldout.HELDOUT_SHA256:
        raise SystemExit('留出题集与冻结指纹不符；不得在查看结果后修改题目（见 evals/heldout.py）')
    for operator in OPERATORS:
        compile(assemble({}, operator.apply()), operator.name, 'exec')
    scratch = ROOT / '.tmp'
    scratch.mkdir(exist_ok=True)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(dir=scratch, prefix='injection-') as folder:
        if args.reviewer_smoke:
            store = DatasetStore(Path(folder) / 'datasets')
            version = store.import_csv(dataset_bytes(), project_id=PROJECT, dataset_family=FAMILY, currency='CNY', timezone='Asia/Shanghai')
            mutant = next(item for item in plan_mutants() if item['operator'] == 'end_plus_day')
            rows = review([{'label': 'mutant', 'operator': mutant['operator'], 'case': mutant['case'] + 1, 'fields': mutant['fields']},
                           {'label': 'control', 'operator': 'truth', 'case': 1, 'fields': heldout.HELDOUT[0][1], 'repeat': 0}],
                          version, workers=2)
            for row in rows:
                print(row['label'], row['case'], row['accepted'], row['feedback'][:120])
            print(json.dumps(usage_totals(rows), ensure_ascii=False))
            return
        lab = DockerLab(Path(folder), args.image)
        cases = prepare(lab)
        print(f'数据 {len(DATASETS)} 份、用例 {len(cases)} 个，先跑正确代码', flush=True)
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
                if done[0] % 50 == 0:
                    print(f'{done[0]}/{len(jobs)}', round(time.perf_counter() - started), flush=True)
            return record

        with ThreadPoolExecutor(args.workers) as pool:
            records = list(pool.map(mutant_job, jobs))
        versions = {case['dataset']: case['plan'].dataset_version for case in cases}
        statuses = DATASETS[0][3]
        items = plan_mutants()
        print(f'计划变异 {len(items)} 个', flush=True)
        with ThreadPoolExecutor(args.workers) as pool:
            plan_records = list(pool.map(lambda item: run_plan_mutant(lab, item, versions, statuses), items))
        reviews = []
        if args.reviewer:
            jobs = reviewer_jobs(plan_records, args.control_repeats)
            print(f'Reviewer 调用 {len(jobs)} 次（MiMo）', flush=True)
            reviews = review(jobs, lab.store.get(versions['seed-20260928'], PROJECT), args.workers)
    payload = {'date': time.strftime('%Y-%m-%d'), 'git_commit': git_commit(), 'wall_seconds': round(time.perf_counter() - started, 1),
               'config': {'workers': args.workers, 'image': args.image, 'metric_version': METRIC, 'reviewer': args.reviewer,
                          'control_repeats': args.control_repeats, 'metamorphic_seed': metamorphic.SEED,
                          'reviewer_model': 'mimo' if args.reviewer else None, 'prices_yuan_per_million': PRICES['mimo']},
               'heldout_sha256': heldout.HELDOUT_SHA256, 'catalog_sha256': catalog_fingerprint(),
               'base_code_sha256': hashlib.sha256(BASE.encode('utf-8')).hexdigest(),
               'datasets': [{'name': name, 'seed': seed, 'adversarial': adversarial, 'included_statuses': list(statuses)}
                            for name, seed, adversarial, statuses in DATASETS],
               'operators': [{'name': item.name, 'family': item.family, 'kinds': list(item.kinds), 'note': item.note} for item in OPERATORS],
               'baseline': {'cases': len(baseline), 'false_alarms': false_alarms, 'records': baseline},
               'code_summary': summarize_code(records), 'plan_summary': summarize_plans(plan_records, reviews),
               'code_records': records, 'plan_records': plan_records,
               'reviewer_records': [{key: value for key, value in row.items()} for row in reviews]}
    output = ROOT / 'artifacts/evals'
    output.mkdir(parents=True, exist_ok=True)
    (output / args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
    (output / args.markdown).write_text(markdown(payload), encoding='utf-8')
    print(markdown(payload))


if __name__ == '__main__':
    main()
