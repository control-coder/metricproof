"""Olist 计划契约与沙箱输入：按数据结构校验计划、服务端只投影本类分析需要的表与列、单表路径不变。

真实容器用例（DATALAB_DOCKER_TESTS=1）检查多表输入走同一只读挂载与同一组隔离参数。
"""
import base64
import json
import os
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import Mock, patch
import io
from uuid import uuid4

from olist_bundle import bundle, write
from datalab.contracts import AnalysisPlan, OLIST_GROUPS, OLIST_KINDS
from datalab.datasets import olist
from datalab.datasets.service import DatasetStore
from datalab.execution.analysis_frame import assemble
from datalab.execution.pipeline import run_fixed
from datalab.execution.runner import DockerRunner, OutputSpec, sandbox_inputs
from datalab.orchestration.workflow import role_plan

ROOT = Path(__file__).resolve().parents[2]
VERSION = str(uuid4())


def plan(kind='summary', **fields):
    defaults = {'included_statuses': ('delivered',), 'schema': 'olist'}
    if kind in ('summary', 'trend', 'ranking', 'share', 'comparison'):
        defaults['amount_source'] = 'payments'
    if kind == 'category_sales':
        defaults['group_by'] = 'product_category'
    if kind in ('trend',):
        defaults['group_by'] = 'purchase_month'
    if kind in ('ranking', 'share'):
        defaults['group_by'] = 'customer_state'
    if kind == 'comparison':
        defaults.update(start='2017-03-01T00:00:00', end='2017-04-01T00:00:00',
                        previous_start='2017-02-01T00:00:00', previous_end='2017-03-01T00:00:00')
    return AnalysisPlan(VERSION, 'olist-v1', kind=kind, **{**defaults, **fields})


class OlistPlanTests(unittest.TestCase):
    def test_orders_plan_payload_is_unchanged(self):
        value = AnalysisPlan(VERSION, 'v1', kind='ranking', group_by='channel').payload()
        self.assertEqual(list(value), ['dataset_version', 'metric_version', 'kind', 'group_by', 'included_statuses', 'start',
                                       'end', 'previous_start', 'previous_end', 'checks', 'time_field'])
        self.assertEqual(AnalysisPlan(**value), AnalysisPlan(VERSION, 'v1', kind='ranking', group_by='channel'))
        for fields in ({'kind': 'repeat_rate'}, {'amount_source': 'payments'}, {'group_by': 'customer_state'},
                       {'included_statuses': ('delivered',)}, {'schema': 'other'}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                AnalysisPlan(VERSION, 'v1', **fields)

    def test_every_allowed_olist_combination_is_accepted(self):
        for kind in OLIST_KINDS:
            for group in OLIST_GROUPS[kind]:
                with self.subTest(kind=kind, group=group):
                    value = plan(kind, group_by=group)
                    self.assertEqual(value.time_field, 'order_purchase_timestamp')
                    self.assertEqual(AnalysisPlan(**value.payload()), value)
                    self.assertEqual(role_plan(value)['schema'], 'olist')
        self.assertEqual(plan('delivery_delay', time_field='order_delivered_customer_date').time_field,
                         'order_delivered_customer_date')

    def test_nonsense_olist_combinations_are_rejected(self):
        cases = [('quality', {}), ('trend', {'group_by': 'customer_state'}), ('summary', {'group_by': 'channel'}),
                 ('summary', {'group_by': 'product_category'}), ('ranking', {'group_by': None}),
                 ('category_sales', {'group_by': None}), ('cohort_retention', {'group_by': 'customer_state'}),
                 ('summary', {'amount_source': None}), ('summary', {'amount_source': 'invoice'}),
                 ('summary', {'included_statuses': ('paid',)}), ('summary', {'included_statuses': ()}),
                 ('summary', {'included_statuses': ('delivered', 'delivered')}),
                 ('summary', {'time_field': 'order_delivered_customer_date'}),
                 ('delivery_delay', {'included_statuses': ('shipped',)}),
                 ('repeat_rate', {'time_field': 'order_approved_at'}), ('repeat_rate', {'group_by': 'payment_type'}),
                 ('comparison', {'previous_start': None}), ('summary', {'start': '2017-01-01'})]
        for kind, fields in cases:
            with self.subTest(kind=kind, fields=fields), self.assertRaises(ValueError):
                plan(kind, **fields)

    def test_needed_tables_and_columns_per_kind(self):
        self.assertEqual(olist.needed(plan('summary')), {
            'orders': ('order_id', 'order_status', 'order_purchase_timestamp'),
            'order_payments': ('order_id', 'payment_value_cents')})
        self.assertEqual(olist.needed(plan('share', amount_source='items')), {
            'orders': ('order_id', 'order_status', 'order_purchase_timestamp', 'customer_id'),
            'customers': ('customer_id', 'customer_state'),
            'order_items': ('order_id', 'price_cents', 'freight_cents')})
        self.assertEqual(olist.needed(plan('repeat_rate', group_by='customer_state')), {
            'orders': ('order_id', 'order_status', 'order_purchase_timestamp', 'customer_id'),
            'customers': ('customer_id', 'customer_unique_id', 'customer_state')})
        self.assertEqual(olist.needed(plan('cohort_retention')), {
            'orders': ('order_id', 'order_status', 'order_purchase_timestamp', 'customer_id'),
            'customers': ('customer_id', 'customer_unique_id')})
        self.assertEqual(olist.needed(plan('delivery_delay', time_field='order_delivered_customer_date')), {
            'orders': ('order_id', 'order_status', 'order_delivered_customer_date', 'order_estimated_delivery_date')})
        self.assertEqual(set(olist.needed(plan('category_sales'))), {'orders', 'order_items', 'products', 'category_translation'})
        # 金额来源只影响订单级金额分析。
        self.assertEqual(olist.needed(plan('repeat_rate', amount_source='items')), olist.needed(plan('repeat_rate')))
        for kind in OLIST_KINDS:
            for table, fields in olist.needed(plan(kind)).items():
                self.assertLessEqual(set(fields), set(olist.columns(table)), (kind, table))
            self.assertNotIn('order_reviews', olist.needed(plan(kind)))


class OlistSandboxFixture(unittest.TestCase):
    def setUp(self):
        scratch = (ROOT / '.tmp').resolve()
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch, prefix='olist-run-')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = DatasetStore(root / 'datasets')
        self.version = self.store.import_olist(write(root / 'raw', bundle(random.Random(5), 50)),
                                               project_id='demo', dataset_family='olist')
        self.orders = self.store.import_csv((ROOT / 'examples/orders.csv').read_bytes(), project_id='demo',
                                            dataset_family='orders', currency='CNY', timezone='Asia/Shanghai')
        with patch('datalab.execution.runner.shutil.which', return_value='docker'):
            self.runner = DockerRunner(self.store, root / 'runs')

    def plan(self, kind='summary', **fields):
        return AnalysisPlan(**{**plan(kind, **fields).payload(), 'dataset_version': self.version.dataset_version})


class OlistSandboxTests(OlistSandboxFixture):
    def test_projection_contains_only_needed_columns_and_all_rows(self):
        value = self.plan('repeat_rate', group_by='customer_state')
        files = sandbox_inputs(self.store, value)['input_files']
        self.assertEqual(set(files), {'orders.csv', 'customers.csv'})
        lines = files['orders.csv'].decode('utf-8').splitlines()
        self.assertEqual(lines[0], 'order_id,order_status,order_purchase_timestamp,customer_id')
        self.assertEqual(len(lines) - 1, self.version.row_count)
        self.assertEqual(sandbox_inputs(self.store, AnalysisPlan(self.orders.dataset_version, 'v1')), {})

    def test_runner_rejects_wrong_input_shape_before_docker(self):
        value = self.plan()
        files = sandbox_inputs(self.store, value)['input_files']
        cases = [(self.version.dataset_version, {}), (self.orders.dataset_version, {'input_files': files}),
                 (self.version.dataset_version, {'input_files': {'../x.csv': b'a'}}),
                 (self.version.dataset_version, {'input_files': {'analysis.py': b'a'}}),
                 (self.version.dataset_version, {'input_files': {'orders.csv': 'text'}}),
                 (self.version.dataset_version, {'input_files': {}}),
                 (self.version.dataset_version, {'input_files': files, 'orders_csv': b'x'}),
                 (self.version.dataset_version, {'input_files': {'orders.csv': b'x' * (32 * 1024 * 1024 + 1)}})]
        with patch.object(self.runner, '_control') as control:
            for version, extra in cases:
                with self.subTest(extra=list(extra)), self.assertRaises(ValueError):
                    self.runner.execute_python(str(uuid4()), version, '', 1, OutputSpec(), project_id='demo',
                                               metric_version='v1', **extra)
            with self.assertRaises(PermissionError):
                self.runner.execute_python(str(uuid4()), self.version.dataset_version, '', 1, OutputSpec(),
                                           project_id='other', metric_version='v1', input_files=files)
            control.assert_not_called()

    def test_input_files_use_same_mount_and_flags(self):
        wire = json.dumps({'outputs': {'result.json': base64.b64encode(b'{}').decode()}}).encode()
        commands = []

        def control(*args):
            if args[0] == 'create':
                commands.append(args)
            if args[0] == 'image':
                return 'sha256:' + 'a' * 64
            if args[0] == 'inspect':
                return json.dumps({'ExitCode': 0, 'OOMKilled': False, 'Running': False})
            return ''

        def process(*args, **kwargs):
            item = Mock()
            item.stdout = io.BytesIO(wire)
            item.poll.return_value = 0
            item.returncode = 0
            return item
        files = sandbox_inputs(self.store, self.plan('category_sales'))['input_files']
        with patch.object(self.runner, '_control', side_effect=control), \
             patch('datalab.execution.runner.subprocess.Popen', side_effect=process):
            default = self.runner.execute_python(str(uuid4()), self.orders.dataset_version, '', 1,
                                                 OutputSpec(files=('result.json',)), project_id='demo', metric_version='v1')
            multi = self.runner.execute_python(str(uuid4()), self.version.dataset_version, '', 1,
                                               OutputSpec(files=('result.json',)), project_id='demo', metric_version='v1',
                                               input_files=files)
        self.assertEqual((default.status, multi.status), ('COMPLETED', 'COMPLETED'))
        folder = self.runner.root / multi.run_id / 'input'
        self.assertEqual(sorted(path.name for path in folder.iterdir()), sorted([*files, 'analysis.py', 'runner.py']))
        normalize = lambda args: [item for item in args if default.run_id not in item and multi.run_id not in item  # noqa: E731
                                  and not item.startswith('datalab-exec-')]
        self.assertEqual(normalize(commands[0]), normalize(commands[1]))


# 只读取并回报输入形状，不做分析；用来确认框架给模型的 data 结构。
PROBE = '''
def analyze(data, plan):
    import os
    from pathlib import Path
    assert os.getuid() == 65532
    assert 'CapEff:\\t0000000000000000' in Path('/proc/self/status').read_text()
    try:
        open('/input/orders.csv', 'w')
    except OSError:
        pass
    else:
        raise AssertionError('输入可写')
    return {"kind": plan["kind"], "total": {name: len(rows) for name, rows in data.items()},
            "rows": [{"group": name, "columns": sorted(rows[0])} for name, rows in sorted(data.items())],
            "files": sorted(os.listdir('/input'))}
'''


@unittest.skipUnless(os.environ.get('DATALAB_DOCKER_TESTS') == '1', '需显式启用真实 Docker 集成')
class OlistDockerTests(OlistSandboxFixture):
    def setUp(self):
        super().setUp()
        self.runner = DockerRunner(self.store, Path(self.temp.name) / 'runs')

    def test_fixed_code_for_every_new_kind_passes_in_real_container(self):
        for fields in ({'kind': 'repeat_rate', 'group_by': 'customer_state'}, {'kind': 'cohort_retention'},
                       {'kind': 'delivery_delay', 'group_by': 'customer_state'}, {'kind': 'category_sales'},
                       {'kind': 'trend', 'group_by': 'purchase_date', 'amount_source': 'items'}):
            value = self.plan(**fields, included_statuses=('delivered', 'shipped'))
            with self.subTest(kind=value.kind):
                report = run_fixed(self.runner, value, 'demo')
                self.assertEqual(report['status'], 'SUCCEEDED', report)
                self.assertNotIn('narrative', report)

    def test_framework_receives_only_projected_tables(self):
        value = self.plan('delivery_delay', group_by='customer_state')
        files = sandbox_inputs(self.store, value)['input_files']
        manifest = self.runner.execute_python(str(uuid4()), value.dataset_version, assemble(role_plan(value), PROBE), 30,
                                              OutputSpec(), project_id='demo', metric_version='v1', input_files=files)
        self.assertEqual(manifest.status, 'COMPLETED', manifest)
        result = json.loads((self.runner.root / manifest.run_id / 'output/result.json').read_text(encoding='utf-8'))
        self.assertEqual(result['files'], sorted([*files, 'analysis.py', 'runner.py']))
        self.assertEqual(result['total'], {'orders': self.version.row_count, 'customers': self.version.row_count})
        self.assertEqual({row['group']: row['columns'] for row in result['rows']},
                         {name[:-4]: sorted(fields) for name, fields in
                          ((f'{table}.csv', columns) for table, columns in olist.needed(value).items())})
