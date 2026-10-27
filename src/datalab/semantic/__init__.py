"""语义层：指标定义、维度、粒度校验、SQL 编译、内存引擎、独立 Python 参考与指标树。

口径与 datasets/olist.py、verification/olist_sql.py 同一契约；本包只服务 Olist 多表数据，
单表 orders 与旧九类统计任务不走这里。指标与维度的粒度规则见 metrics.py、dimensions.py；
编译与引擎见 compiler.py、engine.py；L1 核验用 reference.py 的纯 Python 独立实现；指标树见 tree.py。
"""
