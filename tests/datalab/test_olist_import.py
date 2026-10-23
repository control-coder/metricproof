"""Olist 多表导入：服务端固定的表与类型、连接键完整性、整批拒绝、每表上限、概况与结构签名。"""
import copy
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from olist_bundle import bundle, write
from datalab.datasets import olist
from datalab.datasets.service import ORDERS_SIGNATURE, DatasetError, DatasetStore, schema_of

ROOT = Path(__file__).resolve().parents[2]


class OlistImportTests(unittest.TestCase):
    def setUp(self):
        scratch = (ROOT / '.tmp').resolve()
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch, prefix='olist-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = DatasetStore(self.root / 'datasets')
        self.tables = bundle(random.Random(7), 60)

    def load(self, tables, name='raw'):
        raw = write(self.root / name, tables)
        return self.store.import_olist(raw, project_id='demo', dataset_family='olist')

    def rejected(self, tables, name):
        with self.assertRaises(DatasetError) as caught:
            self.load(tables, name)
        return caught.exception

    def test_good_bundle_is_normalized_and_profiled(self):
        version = self.load(self.tables)
        self.assertEqual((version.schema, version.schema_signature, version.currency), ('olist', olist.SIGNATURE, 'BRL'))
        self.assertNotEqual(olist.SIGNATURE, ORDERS_SIGNATURE)
        self.assertEqual((schema_of(olist.SIGNATURE), schema_of(ORDERS_SIGNATURE), schema_of('s')), ('olist', 'orders', 'orders'))
        folder = self.store.directory(version.dataset_version)
        self.assertEqual(sorted(path.name for path in folder.iterdir()),
                         sorted([f'{name}.csv' for name in olist.TABLES] + ['metadata.json']))
        orders = (folder / 'orders.csv').read_text(encoding='utf-8').splitlines()
        self.assertEqual(orders[0], ','.join(olist.columns('orders')))
        self.assertRegex(orders[1].split(',')[3], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$')
        items = (folder / 'order_items.csv').read_text(encoding='utf-8').splitlines()
        self.assertEqual(items[0], 'order_id,order_item_id,product_id,seller_id,shipping_limit_date,price_cents,freight_cents')
        # 评论正文、城市与邮编不进入规范化表。
        self.assertNotIn('review_comment_message', (folder / 'order_reviews.csv').read_text(encoding='utf-8'))
        profile = version.profile
        self.assertEqual(profile['tables']['orders']['row_count'], 60)
        self.assertEqual(sum(profile['status_counts'].values()), 60)
        self.assertEqual(profile['orphans']['products.product_category_name'], 2)
        self.assertEqual({key: value for key, value in profile['orphans'].items() if key != 'products.product_category_name'},
                         dict.fromkeys(['orders.customer_id', 'order_items.order_id', 'order_items.product_id',
                                        'order_payments.order_id', 'order_reviews.order_id'], 0))
        reviews = self.tables['order_reviews']
        self.assertEqual(profile['duplicate_review_ids'], len(reviews) - len({row[0] for row in reviews}))
        self.assertEqual(profile['childless']['orders_without_items'],
                         60 - len({row[0] for row in self.tables['order_items']}))
        self.assertLessEqual(profile['time_min'], profile['time_max'])
        self.assertEqual(self.store.get(version.dataset_version, 'demo'), version)
        with self.assertRaises(PermissionError):
            self.store.get(version.dataset_version, 'other')
        json.dumps(profile)

    def test_broken_keys_reject_whole_bundle_with_counts_only(self):
        for table, index, column, value in (('order_items', 0, 0, 'o-missing'), ('orders', 3, 1, 'c-missing'),
                                            ('order_payments', 0, 0, 'o-missing'), ('order_reviews', 1, 1, 'o-missing'),
                                            ('order_items', 2, 2, 'p-missing')):
            tables = copy.deepcopy(self.tables)
            tables[table][index][column] = value
            with self.subTest(table=table, column=column):
                error = self.rejected(tables, f'keys-{table}-{column}')
                self.assertEqual(len(error.issues), 1)
                self.assertEqual(error.issues[0]['table'], table)
                self.assertEqual(error.issues[0]['count'], 1)
                self.assertNotIn('missing', json.dumps(error.issues))
        self.assertEqual(list((self.root / 'datasets').iterdir()), [])

    def test_bad_values_report_table_row_and_reason(self):
        cases = [('orders', 0, 3, '2017-01-01T10:00:00+03:00'), ('orders', 1, 3, '2017-13-01 00:00:00'),
                 ('orders', 2, 3, '2017-01-01'), ('orders', 3, 3, ''), ('orders', 4, 2, 'paid'),
                 ('order_items', 0, 5, '1.234'), ('order_items', 1, 6, '-1.00'), ('order_items', 2, 5, 'NaN'),
                 ('order_payments', 0, 1, 'x'), ('order_reviews', 0, 2, '6'), ('customers', 0, 4, 'sp'),
                 ('products', 0, 1, 'Cama Mesa')]
        for table, index, column, value in cases:
            tables = copy.deepcopy(self.tables)
            tables[table][index][column] = value
            with self.subTest(table=table, value=value):
                error = self.rejected(tables, f'bad-{table}-{index}-{column}')
                self.assertEqual(error.issues[0]['table'], table)
                self.assertEqual(error.issues[0]['row'], index + 2)
                if value:
                    self.assertNotIn(value, json.dumps(error.issues, ensure_ascii=False))
        # 送达等可空日期允许为空。
        tables = copy.deepcopy(self.tables)
        tables['orders'][0][6] = ''
        self.load(tables, 'empty-delivered')

    def test_duplicates_header_missing_files_and_limits(self):
        tables = copy.deepcopy(self.tables)
        tables['orders'].append(list(tables['orders'][0]))
        self.assertIn('主键重复', self.rejected(tables, 'dup').issues[0]['reason'])
        raw = write(self.root / 'header', self.tables)
        path = raw / olist.TABLES['customers']['file']
        path.write_text(path.read_text(encoding='utf-8').replace('customer_city', 'city'), encoding='utf-8')
        with self.assertRaises(DatasetError):
            self.store.import_olist(raw, project_id='demo', dataset_family='olist')
        (raw / olist.TABLES['products']['file']).unlink()
        with self.assertRaises(DatasetError) as caught:
            self.store.import_olist(raw, project_id='demo', dataset_family='olist')
        self.assertIn({'table': 'products', 'reason': '文件缺失'}, caught.exception.issues)
        limited = copy.deepcopy(olist.TABLES)
        limited['order_items']['limits'] = (5, 32 * 1024 * 1024)
        with patch.dict(olist.TABLES, limited):
            self.assertEqual(self.rejected(self.tables, 'rows').issues, [{'table': 'order_items', 'reason': '行数超限'}])
        limited['order_items']['limits'] = (200_000, 64)
        with patch.dict(olist.TABLES, limited):
            self.assertEqual(self.rejected(self.tables, 'bytes').issues, [{'table': 'order_items', 'reason': '文件过大'}])
        with self.assertRaises(DatasetError):
            self.store.import_olist(write(self.root / 'family', self.tables), project_id='demo', dataset_family=' ')

    def test_issue_list_is_bounded(self):
        tables = copy.deepcopy(self.tables)
        for row in tables['orders']:
            row[2] = 'paid'
        self.assertEqual(len(self.rejected(tables, 'many').issues), olist.MAX_ISSUES)


if __name__ == '__main__':
    unittest.main()
