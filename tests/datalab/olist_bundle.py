"""测试用合成 Olist 多表数据：按原始文件名与原始表头写出（空格分隔的时间、两位小数金额），不含任何真实数据。

固定种子生成，覆盖重复购买客户、跨州客户、空送达日期、同日送达、未翻译与空品类、无明细或无支付订单、
同一 review_id 对应多笔订单等边界。
"""
import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

from datalab.datasets import olist

STATES = ('SP', 'RJ', 'MG')
CATEGORIES = ('cama_mesa_banho', 'esporte_lazer', 'pc_gamer', '')
TRANSLATED = {'cama_mesa_banho': 'bed_bath_table', 'esporte_lazer': 'sports_leisure', 'unused_cat': 'unused'}
TYPES = ('credit_card', 'boleto', 'voucher')
WEIGHTED = ('delivered',) * 6 + olist.STATUSES[1:]
# 2017-01 起若干月；时间点特意包含月初零点与月末最后一秒。
EPOCH = datetime(2017, 1, 1)


def stamp(value: datetime, separator=' ') -> str:
    return value.isoformat(sep=separator, timespec='seconds')


def money(cents: int) -> str:
    return f'{cents // 100}.{cents % 100:02d}'


def random_time(rng: random.Random, months: int) -> datetime:
    month = rng.randrange(months)
    start = datetime(2017 + month // 12, month % 12 + 1, 1)
    edge = rng.random()
    if edge < 0.1:
        return start
    if edge < 0.2:
        following = datetime(2017 + (month + 1) // 12, (month + 1) % 12 + 1, 1)
        return following - timedelta(seconds=1)
    return start + timedelta(seconds=rng.randrange(27 * 86400))


def bundle(rng: random.Random, orders: int = 40, people: int | None = None, months: int = 6) -> dict[str, list[list[str]]]:
    """返回 {表名: 原始行（不含表头）}；表名与 datasets/olist.TABLES 一致。"""
    people = people or max(1, orders * 2 // 3)
    persons = [f'u{index:05d}' for index in range(people)]
    home = {person: rng.choice(STATES) for person in persons}
    products = [[f'p{index:04d}', CATEGORIES[index % len(CATEGORIES)], '10', '100', '1', '500', '10', '10', '10']
                for index in range(8)]
    tables = {name: [] for name in olist.TABLES}
    tables['products'] = products
    tables['category_translation'] = [[name, english] for name, english in TRANSLATED.items()]
    review_ids = []
    for index in range(orders):
        order_id, customer_id = f'o{index:05d}', f'c{index:05d}'
        person = rng.choice(persons)
        # 少数客户换州下单，按州分组时需要明确的归属规则。
        state = home[person] if rng.random() < 0.85 else rng.choice(STATES)
        tables['customers'].append([customer_id, person, f'{rng.randrange(10000, 99999)}', 'cidade', state])
        status = rng.choice(WEIGHTED)
        bought = random_time(rng, months)
        estimated = datetime.combine((bought + timedelta(days=rng.randrange(3, 20))).date(), datetime.min.time())
        delivered = ''
        if status == 'delivered' and rng.random() < 0.95 or status == 'canceled' and rng.random() < 0.1:
            choice = rng.random()
            moment = estimated + timedelta(hours=rng.randrange(1, 23)) if choice < 0.3 else \
                estimated + timedelta(days=rng.randrange(-5, 6), seconds=rng.randrange(86400))
            delivered = stamp(max(moment, bought + timedelta(hours=1)))
        approved = '' if rng.random() < 0.05 else stamp(bought + timedelta(minutes=rng.randrange(1, 600)))
        tables['orders'].append([order_id, customer_id, status, stamp(bought), approved, '', delivered, stamp(estimated)])
        price_total = 0
        if rng.random() > 0.1:
            for item in range(1, rng.randrange(1, 4) + 1):
                price, freight = rng.choice([0, 1, 990, rng.randrange(100, 90000)]), rng.randrange(0, 3000)
                price_total += price + freight
                tables['order_items'].append([order_id, str(item), rng.choice(products)[0], 's0001',
                                              stamp(bought + timedelta(days=2)), money(price), money(freight)])
        if rng.random() > 0.05:
            parts = 1 if rng.random() < 0.8 else 2
            value = price_total if rng.random() < 0.8 else price_total + rng.randrange(-500, 2000)
            value = max(value, 0)
            shares = [value] if parts == 1 else [value // 2, value - value // 2]
            for sequence, share in enumerate(shares, 1):
                tables['order_payments'].append([order_id, str(sequence), rng.choice(TYPES), str(rng.randrange(0, 6)),
                                                 money(share)])
        review = rng.choice(review_ids) if review_ids and rng.random() < 0.05 else f'r{index:05d}'
        review_ids.append(review)
        tables['order_reviews'].append([review, order_id, str(rng.randrange(1, 6)), '', '',
                                        stamp(bought + timedelta(days=5)), stamp(bought + timedelta(days=6))])
    return tables


def write(folder: Path, tables: dict[str, list[list[str]]]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        spec = olist.TABLES[name]
        with (folder / spec['file']).open('w', newline='', encoding='utf-8') as handle:
            writer = csv.writer(handle, lineterminator='\n')
            writer.writerow(spec['header'])
            writer.writerows(rows)
    return folder
