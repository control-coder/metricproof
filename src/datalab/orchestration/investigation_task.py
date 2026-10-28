"""异动归因调查任务（A9）：把 agents.investigation 的一次完整调查接到任务状态机。

状态序列（沿用 workflow.Task.move，时间线与检查点由既有 _on_transition 落库）：
QUEUED → PROFILING → PLANNING（整个主/子/审查 Agent 运行在此阶段）→
  stop_reason == 'waiting_confirmation' → WAITING_CONFIRMATION；
  否则 → EXECUTING（汇总工具结果与报告）→ VERIFYING（核验等级与报告有效性已定，落产物）→
  报告存在且 valid 为 True → SUCCEEDED；报告存在但无效 → FAILED/REPORT_INVALID（报告仍保存）；
  无报告 → FAILED/INVESTIGATION_STOPPED（stop_reason 在时间线）。

取消的已知限制：run_investigation 没有取消钩子，无法中断在途的模型调用或沙箱执行；
本模块只在调查开始前与结束后检查 cancelled。结束后收到取消时按 CANCELLED 收尾，
已生成的调查报告只保存不带成功结论（报告 state 改写为 CANCELLED），与统计任务一致。

租约恢复：调查无状态（resume 不保存模型上下文），恢复到非 QUEUED 检查点时从头重跑。
"""
from __future__ import annotations

import json
from uuid import uuid4

from datalab.agents.investigation import Investigation, InvestigationBudget
from datalab.datasets.service import DatasetStore
from datalab.execution.runner import DockerRunner, ExecutionError
from datalab.memory.postgres import PostgresMemoryRepository
from datalab.memory.service import MemoryService, Scope
from datalab.orchestration.workflow import State, Task
from datalab.roles.llm import ChatClient
from datalab.roles.model_config import load_model_config
from datalab.semantic.snapshot import SemanticSnapshot

# 一次调查的预算（调用数、token、墙钟秒）：按计划第八节约 15 次调用留余量。
BUDGET = (30, 300_000, 600)
# 调查报告收录的数值类工具结果；元数据、分派与 finish 不进入 results。
RESULT_TOOLS = ('decompose_metric', 'contribution', 'find_root_cause', 'detect_anomaly',
                'query_metric', 'correlate', 'run_sql')
# 单条结果里行列表的截断上限；contribution 的 rows 先按 |contribution| 降序再截断。
MAX_ROWS = 20
MAX_CLARIFICATION_ROUNDS = 3


def trim_value(value):
    """结果截断：任何超过 20 项的列表只留前 20 项；contribution 的 rows 先按 |contribution| 降序再截断。

    返回 (裁剪后的值, 是否发生了截断)；只处理 dict/list 嵌套，其余类型原样返回。
    """
    if isinstance(value, list):
        trimmed = []
        truncated = len(value) > MAX_ROWS
        for item in value[:MAX_ROWS]:
            trimmed_item, cut = trim_value(item)
            trimmed.append(trimmed_item)
            truncated = truncated or cut
        return trimmed, truncated
    if isinstance(value, dict):
        trimmed = {}
        truncated = False
        rows = value.get('rows')
        for key, item in value.items():
            if key == 'rows' and isinstance(rows, list) and _has_contribution(rows):
                item = sorted(rows, key=_contribution_key)
            trimmed_item, cut = trim_value(item)
            trimmed[key] = trimmed_item
            truncated = truncated or cut
        return trimmed, truncated
    return value, False


def _contribution_key(row) -> float:
    try:
        return -abs(float(row.get('contribution')))
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _has_contribution(rows: list) -> bool:
    """行列表是否带数值型 contribution 字段（contribution 工具的 rows）。"""
    for row in rows:
        if isinstance(row, dict):
            try:
                float(row.get('contribution'))
                return True
            except (TypeError, ValueError):
                continue
    return False


def collect_results(store) -> tuple[dict, dict | None]:
    """从 ResultStore 生成报告 results 段与 metric_tree（首个成功 decompose_metric 的完整存储结果）。

    results: {result_id: {tool, level, arguments, result, truncated?}}；只收录 RESULT_TOOLS 的成功结果。
    metric_tree: 首个 decompose_metric 存储条目的 {tool, arguments, result}（完整、不截断）。
    """
    results = {}
    metric_tree = None
    # ResultStore 按 result_id 查询；编号形如 rN-tool，N 从 1 连续增长。
    for index in range(1, len(store) + 1):
        prefix = f'r{index}-'
        entry = next((store.get(prefix + tool) for tool in RESULT_TOOLS
                      if store.get(prefix + tool) is not None), None)
        if entry is None:
            continue
        if metric_tree is None and entry['tool'] == 'decompose_metric':
            metric_tree = {'tool': entry['tool'], 'arguments': entry['arguments'], 'result': entry['result']}
        trimmed, truncated = trim_value(entry['result'])
        record = {'tool': entry['tool'], 'level': entry['level'],
                  'arguments': entry['arguments'], 'result': trimmed}
        if truncated:
            record['truncated'] = True
        results[entry['result_id']] = record
    return results, metric_tree


def report_payload(task: Task, result, store, *, request: dict) -> dict:
    """调查报告.json 的完整内容；键名与 docs/api.md 的约定一致。"""
    snapshot = {'version': task.metric.version, 'included_statuses': list(task.metric.included_statuses),
                'amount_source': task.metric.amount_source}
    results, metric_tree = collect_results(store)
    branches = [{'branch': item.branch.get('branch'), 'dimensions': item.branch.get('dimensions'),
                 'stop_reason': item.stop_reason, 'steps': item.steps}
                for item in result.branches]
    usage = {'model_calls': result.model_calls, 'tokens': result.tokens, 'seconds': result.seconds}
    investigation = request.get('investigation') or {}
    return {'type': 'investigation', 'task_id': task.task_id, 'state': task.state.value,
            'stop_reason': result.stop_reason, 'question': task.question,
            'metric': investigation.get('metric'), 'period': investigation.get('period'),
            'base_period': investigation.get('base_period'), 'snapshot': snapshot,
            'clarifications': list(investigation.get('clarifications') or []),
            'pending': result.pending, 'report': result.report, 'metric_tree': metric_tree,
            'results': results, 'verification': result.verification, 'trace': result.trace,
            'branches': branches, 'usage': usage, 'episode_id': result.episode_id}


def _sandbox(settings):
    """run_sql 的执行沙箱；没有 Docker 时返回 None（run_sql 观察为「未配置」），不拖垮任务。"""
    try:
        return DockerRunner(None, settings.artifact_root / 'sql_runs', image=settings.executor_image)
    except ExecutionError:
        return None


def run_task(task: Task, row: dict, repository, token, settings, cancelled, *,
             client_factory=None, store_factory=None, memory=None):
    """调查任务的 worker 入口；client_factory/store_factory/memory 供离线测试注入。

    client_factory(provider) 返回带 complete_tools 的客户端；默认按任务创建时记录的供应商
    读取 .env 构造 ChatClient（真实模型只用 MiMo，不经此路径调用其他供应商）。
    store_factory() 返回 DatasetStore（默认 settings.artifact_root / 'datasets'）。
    memory 为 (MemoryService, Scope)；默认用 settings.database_url 的 PostgreSQL 实现。
    """
    request = row['request']
    investigation = request['investigation']
    store_factory = store_factory or (lambda: DatasetStore(settings.artifact_root / 'datasets'))
    resumed = task.state == State.WAITING_CONFIRMATION
    if not resumed and task.state != State.QUEUED:
        # 租约恢复：调查不保存模型上下文，检查点之后的进度无法接续，回到排队态从头重跑。
        task.timeline.append({'from': task.state.value, 'to': State.QUEUED.value, 'note': '调查无状态，租约恢复后从头重跑'})
        task.state = State.QUEUED
    if not resumed:
        task.move(State.PROFILING, '程序计算概况与语义层快照')
    folder = store_factory().directory(task.dataset_version)
    profile = (store_factory().get(task.dataset_version, task.project_id).profile or {})
    snapshot = SemanticSnapshot(task.metric.version, tuple(task.metric.included_statuses),
                                task.metric.amount_source)
    if client_factory is None:
        def client_factory(provider):
            return ChatClient(load_model_config().profile(provider))
    client = client_factory(request['model']['provider'])
    if memory is None:
        scope = Scope(task.project_id, task.metric.dataset_family, task.metric.schema_signature)
        memory = (MemoryService(PostgresMemoryRepository(settings.database_url)), scope)
    investigation_agent = Investigation(task.question, folder, snapshot, client, parallel=True,
                                        budget=InvestigationBudget(*BUDGET), profile=profile,
                                        review=True, memory=memory, sandbox=_sandbox(settings))
    task.move(State.PLANNING, '用户已答复，带澄清重新发起调查（无状态恢复）' if resumed
              else '主 Agent 计划、分派子 Agent 排查并由审查 Agent 复核')
    if cancelled.is_set():
        task.move(State.CANCELLED, '调查开始前收到取消')
        repository.save(task, token, release=True)
        return
    try:
        result = investigation_agent.run(metric=investigation.get('metric'), period=investigation.get('period'),
                                         base_period=investigation.get('base_period'), dimensions_hint=None,
                                         clarifications=list(investigation.get('clarifications') or []))
        store = investigation_agent.toolset.registry.store
    finally:
        investigation_agent.close()
    if cancelled.is_set():
        task.move(State.CANCELLED, '调查结束后提交前收到取消')
        repository.save(task, token, release=True)
        return
    if result.stop_reason == 'waiting_confirmation':
        # 等待态同样写调查报告：界面与 answer 接口从中读取 pending 的问题与选项。
        task.confirmation = (result.pending or {}).get('question')
        task.move(State.WAITING_CONFIRMATION, '调查需要用户澄清或确认新口径')
    else:
        task.move(State.EXECUTING, '汇总工具结果与调查报告')
        task.move(State.VERIFYING, '核验等级标注与报告有效性判定')
        if result.report is not None and result.report.get('valid') is True:
            task.move(State.SUCCEEDED, '调查报告通过代码核对')
        elif result.report is not None:
            task.failure = 'REPORT_INVALID'
            task.move(State.FAILED, '调查报告未通过代码核对（报告仍保存）')
        else:
            task.failure = 'INVESTIGATION_STOPPED'
            task.move(State.FAILED, f'调查未产出报告（停止原因：{result.stop_reason}）')
    run_id = str(uuid4())
    folder_out = settings.artifact_root / 'runs' / run_id
    folder_out.mkdir(parents=True, exist_ok=False)
    # 文件名带 fence：旧 worker 迟到的写入落在自己的文件里，不会覆盖新领取者的报告。
    name = f'report.{row["fence"]}.json'
    payload = report_payload(task, result, store, request=request)
    (folder_out / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    repository.add_artifact(task.task_id, run_id, '调查报告.json', f'runs/{run_id}/{name}', token)
    repository.save(task, token, release=True)
    if task.state == State.CANCELLED:
        # 终态提交吸收了并发取消：报告只保留过程证据，不保留成功结论。
        payload['state'] = 'CANCELLED'
        payload['report'] = None
        (folder_out / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
