"""Olist 新分析类型：SQL 参考、纯 Python 参考与固定分析三份实现的性质测试，手算小例子固定口径，
以及不变量、核验入口、口径记忆与固定演示工作流的接线。"""
import copy
import json
from pathlib import Path
import random
import tempfile
import threading
import unittest
from uuid import uuid4

from olist_bundle import bundle, write
from datalab.contracts import AnalysisPlan, OLIST_GROUPS, OLIST_KINDS, OLIST_STATUSES, ExecutionManifest
from datalab.datasets import olist
from datalab.datasets.service import DatasetStore
from datalab.execution import fixed_olist
from datalab.execution.codegen import fixed_code
from datalab.memory.service import MemoryService, Scope
from datalab.orchestration.workflow import MetricSnapshot, State, Task, Workflow, model_metric
from datalab.roles.guided import GuidedBackend
from datalab.roles.runtime import RoleRuntime
from datalab.verification import invariants
from datalab.verification.olist_python import python_reference
from datalab.verification.olist_sql import reference_result
from datalab.verification.python_reference import agreement
from datalab.verification.suite import run_checks
from test_memory import FakeMemoryRepository

ROOT = Path(__file__).resolve().parents[2]
BOUNDS = [None, '2017-01-01T00:00:00', '2017-02-01T00:00:00', '2017-02-14T12:00:00', '2017-03-01T00:00:00',
          '2017-03-31T23:59:59', '2017-04-01T00:00:00', '2017-05-01T00:00:00', '2017-06-17T00:00:00', '2017-08-01T00:00:00']


def period(rng):
    while True:
        start, end = rng.choice(BOUNDS), rng.choice(BOUNDS)
        if not (start and end and start >= end):
            return start, end


def random_plan(rng, version):
    kind = rng.choice(OLIST_KINDS)
    group = rng.choice(sorted(OLIST_GROUPS[kind], key=str))
    statuses = tuple(name for name in OLIST_STATUSES if rng.random() < (0.9 if name == 'delivered' else 0.3))
    if kind == 'delivery_delay' and 'delivered' not in statuses:
        statuses = ('delivered',) + statuses
    statuses = statuses or ('shipped',)
    fields = {'amount_source': rng.choice(['payments', 'items'])} if kind in ('summary', 'trend', 'ranking', 'share', 'comparison') else {}
    if kind == 'delivery_delay':
        fields['time_field'] = rng.choice(['order_purchase_timestamp', 'order_delivered_customer_date'])
    if kind == 'comparison':
        while True:
            (start, end), (previous_start, previous_end) = period(rng), period(rng)
            if all((start, end, previous_start, previous_end)):
                break
        fields.update(start=start, end=end, previous_start=previous_start, previous_end=previous_end)
    else:
        fields['start'], fields['end'] = period(rng)
    return AnalysisPlan(version, 'olist-prop-v1', kind=kind, group_by=group, included_statuses=statuses,
                        schema='olist', **fields)


class Fixture(unittest.TestCase):
    def setUp(self):
        scratch = (ROOT / '.tmp').resolve()
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch, prefix='olist-kinds-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = DatasetStore(self.root / 'datasets')

    def dataset(self, tables, name='raw'):
        version = self.store.import_olist(write(self.root / name, tables), project_id='demo', dataset_family='olist')
        return version.dataset_version, self.store.directory(version.dataset_version)

    def fixed(self, folder, plan):
        """固定分析只读取服务端投影的输入表：写到临时输入目录后在本进程调用。"""
        inputs = self.root / ('inputs-' + uuid4().hex)
        inputs.mkdir()
        for name, data in olist.dataset_inputs(folder, plan).items():
            (inputs / name).write_bytes(data)
        return json.loads(json.dumps(fixed_olist.analyze(str(inputs), plan.payload())))


class ReferencePropertyTests(Fixture):
    def test_three_implementations_agree_on_random_bundles(self):
        # 200 份种子数据 × 2 个随机计划；每组同时满足全部不变量，两份参考一致性检查通过。
        combos, kinds, nonempty = 0, set(), 0
        for seed in range(200):
            rng = random.Random(5000 + seed)
            version, folder = self.dataset(bundle(rng, rng.choice([1, 3, 12, 40, 90]), months=rng.choice([1, 3, 7])), f'raw{seed}')
            for _ in range(2):
                plan = random_plan(rng, version)
                with self.subTest(seed=seed, plan=plan.payload()):
                    sql = reference_result(folder, plan)
                    self.assertEqual(python_reference(folder, plan), sql)
                    self.assertEqual(self.fixed(folder, plan), sql)
                    self.assertEqual(invariants.violations(plan, sql), [])
                    self.assertTrue(agreement(folder / 'orders.csv', plan, sql)['passed'])
                combos += 1
                kinds.add(plan.kind)
                nonempty += bool(sql['rows']) and any(type(value) is int and value for value in sql['total'].values())
        self.assertGreaterEqual(combos, 400)
        # 不只是空结果互相相等：相当一部分组合有分组行且总计非零（其余含不分组的计划与极小数据）。
        self.assertGreaterEqual(nonempty, 150)
        self.assertEqual(kinds, set(OLIST_KINDS))


def raw_order(order_id, customer, status, bought, delivered='', estimated='2017-01-20 00:00:00'):
    return [order_id, customer, status, bought, '', '', delivered, estimated]


class HandComputedTests(Fixture):
    """小数据手算，固定每类口径中最容易产生分歧的细节。"""

    def setUp(self):
        super().setUp()
        tables = {name: [] for name in olist.TABLES}
        people = {'c1': ('u1', 'SP'), 'c2': ('u1', 'RJ'), 'c3': ('u2', 'RJ'), 'c4': ('u3', 'MG'), 'c5': ('u1', 'SP'),
                  'c6': ('u4', 'SP'), 'c7': ('u5', 'MG')}
        tables['customers'] = [[cid, uid, '01000', 'x', state] for cid, (uid, state) in people.items()]
        tables['orders'] = [
            # u1：1 月首单（SP），2 月在 RJ 再买，3 月再买；u2：1 月一单；u3：2 月一单，取消一单。
            raw_order('o1', 'c1', 'delivered', '2017-01-05 10:00:00', '2017-01-20 18:00:00'),   # 同日送达不算延迟
            raw_order('o2', 'c2', 'delivered', '2017-02-03 09:00:00', '2017-02-25 08:00:00', '2017-02-24 00:00:00'),
            raw_order('o3', 'c3', 'shipped', '2017-01-31 23:59:59'),
            raw_order('o4', 'c4', 'delivered', '2017-02-10 00:00:00', '', '2017-02-20 00:00:00'),  # 没有送达日期
            raw_order('o5', 'c5', 'delivered', '2017-03-15 12:00:00', '2017-03-30 00:00:00', '2017-03-29 00:00:00'),
            raw_order('o6', 'c6', 'canceled', '2017-02-11 00:00:00', '2017-02-12 00:00:00', '2017-02-20 00:00:00'),
            raw_order('o7', 'c7', 'delivered', '2017-04-02 00:00:00', '2017-04-09 00:00:00', '2017-04-10 00:00:00'),
        ]
        tables['products'] = [['p1', 'cama_mesa_banho', '1', '1', '1', '1', '1', '1', '1'],
                              ['p2', 'pc_gamer', '1', '1', '1', '1', '1', '1', '1'],
                              ['p3', '', '', '', '', '1', '1', '1', '1']]
        tables['sellers'] = [['s', '01000', 'x', 'SP']]
        tables['category_translation'] = [['cama_mesa_banho', 'bed_bath_table']]
        tables['order_items'] = [['o1', '1', 'p1', 's', '2017-01-06 00:00:00', '10.00', '1.00'],
                                 ['o1', '2', 'p2', 's', '2017-01-06 00:00:00', '5.00', '0.50'],
                                 ['o2', '1', 'p1', 's', '2017-02-04 00:00:00', '20.00', '2.00'],
                                 ['o3', '1', 'p3', 's', '2017-02-01 00:00:00', '7.00', '0.00'],
                                 ['o5', '1', 'p2', 's', '2017-03-16 00:00:00', '1.00', '1.00']]
        tables['order_payments'] = [['o1', '1', 'credit_card', '1', '16.50'], ['o2', '1', 'boleto', '1', '20.00'],
                                    ['o2', '2', 'voucher', '1', '3.00'], ['o3', '1', 'boleto', '1', '7.00'],
                                    ['o6', '1', 'boleto', '1', '9.99']]
        tables['order_reviews'] = [[f'r{index}', order[0], '5', '', '', '2017-05-01 00:00:00', '2017-05-02 00:00:00']
                                   for index, order in enumerate(tables['orders'])]
        self.version, self.folder = self.dataset(tables)

    def check(self, expected, **fields):
        plan = AnalysisPlan(self.version, 'hand-v1', schema='olist', **fields)
        for compute in (reference_result, python_reference, self.fixed):
            self.assertEqual(compute(self.folder, plan), expected, compute)
        self.assertEqual(invariants.violations(plan, expected), [])

    def test_order_amount_sources_differ(self):
        statuses = ('delivered', 'shipped')
        self.check({'kind': 'summary', 'total': {'order_count': 6, 'amount_cents': 4650}, 'rows': []},
                   kind='summary', included_statuses=statuses, amount_source='payments')
        # 明细：o1 1650 + o2 2200 + o3 700 + o5 200；o4、o7 没有明细计 0。
        self.check({'kind': 'ranking', 'total': {'order_count': 6, 'amount_cents': 4750},
                    'rows': [{'group': 'RJ', 'order_count': 2, 'amount_cents': 2900},
                             {'group': 'SP', 'order_count': 2, 'amount_cents': 1850},
                             {'group': 'MG', 'order_count': 2, 'amount_cents': 0}]},
                   kind='ranking', group_by='customer_state', included_statuses=statuses, amount_source='items')
        # 期间 [1 月 31 日, 3 月 1 日)：o3 恰在 1 月最后一秒，o2 支付两笔共 2300，o4 没有支付计 0。
        self.check({'kind': 'trend', 'total': {'order_count': 3, 'amount_cents': 3000},
                    'rows': [{'group': '2017-01', 'order_count': 1, 'amount_cents': 700},
                             {'group': '2017-02', 'order_count': 2, 'amount_cents': 2300}]},
                   kind='trend', group_by='purchase_month', included_statuses=statuses, amount_source='payments',
                   start='2017-01-31T00:00:00', end='2017-03-01T00:00:00')

    def test_repeat_rate_counts_people_and_uses_first_order_state(self):
        # 纳入 delivered/shipped：u1 三单（首单 SP），u2 一单（RJ），u3 一单（MG），u5 一单（MG）。
        self.check({'kind': 'repeat_rate', 'total': {'customer_count': 4, 'repeat_customer_count': 1, 'repeat_rate': '0.250000'},
                    'rows': [{'group': 'MG', 'customer_count': 2, 'repeat_customer_count': 0, 'repeat_rate': '0.000000'},
                             {'group': 'RJ', 'customer_count': 1, 'repeat_customer_count': 0, 'repeat_rate': '0.000000'},
                             {'group': 'SP', 'customer_count': 1, 'repeat_customer_count': 1, 'repeat_rate': '1.000000'}]},
                   kind='repeat_rate', group_by='customer_state', included_statuses=('delivered', 'shipped'))
        # 期间从 2 月开始：u1 的首单不在期间内，按期间内第一单（RJ）归州。
        self.check({'kind': 'repeat_rate', 'total': {'customer_count': 3, 'repeat_customer_count': 1, 'repeat_rate': '0.333333'},
                    'rows': [{'group': 'MG', 'customer_count': 2, 'repeat_customer_count': 0, 'repeat_rate': '0.000000'},
                             {'group': 'RJ', 'customer_count': 1, 'repeat_customer_count': 1, 'repeat_rate': '1.000000'}]},
                   kind='repeat_rate', group_by='customer_state', included_statuses=('delivered',), start='2017-02-01T00:00:00')

    def test_cohort_retention_first_order_and_right_censoring(self):
        statuses = ('delivered', 'shipped')
        # 观察终点为最晚下单时间 + 1 秒（4 月 2 日），4 月不完整：最后完整月份为 3 月。
        rows = [('2017-01', 0, 2, 2), ('2017-01', 1, 2, 1), ('2017-01', 2, 2, 1), ('2017-02', 0, 1, 1), ('2017-02', 1, 1, 0)]
        self.check({'kind': 'cohort_retention', 'total': {'customer_count': 3, 'last_complete_month': '2017-03'},
                    'rows': [{'cohort': cohort, 'offset': offset, 'customer_count': size, 'retained_count': kept,
                              'retention': f'{kept / size:.6f}'} for cohort, offset, size, kept in rows]},
                   kind='cohort_retention', included_statuses=statuses)
        # start 只筛 cohort，不改变首单：u1 首单在 1 月，所以 2 月起的 cohort 不含 u1；终点 3 月 1 日时只剩 2 月 offset 0。
        self.check({'kind': 'cohort_retention', 'total': {'customer_count': 1, 'last_complete_month': '2017-02'},
                    'rows': [{'cohort': '2017-02', 'offset': 0, 'customer_count': 1, 'retained_count': 1, 'retention': '1.000000'}]},
                   kind='cohort_retention', included_statuses=statuses, start='2017-02-01T00:00:00', end='2017-03-01T00:00:00')

    def test_delivery_delay_is_date_level(self):
        # o1 同日送达不算延迟；o2、o5 晚一天；o4 没有送达日期、o6 已取消，都不计入。
        self.check({'kind': 'delivery_delay', 'total': {'delivered_count': 4, 'late_count': 2, 'late_rate': '0.500000'},
                    'rows': [{'group': 'MG', 'delivered_count': 1, 'late_count': 0, 'late_rate': '0.000000'},
                             {'group': 'RJ', 'delivered_count': 1, 'late_count': 1, 'late_rate': '1.000000'},
                             {'group': 'SP', 'delivered_count': 2, 'late_count': 1, 'late_rate': '0.500000'}]},
                   kind='delivery_delay', group_by='customer_state', included_statuses=('delivered', 'canceled'))
        self.check({'kind': 'delivery_delay', 'total': {'delivered_count': 1, 'late_count': 1, 'late_rate': '1.000000'},
                    'rows': []}, kind='delivery_delay', included_statuses=('delivered',),
                   time_field='order_delivered_customer_date', start='2017-03-01T00:00:00', end='2017-04-01T00:00:00')

    def test_category_sales_translation_fallback_and_unknown(self):
        self.check({'kind': 'category_sales', 'total': {'order_count': 4, 'item_count': 5, 'price_cents': 4300, 'freight_cents': 450},
                    'rows': [{'group': 'bed_bath_table', 'order_count': 2, 'item_count': 2, 'price_cents': 3000, 'freight_cents': 300},
                             {'group': 'unknown', 'order_count': 1, 'item_count': 1, 'price_cents': 700, 'freight_cents': 0},
                             {'group': 'pc_gamer', 'order_count': 2, 'item_count': 2, 'price_cents': 600, 'freight_cents': 150}]},
                   kind='category_sales', group_by='product_category', included_statuses=('delivered', 'shipped'))


class InvariantTests(Fixture):
    def setUp(self):
        super().setUp()
        self.version, self.folder = self.dataset(bundle(random.Random(31), 120, months=7))

    def result(self, **fields):
        plan = AnalysisPlan(self.version, 'inv-v1', schema='olist', **fields)
        return plan, reference_result(self.folder, plan)

    def issues(self, plan, result):
        return {(item.get('path'), item['issue']) for item in invariants.violations(plan, result)}

    def test_targeted_wrong_results_fail(self):
        def mutate(fields, change, expected):
            plan, result = self.result(**fields)
            self.assertEqual(invariants.violations(plan, result), [])
            result = copy.deepcopy(result)
            change(result)
            with self.subTest(expected=expected):
                self.assertIn(expected, self.issues(plan, result))
        statuses = ('delivered', 'shipped', 'invoiced')
        repeat = {'kind': 'repeat_rate', 'group_by': 'customer_state', 'included_statuses': statuses}
        mutate(repeat, lambda r: r['total'].update(repeat_customer_count=r['total']['customer_count'] + 1),
               ('total.repeat_customer_count', 'bound'))
        mutate(repeat, lambda r: r['total'].update(repeat_rate='0.999999'), ('total.repeat_rate', 'ratio_mismatch'))
        mutate(repeat, lambda r: r['rows'][0].update(customer_count=r['rows'][0]['customer_count'] + 1), ('total', 'sum_mismatch'))
        mutate(repeat, lambda r: r['rows'].reverse(), ('rows[1]', 'unsorted'))
        mutate(repeat, lambda r: r['rows'].append(dict(r['rows'][0])), ('rows', 'duplicate_group'))
        mutate(repeat, lambda r: r['rows'][0].update(group='Sao Paulo'), ('rows[0].group', 'invalid_group'))
        cohort = {'kind': 'cohort_retention', 'included_statuses': statuses, 'end': '2017-06-01T00:00:00'}
        mutate(cohort, lambda r: r['rows'][0].update(retained_count=r['rows'][0]['retained_count'] - 1,
                                                     retention=invariants.ratio_text(r['rows'][0]['retained_count'] - 1,
                                                                                     r['rows'][0]['customer_count'])),
               ('rows[0].retained_count', 'offset_zero'))
        mutate(cohort, lambda r: r['rows'][1].update(retained_count=r['rows'][1]['customer_count'] + 1,
                                                     retention=invariants.ratio_text(r['rows'][1]['customer_count'] + 1,
                                                                                     r['rows'][1]['customer_count'])),
               ('rows[1].retained_count', 'bound'))
        mutate(cohort, lambda r: r['rows'].append({**r['rows'][-1], 'offset': r['rows'][-1]['offset'] + 1}),
               (f'rows[{len(self.result(**cohort)[1]["rows"])}].offset', 'censoring'))
        mutate(cohort, lambda r: r['total'].update(last_complete_month='2017-06'), ('total.last_complete_month', 'censoring'))
        # 1 月 cohort 的 offset 0–4 都是完整月份，去掉 offset 4 即未完整输出。
        mutate(cohort, lambda r: r['rows'].pop(4), ('rows[3].offset', 'censoring'))
        mutate(cohort, lambda r: r['total'].update(customer_count=r['total']['customer_count'] + 1), ('total.customer_count', 'sum_mismatch'))
        delay = {'kind': 'delivery_delay', 'group_by': 'customer_state', 'included_statuses': ('delivered',)}
        mutate(delay, lambda r: r['rows'][0].update(late_count=r['rows'][0]['delivered_count'] + 1), ('rows[0].late_count', 'bound'))
        mutate(delay, lambda r: r['total'].update(late_rate=None), ('total.late_rate', 'type'))
        mutate(delay, lambda r: r['rows'].append({'group': 'ZZ', 'delivered_count': 0, 'late_count': 0, 'late_rate': None}),
               (f'rows[{len(self.result(**delay)[1]["rows"])}].delivered_count', 'empty_group'))
        mutate({**delay, 'group_by': None}, lambda r: r['rows'].append({'group': 'SP'}), ('rows', 'rows_not_empty'))
        sales = {'kind': 'category_sales', 'group_by': 'product_category', 'included_statuses': statuses}
        mutate(sales, lambda r: r['rows'].reverse(), ('rows[1]', 'unsorted'))
        mutate(sales, lambda r: r['total'].update(price_cents=r['total']['price_cents'] + 1), ('total', 'sum_mismatch'))
        mutate(sales, lambda r: r['total'].update(order_count=r['total']['item_count'] + 1), ('total.order_count', 'bound'))
        mutate(sales, lambda r: r['rows'][0].update(item_count=-1), ('rows[0].item_count', 'negative'))
        mutate(sales, lambda r: r.update(kind='summary'), ('kind', 'kind_mismatch'))
        mutate({'kind': 'trend', 'group_by': 'purchase_month', 'included_statuses': statuses, 'amount_source': 'items',
                'start': '2017-02-01T00:00:00', 'end': '2017-04-01T00:00:00'},
               lambda r: r['rows'][0].update(group='2017-01'), ('rows[0].group', 'group_out_of_range'))

    def test_malformed_results_do_not_raise(self):
        for kind, group in (('repeat_rate', 'customer_state'), ('cohort_retention', None), ('delivery_delay', None),
                            ('category_sales', 'product_category')):
            plan = AnalysisPlan(self.version, 'inv-v1', schema='olist', kind=kind, group_by=group, included_statuses=('delivered',))
            for value in ([], {'kind': kind, 'total': [], 'rows': {}}, {'kind': kind, 'total': {'customer_count': 'x'},
                                                                         'rows': [1, {'group': 5}, {'cohort': 'x', 'offset': 0}]}):
                with self.subTest(kind=kind, value=value):
                    self.assertFalse(invariants.check(plan, value)['passed'])


class SuiteTests(Fixture):
    def test_run_checks_passes_fixed_code_and_catches_tampered_table(self):
        version, folder = self.dataset(bundle(random.Random(41), 80, months=5))
        for fields in ({'kind': 'repeat_rate', 'group_by': 'customer_state'}, {'kind': 'cohort_retention'},
                       {'kind': 'delivery_delay', 'group_by': 'customer_state'},
                       {'kind': 'category_sales', 'group_by': 'product_category'},
                       {'kind': 'share', 'group_by': 'customer_state', 'amount_source': 'payments'}):
            plan = AnalysisPlan(version, 'suite-v1', schema='olist', included_statuses=('delivered', 'shipped'), **fields)
            output = self.root / ('out-' + plan.kind)
            inputs = self.root / ('in-' + plan.kind)
            inputs.mkdir()
            for name, data in olist.dataset_inputs(folder, plan).items():
                (inputs / name).write_bytes(data)
            fixed_olist.export_result(fixed_olist.analyze(str(inputs), plan.payload()), output)
            actual = json.loads((output / 'result.json').read_text(encoding='utf-8'))
            with self.subTest(kind=plan.kind):
                report = run_checks(folder / 'orders.csv', plan, actual, output / 'table.csv')
                self.assertTrue(report.passed, report.checks)
                self.assertEqual([item['name'] for item in report.checks][-3:], ['download_table', 'invariants', 'reference_agreement'])
                self.assertEqual('group_total' in [item['name'] for item in report.checks], plan.group_by is not None)
                text = (output / 'table.csv').read_text(encoding='utf-8-sig').splitlines()
                header = text[0].split(',')
                cells = text[1].split(',')
                column = next(index for index, name in enumerate(header) if name.endswith('_count'))
                cells[column] = str(int(cells[column]) + 1)
                (output / 'table.csv').write_text('\n'.join([text[0], ','.join(cells), *text[2:]]) + '\n', encoding='utf-8')
                report = run_checks(folder / 'orders.csv', plan, actual, output / 'table.csv')
                table = next(item for item in report.checks if item['name'] == 'download_table')
                self.assertEqual((table['passed'], table['issues'][0]['issue']), (False, 'row_content'))
        self.assertIn('/input', fixed_code(plan))


class WiringTests(Fixture):
    def test_memory_statuses_and_amount_source_follow_schema(self):
        service = MemoryService(FakeMemoryRepository())
        olist_scope, orders_scope = Scope('p', 'olist', olist.SIGNATURE), Scope('p', 'orders', 'orders-sig')
        entry = service.propose(olist_scope, 'metric', '成交', {'definition': '已送达', 'included_statuses': ['delivered'],
                                                               'amount_source': 'items'}, 'run')
        entry = service.confirm(olist_scope, entry.memory_id, actor='user', expected_current_id=None)
        snapshot = MemoryService.metric_snapshot(entry)
        self.assertEqual((snapshot.amount_source, snapshot.included_statuses), ('items', ('delivered',)))
        self.assertEqual(model_metric(snapshot)['amount_source'], 'items')
        for scope, content in ((orders_scope, {'definition': 'x', 'included_statuses': ['delivered']}),
                               (olist_scope, {'definition': 'x', 'included_statuses': ['paid']}),
                               (orders_scope, {'definition': 'x', 'included_statuses': ['paid'], 'amount_source': 'items'}),
                               (olist_scope, {'definition': 'x', 'included_statuses': ['delivered'], 'amount_source': 'both'})):
            with self.subTest(content=content), self.assertRaises(ValueError):
                service.propose(scope, 'metric', '成交', content, 'run')
        self.assertNotIn('amount_source', model_metric(MetricSnapshot('v1', ('paid',), 's', 'p', 'f', 'sig')))

    def test_guided_workflow_takes_schema_and_amount_source_from_server(self):
        version, folder = self.dataset(bundle(random.Random(9), 30))
        store = self.store

        class Services:
            def __init__(self):
                self.plans = []

            def profile(self, task):
                item = store.get(task.dataset_version, task.project_id)
                return {'dataset_family': item.dataset_family, 'schema_signature': item.schema_signature, 'quality': item.profile}

            def execute(self, task, code, timeout, cancelled):
                self.plans.append(task.plan)
                return ExecutionManifest(str(uuid4()), task.dataset_version, task.plan.metric_version, 'x', 'a', [], 0, 'COMPLETED')

            def verify(self, task, manifest):
                from datalab.contracts import VerificationReport
                return VerificationReport(True, [{'name': 'independent_reference', 'passed': True}])

            def code(self, manifest):
                return ''
        metric = MetricSnapshot('m:v1', ('delivered',), 'trusted', 'demo', 'olist', olist.SIGNATURE, 'payments')
        services = Services()
        task = Workflow(RoleRuntime(GuidedBackend({'kind': 'ranking', 'group_by': 'customer_state'})), services).run(
            Task('demo', version, '各州订单金额', metric=metric), cancelled=threading.Event())
        self.assertEqual(task.state, State.SUCCEEDED, task.timeline)
        self.assertEqual((services.plans[0].schema, services.plans[0].amount_source), ('olist', 'payments'))
        # Planner 不能改金额来源。
        proposal = {'plan': {**services.plans[0].payload(), 'amount_source': 'items'}}
        with self.assertRaises(PermissionError):
            Workflow._accept_plan(task, proposal, metric)
        self.assertEqual(Workflow._accept_plan(task, {'plan': {k: v for k, v in services.plans[0].payload().items()
                                                              if k not in ('schema', 'amount_source')}}, metric), services.plans[0])


if __name__ == '__main__':
    unittest.main()
