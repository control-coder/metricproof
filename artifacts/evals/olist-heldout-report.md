# Olist 真实模型评测（heldout）

提交 `37ba8f9+dirty`，日期 2026-10-23，模型 `mimo-v2.6-flash`，Olist 提示指纹 `7178d0e4736c…`，单表提示指纹 `5d1b2280dd3c…`，留出集指纹 `aad2fb967cf3…`。40 题 × 3 批，墙钟 3564.1 秒。区间为 95% Wilson。

| 对照 | 批次 | 例数 | 正确 | 以题为单位 | 静默错误 | 首次即对 | 重试 | 澄清召回 | 多余澄清 | p50 / p90 秒 | 调用 | 输入 / 缓存 / 输出 token | 费用（元） |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| three_roles | b1 | 40 | 35/40 [73.9, 94.5] | 87.5% [73.9, 94.5] | 0/40 | 35/40 | 0 | 4/4 | 0/36 | 50.7 / 116.205 | 98 | 170540 / 67200 / 82257 | 0.2692 |
| three_roles | b2 | 40 | 34/40 [70.9, 92.9] | 85.0% [70.9, 92.9] | 0/40 | 33/40 | 1 | 4/4 | 0/36 | 49.7 / 158.892 | 100 | 174081 / 66816 / 77485 | 0.2636 |
| three_roles | b3 | 40 | 38/40 [83.5, 98.6] | 95.0% [83.5, 98.6] | 0/40 | 35/40 | 3 | 4/4 | 0/36 | 47.5 / 122.57 | 107 | 188953 / 76672 / 76723 | 0.2673 |
| three_roles | 合计 | 120 | 107/120 [82.3, 93.6] | 89.2% [75.9, 95.5] | 0/120 | 103/120 | 4 | 12/12 | 0/108 | 48.2 / 128.145 | 305 | 533574 / 210688 / 236465 | 0.8000 |
| single_agent | b1 | 40 | 34/40 [70.9, 92.9] | 85.0% [70.9, 92.9] | 0/40 | 32/40 | 2 | 4/4 | 0/36 | 25.5 / 68.681 | 37 | 129435 / 81728 / 32605 | 0.1146 |
| single_agent | b2 | 40 | 38/40 [83.5, 98.6] | 95.0% [83.5, 98.6] | 0/40 | 36/40 | 2 | 4/4 | 0/36 | 31.7 / 67.158 | 40 | 140548 / 89536 / 42694 | 0.1382 |
| single_agent | b3 | 40 | 39/40 [87.1, 99.6] | 97.5% [87.1, 99.6] | 0/40 | 36/40 | 3 | 4/4 | 0/36 | 23.9 / 51.52 | 42 | 147873 / 95552 / 41649 | 0.1375 |
| single_agent | 合计 | 120 | 111/120 [86.4, 96.0] | 92.5% [80.1, 97.4] | 0/120 | 104/120 | 7 | 12/12 | 0/108 | 25.2 / 67.158 | 119 | 417856 / 266816 / 116948 | 0.3903 |

## 未答对的用例

| 对照 | 批次 | 题 | 类型 | 结果 | 说明 |
| --- | --- | --- | --- | --- | --- |
| single_agent | 1 | 2 2018年7月的订单金额合计。 | summary | MODEL_ERROR | `["模型服务连接失败"]` |
| single_agent | 1 | 5 2018年每月订单金额的变化趋势。 | trend | MODEL_ERROR | `["模型服务连接失败"]` |
| single_agent | 1 | 15 2018年8月各州的销售额份额。 | share | MODEL_ERROR | `["模型服务返回 HTTP 500"]` |
| single_agent | 1 | 25 从2017年6月起首次下单的客户，按首单月份看之后几个月的留存。 | cohort_retention | MODEL_ERROR | `{"kind": "cohort_retention", "start": "2017-06-01T00:00:00", "time_field": "order_purchase_timestamp"}` |
| single_agent | 1 | 26 观察截止到2018年7月1日，各月新客户的留存率。 | cohort_retention | MODEL_ERROR | `["模型服务返回 HTTP 500"]` |
| single_agent | 1 | 27 2018年1月起首单、观察到2018年9月1日为止的月度留存。 | cohort_retention | MODEL_ERROR | `["模型服务连接失败"]` |
| single_agent | 2 | 21 2018年下过单的客户里，有多少比例下了不止一单？ | repeat_rate | MODEL_ERROR | `["模型服务连接失败"]` |
| single_agent | 2 | 29 2017年下单的订单中，送达延迟的比例是多少？ | delivery_delay | MODEL_ERROR | `["模型服务返回 HTTP 500"]` |
| single_agent | 3 | 9 整个期间哪个州的销售额最高？按州排序。 | ranking | MODEL_ERROR | `["模型服务返回 HTTP 500"]` |
| three_roles | 1 | 1 2017年第四季度一共多少订单、多少销售额？ | summary | EXECUTION_FAILED | `{"kind": "summary", "start": "2017-10-01T00:00:00", "end": "2018-01-01T00:00:00", "time_field": "order_purchase_timestamp"}` |
| three_roles | 1 | 12 2018年3月各州销售额从高到低。 | ranking | MODEL_ERROR | `["Planner: 模型服务连接失败"]` |
| three_roles | 1 | 16 2017年下半年各州订单金额构成。 | share | MODEL_ERROR | `{"kind": "share", "group_by": "customer_state", "start": "2017-07-01T00:00:00", "end": "2018-01-01T00:00:00", "time_field": "order_purchase_timestamp"}` |
| three_roles | 1 | 29 2017年下单的订单中，送达延迟的比例是多少？ | delivery_delay | MODEL_ERROR | `{"kind": "delivery_delay", "start": "2017-01-01T00:00:00", "end": "2018-01-01T00:00:00", "time_field": "order_purchase_timestamp"}` |
| three_roles | 1 | 33 各商品品类的销售额排行（全部数据）。 | category_sales | MODEL_ERROR | `["Planner: 模型服务返回 HTTP 500"]` |
| three_roles | 2 | 2 2018年7月的订单金额合计。 | summary | MODEL_ERROR | `{"kind": "summary", "start": "2018-07-01T00:00:00", "end": "2018-08-01T00:00:00", "time_field": "order_purchase_timestamp"}` |
| three_roles | 2 | 7 看看整个数据期间按月的销售额走势。 | trend | MODEL_ERROR | `{"kind": "trend", "group_by": "purchase_month", "time_field": "order_purchase_timestamp"}` |
| three_roles | 2 | 10 2018年第二季度各州订单金额排名。 | ranking | MODEL_ERROR | `{"kind": "ranking", "group_by": "customer_state", "start": "2018-04-01T00:00:00", "end": "2018-07-01T00:00:00", "time_field": "order_purchase_timestamp"}` |
| three_roles | 2 | 14 2017年各月销售额占全年的比例。 | share | MODEL_ERROR | `{"kind": "share", "group_by": "purchase_month", "start": "2017-01-01T00:00:00", "end": "2018-01-01T00:00:00", "time_field": "order_purchase_timestamp"}` |
| three_roles | 2 | 19 2018年上半年相比2017年下半年，销售额涨跌幅度是多少？ | comparison | MODEL_ERROR | `["Planner: 模型服务返回 HTTP 500"]` |
| three_roles | 2 | 22 各州客户的复购率（全部数据）。 | repeat_rate | MODEL_ERROR | `{"kind": "repeat_rate", "group_by": "customer_state", "time_field": "order_purchase_timestamp"}` |
| three_roles | 3 | 4 2017年11月24日黑五当天的销售额和订单数。 | summary | MODEL_ERROR | `["Planner: 模型服务返回 HTTP 500"]` |
| three_roles | 3 | 7 看看整个数据期间按月的销售额走势。 | trend | MODEL_ERROR | `["Planner: 模型服务连接失败"]` |
