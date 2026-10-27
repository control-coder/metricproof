"""归因算法（A2）：异动检测、指标拆解、贡献度、根因定位与相关分析，全部为确定性实现。

输入是任意带 ``query(metric, period, compare_period=None, filters=None, group_by=None)`` 方法、
返回语义层引擎（semantic.engine）同结构结果的数据源：语义层引擎本身，或包装
semantic.reference 的独立纯 Python 计算 ``ReferenceSource``。期间统一为 (start, end) 元组，
左闭右开、秒级 ISO（与语义层一致，允许 None 表示不限）。

口径总览（细节见各函数文档字符串）：
- 加法型指标的维度贡献度用 Delta 法：change_i = current_i − base_i，
  contribution_i = change_i / 基期总量，Σchange_i 精确等于总变化（内部全程 Fraction）。
- 比率型指标用对称组间组内拆解（between-within）：structure_i = (w1−w0)·(r0+r1)/2，
  within_i = (w0+w1)/2·(r1−r0)，Σ(structure_i+within_i) 精确等于 R1−R0。
  一期缺席的组：该期份额 w=0，组内比率 r 取另一期的值（组整体进出，全部变化计入结构项，
  无组内信息）；任一期分母为 0 的组：该期份额按 0 处理，r 同上替代。
- 乘法型拆解用 LMDI-I 加法形式：贡献_k = L(V1,V0)·ln(x_k1/x_k0)，浮点，残差输出。
- 异动检测用中位数 ± MAD 的稳健 z 分数（Iglewicz–Hoaglin，阈值 3.5）。
- 多维根因定位参考 Adtributor（Bhagwan et al., NSDI 2014）的解释力 EP 与惊奇度 S。
- 相关分析给 Pearson 与 Spearman（平均秩）系数，只描述相关、不推断因果。

数值输出：金额与计数为整数；比值、贡献度、份额与系数为 6 位小数字符串（与语义层一致）；
LMDI 贡献为浮点数。核验入口 ``verify_attribution`` 用 ReferenceSource 重算逐项比较（L1）。
"""
from datalab.attribution.attribution import (contribution, correlate, decompose_metric, detect_anomaly,
                                             find_root_cause, verify_attribution)
from datalab.attribution.sources import ReferenceSource

__all__ = ["contribution", "correlate", "decompose_metric", "detect_anomaly", "find_root_cause",
           "verify_attribution", "ReferenceSource"]
