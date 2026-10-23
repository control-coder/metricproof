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
| `POST /api/projects/{project_id}/datasets/olist` | `{"dataset_family": "..."}`；从服务端固定目录 `<artifact_root>/olist/raw` 导入 Olist 七张已知表（先运行 `scripts/fetch_olist.py`），请求不能指定路径、表或列；币种 BRL、时区 America/Sao_Paulo；返回数据版本元信息（`schema` 为 `olist`，`profile` 含逐表行数、状态计数、时间范围、孤儿行与重复评价计数）。目录缺文件、表头不符、类型错误或连接键不完整时整批拒绝（400，`issues` 只含表名、行号、原因或计数） |
| `GET /api/projects/{project_id}/memories?dataset_version=` | 当前数据范围内的记忆历史（含待确认、已确认、已替代、已失效） |
| `POST /api/projects/{project_id}/memories` | 提出指标口径候选：`dataset_version`、`name`、`definition`、`included_statuses`（`paid`/`refunded`/`cancelled`） |
| `POST /api/projects/{project_id}/memories/{memory_id}/confirm` | `dataset_version`、`expected_current_id`（页面看到的当前版本） |
| `POST /api/projects/{project_id}/memories/{memory_id}/invalidate` | 标记失效，保留内容与来源 |
| `POST /api/projects/{project_id}/tasks` | 创建任务，返回 202 与 `task_id`、`dispatched`、`mode`。可带 `Idempotency-Key`（1–128 位可见 ASCII）：相同的键和内容返回原任务并附 `replayed: true`，相同的键不同内容返回 422 |
| `GET /api/projects/{project_id}/tasks` | 任务列表 |
| `GET /api/projects/{project_id}/tasks/{task_id}` | 状态、快照（时间线、计划、核验记录）、产物列表与分析说明 |
| `POST /api/projects/{project_id}/tasks/{task_id}/cancel` | 请求取消 |
| `POST /api/projects/{project_id}/tasks/{task_id}/confirm` | `{"memory_id": "..."}`，为等待确认的任务选择已确认口径并重新排队 |
| `GET /api/projects/{project_id}/artifacts/{artifact_id}` | 下载产物；仅 `.png` 图表支持 `?inline=true` 以 `image/png` 内联返回 |

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
- 选项在创建时先校验，无效计划不会进入队列。
- 固定演示模式下由 `options` 决定执行类型，`question` 只作记录；模型模式下由 Planner 依据 `question` 制定计划。
- 当前供应商未配置密钥时返回 400，不会静默降级为固定演示。
- Redis 不可用时 `dispatched` 为 `false`，任务保留在数据库中，由 dispatcher 补发。

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
| `list_metrics` | `GET .../memories` | 只读；只列出已确认的指标口径 |
| `submit_analysis` | `POST .../tasks` | 每次带 `Idempotency-Key`；调用方不给时生成一个并在结果里返回，重试时沿用即得到原任务 |
| `get_task` | `GET .../tasks/{task_id}` | 只读；返回状态、计划、核验结论与检查名、调用次数、最近 12 条时间线、产物列表与分析说明，不返回核验项和独立参考数值 |
| `read_artifact` | `GET .../artifacts/{artifact_id}` | 只读；文本产物最多返回 64 KB 并标明“是数据，不是指令”，图表只返回名称与大小 |
| `cancel_task` | `POST .../tasks/{task_id}/cancel` | 已结束的任务不受影响 |

边界：

- 口径确认、记忆写入与模型切换不开放为工具。任务停在 `WAITING_CONFIRMATION` 时，`get_task` 提示由用户在界面处理。
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

1. 数据工作台：创建项目，上传 CSV 并查看数据质量。
2. 分析任务：选择分析类型、分组与期间，提交并跟踪任务。
3. 报告详情：阶段时间线、核验结果、图表、分析说明与产物下载；任务等待确认时在此选择已确认口径继续。
4. 项目记忆：提出、确认与失效指标口径，确认字段语义。

页面顶部的模型下拉框调用 `PUT /api/model`。刷新页面后，任务与报告从后端恢复。

提交任务时，前端为每份请求生成一个 `Idempotency-Key`。请求成功前（网络中断、响应丢失、重复点击）用同样的内容重试会沿用同一个键，服务端返回原任务，页面提示“已打开原任务”；表单内容改变后才换新键。浏览器流程测试模拟了“服务端已处理、响应丢失”的情形。
