# 架构与取舍

OrderProof 是模块化单体：FastAPI 接口、Celery worker 与领域工作流在同一个 Python 包 `datalab` 中，按职责分模块，不拆微服务。PostgreSQL 是数据版本、业务记忆、任务状态与产物索引的唯一权威；Redis 只携带任务 ID；生成的分析代码只在专用的无网络 Docker 容器中执行。

```mermaid
flowchart TB
    UI[React 四页面] -->|HTTP 回环| API[FastAPI]
    API --> DS[datasets<br/>CSV 规范化与数据版本]
    API --> MEM[memory<br/>口径与字段语义]
    API --> DB[(PostgreSQL)]
    API -->|task_id| Q[(Redis)]
    BEAT[dispatcher<br/>补发待投递任务] --> DB
    BEAT --> Q
    Q --> W[Celery worker]
    W --> WF[orchestration<br/>状态机与预算]
    WF --> P[Planner]
    WF --> A[Analyst]
    WF --> R[Reviewer]
    A --> FRAME[analysis_frame<br/>服务端代码框架]
    FRAME --> EXE[execution<br/>受限容器]
    EXE --> VER[verification<br/>独立参考、表格比对、不变量与蜕变关系]
    VER --> R
    VER --> ART[artifacts<br/>图表与模板说明]
    W --> DB
```

## 模块

| 模块 | 职责 |
| --- | --- |
| `api/` | HTTP 接口、请求体上限、Host 与写入来源校验、产物下载 |
| `datasets/` | 标准 CSV 模板与字段映射、Olist 多表导入、质量检查、数据版本与 schema 签名 |
| `semantic/` | 语义层：指标定义、维度与粒度校验、SQL 编译、内存引擎、独立 Python 参考与指标树（A1） |
| `attribution/` | 归因算法：异动检测、指标拆解、贡献度、根因定位、相关分析与 L1 核验（A2），全部确定性实现 |
| `orchestration/` | 任务状态机、有界修正、Celery worker、租约与补发 |
| `roles/` | 三角色运行时与工具白名单、固定演示后端、OpenAI 兼容模型适配与提示 |
| `memory/` | 业务记忆的范围、版本、确认、失效与冲突，PostgreSQL 实现 |
| `execution/` | 六类固定分析、代码框架组装、Docker runner 与容器内收集器 |
| `verification/` | 独立 SQL 参考复算与下载表比对；不依赖参考值的结果不变量、蜕变关系，以及第二份纯 Python 参考 |
| `artifacts/` | 核验通过后的确定性图表与模板化说明 |
| `storage/` | 项目、数据集、任务快照与产物索引 |

业务范围（第一版）：UTF-8 CSV，一行一笔订单，字段为订单编号、支付时间、金额、状态、渠道、品类；单文件最多 20 MB、10 万行；单币种、统一时区，不支持部分退款。分析类型为汇总（summary）、趋势（trend）、排行（ranking）、占比（share）、期间对比（comparison）与数据质量（quality）。

第二版加入结构固定的 Olist 多表数据（下节）：表、列与连接键由服务端定义，分析代码只按这些连接键关联，模型和请求都不能指定表、列或连接方式。仍然不做任意 Excel/PDF 或任意表、自动推断连接、联网、数据库直连、预测、机器学习建模或因果推断；分组说明只是描述性分解。

### 多表数据（Olist）

除单表 `orders` 外，数据版本可以是结构固定的 Olist 多表 `olist`（`datasets/olist.py`）。表、列、类型与连接键全部由服务端定义，只从固定目录读取八个已知文件名，不接受任意表或列；结构签名由这份定义计算，与单表签名不同，业务记忆据此分开范围。单表路径的导入、签名与数据版本格式不变（`DatasetVersion.schema` 新增，旧元数据缺省为 `orders`）。A1 起加入卖家表 `sellers`：新导入使用含卖家表的八表签名 `olist.SIGNATURE`；A1 之前导入的七表数据版本仍由旧签名 `olist.LEGACY_SIGNATURE`（固定值，逐字节与旧计算一致）识别为 `olist`，旧九类统计任务只读 `needed(plan)` 的表、不受影响，但语义层使用卖家维度时对该版本给出「该数据版本导入时没有卖家表，请重新导入」的错误。新旧签名分属不同的业务记忆范围：八表版本的口径需重新确认，不继承七表版本的已确认口径。

| 表 | 规范化列 | 主键 | 上限（行 / 字节） |
| --- | --- | --- | --- |
| `orders` | order_id、customer_id、order_status、order_purchase_timestamp、order_approved_at、order_delivered_carrier_date、order_delivered_customer_date、order_estimated_delivery_date | order_id | 15 万 / 32 MB |
| `customers` | customer_id、customer_unique_id、customer_state | customer_id | 15 万 / 16 MB |
| `order_items` | order_id、order_item_id、product_id、seller_id、shipping_limit_date、price_cents、freight_cents | (order_id, order_item_id) | 20 万 / 32 MB |
| `order_payments` | order_id、payment_sequential、payment_type、payment_installments、payment_value_cents | (order_id, payment_sequential) | 20 万 / 16 MB |
| `order_reviews` | review_id、order_id、review_score、review_creation_date、review_answer_timestamp | (review_id, order_id) | 15 万 / 32 MB |
| `products` | product_id、product_category_name | product_id | 5 万 / 8 MB |
| `category_translation` | product_category_name、product_category_name_english | product_category_name | 1000 / 1 MB |
| `sellers` | seller_id、seller_state | seller_id | 5 万 / 4 MB |

- 校验：原始表头必须与 Olist 原文完全一致；时间接受空格或 `T` 分隔的秒级本地时间，统一写成 `YYYY-MM-DDTHH:MM:SS`，带偏移即拒绝；批准、揽收、送达三个日期允许为空（Olist 原始数据确有缺失），其余时间必填；金额最多两位小数，转为整数分；订单状态只允许 Olist 词表（delivered、shipped、canceled、unavailable、invoiced、processing、created、approved）；评分 1–5；标识、州缩写、品类名按固定格式。任一行出错整批拒绝，最多报告 20 条（表名、行号、原因），不回显内容。评论正文、城市、邮编、商品尺寸等未用到的自由文本或列不进入规范化表。
- 连接键：订单→客户、明细→订单、明细→商品、明细→卖家、支付→订单、评价→订单任一孤儿行整批拒绝（只报告表名与计数），否则按客户、卖家、品类或金额汇总会静默丢行；商品品类→英文翻译允许缺失，只计数；没有明细、支付或评价的订单与没有订单的客户允许存在，计数写进概况。
- 概况（`profile`）：逐表行数、订单状态计数、下单时间范围（`time_min`/`time_max`）与送达、预计送达、评价日期范围、各列空值数、孤儿行计数、无子行计数、重复 review_id 数、不同 customer_unique_id 数、卖家数、状态为 delivered 但没有送达日期的订单数、两种订单金额来源不一致的订单数与总差额、原始文件 SHA-256。
- 存储：规范化表平铺写入数据版本目录（`orders.csv`、`customers.csv` 等），原始文件不复制。

分析类型与口径（`verification/olist_sql.py` 模块说明为准，`contracts.OLIST_GROUPS` 列出允许的分组，其他组合在计划校验时拒绝；Olist 数据不支持 quality）：

- 共同：纳入订单 = 订单状态属于已确认口径的状态（Olist 词表），且计划时间字段非空、落在 [start, end) 内。时间字段默认下单时间 `order_purchase_timestamp`，只有送达延迟可以改用送达日期 `order_delivered_customer_date`。比值为 6 位小数字符串，分母为 0 时为 null；金额为整数分。
- 订单级 summary / trend / ranking / share / comparison：输出与单表契约相同。订单金额来源 `amount_source` 是口径字段，只能随口径由用户确认（`payments`：该单全部支付之和；`items`：全部明细价格加运费之和；没有子行记 0），计划里由服务端按口径填写，Planner 给出不同的值即拒绝。分组可选 `customer_state`、`purchase_date`、`purchase_month`（trend 只能按日或按月）；不按品类或支付方式分组，因为一单可以有多个品类、多种支付方式，分组之和不再等于总计。
- `repeat_rate`：客户为 customer_unique_id；期间内至少 1 笔纳入订单的客户数 `customer_count`，至少 2 笔的 `repeat_customer_count`，`repeat_rate` 为二者之比。按州分组时，客户归入其期间内第一笔纳入订单（按时间、再按 order_id）所在的州，各州之和等于总计。
- `cohort_retention`：观察终点 H 为 end（未给 end 时为全部订单最晚下单时间 + 1 秒）；客户首单取 H 之前全部纳入订单中最早的一笔，不受 start 限制，cohort 为首单所在月，start 只筛选 cohort。月份 M 完整当且仅当下个月 1 日零点不晚于 H。每个 cohort 输出 offset 0–6 中月份完整的行 `{cohort, offset, customer_count, retained_count, retention}`，之后右删失、不输出；`total` 给出这些 cohort 的客户总数与 `last_complete_month`。offset 0 的留存恒为 1。
- `delivery_delay`：只统计状态为 delivered 且送达、预计送达日期都不为空的纳入订单（口径必须含 delivered）；按日期比较，送达日期晚于预计送达日期为延迟，同一天送达不算。输出 `delivered_count`、`late_count`、`late_rate`，可按州分组。
- `category_sales`：纳入订单的全部明细按商品品类英文名分组；有品类没有翻译时用原品类名，品类为空时为 `unknown`。每组 `order_count`（不同订单数）、`item_count`、`price_cents`、`freight_cents`，按 price_cents 降序、组名升序；总计的订单数按订单去重，所以各组订单数之和可以大于总计。

真实数据（`scripts/fetch_olist.py` 固定的版本）的实测概况：订单、客户各 99441 行，明细 112650、支付 103886、评价 100000、商品 32951、翻译 71、卖家 3095 行；下单时间 2016-09-04 至 2018-10-17；严格连接键孤儿行全为 0；2 个品类（13 个商品）没有英文翻译，610 个商品品类为空；775 单没有明细、1 单没有支付；827 个 review_id 对应不止一笔订单；8 单状态为 delivered 却没有送达日期；96096 个不同的 customer_unique_id。按支付合计与按明细价格加运费计算的订单金额有 1349 单不一致，支付合计比明细合计多 16531888 分（165318.88 BRL）。本机导入八张表约 10 秒。

### 语义层与指标树（A1）

`semantic/` 是 MetricProof 改造的第一个新模块：把经营指标按统一口径声明、校验并编译为查询，供归因工具（A2 起）与未来的 Agent 工具层（A4 起）调用，替代「每类分析一份固定实现」的扩展方式。它只服务 Olist 多表数据，单表与旧九类统计任务仍走原有路径。模块：指标定义 `metrics.py`、维度 `dimensions.py`、编译器 `compiler.py`、引擎 `engine.py`、独立 Python 参考 `reference.py`、指标树 `tree.py`；口径快照 `snapshot.py` 只含计算所需字段，由服务端从已确认口径建立。

指标（名称、粒度、类型、单位；比率型给出分子/分母成分）：

| 指标 | 中文名 | 粒度 | 类型 | 口径要点 |
| --- | --- | --- | --- | --- |
| gmv | 订单金额合计 | 订单 | 加法型（分） | 金额来源按已确认口径 amount_source：payments 为该单全部支付之和，items 为全部明细 price+freight 之和；没有子行计 0（与 olist_sql._orders 一致） |
| order_count | 订单量 | 订单 | 加法型（个） | 纳入订单数 |
| aov | 客单价 | 订单 | 比率型 gmv/order_count | — |
| cancel_rate | 取消率 | 订单 | 比率型 cancel_count/all_order_count | 不受 included_statuses 约束：分母为期间内全部状态的订单，分子为其中 canceled 订单 |
| late_delivery_rate | 送达延迟率 | 订单 | 比率型 late_count/delivered_dated_count | 与 delivery_delay 同口径（按日期比较，同日不算延迟；要求 delivered 且两个日期非空） |
| avg_delivery_days | 平均配送天数 | 订单 | 比率型 delivery_days_sum/delivered_dated_count | 按日期差取整天（送达日期 − 下单日期），不用时间戳秒差，可精确复算 |
| item_gmv / category_gmv | 明细金额 / 品类 GMV | 明细 | 加法型（分） | 明细 price_cents + freight_cents 之和；category_gmv 是它按 product_category 分组的别名，与 category_sales 的 price+freight 可对照 |
| item_count | 明细件数 | 明细 | 加法型（个） | — |
| avg_item_price | 件单价 | 明细 | 比率型 item_price_sum/item_count | 分子只用 price，不含运费 |
| items_per_order | 每单件数 | 明细 | 比率型 item_count/item_order_count | 分母为有明细的不同订单数，总计按订单去重、分组按组内去重（一单跨组时各组各计 1） |
| freight_ratio | 运费占比 | 明细 | 比率型 freight_sum/item_gmv | — |
| avg_review_score | 平均评分 | 评价 | 比率型 review_score_sum/review_count | 评价经 order_id 归到订单，期间与状态按订单过滤；同一订单多条评价全部计入，不按订单去重 |
| bad_review_rate | 差评率 | 评价 | 比率型 bad_review_count/review_count | 差评 = 评分 ≤ 2 |
| repeat_rate | 复购率 | 客户 | 比率型 repeat_customer_count/customer_count | 与 olist_sql._repeat 同口径 |
| new_customers / returning_customers | 新客数 / 老客数 | 客户 | 加法型（个） | 按 customer_unique_id 全部历史（不受期间起点限制）中第一笔纳入订单判定：首单落在期间内为新客；期间内有纳入订单但首单在起点之前为老客 |

维度（粒度；取值规则）：customer_state（订单级，经客户表；客户级指标按首单所在客户行的州归州）、seller_id 与 seller_state（明细级，层级 seller_state → seller_id）、product_category（明细级，英文翻译退回原品类名、空为 unknown）、price_band（明细级，按 price_cents 分带：lt_50 / 50_100 / 100_200 / 200_500 / ge_500，单位 BRL）、payment_type 与 payment_installments（订单级：取该订单 payment_sequential 最小的那笔支付，无支付为 unknown）、customer_type（订单级：new/returning，判定同新客口径）、delivery_status（订单级：late / on_time / not_delivered，按日期比较）、purchase_date 与 purchase_month（下单时间前 10 / 前 7 位）。

粒度规则：维度粒度比指标粒度细时拒绝并给出建议（`SemanticError`），例如订单级 gmv 按 product_category 拆——一单可含多个品类，各组之和不等于总计，建议改用 item_gmv/category_gmv；客户级指标只允许 customer_state（按首单归州，不会重复计数），其余维度都会让客户重复计数。明细级指标可以用订单级维度（明细属于订单）。

编译器（`compiler.py`）：`compile_query(metric, period, filters, group_by, snapshot)` 把查询编译为参数化 SQLite SQL。表名、列名、维度表达式全部来自服务端常量；取值一律参数绑定；filters 只允许「维度 = 值」或「维度 IN 值列表」，维度名必须在指标白名单内，取值按词表校验格式。期间为左闭右开 [start, end)，期间字段固定为下单时间。比率型指标编译为分子、分母两条成分查询。

引擎（`engine.py`）：`SemanticEngine(folder, snapshot)` 把需要的规范化表载入内存 SQLite（固定连接键与下单时间列建索引，载齐后跑一次 ANALYZE 让规划器按真实行数选驱动表），同一任务多次查询复用连接；`query(metric, period, compare_period=None, filters=None, group_by=None)` 返回 `{metric, grain, type, unit, period, total: {numerator, denominator, value}, rows: [{group, numerator, denominator, value}], compare?, delta?}`；比值为 6 位小数字符串（分母 0 为 null），金额为整数分；总计与分组分别编译执行（分组行加总不一定等于总计）。两处性能措施：customer_type 维度与客户级指标依赖的「客户首单集合」（窗口子查询，单次约 2 秒）在首次用到时物化为 `_first_order` 表（建 u、first_order 两个索引），同连接内全部查询直接 JOIN；引擎内部按 (指标, 期间, 过滤器, 分组) 复用单次运行结果（连接期间数据不变，find_root_cause 等归因函数会对同一分组结果重复查询数十次）。旧七表数据版本没有 sellers.csv，使用卖家维度时给出「该数据版本导入时没有卖家表，请重新导入」的错误。

L1 核验（`reference.py`）：纯 Python 逐行实现同一套指标与维度，不用 SQL、不与编译器共享计算代码；`verify(engine_result, folder, snapshot, filters=, group_by=)` 逐项比较语义层结果与参考。参考侧同样有性能措施：`_Cache` 按 (folder, snapshot) 缓存读入的规范化表、首单集合与逐粒度派生记录（每单/每明细/每评价一行，金额、首笔支付、品类、卖家、评分等在构建时一次算好），之后每个查询只做期间与过滤器判断；订单级金额来源为 payments 时不读 order_items。缓存只是同一套逐行算法的预计算，不共享时（`python_reference` 直接调用）逐次读表、行为不变。`tests/datalab/test_semantic.py` 在固定种子合成数据上验证：与 olist_sql 重叠的指标（gmv/order_count 不分组、按州、按月 × 两种金额来源，late_delivery_rate 含按州，repeat_rate 含按州，明细金额/件数按品类）逐行一致；全部指标 × 允许维度与 Python 参考逐项一致；粒度不匹配、未知维度、过滤器注入（维度名为任意字符串、取值含引号）被拒绝或安全绑定；指标树结构校验通过。

指标树（`tree.py`）：声明拆解关系供 A2 的 decompose_metric 使用，本轮只做声明与结构校验，不做归因算法。乘法型：gmv = order_count × aov、item_gmv = item_count × avg_item_price；加法型：order_count 按 customer_type 拆为新老客订单之和；比率型指标声明分子/分母关系（归因时分别计算分子分母的变化再合成）。

真实数据冒烟（一次性，数据不入库）：导入八张表约 10 秒；随后 13 组代表性查询（gmv 按州/月、repeat_rate 按州、item_gmv 按品类/卖家州/卖家 ID 带过滤、评价按送达状态、新老客按州、freight_ratio 按价格带等）引擎耗时 0.01–5.8 秒（首单窗口查询最慢），每组 verify 全部通过。语义层结果未与既有九类任务混算，后续归因轮次单独报告。

性能优化（2026-10-27，A5 前的遗留问题修复）：定位到三个慢点——首单窗口子查询逐次执行（customer_type 查询 2.1 秒、repeat_rate 5.3–6.4 秒）、归因层对同一分组结果逐元素重复查询（find_root_cause 3 个维度共 236 次 query）、参考侧逐次读 CSV 与逐次连接。修复后实测（引擎首次载入 4 表+建索引+ANALYZE 约 3.1–3.5 秒、首单表物化约 1 秒，均为一次性开销）：常见单次查询 0.03–0.99 秒（明细级分组 0.07–0.30 秒、gmv 按 customer_type 0.76–0.99 秒、按月份 0.67–0.93 秒、repeat_rate 按州 0.16–0.17 秒）；detect_anomaly(history=6) 2.0–3.1 秒；find_root_cause(item_gmv, [product_category, customer_state, seller_state]) 0.16–0.44 秒。参考路径同量级：verify(item_gmv 按品类) 约 2.5 秒、verify_attribution(detect_anomaly) 3.4–5.6 秒、verify_attribution(find_root_cause) 2.2–3.5 秒（含一次性建派生记录约 1.7 秒）。逐位一致：test_semantic、test_attribution 全部通过且断言未放宽。

### 归因算法（A2）

`attribution/` 是归因工具层：`detect_anomaly`、`decompose_metric`、`contribution`、`find_root_cause`、`correlate` 五个确定性算法与 L1 核验入口 `verify_attribution`，全部为纯函数，输入是任意带 `query(metric, period, compare_period=None, filters=None, group_by=None)` 方法、返回语义层引擎同结构结果的数据源。数据源有两种：语义层引擎本身，以及 `ReferenceSource(folder, snapshot)`——包装 `semantic/reference.py` 的独立纯 Python 计算（不用 SQL、不与编译器共享代码，查询结果按 (folder, snapshot, 指标, 期间, 过滤器, 分组) 做模块级 LRU 缓存，派生记录经 `semantic.reference._Cache` 按 (folder, snapshot) 共享，同一数据版本上的多个 ReferenceSource 实例不重复读表），供 L1 核验路径独立重算。期间统一为 (start, end) 元组，左闭右开、秒级 ISO。金额与计数输出为整数，比值、贡献度与份额为 6 位小数字符串（与语义层一致），加法和比率拆解内部全程 Fraction，不变量精确成立。

- `contribution(source, metric, dimension, period, base_period, filters=None)`：维度下钻贡献度。加法型用 Delta 法：change_i = current_i − base_i，contribution_i = change_i / 基期总量（计划术语表「各维度值的差值除以基期总量」；基期总量为 0 时贡献度为 null），share_of_change_i = change_i / 总变化（总变化为 0 时各组 share 为 null 并给 status: "no_change"）；不变量 Σchange_i = 总变化精确成立。比率型用对称组间组内拆解（between-within）：w 为组分母份额、r 为组内比率、R̄ = (R0+R1)/2，structure_i = (w1−w0)·((r0+r1)/2 − R̄)，within_i = (w0+w1)/2·(r1−r0)，Σ(structure_i+within_i) = R1−R0 精确成立（Σ(w1−w0)=0，居中项合计为 0）。结构项居中（2026-10-27 A5 调试时修正）：不居中时每个份额变化的组都带上 (w1−w0)·r 的大额抵消项，排名失去意义；两期之一没有分组行时不居中。一期缺席的组（该期没有分组行或分母为 0）：该期 w 按 0、r 取另一期的值——组整体进出，没有组内信息，全部变化计入结构项（此时 within_i 恰好为 0）。总比率 R0/R1 与份额分母都按分组行分子、分母之和复算，不取引擎总计的 6 位舍入值，保证不变量精确。rows 按 |change|（比率型按 |structure+within|）降序、组名升序。输出 {metric, dimension, method: "delta"|"between_within", period, base_period, base_total, current_total, total_change, change_rate, rows, invariant: {passed, residual}}。
- `decompose_metric(source, metric, period, base_period)`：按指标树声明拆解。乘法型（gmv = order_count × aov 等）用 LMDI-I 加法形式：贡献_k = L(V1,V0)·ln(x_k1/x_k0)，L 为对数平均 (a−b)/(ln a − ln b)，Σ贡献 = V1−V0（浮点，残差 ≤ 1e-6·|V1−V0| 或绝对 1e-6，随结果输出）；比率型父子（aov = gmv / order_count 等）的分母因子以倒数参与乘积，贡献取符号相反的对数项。item_gmv = item_count × avg_item_price 的乘积只在不含运费的价格口径上精确成立（item_gmv 含运费），指标树用 `target: item_price_sum` 声明拆解目标为 item_price_sum，输出附 target 字段。边界：V1 = V0 时各因子贡献为 0.0（status: "no_change"）；父值或任一因子任一期为 0 / null（对数平均无定义）时返回 status: "undefined"、factors 为空。加法型（order_count 按 customer_type）子项即父指标按声明维度分组的各组，贡献等于各组变化量，Σ贡献 = 总变化精确成立。输出 {metric, kind, method, factors: [{metric, base, current, contribution, share}], total_change, invariant}（LMDI 的贡献与 share 为浮点）。
- `detect_anomaly(source, metric, period, history=6)`：异动检测。历史期取与 period 同长度、紧邻的前 history 个期间：period 两端都是月初零点时按日历月平移（保持自然月语义），否则按时间差平移。数据源可提供 `data_floor` 属性（数据的最早可用起点，引擎与 ReferenceSource 都按全部订单的最早下单时间给出）：起点早于它的历史期按「还没有这份数据」丢弃，不计入有效历史——语义层对没有数据的期间返回 0 而不是「无数据」，归因靠 floor 区分这两种情形。环比是与紧邻上一期；同比是去年同期（两端各减一年，2 月 29 日退到 2 月 28 日；去年同期早于最早有效历史期起点时为 null）。稳健 z 分数 z = 0.6745·(x − median)/MAD（Iglewicz–Hoaglin），|z| > 3.5 判为异动；有效历史不足 3 期给 status: "insufficient_history"（不判异动），MAD 为 0 给 status: "zero_mad"（历史值全部相同，本期偏离中位数即异动、z 为 null）。中位数与 MAD 先吸附到 1/(2×10⁶) 有理数网格（历史值都是 6 位精度有理数，统计量落在半整数网格上，浮点误差远小于网格间距），保证两条路径逐位一致。输出 {metric, period, value, history, mom, yoy, median, mad, robust_z, is_anomaly, status}。
- `find_root_cause(source, metric, dimensions, period, base_period, filters=None, tep=0.8, teep=0.1, top=3)`：多维根因定位，参考 Adtributor（Bhagwan et al., NSDI 2014）。元素解释力 EP_ij 取 contribution 的结果（加法型 change_i / 总变化，比率型 (structure_i+within_i)/(R1−R0)），各元素 EP 之和精确为 1；惊奇度 S_ij 用基期份额 p 与本期份额 q 的 Jensen–Shannon 散度项 D(p‖m)+D(q‖m)（m=(p+q)/2，自然对数），加法型按指标值份额、比率型分别对分子份额与分母份额各算一次相加（两个成分的分布变化都算惊奇）。维度内元素按 S 降序（并列按取值升序），EP ≥ teep 的依次加入候选集，累计 EP ≥ tep 即停止；维度按候选集的平均惊奇度降序（并列按维度名升序）取前 top 个（原文用惊奇度之和，偏向取值多的品类、卖家维度；2026-10-27 改为平均，dev 注入案例上无模型基线 Top-1 由 3/7 升到 4/7）。比率型元素另给 effect（between 组间为主 / within 组内为主）。candidates 是扁平化的根因候选列表：先按所在维度排名、再按维度内解释力降序，排序确定性，供 A3/A5 计算 AC@1/AC@3。总变化为 0 时返回空候选与 status: "no_change"。输出 {metric, period, base_period, total_change, dimensions: [{dimension, elements, explained, surprise}], candidates: [{dimension, value, explanatory_power, surprise}]}。
- `correlate(source, metric_a, metric_b, by, period)`：两个指标按维度 by 分组的相关分析：Pearson r、Spearman ρ（平均秩处理并列）与样本量 n。只用两个指标都有值（比率型分母非 0）的组；n < 3 时系数为 null（样本太少，相关无意义）。输出附 note「相关不代表因果，报告中只能表述为相关」——只做描述性归因，不做因果推断。
- L1 核验 `verify_attribution(kind, result, folder, snapshot, **arguments)`：用 ReferenceSource 独立重算同一函数并逐项比较（Fraction 精确结果比较格式化后的字符串/整数，LMDI 贡献与相关系数为浮点允许 1e-9 相对误差），并检查不变量；返回 {passed, differences, invariant}。这是计划中「L1 由语义层与独立参考复算；贡献度满足各项之和等于总变化」的实现。

A1 的语义层有两处小改动配合本轮：`tree.py` 的 Decomposition 增加 `target` 字段（乘法拆解的目标指标，None 表示 parent 本身；item_gmv 声明为 item_price_sum）；`engine.py` 增加 `data_floor` 只读属性（全部订单的最早下单时间），行为不变。

测试（`tests/datalab/test_attribution.py`，20 个用例）：手算小样本用最小假数据源覆盖 Delta 法（含基期为 0、总变化为 0）、组间组内（含一期缺席的组）、LMDI（V0=100=10×10、V1=150=12×12.5，含比率型父子的分母取倒数与 0 因子边界）、MAD z 分数（含 insufficient_history、zero_mad、data_floor 裁剪、非月初期间按时间差平移）、Adtributor 选元素与排序（ΣEP=1、候选确定性）、Pearson/Spearman 已知值（含并列平均秩与 n<3）。不变量在 olist_bundle 合成数据（6 个种子 × 10 组指标×维度，共 60 组）上验证 contribution 的 Σ=总变化精确成立、decompose_metric 的 LMDI 残差在容差内；L1 在同一合成数据上验证引擎与 ReferenceSource 两条路径对五类函数逐项一致，篡改一个数字即被检出。

真实数据冒烟（一次性，数据不入库，导入临时目录用完删除）：导入八表约 10.3 秒；2017-11 vs 2017-10 的 gmv 检测为异动（环比 +40,785,395 分即 +53.3%，稳健 z 5.36），LMDI 拆解订单量贡献 +46,768,269 分、客单价贡献 −5,982,874 分（残差 0.001，在 1e-6·|总变化| 容差内）；item_gmv 按品类的贡献度 Top-3 为 bed_bath_table（+5,094,662 分）、health_beauty（+4,069,126 分）、furniture_decor（+4,014,444 分），不变量精确成立；find_root_cause(item_gmv, [product_category, customer_state, seller_state]) 的 Top-3 候选为 seller_state=RJ（EP 0.1455）、PR（EP 0.1293）、SP（EP 0.5874）。A1 引擎优化前的耗时：detect_anomaly 4.96 秒、decompose_metric 1.35 秒、contribution 0.38 秒、find_root_cause 26.1 秒（3 个维度、归因层对同一分组结果逐元素重复查询，共 236 次 query）。优化后实测：detect_anomaly 2.0–3.1 秒、find_root_cause 0.16–0.44 秒；verify_attribution(detect_anomaly) 3.4–5.6 秒、verify_attribution(find_root_cause) 2.2–3.5 秒。

## 三角色与状态

### 上下文与权限

每次调用都深复制显式交接，角色之间不共享聊天历史，也不发送订单明细。

| 角色 | 接收 | 输出 | 权限 |
| --- | --- | --- | --- |
| Planner | 问题、数据概况（聚合统计）、已确认口径快照 | `plan` 或 `confirmation` | 无执行权限；不能更换数据版本、指标版本、纳入状态或 Olist 订单金额来源 |
| Analyst | 计划、尝试产物 ID、必要反馈 | 只写 `analyze(data, plan)` 函数 | 代码只进入受限容器 |
| Reviewer | 问题、数据概况、计划、产物 ID、核验检查名与结论 | `accepted` 与 `feedback` | 不执行代码；收不到独立参考数值 |

服务端 `BoundTools` 把角色和当前任务绑定到固定工具白名单，每个角色只有一个工具：Planner 读数据概况（`profile`），Analyst 提交沙箱运行（`execute_python`），Reviewer 调用独立核验（`verify`）。记忆读取与口径确认由编排层在调用前后完成，不作为角色工具。模型响应不能声明自身身份、启用 bash 或传入工具注册表。计划常量、CSV 读写、编码与公式注入防护由 `execution/analysis_frame.py` 生成，组装后的脚本仍进入同一容器并接受独立核验。角色交接中的计划不带 `checks`，核验项由服务端决定。

Olist 数据的三个角色使用 `roles/olist_prompts.py` 中的提示：Planner 提示列出九类分析允许的分组与时间字段，Analyst 提示写明每类投影进容器的表与列和各自的输出契约，Reviewer 提示说明新类型与分组的含义。适配器按交接中的 `schema`（Planner、Reviewer 看概况视图，Analyst 看计划）选择提示。单表提示所在的 `roles/prompts.py` 没有改动，单表任务发给模型的提示与交接逐字节不变，由 `tests/datalab/test_prompt_compat.py` 检查（文件指纹与一条完整回退路径的请求摘要）。计划解析时 Olist 计划可以给 `time_field`；`schema` 与订单级分析的 `amount_source` 由服务端按数据版本和已确认口径填写，Planner 写出不同的金额来源时任务以 `MODEL_ERROR` 失败、不执行。

### 模型可见字段与 trace

模型看到的概况与口径是白名单视图（`model_profile`、`model_metric`）：概况只含行数、状态计数、起止时间、币种与时区，并逐字段校验类型与格式；Olist 概况另含逐表行数（表名来自固定词表）与下单、送达、预计送达三个日期的范围，状态计数只取 Olist 词表，孤儿行、空值、金额来源差异与原始文件哈希不进入提示。口径只含版本号与纳入状态（Olist 口径另含金额来源）。数据语义家族、结构签名、口径来源、项目标识这些自由文本或内部标识只用于服务端的范围校验，不进入提示。核验反馈中的字段路径也只保留输出契约内的键名（`RESULT_KEYS`）与数组下标，其余键换成 `<key>`：路径来自模型代码写出的 `result.json`，键名可能取自订单文本。模型能看到的自由文本只剩用户的问题，这是用户自己的输入，边界由输出侧校验把住：计划的纳入状态和版本必须等于已确认口径，否则任务失败且不执行。

每次角色调用在任务里追加一条 trace：序号、角色、所处状态、交接的 SHA-256 与字段路径清单、模型用量、耗时和结果类别（成功或异常类名）。不保存提示、响应或数据内容。trace 随任务快照落库，并写入核验报告，用来事后核对某次调用实际能看到哪些字段。`tests/datalab/test_prompt_injection.py` 用金丝雀检查这一点，见 [评测与验证](evaluation.md)。

### 状态机

```mermaid
stateDiagram-v2
    [*] --> QUEUED
    QUEUED --> PROFILING
    PROFILING --> PLANNING
    PLANNING --> WAITING_CONFIRMATION: 缺少确认口径或需要澄清
    WAITING_CONFIRMATION --> PLANNING: 用户确认后重新排队
    PLANNING --> EXECUTING
    EXECUTING --> EXECUTING: 执行失败，修正一次
    EXECUTING --> VERIFYING
    VERIFYING --> EXECUTING: 核验失败，反馈一次
    VERIFYING --> PLANNING: Reviewer 驳回，Planner 复核一次
    VERIFYING --> SUCCEEDED
    SUCCEEDED --> [*]
```

任一非终态都可以进入 `FAILED` 或 `CANCELLED`；失败原因记录为失败码，如 `MODEL_ERROR`、`REVIEW_REJECTED`、`REFERENCE_DISAGREEMENT`（两份服务端参考不一致，见[核验](#核验)）。

- 缺少确认口径时进入 `WAITING_CONFIRMATION` 并释放 worker；确认快照保存后从计划边界重新排队。等待期间不计时，恢复后已用的调用、token 和时间不归零。
- 预算：固定演示默认 8 次调用、12000 token、120 秒；模型任务由 `.env` 配置，默认 8 次、15 万 token、600 秒。8 次是代码修正、核验反馈和驳回后重规划全部触发时的最长路径（Planner 2 次、Analyst 4 次、Reviewer 2 次），由 `LONGEST_PATH_CALLS` 给出并有测试对照。预算调低后，驳回时剩余调用不足 3 次（重规划、重新执行、再审查各一次）就不开始重规划，直接采用已通过核验的结果并把驳回意见记入时间线。单次请求超时取供应商超时与剩余预算的较小值，不自动重试。
- 代码执行失败最多修正一次。独立核验失败时，程序把定位信息直接回传 Analyst，最多一次，不调用 Reviewer。反馈只给出错误位置，不给期望值：`independent_reference` 报告不一致的字段路径与类型（如 `comparison.growth` 的 value、`total.order_count` 的 type），`download_table` 报告文件缺失、列顺序错误、列不符、行数不符或具体行列；转发前按白名单只保留定位字段。
- Reviewer 只在核验通过后调用，只判断计划是否读对问题（类型、分组、期间），不审查口径、核验项或数值。驳回交回 Planner 复核：维持原计划则采用已通过核验的结果并把意见记入时间线；给出新计划则重新执行、核验和审查，再次驳回则以失败码 `REVIEW_REJECTED` 失败。
- 审查是咨询性的。Reviewer 或复核 Planner 超时、截断、连接失败、输出不合契约或预算耗尽时，跳过审查并记录 `review_skipped`，采用已通过独立核验的结果。Reviewer 单独使用较低输出上限（默认 2048 token）和较短超时（默认 60 秒）。
- Planner 或 Analyst 调用异常、输出不合契约时以 `MODEL_ERROR` 失败；错误信息不含密钥或响应原文。即使 Reviewer 接受，独立核验不通过也不能成功。

## 受限执行

可信宿主 runner 管理 Docker；生成代码只在专用镜像 `datalab-executor:py312-v2` 中执行，没有宿主执行降级。

- 挂载：只读挂载本次复制的规范化 CSV、分析代码与收集器；不挂载工作区、原始数据目录、Docker socket 或凭据。每次尝试使用新的 UUID 目录和随机容器名。
- 隔离：非 root、默认 seccomp、丢弃全部 capabilities、禁止权限提升、无网络、只读根文件系统；CPU 1 核、内存 256 MB 且无额外 swap、32 个进程、文件描述符上限。只有 16 MB 的 `/output` tmpfs 可写。
- 镜像：运行时不安装依赖；执行前把镜像标签解析为内容 ID，按该 ID 创建容器并写入执行清单，不自动拉取。
- 输出：stdout/stderr 不直接暴露。收集器只读取白名单内的平面普通文件，拒绝符号链接和硬链接，经受限 JSON/base64 通道返回；宿主再次检查字段、文件名和解码后大小，总输出上限 8 MB。
- 生命周期：超时、取消、异常都会强制移除本次容器；无法确认清理时标为 `CLEANUP_PENDING`，不返回成功。容器正常退出只记 `COMPLETED`，不等于分析成功。

容器内产生的任何字节都不可信。数值正确性由宿主的独立 SQL 参考复算 `result.json`，并独立比对下载 CSV。

### 多表输入

Olist 数据版本的容器输入由服务端按计划投影（`datasets/olist.needed`）：只给本类分析用到的表和列（例如复购率只有订单的编号、状态、下单时间、客户编号与客户的 customer_unique_id），所有行原样保留，不预先按状态、期间过滤或连接，这些都由被核验的代码完成；评价表不进入任何分析的输入。投影结果作为 `/input/<表名>.csv` 经 `DockerRunner.execute_python(..., input_files=...)` 写入同一只读挂载，容器参数与单表路径逐项相同（有离线测试对照）。单表数据版本不接受 `input_files`，多表数据版本必须提供；文件名只能是小写表名加 `.csv`，最多 8 个，合计不超过 32 MB。框架尾部（`analysis_frame.TAIL`）按计划的 `schema` 决定 `data`：单表仍是订单行列表；`olist` 是 `{表名: 行列表}`，值都是字符串。

用真实数据（`scripts/verify_olist.py`）在生产限额（256 MB、1 核、30 秒）下测量只读入输入表的开销：每类代表性计划的输入 10.5–18.3 MB、20–31 万行，容器运行（含创建与清理）1.5–2.0 秒，进程峰值 RSS 85–132 MiB，cgroup `memory.peak` 90–137 MiB；宿主投影输入另需 0.8–1.3 秒。内存与时间限额不变。

### 自由查询 run_sql（A8）

子 Agent 在语义层工具表达不了时可以写 SQL。流程分三层：

1. 宿主白名单（`execution/sql_guard.py`，sqlglot，DuckDB 方言）：只放行单条只读 SELECT（可含 WITH 与集合运算）；树中出现任何写操作、DDL、COPY、ATTACH、PRAGMA、SET、INSTALL/LOAD 或 SELECT INTO 节点即拒绝；表只能是 Olist 八张规范化表或查询内的 CTE，不能带库名前缀，不能是表函数或文件路径；读文件、环境与元数据的函数（`read_*`、`glob`、`getenv`、`duckdb_*` 等）拒绝；列经 sqlglot 的 qualify 逐个解析到白名单列；外层必须有 1–200 的整数字面量 LIMIT。校验同时得出每张表实际引用的列。
2. 沙箱执行（`execution/sql_sandbox.py`）：宿主只投影这些列写入只读输入，容器里运行服务端生成的固定脚本，SQL 以 base64 载荷传入，不拼进源码。DuckDB 1.5.6 预装在执行镜像 `datalab-executor:py312-v2` 中，运行时不安装；连接后设单线程、160 MB 内存、`allowed_directories=['/input']`、关闭外部访问与扩展自动安装，再锁定配置。即使绕过白名单，读 `/etc/passwd`、`COPY TO`、改配置、`INSTALL` 在容器内也会失败（有集成测试）。容器参数与 `execute_python` 相同，经 `DockerRunner.execute_inputs` 运行。
3. L2 检查（`verification/sql_result.py`）：不变量（列名唯一、列数一致、数值有限、行数不超过 LIMIT）；蜕变关系：同一容器内在按固定种子打乱行序的表上再执行一次，结果作为多重集合必须相同，没有确定排序的 LIMIT 查询会因此不通过；载入比对：沙箱载入的每表行数等于宿主投影行数；汇总比对：模型给出 `reconcile {metric, period, column}` 时，结果列之和须等于语义层该加法型指标在该期间的值。检查结论随结果存储，报告引用通过检查的 run_sql 结果时标 L2，不通过为 L3。

自由 SQL 没有独立参考实现，L2 只是必要条件：不变量与置换检查不验证查询语义，只有汇总比对把结果与语义层对上。`run_python` 的 Agent 接线不在 A8 范围内，调用时返回「未接线」。

## 核验

所有检查由 `verification/suite.py` 的 `run_checks` 统一执行，工作流（`ExecutionServices.verify`）与固定演示（`pipeline.run_fixed`）调用同一入口。现有六类分析都写得出参考 SQL，所以“与参考逐项一致”是主判据；后面三层不需要知道正确值，是为以后放开任务（写不出参考的分析）准备的。

| 检查 | 需要参考值 | 成本 | 做什么 |
| --- | --- | --- | --- |
| `independent_reference`、`group_total` | 是 | 宿主内 | SQL 参考逐字段比较（含 JSON 类型）；分组行之和等于总计 |
| `download_table` | 是 | 宿主内 | 下载 CSV 与参考逐行逐列比对 |
| `invariants` | 否 | 宿主内 | 只用输出契约、计划和结果自身数字检查自洽：计数与金额为非负整数，没有订单的总计金额为 0；分组唯一、每组至少一单、各组之和等于总计；占比等于本行金额 / 总额的 6 位小数字符串，合计与 1 的偏差不超过 行数 × 0.5e-6；排行按金额降序、组名升序，其他按组名升序；按日分组为合法日期且落在 [start, end) 内；对比的 delta 等于本期减上期，growth 等于 delta / 上期金额（上期为 0 时为 null）；质量结果的状态计数之和等于行数，键只能是三种状态 |
| `reference_agreement` | 否（比较两份参考） | 宿主内 | 第二份参考（`python_reference.py`：纯 Python 逐行累加，比值用整数银行家舍入，不用 SQL、不共享计算代码）与 SQL 参考逐项比较。不一致说明服务端参考本身有缺陷，任务以 `REFERENCE_DISAGREEMENT` 直接失败，不回传 Analyst |
| `metamorphic` | 否 | 每条关系 1–2 次沙箱运行 | 在同一受限容器里用服务端变换过的输入重跑本次尝试的同一份代码，只比较几次输出彼此的关系，见下 |

蜕变关系（`verification/metamorphic.py`）：

- `permutation`：订单行按固定种子打乱，结果必须完全相同。
- `irrelevant_rows`：追加按计划必然被排除的合成订单，结果必须完全相同。合成订单有两种：状态不在口径内、时间在期间内（优先放在起点上）；状态在口径内、时间恰在期间终点或起点前一秒。对比分析的合成时间同时避开本期与上期。quality 统计全部行，不适用。
- `window_split`：summary、trend、ranking 在数据中位于中间的一个支付时间处把期间切成两段，两段的总计与各组之和必须等于全期间结果。切点上恰有订单，所以两段的边界都会被用到。share 与 comparison 含比值、quality 不按期间，不适用；只能识别服务端框架脚本和固定脚本，其他脚本记为不适用。

变换输入通过 `DockerRunner.execute_python(..., orders_csv=...)` 写进同一个只读输入挂载，容器参数与默认路径逐项相同（有离线测试对照），参数不接受模型或请求方提供的内容。每次重跑使用新的任务目录和容器。剩余预算时间少于 5 秒时跳过后续关系并记为 `skipped`，跳过不算失败。只在前面的检查全部通过时才运行，前面已失败时结论不会变，不再花沙箱时间。

蜕变关系由运行配置 `artifacts/local/runtime.json` 的 `"metamorphic_checks": true` 开启（`Settings.metamorphic_checks`，默认 `false`）；固定演示用 `run_fixed(..., metamorphic=True)`。默认关闭，是因为现有六类都有独立参考，蜕变关系不改变结论，每个任务却要多 4 次沙箱运行。以后加入写不出参考的分析类型时，计划对这些类型默认开启。

各层分别能拦下哪类错误，见[评测文档的错误注入基准](evaluation.md#错误注入基准)。Olist 的错误注入基准见[评测文档](evaluation.md#olist-错误注入基准)：Olist 特有的口径错误几乎只有独立参考能拦下。

Olist 数据走同一个 `run_checks`：`independent_reference` 用 `verification/olist_sql.py`（内存 SQLite，连接与聚合用 SQL，留存的月份用 SQL 整数运算），`reference_agreement` 的第二份参考是 `verification/olist_python.py`（逐行连接、月份用 datetime 推算、整数比值）。两份参考读数据版本目录中的完整规范化表，固定分析 `execution/fixed_olist.py` 只读沙箱里的投影输入；`tests/datalab/test_olist_kinds.py` 在 200 份固定种子的合成 Olist 数据 × 2 个随机计划（共 400 组，覆盖全部九类）上断言三者逐项相等且满足不变量，另有手算小例子固定同日送达、首单归州、右删失、未翻译品类等口径细节。`group_total` 对新类型只比较可加字段（复购的客户数与复购客户数、送达延迟的两个计数、品类销售的明细数与金额）；留存没有分组。新类型的不变量：

- 键与类型符合各自契约；计数非负整数；比值等于分子 / 分母的 6 位小数，分母为 0 时为 null；复购客户数 ≤ 客户数、延迟数 ≤ 送达数、留存人数 ≤ cohort 人数、品类订单数 ≤ 明细数（`bound`）。
- 分组唯一、每组至少一个对象、按契约排序；州名为两个大写字母；可加字段之和等于总计；品类各组订单数之和不少于去重总数、单组不超过总数。
- 留存：每个 cohort 的 offset 从 0 连续、不超过 6、人数一致，offset 0 留存为 1（`offset_zero`）；不越过最后一个完整月份，也不在完整月份之前提前截断；给了 end 时最后完整月份必须是 end 所在月的上一个月（`censoring`）；cohort 人数之和等于总计，cohort 不早于 start 所在月。

Olist 的蜕变关系与单表同名同开关：`permutation` 每张表各自按固定种子打乱；`irrelevant_rows` 追加属于新客户（新的 customer_unique_id）的合成订单及其客户、明细、支付与评价子行，口径外状态放在期间内，口径内状态放在期间终点或起点前一秒，所有日期字段取同一时间，留存未给终点时合成订单不晚于最晚下单时间（否则观察终点会变）；`window_split` 用于订单级 summary/trend/ranking、品类销售（订单数、明细数、金额）与送达延迟（两个计数），复购率与留存按客户跨期间计数、share 与 comparison 含比值，均不适用。变换后的规范化表经同一服务端投影成输入表，通过 `input_files` 写入同一只读挂载。离线测试（`tests/datalab/test_olist_verification.py`）确认固定代码与框架版本都满足关系，含终点的期间、依赖行序的归州和不可加的总计分别被三条关系拦下；开启后的金丝雀测试同样不泄漏数据。

真实数据实测（`scripts/verify_olist.py`，生产限额 256 MB、1 核、30 秒，七个代表性计划）：

| 测量 | 结果 |
| --- | --- |
| 固定分析在容器内的峰值 | 进程 RSS 94–147 MiB，cgroup `memory.peak` 99–152 MiB；容器运行（含创建与清理）1.6–2.3 秒 |
| `run_fixed` 完整核验（投影、执行、两份参考、下载表、不变量） | 全部通过，宿主墙钟 5.2–10.7 秒 |
| 同上并开启蜕变关系 | 全部通过，14.8–23.3 秒 |

峰值最高的是按州的订单金额排行（订单、客户、明细三张表），离 256 MB 仍有约 100 MiB 余量，内存与时间限额不变。宿主侧的两份参考每次都从数据版本目录重新读取完整表，是墙钟时间的主要部分。

反馈与提示注入：新检查的问题只含字段路径、关系名和封闭词表里的问题类型，不含数值。工作流转发前对检查名（`CHECK_NAMES`）、关系名（`RELATION_NAMES`）和问题类型（`ISSUE_NAMES`）逐项白名单过滤，词表外一律换成 `other`；路径仍经 `safe_path` 处理。按项目规则，第二份数值实现必须是服务端代码，不能让模型再算一遍。

不变量与蜕变关系看不到计划读错题（参考跟着计划走），也看不到恰好自洽的错误口径；Analyst 提示没有为新问题类型补充说明，问题名本身是自解释的英文词。

## 业务记忆

- 范围：每条记忆都属于 `project_id`、`dataset_family` 与 `schema_signature`，先过滤范围再检索。数据家族描述语义，不从 schema 自动推断；字段相同但语义不同时必须重新确认。
- 生命周期：指标口径与字段语义先以 `pending` 写入，只有用户明确确认或可信预配置后才能被默认使用；新版本确认后旧版本变为 `superseded`，内容不覆盖；失效标记 `invalidated` 并记录操作人和时间。默认检索只返回已确认且生效的条目，数值计算只从已确认的 metric 建立快照，报告保留快照，重放时不会重新选择“最新”口径。
- 并发：确认时必须带上看到的当前 `memory_id`；页面旧版本或并发确认触发 `MemoryConflict`（HTTP 409）。每个范围使用短事务与事务级咨询锁，同名当前确认版本有部分唯一索引，SQL 全部参数绑定。
- 字段语义：上传 CSV 时按字段映射写入 `field_semantics` 待确认候选，内容相同时不重复写版本。当前版本中字段语义只作可追溯记录，不进入 Planner 交接或计算。Olist 导入不写字段语义候选。
- Olist 口径：范围由结构签名区分，纳入状态只能取 Olist 词表（单表口径只能取 paid/refunded/cancelled），并可带 `amount_source`（payments 或 items），确认后进入口径快照（`MetricSnapshot.amount_source`）与模型可见的口径视图。没有确认金额来源的口径不能用于订单级金额分析，任务创建或确认时返回 400。
- `initialize()` 建表并用 `ADD COLUMN IF NOT EXISTS` 补齐后加的列，可重复执行；它不是通用迁移工具，改列类型或删列需要显式迁移。

## 任务持久化与派发

- 任务快照、状态与产物索引在 PostgreSQL 中；Redis/Celery 只传任务 ID。
- worker 通过数据库租约（30 秒，心跳续期）领取任务，重复投递由原子租约去重；旧 worker 不能覆盖终态。每次领取同时把任务行的 `fence` 加一。
- 投递失败时任务保留为待执行，dispatcher（Celery beat，每 5 秒）从数据库补发。
- 租约过期的处理（2026-10 起）：此前一律标为失败；现在按检查点恢复，每个任务最多一次。
  - 检查点就是每次状态转换时落库的任务快照，另外保存了下一次 Analyst 调用要带的定位反馈。dispatcher 发现租约过期后，把任务重新排队并在时间线写一条记录，新 worker 领取后调用 `Workflow.recover`，按快照状态接续：规划中重新规划；执行中带着保存的反馈重新生成并执行代码；核验中直接重跑确定性的独立核验，不再调用 Analyst。
  - 不恢复容器里跑了一半的 Python：半成品不可复用，重新生成代码。已用的调用、token 和时间不归零；检查点之后那次在途调用的用量没有落库，所以恢复后可能少记最多一次调用。Reviewer 若在崩溃前已被调用，恢复后会再调用一次。
  - 恢复次数用尽后再次过期，仍标为失败（`WORKER_LEASE_EXPIRED`）并保留证据；租约过期时若已有取消请求，按取消收尾。
  - 没有做的：崩溃时遗留的孤儿容器不会被主动回收，依赖容器自身的超时与命名约定；这一点没有专门验证。
- 旧 worker 迟到的写入：任务行的写入都带 `WHERE lease_id=…`，租约失效即被拒绝。租约 UUID 对同一行已经等价于比较令牌，`fence` 的用处在数据库行之外：产物文件无法做条件写，所以报告文件名带 fence（`report.<fence>.json`），旧 worker 的迟到写入落在它自己的文件里，不会覆盖新领取者的；产物索引行登记时同样校验租约，并只允许被更大的 fence 覆盖，下载只认索引。图表和说明由同一份 `result.json` 确定性生成，重复写入内容相同，没有加围栏。
- 任务创建支持 `Idempotency-Key` 请求头：同一项目内相同的键和相同的请求内容返回原任务（响应带 `replayed`），不创建、不派发；相同的键但内容不同返回 422；并发重试由唯一索引和 `ON CONFLICT` 保证只创建一次。键只对任务创建有意义，取消和确认本身对重复请求是幂等的。
- 取消在终态提交前以行锁检查，执行期间通过心跳检查；取消先于成功提交确认时不会发布成功。
- 报告引用数据版本、口径快照、代码、执行清单、核验记录和结果产物 ID。

## 图表与说明

核验通过后，可信 worker 中的 `artifacts/derive.py` 只读取已通过独立核验的 `result.json` 与计划，确定性生成图表与说明；生成失败只写 `derived_error`，不推翻核验结论。

- 图表：matplotlib Agg，最多 3 张 PNG。summary 无图；排行和分组为金额、订单数横向条形，占比额外加占比图；趋势为每日金额折线（只标最高点与末点）和每日订单数柱形，一年内缺失日期按 0 补齐；期间对比为上期/本期金额与订单数；数据质量为各状态订单数。超过 15 组时合并为“其他”。
- 可复现：固定 rcParams、关闭 mathtext、PNG 不写 Software 元数据；同一 `result.json`、matplotlib 次版本与字体输出相同字节。字体随机器变化会导致 PNG 不同。
- 安全：分组名来自用户 CSV，去控制字符、截断后只作纯文本刻度。只有 `.png` 图表允许 `?inline=true` 内联返回，其余产物一律作为附件下载。
- 说明：`artifacts/narrative.py` 按分析类型套固定模板生成 2–3 句中文，每句记录引用的结果字段路径。首句写明口径版本、纳入状态与期间；分组类注明“描述性分解，不推断原因”。
- Olist：订单级金额分析沿用单表模板（州、下单日期、下单月份分组；按月趋势画月度折线与柱形），说明首句另写订单金额来源。复购率与送达延迟按州时画比值条形图，标签同时写出分母（下单客户数或已送达单数），不分组时画两项计数；留存画各首单月份的新客户数与首单后第 1 个月仍下单的比例（柱上标比例与人数，注明右删失、不显示的 cohort）；品类销售画商品金额与明细件数。比值轴刻度只取 1、2、5 的十进倍数。报告结论每类一条：金额合计、复购率、延迟率、商品金额或 cohort 人数。

## 安全边界

- 首版是单用户、回环地址上的本地演示，不是公网多租户平台。API 校验项目归属、Host，以及写请求的 Origin 与客户端标识；这个标识不是登录凭据。对公网提供服务前，必须补齐身份认证、授权、CSRF 防护、传输加密和审计。
- 发送给模型的只有问题、聚合概况和已确认口径的白名单视图，不含订单明细，也不含上传时填写的自由文本；表格文本在提示中声明为数据而非指令。防线靠的是“不可信文本没有路径进入提示”，而不是指望模型不被劝服；用户的问题是唯一进入提示的自由文本，模型被它带偏时，输出侧校验仍会拒绝改动口径的计划。这不排除模型在问题范围内读错题意，那类错误由 Reviewer 和独立核验部分覆盖，见评测文档的静默错误率。
- 应用 API、队列和数据库本身不运行在分析沙箱中。若可信宿主进程或本机账户被攻陷，回环地址与普通容器隔离不能保证数据安全。上述描述是实现配置，不是绝对的安全承诺。
