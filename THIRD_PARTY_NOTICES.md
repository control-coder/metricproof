# Third-party Notices

## 设计参考：Poirot

- 项目：Poirot，`https://github.com/HezaoHezao/poirot`
- 许可：MIT，Copyright (c) 2026 Poirot Authors
- 关系：OrderProof 在角色职责拆分、角色运行时与容器执行接口等方面参考了 Poirot 的设计思路。本仓库不包含 Poirot 源码、技能或资源文件，也不在运行时依赖 Poirot。

## 运行依赖

以下依赖由包管理器在安装时获取，不随本仓库分发，各自遵循其上游许可证。

| 组件 | 用途 | 许可 |
| --- | --- | --- |
| FastAPI、Uvicorn、python-multipart | HTTP 接口与上传 | MIT / BSD-3-Clause / Apache-2.0 |
| Celery、redis-py | 任务派发 | BSD-3-Clause / MIT |
| psycopg | PostgreSQL 访问 | LGPL-3.0（动态链接使用，未修改） |
| httpx | 模型接口调用 | BSD-3-Clause |
| matplotlib | 图表渲染 | Matplotlib License（PSF 风格） |
| React、React DOM | 前端界面 | MIT |
| Vite、TypeScript、Playwright | 前端构建与浏览器测试（开发依赖） | MIT / Apache-2.0 / Apache-2.0 |

完整依赖与版本见 `pyproject.toml` 与 `frontend/package-lock.json`。

## 数据

`examples/orders.csv` 与评测脚本使用的订单数据均为本项目生成的模拟数据（`evals/run_suite.py` 种子 20260926，`evals/model_compare.py` 种子 20260928），不含真实客户信息，也不来自第三方数据集。

### Olist 巴西电商公开数据集（不随仓库分发）

- 数据：Brazilian E-Commerce Public Dataset by Olist，`https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce`
- 许可：CC BY-NC-SA 4.0（署名-非商业性使用-相同方式共享），`https://creativecommons.org/licenses/by-nc-sa/4.0/`；版权归 Olist 所有
- 获取方式：`scripts/fetch_olist.py` 从 GitHub 镜像 `mara/mara-olist-ecommerce-data` 的固定提交 `663660edff1b4cd711a172027081915771628b9f`（`data/olist-ecommerce/`）下载七张表，按脚本中固定的字节数、SHA-256 与记录数校验；镜像仓库代码为 MIT 许可，数据仍遵循上述原始许可。地理位置表与卖家表不下载。
- 本仓库只包含下载与导入代码，不包含任何 Olist 数据或其派生表；数据只写入被忽略的 `artifacts/`。导入时丢弃评论正文、城市、邮编等未用到的列。使用这些数据须遵守非商业与相同方式共享条款。
- 测试中的 Olist 形状数据（`tests/datalab/olist_bundle.py`）由固定种子生成，不含真实记录。
