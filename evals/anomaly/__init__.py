"""合成异动评测集（A3）：在 Olist 上注入已知异动的 40 个归因案例。

- inject.py：六类注入器与物化；
- cases.py：题集加载与完整性校验（冻结文件为同目录 anomaly_cases.json）；
- score.py：AC@k 与贡献度绝对误差；
- build.py：真实数据上的物化、质量检查、gold 计算与题集冻结脚本（数据不入库）。
"""
