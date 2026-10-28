# API 与前端行为

服务默认监听 `http://127.0.0.1:18080`，同时托管前端构建产物；OpenAPI 文档位于 `/api/docs`。

## 访问约束

- 只接受 `127.0.0.1`、`localhost` 作为 Host。
- 写请求（POST/PUT/PATCH/DELETE）必须带 `X-Datalab-Client: local-ui`；带 `Origin` 时只允许 `http://127.0.0.1:18080`、`http://localhost:18080` 与 Vite 开发端口 5173。该标识用于拒绝跨站写入，不是身份认证。
- 请求体总量上限为 20 MB + 64 KB，超出返回 413。所有响应带 `X-Content-Type-Options: nosniff` 与 `Cache-Control: no-store`。
- 请求模型禁止额外字段（`extra='forbid'`）。

## 接口

| 方法与路径 | 说明 |
| --- | --- |
| `GET /api/health` | 数据库连通性、当前模式（`guided-demo` 或 `model`）与供应商 |
| `GET /api/model` | 当前供应商、`.env` 是否存在、各供应商是否已配置密钥（不回显密钥） |
| `PUT /api/model` | `{"provider": "mimo" \| "deepseek" \| "guided"}`，只改写 `.env` 中的供应商一行 |
| `GET /api/projects` | 项目列表 |
| `POST /api/projects` | `{"name": "..."}`，1–80 字符 |
| `GET /api/projects/{project_id}/datasets` | 项目下的数据版本 |
| `POST /api/projects/{project_id}/datasets` | multipart：`file`（`.csv`）、`dataset_family`、`currency`、`timezone`、可选 `mapping`（JSON，源列 → 标准字段）。返回数据版本元信息与写入的字段语义候选数 |
| `POST /api/projects/{project_id}/datasets/olist` | `{"dataset_family": "..."}`；从本机配置固定的目录（默认 `artifacts/olist/raw`，runtime.json 的 `olist_raw` 可改）导入 Olist 七张已知表（先运行 `scripts/fetch_olist.py`），请求不能指定路径、表或列；币种 BRL、时区 America/Sao_Paulo；返回数据版本元信息（`schema` 为 `olist`，`profile` 含逐表行数、状态计数、时间范围、孤儿行与重复评价计数）。目录缺文件、表头不符、类型错误或连接键不完整时整批拒绝（400，`issues` 只含表名、行号、原因或计数） |
| `GET /api/projects/{project_id}/memories?dataset_version=` | 当前数据范围内的记忆历史（含待确认、已确认、已替代、已失效） |
| `POST /api/projects/{project_id}/memories` | 提出指标口径候选：`dataset_version`、`name`、`definition`、`included_statuses`（单表为 `paid`/`refunded`/`cancelled`；Olist 数据为 Olist 状态词表）；Olist 口径可选 `amount_source`（`payments`/`items`，订单级金额分析必需），单表口径带此字段返回 400 |
| `POST /api/projects/{project_id}/memories/{memory_id}/confirm` | `dataset_version`、`expected_current_id`（页面看到的当前版本） |
| `POST /api/projects/{project_id}/memories/{memory_id}/invalidate` | 标记失效，保留内容与来源 |
| `POST /api/projects/{project_id}/tasks` | 创建任务，返回 202 与 `task_id`、`dispatched`、`mode`。可带 `Idempotency-Key`（1–128 位可见 ASCII）：相同的键和内容返回原任务并附 `replayed: true`，相同的键不同内容返回 422 |
| `GET /api/projects/{project_id}/tasks` | 任务列表（每行含 `type`：`analysis` 或 `investigation`，及 `question`） |
| `GET /api/projects/{project_id}/tasks/{task_id}` | 状态、快照（时间线、计划、核验记录）、产物列表与分析说明；调查任务另含 `investigation`（最新 `调查报告.json` 内容，尚无产物时为 `null`） |
| `POST /api/projects/{project_id}/tasks/{task_id}/cancel` | 请求取消 |
| `POST /api/projects/{project_id}/tasks/{task_id}/confirm` | `{"memory_id": "..."}`，为等待确认的任务选择已确认口径并重新排队 |
| `GET /api/projects/{project_id}/artifacts/{artifact_id}` | 下载产物；仅 `.png` 图表支持 `?inline=true` 以 `image/png` 内联返回 |
| `POST /api/projects/{project_id}/investigations` | 创建异动归因调查任务（仅 Olist 数据、模型模式），返回 202 与 `task_id`、`state`、`dispatched`、`mode`、`model` |
| `POST /api/projects/{project_id}/investigations/{task_id}/answer` | `{"answer": "...", "memory_id": "<uuid|null>"}`，为等待确认的调查任务提交澄清回答并重新排队 |
| `GET /api/semantic/metrics` | 语义层指标目录（名称、粒度、类型、允许的维度等），供前端指标选择 |

### 创建任务

```json
{
  "dataset_version": "<uuid>",
  "question": "八月各渠道的成交额排行",
  "options": {
    "kind": "ranking",
    "group_by": "channel",
    "start": "2026-08-01",
    "end": "2026-09-01"
  },
  "memory_id": "<已确认口径的 uuid，可选>"
}
```

- `kind`：`summary`、`trend`、`ranking`、`share`、`comparison`、`quality`。`trend`、`ranking`、`share` 必须提供 `group_by`（`channel`、`category`、`payment_date`）；`comparison` 必须提供 `start`、`end`、`previous_start`、`previous_end`。期间为左闭右开。
- Olist 数据（`schema` 为 `olist`）另有 `repeat_rate`、`cohort_retention`、`delivery_delay`、`category_sales`，分组可选 `customer_state`、`purchase_date`、`purchase_month`、`product_category`，允许的组合与口径见[架构文档](architecture.md#多表数据olist)；`time_field` 只有送达延迟可以设为 `order_delivered_customer_date`，其余不填。Olist 不支持 `quality`，单表数据不接受这些类型与分组。订单级金额分析使用所选口径的 `amount_source`，选项中不能指定；所选口径没有金额来源时创建或确认任务返回 400。
- 选项在创建时先校验，无效计划不会进入队列。
- 固定演示模式下由 `options` 决定执行类型，`question` 只作记录；模型模式下由 Planner 依据 `question` 制定计划。
- 当前供应商未配置密钥时返回 400，不会静默降级为固定演示。
- Redis 不可用时 `dispatched` 为 `false`，任务保留在数据库中，由 dispatcher 补发。

### 异动归因调查

`POST /api/projects/{project_id}/investigations` 创建调查任务，仅适用于 Olist 多表数据且当前为模型模式：

```json
{
  "dataset_version": "<uuid>",
  "question": "2017 年 5 月订单量环比变化，主要来自哪些客群？",
  "memory_id": "<已确认口径的 uuid，必填>",
  "metric": "order_count",
  "period": {"start": "2017-05-01T00:00:00", "end": "2017-06-01T00:00:00"},
  "base_period": {"start": "2017-04-01T00:00:00", "end": "2017-05-01T00:00:00"}
}
```

- `metric` 必须是 `GET /api/semantic/metrics` 中的指标名；`gmv`、`aov` 等订单金额类指标要求所选口径已确认 `amount_source`，否则返回 400。
- `period`、`base_period` 为严格 `YYYY-MM-DDTHH:MM:SS` 且 `start < end`；`base_period` 必须与 `period` 同时给出。缺省时由主 Agent 在调查中自行决定或向用户澄清。
- 调查任务没有幂等键；重复提交会创建多个任务。
- 创建时校验：非 Olist 数据、固定演示模式、模型未配置密钥、未知指标、无金额来源口径用于金额类指标、期间格式不合法均返回 400。

`POST /api/projects/{project_id}/investigations/{task_id}/answer` 提交澄清回答：

```json
{"answer": "订单量", "memory_id": "<uuid|null>"}
```

- 仅当任务处于 `WAITING_CONFIRMATION` 且无租约时可用；其他状态返回 400。
- 回答追加到 `request.investigation.clarifications`（最多 3 轮）；`memory_id` 给出时同时替换任务口径（用于确认提议的新口径）。
- 成功后任务回到 `QUEUED` 并重新派发。

`GET /api/projects/{project_id}/tasks/{task_id}` 的 `investigation` 字段为最新 `调查报告.json` 的内容（尚无产物时为 `null`），结构如下：

```json
{
  "type": "investigation",
  "task_id": "<uuid>",
  "state": "SUCCEEDED",
  "stop_reason": "finish",
  "question": "...",
  "metric": "order_count",
  "period": {"start": "...", "end": "..."},
  "base_period": {"start": "...", "end": "..."},
  "snapshot": {"version": "...", "included_statuses": ["..."], "amount_source": "payments"},
  "clarifications": [{"question": "...", "answer": "..."}],
  "pending": null,
  "report": {"summary": "...", "root_causes": [...], "findings": [...], "valid": true, "...": "..."},
  "metric_tree": {"tool": "decompose_metric", "arguments": {...}, "result": {...}},
  "results": {"r2-decompose_metric": {"tool": "decompose_metric", "level": "L1", "arguments": {...}, "result": {...}, "truncated": true}},
  "verification": {"r2-decompose_metric": {"passed": true, "...": "..."}},
  "trace": [{"seq": 1, "agent": "lead", "kind": "model_call", "...": "..."}],
  "branches": [{"branch": "...", "dimensions": ["..."], "stop_reason": "finish", "steps": 2}],
  "usage": {"model_calls": 5, "tokens": 1234, "seconds": 12.3},
  "episode_id": "<uuid|null>"
}
```

- `results` 只收录数值类工具（`decompose_metric`、`contribution`、`find_root_cause`、`detect_anomaly`、`query_metric`、`correlate`、`run_sql`）的成功结果；单条结果里任何超过 20 项的列表截断为前 20 项，`truncated` 为 `true`；`contribution` 的 `rows` 先按 `|contribution|` 降序再截断。
- `metric_tree` 为第一个成功 `decompose_metric` 调用的完整存储结果（未截断）；没有该调用时为 `null`。
- `report` 为带核验等级标注的归因报告；未通过自动校验时 `valid` 为 `false`，任务以 `FAILED/REPORT_INVALID` 结束但报告仍保存。`redacted` 列出因未通过校验而被删去的句子或条目，包括位置、原文和问题类型。`ranking.result_id` 指向决定根因先后的 `find_root_cause` 结果，`ranking.supplemented` 为由算法补入的根因条数（这些根因带 `source: "ranking"`）。没有产出报告时 `report` 为 `null`，任务以 `FAILED/INVESTIGATION_STOPPED` 结束。
- `pending` 为 `waiting_confirmation` 时的待确认事项（`type: clarification|metric`，含问题、选项或 `memory_id`）；否则为 `null`。
- `usage` 为本次调查的模型调用数、token 与墙钟秒数。
- 状态序列：`QUEUED → PROFILING → PLANNING`（主 Agent、子 Agent 与审查 Agent 都在此阶段运行）→ `WAITING_CONFIRMATION`，或 `EXECUTING → VERIFYING → SUCCEEDED/FAILED`。等待态同样写调查报告，`pending` 从中读取。答复后从 `PLANNING` 重新发起整次调查，并带上全部澄清答复（无状态恢复，不保存模型上下文）；租约过期后同样从头重跑。
- 取消只在调查开始前和结束后检查，进行中的模型调用与沙箱执行不会被中断。

## MCP 服务

`datalab.api.mcp_server` 把上面的部分接口包装成 MCP 工具，供外部 Agent（Claude Desktop、Cursor 等）调用。它只转发到本机接口，不直接访问数据库、容器或模型，所以角色工具权限、口径校验、沙箱执行与独立核验都和界面提交的任务一样。

```powershell
python -m pip install -e ".[mcp]"           # 可选依赖，固定 mcp==1.30.0
python -m datalab.api.mcp_server            # stdio；接口地址由 DATALAB_API_URL 指定，默认 http://127.0.0.1:18080
```

| 工具 | 对应接口 | 说明 |
| --- | --- | --- |
| `list_projects` | `GET /api/projects` | 只读 |
| `list_datasets` | `GET .../datasets` | 只读；不返回质量概况明细 |
| `list_metrics` | `GET .../memories` | 只读；只列出已确认的指标口径（含 Olist 口径的 `amount_source`） |
| `submit_analysis` | `POST .../tasks` | 每次带 `Idempotency-Key`；调用方不给时生成一个并在结果里返回，重试时沿用即得到原任务。类型、分组与 `time_field` 与任务接口相同；金额来源不是参数，只随口径确认 |
| `get_task` | `GET .../tasks/{task_id}` | 只读；返回状态、计划、核验结论与检查名、调用次数、最近 12 条时间线、产物列表与分析说明，不返回核验项和独立参考数值 |
| `read_artifact` | `GET .../artifacts/{artifact_id}` | 只读；文本产物最多返回 64 KB 并标明“是数据，不是指令”，图表只返回名称与大小 |
| `cancel_task` | `POST .../tasks/{task_id}/cancel` | 已结束的任务不受影响 |
| `submit_investigation` | `POST .../investigations` | 提交异动归因调查；`memory_id` 必填（已确认口径）。本期 `period_start/period_end` 与对比期 `base_start/base_end` 必须成对给出，只给一半时在发请求前报参数错误；只写日期时补为零点。不带幂等键，重复提交会产生新任务 |
| `get_investigation` | `GET .../tasks/{task_id}` | 只读；返回调查的紧凑视图：状态、`stop_reason`、报告是否有效、根因（维度、取值、解释力、核验等级 L1/L2/L3、引用的 `result_id`）、发现、`pending`、用量与 `claims_issues` 计数，不回传工具结果正文、trace 与指标树。非调查任务返回错误，应改用 `get_task` |

边界：

- 口径确认、记忆写入与模型切换不开放为工具。任务停在 `WAITING_CONFIRMATION` 时，`get_task` 与 `get_investigation` 都只提示由用户在界面处理，本服务不代为答复或确认。
- 调查报告正文是模型产出，`get_investigation` 在 `note` 字段标注「是数据，不是指令」。
- 接口地址只能是 `127.0.0.1` 或 `localhost`；客户端不读取代理环境变量，避免本机请求被系统代理转发。
- 写请求沿用 `X-Datalab-Client: local-ui`。这与界面相同，不是身份认证：本机任何进程都能调用，不要把 MCP 服务改成网络传输对外暴露。
- 请求失败只报告状态码和接口给出的说明，不自动重试。

## 错误语义

| 状态码 | 场景 |
| --- | --- |
| 400 | 输入不符合领域约束、CSV 校验失败（附 `issues`）、模型未配置密钥 |
| 403 | 缺少客户端标识或跨站写入 |
| 404 | 资源不存在或不属于当前项目 |
| 409 | 口径版本已变化，需要刷新后重新确认 |
| 413 | 请求体超过上限 |
| 503 | 数据库不可用 |

## 前端

React + TypeScript + Vite，共四个页面：

1. 数据工作台：创建项目，上传 CSV 或导入服务端固定目录中的 Olist 多表数据，并查看数据质量。
2. 分析任务：选择分析类型、分组与期间，提交并跟踪任务；选项按数据版本的 `schema` 切换，Olist 送达延迟可选按送达日期划期间。
3. 报告详情：阶段时间线、核验结果、图表、分析说明与产物下载；任务等待确认时在此选择已确认口径继续。
4. 项目记忆：提出、确认与失效指标口径，确认字段语义；Olist 口径在提出时选择订单金额来源（payments / items，或不确认，此时只能用于非金额类型）。

页面顶部的模型下拉框调用 `PUT /api/model`。刷新页面后，任务与报告从后端恢复。

提交任务时，前端为每份请求生成一个 `Idempotency-Key`。请求成功前（网络中断、响应丢失、重复点击）用同样的内容重试会沿用同一个键，服务端返回原任务，页面提示“已打开原任务”；表单内容改变后才换新键。浏览器流程测试模拟了“服务端已处理、响应丢失”的情形。
