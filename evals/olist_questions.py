"""Olist 真实数据评测题集：开发集与冻结的留出集，都是自然语言中文问题，真值计划由人工按题意写定。

- 数据：scripts/fetch_olist.py 固定版本的 Olist 公开数据，下单时间 2016-09-04 至 2018-10-17。
- 口径：每题指定使用哪一条可信预配置口径（metric）。两条口径的纳入状态相同（不含 canceled、unavailable），
  订单金额来源不同：payments 为该单全部支付之和，items 为全部明细价格加运费之和。题面不提金额来源，
  同类问题在两种口径下真值不同，用来检查金额来源来自已确认口径而不是模型选择。非金额类型只用 payments 口径。
- 澄清题：真值为 {'clarify': True}，正确行为是 Planner 请求澄清（任务停在 WAITING_CONFIRMATION），
  例如数据跨两年却只说“3月”、相对时间“上个月”、预测或评价分析等不支持的问题。
- 期间左闭右开（结束日期取次日零点）；时间字段不写即下单时间。

冻结规则（与 evals/heldout.py 相同）：开发集用于调提示；留出集在第一次留出集运行之前写定并冻结指纹，
之后不得按结果改题、改真值或改提示。修改留出集任何一题都会改变 OLIST_HELDOUT_SHA256，测试与运行脚本会拒绝。
"""
import hashlib
import json

STATUSES = ('delivered', 'shipped', 'invoiced', 'processing', 'approved', 'created')
METRICS = {'payments': '有效订单金额（支付合计）', 'items': '有效订单金额（明细价格加运费）'}


def D(text: str) -> str:
    """YYYY-MM-DD → 当日零点。"""
    return f'{text}T00:00:00'


def M(year: int, month: int) -> str:
    year, month = year + (month - 1) // 12, (month - 1) % 12 + 1
    return f'{year}-{month:02d}-01T00:00:00'


CLARIFY = {'clarify': True}

# (题面, 真值计划, 使用的口径)
DEV = [
    ('2017年全年的订单数和订单金额是多少？', {'kind': 'summary', 'start': M(2017, 1), 'end': M(2018, 1)}, 'payments'),
    ('2018年第一季度的销售额是多少？', {'kind': 'summary', 'start': M(2018, 1), 'end': M(2018, 4)}, 'items'),
    ('2017年每个月的销售额走势。', {'kind': 'trend', 'group_by': 'purchase_month', 'start': M(2017, 1), 'end': M(2018, 1)}, 'payments'),
    ('2018年8月每天的销售额是多少？', {'kind': 'trend', 'group_by': 'purchase_date', 'start': M(2018, 8), 'end': M(2018, 9)}, 'items'),
    ('2017年各州销售额排行。', {'kind': 'ranking', 'group_by': 'customer_state', 'start': M(2017, 1), 'end': M(2018, 1)}, 'payments'),
    ('2018年上半年各州订单金额占比。', {'kind': 'share', 'group_by': 'customer_state', 'start': M(2018, 1), 'end': M(2018, 7)}, 'items'),
    ('2018年5月销售额相比4月变化多少？', {'kind': 'comparison', 'start': M(2018, 5), 'end': M(2018, 6),
                                     'previous_start': M(2018, 4), 'previous_end': M(2018, 5)}, 'payments'),
    ('整个数据期间的复购率是多少？', {'kind': 'repeat_rate'}, 'payments'),
    ('2017年各州客户的复购率。', {'kind': 'repeat_rate', 'group_by': 'customer_state', 'start': M(2017, 1), 'end': M(2018, 1)}, 'payments'),
    ('2018年起首次下单的客户，之后每个月的留存率如何？', {'kind': 'cohort_retention', 'start': M(2018, 1)}, 'payments'),
    ('观察到2017年底为止，各月新客的留存。', {'kind': 'cohort_retention', 'end': M(2018, 1)}, 'payments'),
    ('整体的送达延迟率是多少？', {'kind': 'delivery_delay'}, 'payments'),
    ('2018年各州的送达延迟率，按下单时间算。', {'kind': 'delivery_delay', 'group_by': 'customer_state', 'start': M(2018, 1),
                                       'end': M(2019, 1)}, 'payments'),
    ('2017年各品类销售情况。', {'kind': 'category_sales', 'group_by': 'product_category', 'start': M(2017, 1), 'end': M(2018, 1)}, 'payments'),
    ('8月的复购率是多少？', CLARIFY, 'payments'),
    ('预测下个月各州的销售额。', CLARIFY, 'payments'),
]

HELDOUT = [
    # summary
    ('2017年第四季度一共多少订单、多少销售额？', {'kind': 'summary', 'start': M(2017, 10), 'end': M(2018, 1)}, 'payments'),
    ('2018年7月的订单金额合计。', {'kind': 'summary', 'start': M(2018, 7), 'end': M(2018, 8)}, 'items'),
    ('截至2017年6月底（含）的累计销售额。', {'kind': 'summary', 'end': M(2017, 7)}, 'payments'),
    ('2017年11月24日黑五当天的销售额和订单数。', {'kind': 'summary', 'start': D('2017-11-24'), 'end': D('2017-11-25')}, 'items'),
    # trend
    ('2018年每月订单金额的变化趋势。', {'kind': 'trend', 'group_by': 'purchase_month', 'start': M(2018, 1), 'end': M(2019, 1)}, 'payments'),
    ('2017年11月每天的销售额。', {'kind': 'trend', 'group_by': 'purchase_date', 'start': M(2017, 11), 'end': M(2017, 12)}, 'items'),
    ('看看整个数据期间按月的销售额走势。', {'kind': 'trend', 'group_by': 'purchase_month'}, 'payments'),
    ('2018年1月1日到1月15日（含）每天的订单金额。', {'kind': 'trend', 'group_by': 'purchase_date', 'start': M(2018, 1),
                                           'end': D('2018-01-16')}, 'items'),
    # ranking
    ('整个期间哪个州的销售额最高？按州排序。', {'kind': 'ranking', 'group_by': 'customer_state'}, 'payments'),
    ('2018年第二季度各州订单金额排名。', {'kind': 'ranking', 'group_by': 'customer_state', 'start': M(2018, 4), 'end': M(2018, 7)}, 'items'),
    ('2017年哪个月销售额最高？按月份从高到低排。', {'kind': 'ranking', 'group_by': 'purchase_month', 'start': M(2017, 1),
                                       'end': M(2018, 1)}, 'payments'),
    ('2018年3月各州销售额从高到低。', {'kind': 'ranking', 'group_by': 'customer_state', 'start': M(2018, 3), 'end': M(2018, 4)}, 'payments'),
    # share
    ('各州订单金额分别占总额的比例。', {'kind': 'share', 'group_by': 'customer_state'}, 'items'),
    ('2017年各月销售额占全年的比例。', {'kind': 'share', 'group_by': 'purchase_month', 'start': M(2017, 1), 'end': M(2018, 1)}, 'payments'),
    ('2018年8月各州的销售额份额。', {'kind': 'share', 'group_by': 'customer_state', 'start': M(2018, 8), 'end': M(2018, 9)}, 'payments'),
    ('2017年下半年各州订单金额构成。', {'kind': 'share', 'group_by': 'customer_state', 'start': M(2017, 7), 'end': M(2018, 1)}, 'items'),
    # comparison
    ('2018年第一季度销售额与2017年第一季度相比增长了多少？', {'kind': 'comparison', 'start': M(2018, 1), 'end': M(2018, 4),
                                             'previous_start': M(2017, 1), 'previous_end': M(2017, 4)}, 'payments'),
    ('2017年12月比11月的订单金额变化。', {'kind': 'comparison', 'start': M(2017, 12), 'end': M(2018, 1),
                                   'previous_start': M(2017, 11), 'previous_end': M(2017, 12)}, 'items'),
    ('2018年上半年相比2017年下半年，销售额涨跌幅度是多少？', {'kind': 'comparison', 'start': M(2018, 1), 'end': M(2018, 7),
                                             'previous_start': M(2017, 7), 'previous_end': M(2018, 1)}, 'payments'),
    ('2018年6月对比2018年5月，订单金额差了多少？', {'kind': 'comparison', 'start': M(2018, 6), 'end': M(2018, 7),
                                        'previous_start': M(2018, 5), 'previous_end': M(2018, 6)}, 'items'),
    # repeat_rate
    ('2018年下过单的客户里，有多少比例下了不止一单？', {'kind': 'repeat_rate', 'start': M(2018, 1), 'end': M(2019, 1)}, 'payments'),
    ('各州客户的复购率（全部数据）。', {'kind': 'repeat_rate', 'group_by': 'customer_state'}, 'payments'),
    ('2017年下半年的复购率。', {'kind': 'repeat_rate', 'start': M(2017, 7), 'end': M(2018, 1)}, 'payments'),
    ('2018年上半年各州的复购率分别是多少？', {'kind': 'repeat_rate', 'group_by': 'customer_state', 'start': M(2018, 1),
                                    'end': M(2018, 7)}, 'payments'),
    # cohort_retention
    ('从2017年6月起首次下单的客户，按首单月份看之后几个月的留存。', {'kind': 'cohort_retention', 'start': M(2017, 6)}, 'payments'),
    ('观察截止到2018年7月1日，各月新客户的留存率。', {'kind': 'cohort_retention', 'end': M(2018, 7)}, 'payments'),
    ('2018年1月起首单、观察到2018年9月1日为止的月度留存。', {'kind': 'cohort_retention', 'start': M(2018, 1), 'end': M(2018, 9)}, 'payments'),
    ('全部客户按首单月份的留存曲线。', {'kind': 'cohort_retention'}, 'payments'),
    # delivery_delay
    ('2017年下单的订单中，送达延迟的比例是多少？', {'kind': 'delivery_delay', 'start': M(2017, 1), 'end': M(2018, 1)}, 'payments'),
    ('按州看全部订单的送达延迟率。', {'kind': 'delivery_delay', 'group_by': 'customer_state'}, 'payments'),
    ('2018年3月送达的订单里，晚于预计送达日期的占多少？', {'kind': 'delivery_delay', 'time_field': 'order_delivered_customer_date',
                                            'start': M(2018, 3), 'end': M(2018, 4)}, 'payments'),
    ('2018年上半年各州送达延迟率（按下单时间）。', {'kind': 'delivery_delay', 'group_by': 'customer_state', 'start': M(2018, 1),
                                       'end': M(2018, 7)}, 'payments'),
    # category_sales
    ('各商品品类的销售额排行（全部数据）。', {'kind': 'category_sales', 'group_by': 'product_category'}, 'payments'),
    ('2018年各品类卖了多少件、多少钱？', {'kind': 'category_sales', 'group_by': 'product_category', 'start': M(2018, 1),
                                  'end': M(2019, 1)}, 'payments'),
    ('2017年第四季度各品类的销售情况。', {'kind': 'category_sales', 'group_by': 'product_category', 'start': M(2017, 10),
                                  'end': M(2018, 1)}, 'payments'),
    ('2018年8月各品类的商品金额和运费。', {'kind': 'category_sales', 'group_by': 'product_category', 'start': M(2018, 8),
                                   'end': M(2018, 9)}, 'payments'),
    # 需要澄清或不支持
    ('3月各州的销售额排行。', CLARIFY, 'payments'),
    ('上个月的复购率是多少？', CLARIFY, 'payments'),
    ('下个季度的送达延迟率会是多少？', CLARIFY, 'payments'),
    ('评价分数最低的品类是哪个？', CLARIFY, 'payments'),
]

# 冻结指纹：题面、真值计划与口径的规范化 JSON 的 SHA-256；在第一次留出集运行之前写定。
OLIST_HELDOUT_SHA256 = 'aad2fb967cf3bd0d7c3a853f0beafb9f39f0740ddf41e99fb2ad4afdaa2397ca'


def fingerprint(questions=HELDOUT) -> str:
    return hashlib.sha256(json.dumps(questions, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


if __name__ == '__main__':
    from collections import Counter
    for name, items in (('dev', DEV), ('heldout', HELDOUT)):
        print(name, len(items), dict(Counter('clarify' if 'clarify' in fields else fields['kind'] for _, fields, _ in items)))
    print(fingerprint())
