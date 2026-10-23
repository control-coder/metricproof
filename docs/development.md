# 开发与运行

所有命令都在仓库根目录执行。示例使用 Windows PowerShell，这也是唯一实测过的宿主环境；其他系统需要自行核对 Docker 回环端口、文件权限与 Node 版本。

## 环境要求

- Python 3.12（conda 环境 `datalab`）
- Node.js 与 npm（CI 使用 Node 24）
- Docker 引擎，运行 Linux 容器；`docker version` 必须能返回 Server 信息
- 基础镜像：`python:3.12.12-slim-bookworm`、`postgres:17-alpine`、`redis:7-alpine`

## 安装

```powershell
conda env create -f environment.yml   # 已有环境时跳过
conda activate datalab
python -m pip install --no-build-isolation -e .
npm ci --prefix frontend --no-audit --no-fund
npm run build --prefix frontend
docker build -t datalab-executor:py312-v1 -f scripts/executor/Dockerfile .
```

PowerShell 执行策略阻止 `npm.ps1` 时，改用 `npm.cmd`。如果 Docker Hub 不可访问，可以只为这次构建显式指定一个经过核实的镜像源，例如 `--build-arg PYTHON_IMAGE=mirror.gcr.io/library/python:3.12.12-slim-bookworm`，不需要修改系统全局设置。执行镜像只在构建时安装依赖，运行时不联网。

## 启动本地演示

```powershell
python -B scripts/local_services.py start   # 本项目专用 PostgreSQL/Redis，回环随机端口
python -B scripts/check_environment.py      # 核对镜像与服务
python -B scripts/serve.py                  # API、worker 与 dispatcher
```

打开 <http://127.0.0.1:18080>，按以下顺序操作：

1. 在“数据工作台”创建项目，上传 `examples/orders.csv`。
2. 在“分析任务”提交一个汇总任务。没有确认口径时，任务会停在等待确认。
3. 在“项目记忆”提出“只含 paid”的口径并确认。
4. 回到“报告详情”选择该口径继续，等待独立核验成功，然后下载结果表。

停止和清理：

- `Ctrl+C` 停止 `serve.py` 启动的子进程。
- `python -B scripts/local_services.py stop` 只停止本项目的容器，保留数据库卷和本机配置。
- 连接配置保存在被忽略的 `artifacts/local/runtime.json`，再次运行 `start` 会复用它，不会清空数据。在其中加入 `"metamorphic_checks": true` 可开启蜕变关系核验（默认关闭，每个任务多约 4 次沙箱运行，见[架构文档](architecture.md#核验)），重启 worker 后生效。
- 18080 端口被占用时，请先排查占用者。

### Olist 多表数据（可选）

真实多表数据来自 Olist 公开数据集（CC BY-NC-SA 4.0，见 `THIRD_PARTY_NOTICES.md`），不随仓库分发，需要先下载到被忽略的目录：

```powershell
python -B scripts/fetch_olist.py   # 写入 artifacts/olist/raw/，七张表共约 65 MB，逐个校验字节数、SHA-256 与记录数
```

下载失败会重试；任一校验不符时删除该文件并以非零状态退出。已存在且校验通过的文件不重复下载。导入只读取服务端固定的这个目录（`<artifact_root>/olist/raw`），请求不能指定路径：

```powershell
$headers = @{ 'X-Datalab-Client' = 'local-ui' }
Invoke-RestMethod -Method Post -Headers $headers -ContentType 'application/json' `
  -Uri "http://127.0.0.1:18080/api/projects/<project_id>/datasets/olist" -Body '{"dataset_family":"olist-brazil"}'
```

本机实测导入全部七张表约 9 秒；校验规则与概况字段见[架构文档](architecture.md#多表数据olist)。

只想验证容器闭环、不启动服务时，可以运行：

```powershell
python -B scripts/demo_fixed.py
```

结果写入 `artifacts/demo/runs/<run_id>/`，包含计划、执行清单、JSON、CSV、核验报告、图表与说明。示例使用可信演示口径 `trusted-demo-paid-v1`，只计算 paid。

## 模型配置

三角色可以使用 MiMo 或 DeepSeek 的 OpenAI 兼容 `chat/completions` 接口，也可以使用不调用模型的固定演示（`guided`）。

```powershell
Copy-Item .env.example .env   # 在 .env 中填写 DATALAB_MIMO_API_KEY 或 DATALAB_DEEPSEEK_API_KEY
python -B scripts/switch_model.py            # 查看当前供应商及是否已配置密钥
python -B scripts/switch_model.py deepseek
python -B scripts/switch_model.py mimo
python -B scripts/switch_model.py guided
```

| 供应商 | 默认 BASE_URL | 默认 MODEL |
| --- | --- | --- |
| mimo | `https://api.xiaomimimo.com/v1` | `mimo-v2.6-flash` |
| deepseek | `https://api.deepseek.com` | `deepseek-flash` |

- 密钥也可以通过进程环境变量注入，环境变量优先于 `.env`；接口和页面只显示是否已配置。
- 端点必须是 HTTPS。供应商不支持 `response_format=json_object` 时，把对应的 `*_JSON_MODE` 设为 `false`。
- 切换只影响新建任务，已创建的任务仍使用创建时记录的模型；worker 每个任务都重新读取 `.env`，不需要重启。没有 `.env` 时使用固定演示。
- 两款 flash 模型都是推理模型，思考 token 计入输出上限。实测 DeepSeek 的 Analyst 单次需要 8k–13k 输出 token，所以 `*_MAX_OUTPUT_TOKENS` 默认 16384；改为 4096 会在思考阶段耗尽，返回空正文。
- 预算与审查限额见 `.env.example` 中的 `DATALAB_MODEL_MAX_*`、`*_REVIEWER_*`。

## 测试

```powershell
python -m ruff check src tests scripts evals
python -B -m unittest discover -s tests/datalab -v
npm run build --prefix frontend
```

默认单元测试不依赖 Docker、数据库或模型。需要真实服务的测试通过开关启用；未启用时显示 skipped，不算通过：

| 命令 | 覆盖 |
| --- | --- |
| `$env:DATALAB_DOCKER_TESTS='1'; python -B -m unittest discover -s tests/datalab -p test_execution.py -v` | 真实容器隔离：非 root、seccomp、只读输入、断网、越界、符号链接、内存与输出限制、超时、取消与清理 |
| `python -B scripts/test_postgres.py` | 在临时 PostgreSQL 容器中测试跨连接复用、历史版本、并发确认只有一个胜者 |
| `python -B scripts/test_app.py` | 真实 PostgreSQL、Redis/Celery 与 Docker 的应用集成：确认恢复、下载、重复投递、项目隔离、租约过期后的检查点恢复（含模拟 worker 消失后的端到端恢复）、产物围栏、Idempotency-Key、取消竞争、图表内联 |
| `node frontend/tests/browser-flow.mjs` | 用本机 Edge 跑完整的四页面流程，包括桌面与移动宽度，以及“响应丢失后重试沿用同一个 Idempotency-Key 并拿回原任务”；设置 `DATALAB_SHOT_DIR` 时把四页截图写到该目录（README 的截图用 `docs/images`） |
| `python -B scripts/verify_scale.py` | 十万行模拟订单的规模验证 |
| `python -B evals/run_suite.py` | 固定种子评测：20 项数值、8 项记忆、8 项故障，不调用模型 |

`tests/datalab/test_execution.py` 的 Docker 用例包括蜕变关系在真实容器中的重跑；`test_verification_layers.py` 离线覆盖不变量、两份参考的性质测试（240 组随机数据与计划）和蜕变关系。

运行完后用 `Remove-Item Env:DATALAB_DOCKER_TESTS` 清除开关。`scripts/test_postgres.py` 只清理自己创建的容器；`DATALAB_POSTGRES_TESTS=1` 配合 `DATALAB_TEST_DATABASE_URL` 也可以指向一个专用测试库，但不要指向含业务数据的库。

`python -B evals/injection.py` 是错误注入基准，默认只需要 Docker 与执行镜像、不调用模型（本机 6 路并发约 20 分钟），加 `--reviewer` 才会调用 MiMo；离线测试为 `tests/datalab/test_injection.py`。`evals/` 下其余脚本会调用付费模型，见 [评测与验证](evaluation.md)；`evals/report.py`、`evals/stats.py` 与 `evals/heldout.py` 不发请求，其统计和题集冻结由 `tests/datalab/test_eval_stats.py` 离线检查。

## CI

`.github/workflows/ci.yml` 在推送到 `main` 和每个 Pull Request 时运行：

- backend：安装本包与 `ruff==0.13.3`，执行 `ruff check`（E9 + pyflakes）和离线单元测试。
- integration：GitHub 服务容器提供 PostgreSQL 17 与 Redis 7，作业内构建 `scripts/executor` 镜像，依次运行真实 Docker 隔离与模拟模型闭环、PostgreSQL 记忆、Celery 应用集成和固定种子评测（20 数值、8 记忆、8 故障）。不调用模型，不需要密钥；`scripts/ci_prepare.py` 只按环境变量生成一次性运行配置。评测 JSON 与 worker 日志作为构件保留 14 天。
- frontend：执行 `npm ci`（跳过 Playwright 浏览器下载）和 `npm run build`（`tsc --noEmit` + Vite）。

浏览器流程、真实模型评测和十万行规模验证不在 CI 中运行。integration 作业在 Linux 托管 runner 上运行，与本机 Windows 的 Docker Desktop 环境不同。它第一次运行就暴露了一个本机看不到的问题：测试 worker 依赖本机 .env，CI 里没有而失败；现在测试给 worker 生成独立配置，并在移走本机 .env 的条件下复现验证。修正后该作业在真实 Linux runner 上通过，覆盖受限容器、PostgreSQL、Redis/Celery、租约过期后的恢复和固定种子评测。失败时测试输出和 worker 日志会写成注解。

## 排障

- `CLEANUP_PENDING`：读取对应执行清单中的 `container_name`，只检查和移除该容器；不要运行清理全部 Docker 资源的命令。
- 宿主进程被强制终止后，自动回收遗留容器的功能尚未实现，需要按执行清单人工核对。
- Docker 不可用时任务记为失败（未安装 Docker 时失败原因为 `SANDBOX_UNAVAILABLE`，并释放租约），不会回退到宿主机执行。
- 运行产物默认保留在 `artifacts/` 中，不会自动删除数据库、用户文件或证据。
