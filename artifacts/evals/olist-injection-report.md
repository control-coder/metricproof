# Olist 错误注入基准结果

提交 `b0f7005`，日期 2026-10-23，变异目录指纹 `d0f24908ec6e…`，Olist 留出集指纹 `aad2fb967cf3…`，墙钟 1360.2 秒。数据为真实 Olist 全量（一份）。区间为 95% Wilson，同一算子在不同用例上不是独立样本，区间只作参考。

27 个算子 × 适用的 36 个留出集计划，共 324 个变异用例：暴露 178，未暴露 126，执行失败 20。正确代码在 36 个用例上被任一层误判 0 次。

| 核验层 | 检出 / 暴露（比例，95% 区间） |
| --- | --- |
| independent_reference | 178/178（100.0%，[97.9, 100.0]） |
| group_total | 26/110（23.6%，[16.7, 32.4]） |
| download_table | 152/178（85.4%，[79.5, 89.8]） |
| invariants | 76/178（42.7%，[35.7, 50.0]） |
| reference_agreement | 0/178（0.0%，[0.0, 2.1]） |
| metamorphic | 39/178（21.9%，[16.5, 28.5]） |
| reference_free_union | 105/178（59.0%，[51.6, 66.0]） |

通过全部层却是错的：0；只看不依赖参考的两层会漏过 73 个。蜕变关系各自检出：permutation 0、irrelevant_rows 28、window_split 11。

| 算子 | 用例 | 暴露 | 未暴露 | 执行失败 | 参考 | 不变量 | 蜕变 | 不依赖参考的并集 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| amount_source_swapped | 20 | 0 | 0 | 20 | 0 | 0 | 0 | 0 |
| category_drops_unknown | 4 | 4 | 0 | 0 | 4 | 0 | 0 | 0 |
| category_freight_in_price | 4 | 4 | 0 | 0 | 4 | 0 | 0 | 0 |
| category_total_orders_summed | 4 | 4 | 0 | 0 | 4 | 0 | 0 | 0 |
| category_untranslated_unknown | 4 | 3 | 1 | 0 | 3 | 0 | 0 | 0 |
| cohort_censoring_off_by_one | 4 | 4 | 0 | 0 | 4 | 4 | 0 | 4 |
| cohort_customer_id | 4 | 4 | 0 | 0 | 4 | 0 | 0 | 0 |
| cohort_last_order | 4 | 4 | 0 | 0 | 4 | 0 | 0 | 0 |
| cohort_start_truncates_first | 4 | 2 | 2 | 0 | 2 | 0 | 0 | 0 |
| date_key_month | 12 | 2 | 10 | 0 | 2 | 2 | 0 | 2 |
| delivery_ignores_status | 4 | 0 | 4 | 0 | 0 | 0 | 0 | 0 |
| delta_sign_flipped | 4 | 4 | 0 | 0 | 4 | 4 | 0 | 4 |
| end_inclusive | 32 | 0 | 32 | 0 | 0 | 0 | 0 | 0 |
| growth_wrong_base | 4 | 4 | 0 | 0 | 4 | 4 | 0 | 4 |
| last_group_dropped | 20 | 12 | 8 | 0 | 12 | 12 | 5 | 12 |
| late_timestamp_level | 4 | 4 | 0 | 0 | 4 | 0 | 0 | 0 |
| ranking_ascending | 4 | 4 | 0 | 0 | 4 | 4 | 0 | 4 |
| ratio_five_places | 20 | 20 | 0 | 0 | 20 | 20 | 0 | 20 |
| ratio_float | 20 | 0 | 20 | 0 | 0 | 0 | 0 | 0 |
| repeat_customer_id | 4 | 4 | 0 | 0 | 4 | 0 | 0 | 0 |
| repeat_state_last_order | 4 | 2 | 2 | 0 | 2 | 0 | 0 | 0 |
| repeat_threshold_three | 4 | 4 | 0 | 0 | 4 | 2 | 0 | 2 |
| spurious_group | 20 | 12 | 8 | 0 | 12 | 12 | 0 | 12 |
| start_exclusive | 32 | 1 | 31 | 0 | 1 | 0 | 1 | 1 |
| status_filter_dropped | 32 | 28 | 4 | 0 | 28 | 0 | 28 | 28 |
| status_hardcoded_delivered | 32 | 28 | 4 | 0 | 28 | 0 | 0 | 0 |
| total_in_reais | 20 | 20 | 0 | 0 | 20 | 12 | 5 | 12 |

口径锁：纳入状态或订单金额来源与已确认口径不同的计划被拒绝 56/56。
