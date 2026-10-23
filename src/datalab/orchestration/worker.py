"""Redis 只传 task_id；PostgreSQL 保存任务、阶段和取消状态。"""
from dataclasses import asdict
import json
import threading

from celery import Celery

from datalab.artifacts.derive import derive
from datalab.datasets.service import DatasetStore
from datalab.execution.runner import DockerRunner, ExecutionError
from datalab.orchestration.services import ExecutionServices
from datalab.orchestration.workflow import State, Workflow, TERMINAL
from datalab.roles.guided import GuidedBackend
from datalab.roles.llm import ModelBackend
from datalab.roles.model_config import ModelConfigError, load_model_config
from datalab.roles.runtime import ModelCallError, RoleRuntime
from datalab.settings import Settings
from datalab.storage.repository import Repository, decode_task

settings = Settings.load()
celery_app = Celery('datalab', broker=settings.broker_url)
celery_app.conf.update(task_serializer='json', accept_content=['json'], task_ignore_result=True,
    task_acks_late=True, worker_prefetch_multiplier=1, task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True, broker_connection_max_retries=3,
    beat_schedule={'dispatch-pending': {'task': 'datalab.dispatch', 'schedule': 5.0}},
    broker_transport_options={'visibility_timeout': 300, 'socket_connect_timeout': 3, 'socket_timeout': 3})


def role_backend(request: dict):
    """按任务创建时记录的供应商选择后端；切换模型只影响之后创建的任务。"""
    if request.get('mode') != 'model':
        return GuidedBackend(request['options'])
    # 每次任务重新读取 .env，使补充密钥后无需重启 worker。
    profile = load_model_config().profile(request['model']['provider'])
    return ModelBackend(profile, request['options'])


def process_task(task_id: str):
    repository = Repository(settings.database_url)
    claimed = repository.claim(task_id)
    if not claimed:
        return
    token, row = claimed
    task = decode_task(row['snapshot'])
    cancelled = threading.Event()
    finished = threading.Event()
    if row['cancel_requested']:
        cancelled.set()

    def heartbeat():
        while not finished.wait(1):
            try:
                value = repository.heartbeat(task_id,token)
                if value is None or value:
                    cancelled.set()
                    return
            except Exception:
                cancelled.set()
                return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    task._on_transition = lambda current: repository.save(current, token) if current.state not in TERMINAL else None
    runner = None
    try:
        # 沙箱构造放在 try 内：缺少 Docker 时任务以 SANDBOX_UNAVAILABLE 失败并释放租约，
        # 不会留在已领取状态由心跳无限续租，也不降级为宿主机执行。
        runner = DockerRunner(DatasetStore(settings.artifact_root / 'datasets'), settings.artifact_root / 'runs', image=settings.executor_image)
        workflow = Workflow(RoleRuntime(role_backend(row['request'])),
                            ExecutionServices(runner, metamorphic=settings.metamorphic_checks))
        if task.state == State.WAITING_CONFIRMATION:
            task = workflow.resume(task,task.metric,cancelled=cancelled)
        elif task.state != State.QUEUED:
            # 上一个 worker 的租约过期，任务带着检查点重新排队：从快照状态接续。
            task = workflow.recover(task,cancelled=cancelled)
        else:
            task = workflow.run(task,cancelled=cancelled)
        # 所有索引来自本任务实际尝试；路径不接受模型直接指定。
        for manifest in task.attempts:
            folder = runner.root / manifest.run_id
            if task.plan:
                (folder / 'plan.json').write_text(json.dumps(task.plan.payload(),ensure_ascii=False,indent=2),encoding='utf-8')
            names = {'代码.py': 'input/analysis.py', '执行清单.json': 'manifest.json', '计划.json': 'plan.json'}
            if task.state == State.SUCCEEDED and manifest is task.attempts[-1]:
                names.update({'结果.json':'output/result.json','结果表.csv':'output/table.csv'})
            for name, path in names.items():
                if (folder / path).is_file():
                    repository.add_artifact(task.task_id,manifest.run_id,name,f'runs/{manifest.run_id}/{path}',token)
        derived, derived_error = None, None
        # Olist 多表结果的图表与说明模板尚未实现，暂不派生（不是失败）。
        if task.state == State.SUCCEEDED and task.attempts and task.plan.schema == 'orders':
            # 图表与说明只从已通过核验的 result.json 确定性派生；失败不推翻核验结论，只记录原因。
            latest = task.attempts[-1]
            try:
                version = runner.store.get(task.dataset_version, task.project_id)
                derived = derive(runner.root / latest.run_id, task.plan.payload(), metric_version=task.metric.version,
                                 included_statuses=task.metric.included_statuses, currency=version.currency,
                                 data_range=(version.profile['time_min'], version.profile['time_max']))
                for name, path in derived['files'].items():
                    repository.add_artifact(task.task_id, latest.run_id, name, f'runs/{latest.run_id}/{path}', token)
            except Exception:
                derived, derived_error = None, '图表或说明生成失败；核验结论不受影响'
        if task.attempts:
            latest = task.attempts[-1]
            indexed = repository.artifacts(task.task_id, task.project_id)
            result_artifact = next((item for item in indexed if item['name'] == '结果.json' and str(item['run_id']) == latest.run_id), None)
            claims = []
            if task.state == State.SUCCEEDED and result_artifact:
                reference = task.verifications[-1].reference
                if 'amount_cents' in reference.get('total', {}):
                    claims.append({'text': f"确认口径下订单金额合计 {reference['total']['amount_cents'] / 100:.2f}",
                                   'artifact_id': str(result_artifact['artifact_id']), 'result_pointer': '/total/amount_cents'})
            report = {'mode':row['request'].get('mode','guided-demo'),'model':row['request'].get('model'),'task_id':task.task_id,'state':task.state.value,
                      'dataset_version':task.dataset_version,'metric':asdict(task.metric) if task.metric else None,
                      'plan':task.plan.payload() if task.plan else None,'claims':claims,
                      'verification':[asdict(item) for item in task.verifications],
                      'narrative':derived['narrative'] if derived else None,
                      'charts':derived['charts'] if derived else [],'derived_error':derived_error,
                      'trace':task.trace}
            # 报告文件名带 fence：旧 worker 迟到的写入落在自己的文件里，不会覆盖新领取者的报告；
            # 索引行按 fence 单调更新，下载只认索引。
            report_file=f"report.{row['fence']}.json"
            path=runner.root/latest.run_id/report_file
            path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            repository.add_artifact(task.task_id,latest.run_id,'核验报告.json',f'runs/{latest.run_id}/{report_file}',token)
        task.budget.pause()
        repository.save(task,token,release=True)
        if task.state == State.CANCELLED and task.attempts:
            # 终态提交若吸收了并发取消，报告也须撤销已生成的成功结论。
            report['state'] = 'CANCELLED'
            report['claims'] = []
            report['narrative'] = None
            report['charts'] = []
            path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    except Exception as error:
        # 工作进程错误不能把任务永久留为执行中；不输出底层连接或数据内容。
        task.state = State.FAILED
        if isinstance(error, (ModelCallError, ModelConfigError)):
            task.failure = 'MODEL_ERROR'
        elif isinstance(error, ExecutionError) and runner is None:
            task.failure = 'SANDBOX_UNAVAILABLE'
        else:
            task.failure = 'WORKER_ERROR'
        try:
            repository.save(task,token,release=True)
        except Exception:
            pass
    finally:
        finished.set()
        thread.join(timeout=3)


@celery_app.task(name='datalab.execute')
def execute_task(task_id: str):
    process_task(task_id)


@celery_app.task(name='datalab.dispatch')
def dispatch_pending():
    for task_id in Repository(settings.database_url).pending_ids():
        execute_task.delay(task_id)
