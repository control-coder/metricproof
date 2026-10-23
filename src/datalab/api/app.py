"""单用户本地接口；所有资源都先检查项目归属，不对公网承诺多租户认证。"""
from dataclasses import asdict
import hashlib
import json
import re
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import psycopg
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from datalab.contracts import AMOUNT_KINDS, AnalysisPlan, OLIST_STATUSES, ORDER_STATUSES
from datalab.datasets.service import DatasetError, DatasetStore, FIELDS, MAX_BYTES, schema_of
from datalab.memory.postgres import PostgresMemoryRepository
from datalab.memory.service import MemoryConflict, MemoryService, Scope
from datalab.orchestration.workflow import Task
from datalab.roles.model_config import CHOICES, GUIDED, ModelConfigError, env_path, load_model_config, switch_provider
from datalab.roles.runtime import CallBudget
from datalab.settings import Settings
from datalab.storage.repository import IdempotencyConflict, Repository


class Payload(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ProjectInput(Payload):
    name: str = Field(min_length=1,max_length=80)


class Options(Payload):
    # 后四类与 customer_state 等分组只适用于 Olist 数据，是否与数据结构相符由 AnalysisPlan 校验。
    kind: Literal['summary','trend','ranking','share','comparison','quality',
                  'repeat_rate','cohort_retention','delivery_delay','category_sales'] = 'summary'
    group_by: Literal['channel','category','payment_date',
                      'customer_state','purchase_date','purchase_month','product_category'] | None = None
    start: str | None = None
    end: str | None = None
    previous_start: str | None = None
    previous_end: str | None = None
    # 只有 Olist 送达延迟可以改用送达日期；不填时用默认时间字段。
    time_field: Literal['order_purchase_timestamp','order_delivered_customer_date'] | None = None


def plan_options(options: Options) -> dict:
    """计划选项；未填时间字段时不出现该键，单表请求与之前完全相同。"""
    value = options.model_dump()
    if value['time_field'] is None:
        del value['time_field']
    return value


class OlistImportInput(Payload):
    dataset_family: str = Field(min_length=1,max_length=120)


class TaskInput(Payload):
    dataset_version: UUID
    question: str = Field(min_length=1,max_length=4000)
    options: Options = Field(default_factory=Options)
    memory_id: UUID | None = None


IDEMPOTENCY_KEY = re.compile(r'[!-~]{1,128}')


def request_fingerprint(payload: TaskInput) -> str:
    """同一个幂等键只对应同一份请求内容；用规范化 JSON 的哈希比较。"""
    value = payload.model_dump(mode='json')
    value['options'] = plan_options(payload.options)
    body = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


class RuleInput(Payload):
    dataset_version: UUID
    name: str = Field(min_length=1,max_length=120)
    definition: str = Field(min_length=1,max_length=2000)
    # 单表三种状态或 Olist 状态词表；是否与数据结构相符由记忆服务按结构签名校验。
    included_statuses: list[Literal[ORDER_STATUSES+OLIST_STATUSES]]
    # 只适用于 Olist：订单金额来源随口径一起确认，任务不能另行指定。
    amount_source: Literal['payments','items'] | None = None


class ConfirmInput(Payload):
    dataset_version: UUID
    expected_current_id: UUID | None = None


class TaskConfirmInput(Payload):
    memory_id: UUID


class ModelInput(Payload):
    provider: Literal[CHOICES]


class BodyTooLarge(Exception):
    pass


class RequestBoundary:
    """接收层限制总请求体，防止仅依赖客户端 Content-Length 或上传后的检查。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope,receive,send)
        total = 0
        async def limited_receive():
            nonlocal total
            message = await receive()
            total += len(message.get('body',b''))
            if total > MAX_BYTES + 65536:
                raise BodyTooLarge()
            return message
        try:
            await self.app(scope,limited_receive,send)
        except BodyTooLarge:
            await JSONResponse({'detail':'请求体超过限制'},status_code=413)(scope,receive,send)


def create_app(settings: Settings | None = None):
    settings = settings or Settings.load()
    repository = Repository(settings.database_url)
    memory = MemoryService(PostgresMemoryRepository(settings.database_url))
    datasets = DatasetStore(settings.artifact_root / 'datasets')
    app = FastAPI(title='OrderProof 本地分析',version='0.1.0',docs_url='/api/docs',openapi_url='/api/openapi.json')
    app.add_middleware(RequestBoundary)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['127.0.0.1','localhost','testserver'])

    @app.middleware('http')
    async def local_boundary(request, call_next):
        if request.method in ('POST','PUT','PATCH','DELETE'):
            if request.headers.get('x-datalab-client') != 'local-ui':
                return JSONResponse({'detail':'缺少本机客户端标识'},status_code=403)
            origin = request.headers.get('origin')
            if origin and origin not in {'http://127.0.0.1:18080','http://localhost:18080','http://127.0.0.1:5173','http://localhost:5173'}:
                return JSONResponse({'detail':'不允许跨站写入'},status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(KeyError)
    async def missing(request, error):
        return JSONResponse({'detail':'当前项目中找不到请求的资源'},status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        if isinstance(error,ModelConfigError):
            return JSONResponse({'detail':str(error)},status_code=400)
        if isinstance(error,MemoryConflict):
            return JSONResponse({'detail':'口径版本已变化，请刷新并重新确认当前版本'},status_code=409)
        return JSONResponse({'detail':str(error) if isinstance(error,DatasetError) else '输入不符合领域约束',
                             'issues': error.issues if isinstance(error,DatasetError) else []},status_code=400)

    @app.exception_handler(psycopg.Error)
    async def database_error(request,error):
        return JSONResponse({'detail':'数据库暂不可用，请检查本地服务'},status_code=503)

    def scoped(project_id,version):
        metadata=repository.dataset(str(project_id),str(version))
        return Scope(str(project_id),metadata['dataset_family'],metadata['schema_signature'])

    def metric_by_id(scope,memory_id):
        match=next((entry for entry in memory.retrieve(scope,kind='metric') if entry.memory_id == str(memory_id)),None)
        if match is None:
            raise ValueError('所选口径未确认、已失效或不属于当前数据范围')
        return memory.metric_snapshot(match)

    def validate_options(scope,version,options,metric):
        """按数据结构预检计划选项；Olist 用所选口径的状态与金额来源，未选口径时用占位值只检查形状。"""
        if schema_of(scope.schema_signature)!='olist':
            AnalysisPlan(str(version),'input-validation',**options)
            return
        kind=options.get('kind','summary')
        if metric is not None and kind in AMOUNT_KINDS and metric.amount_source is None:
            raise ValueError('该口径没有确认订单金额来源（payments 或 items），不能用于订单金额分析')
        AnalysisPlan(str(version),'input-validation',schema='olist',**options,
                     included_statuses=metric.included_statuses if metric else ('delivered',),
                     amount_source=metric.amount_source if metric and metric.amount_source else 'payments')

    def enqueue(task_id):
        # Redis 暂不可用时保留 PostgreSQL 待投递记录，beat 恢复后补发。
        try:
            from celery import Celery
            publisher=Celery('datalab-api',broker=settings.broker_url)
            publisher.conf.update(broker_connection_timeout=2,broker_transport_options={'socket_connect_timeout':2,'socket_timeout':2})
            publisher.send_task('datalab.execute',args=[str(task_id)],retry=False)
            publisher.close()
            return True
        except Exception:
            return False

    def model_state():
        # 没有 .env 时保持固定演示，兼容既有本机部署；密钥只报告是否已配置。
        if not env_path().is_file():
            return None,{'active':GUIDED,'config_present':False,'providers':[]}
        config=load_model_config()
        return config,{'active':config.active,'config_present':True,
                       'providers':[profile.public() for profile in config.profiles.values()]}

    @app.get('/api/health')
    def health():
        with repository.connection() as connection:
            connection.execute('SELECT 1')
        _,state=model_state()
        model=state['active']!=GUIDED
        return {'status':'ok','mode':'model' if model else 'guided-demo','model_calls':model,'provider':state['active']}

    @app.get('/api/model')
    def model_info():
        return model_state()[1]

    @app.put('/api/model')
    def switch_model(payload:ModelInput):
        # 只改当前供应商一行；已创建任务仍使用创建时记录的模型。
        switch_provider(payload.provider)
        return model_state()[1]

    @app.get('/api/projects')
    def projects():
        return repository.projects()

    @app.post('/api/projects',status_code=201)
    def create_project(payload:ProjectInput):
        if not payload.name.strip():
            raise ValueError('名称不能为空')
        return repository.create_project(payload.name.strip())

    @app.get('/api/projects/{project_id}/datasets')
    def list_datasets(project_id:UUID):
        repository.require_project(str(project_id))
        return repository.datasets(str(project_id))

    @app.post('/api/projects/{project_id}/datasets',status_code=201)
    async def upload(project_id:UUID,file:UploadFile=File(...),dataset_family:str=Form(...),
                     currency:str=Form(...),timezone:str=Form(...),mapping:str=Form('{}')):
        repository.require_project(str(project_id))
        if not file.filename or not file.filename.lower().endswith('.csv'):
            raise ValueError('只接受 CSV')
        content=await file.read(MAX_BYTES+1)
        try:
            fields=json.loads(mapping)
            if not isinstance(fields,dict) or any(not isinstance(key,str) or not isinstance(value,str) for key,value in fields.items()):
                raise ValueError('字段映射无效')
            metadata=datasets.import_csv(content,project_id=str(project_id),dataset_family=dataset_family,
                                         currency=currency,timezone=timezone,mapping=fields or None)
            repository.add_dataset(metadata)
            # 字段映射同时写入字段语义候选；状态为待确认，不参与计算。
            candidates=memory.suggest_field_semantics(Scope(str(project_id),metadata.dataset_family,metadata.schema_signature),
                fields or {name:name for name in FIELDS},'dataset-import:'+metadata.dataset_version)
            return {**asdict(metadata),'field_semantics_candidates':len(candidates)}
        finally:
            await file.close()

    @app.post('/api/projects/{project_id}/datasets/olist',status_code=201)
    def import_olist(project_id:UUID,payload:OlistImportInput):
        # 只读取本机配置固定的原始目录（默认为 scripts/fetch_olist.py 的下载位置），请求不能指定路径或表。
        repository.require_project(str(project_id))
        metadata=datasets.import_olist(settings.olist_raw,project_id=str(project_id),
                                       dataset_family=payload.dataset_family)
        repository.add_dataset(metadata)
        return asdict(metadata)

    @app.get('/api/projects/{project_id}/memories')
    def memories(project_id:UUID,dataset_version:UUID):
        return [asdict(entry) for entry in memory.history(scoped(project_id,dataset_version))]

    @app.post('/api/projects/{project_id}/memories',status_code=201)
    def propose(project_id:UUID,payload:RuleInput):
        scope=scoped(project_id,payload.dataset_version)
        content={'definition':payload.definition,'included_statuses':payload.included_statuses}
        if payload.amount_source is not None:
            content['amount_source']=payload.amount_source
        return asdict(memory.propose(scope,'metric',payload.name,content,'local-user:'+str(uuid4())))

    @app.post('/api/projects/{project_id}/memories/{memory_id}/confirm')
    def confirm_rule(project_id:UUID,memory_id:UUID,payload:ConfirmInput):
        return asdict(memory.confirm(scoped(project_id,payload.dataset_version),str(memory_id),actor='local-user',
            expected_current_id=str(payload.expected_current_id) if payload.expected_current_id else None))

    @app.post('/api/projects/{project_id}/memories/{memory_id}/invalidate')
    def invalidate_rule(project_id:UUID,memory_id:UUID,payload:ConfirmInput):
        return asdict(memory.invalidate(scoped(project_id,payload.dataset_version),str(memory_id),actor='local-user'))

    @app.post('/api/projects/{project_id}/tasks',status_code=202)
    def create_task(project_id:UUID,payload:TaskInput,idempotency_key:str|None=Header(default=None)):
        if idempotency_key is not None and not IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise HTTPException(422,'Idempotency-Key 需为 1–128 位可见 ASCII 字符')
        scope=scoped(project_id,payload.dataset_version)
        options=plan_options(payload.options)
        metric=metric_by_id(scope,payload.memory_id) if payload.memory_id else None
        # 预先验证类型、分组和期间，避免无效计划反复占用队列。
        validate_options(scope,payload.dataset_version,options,metric)
        config,state=model_state()
        request={'options':options,'mode':'guided-demo'}
        task=Task(str(project_id),str(payload.dataset_version),payload.question,metric=metric)
        if state['active']!=GUIDED:
            profile=config.profile()
            if not profile.configured:
                raise ModelConfigError(f'{profile.provider} 尚未配置 API Key；请在 .env 中填写，或切换到固定演示')
            request={'options':options,'mode':'model','model':{'provider':profile.provider,'model':profile.model}}
            task.budget=CallBudget(config.max_calls,config.max_tokens,config.seconds)
        try:
            task_id,created=repository.create_task(task,request,idempotency_key=idempotency_key,
                request_hash=request_fingerprint(payload) if idempotency_key else None)
        except IdempotencyConflict as error:
            raise HTTPException(422,str(error))
        if not created:
            # 重放：返回原任务，不再创建也不再派发（原任务若还没派发出去，dispatcher 会补发）。
            row=repository.get_task(task_id,str(project_id))
            return {'task_id':task_id,'state':row['state'],'dispatched':False,'replayed':True,
                    'mode':row['request'].get('mode'),'model':row['request'].get('model')}
        return {'task_id':task.task_id,'state':'QUEUED','dispatched':enqueue(task.task_id),'mode':request['mode'],
                'model':request.get('model')}

    @app.get('/api/projects/{project_id}/tasks')
    def task_list(project_id:UUID):
        return repository.tasks(str(project_id))

    def artifact_file(item):
        path=(settings.artifact_root / item['relative_path']).resolve()
        if not path.is_relative_to(settings.artifact_root.resolve()) or not path.is_file():
            raise KeyError('产物不存在')
        return path

    def narrative_of(project_id,state,artifacts):
        # 只有核验通过的任务展示说明；说明由程序模板生成，内容已随产物归档。
        item=next((entry for entry in artifacts if entry['name']=='分析说明.json'),None)
        if state!='SUCCEEDED' or item is None:
            return None
        try:
            return json.loads(artifact_file(repository.artifact(str(item['artifact_id']),project_id)).read_text(encoding='utf-8'))
        except (KeyError,OSError,ValueError):
            return None

    @app.get('/api/projects/{project_id}/tasks/{task_id}')
    def task_detail(project_id:UUID,task_id:UUID):
        row=repository.get_task(str(task_id),str(project_id))
        artifacts=repository.artifacts(str(task_id),str(project_id))
        return {'task_id':str(task_id),'state':row['state'],'snapshot':row['snapshot'],
                'request':row['request'],'cancel_requested':row['cancel_requested'],
                'created_at':row['created_at'],'updated_at':row['updated_at'],
                'artifacts':artifacts,'narrative':narrative_of(str(project_id),row['state'],artifacts)}

    @app.post('/api/projects/{project_id}/tasks/{task_id}/cancel')
    def cancel_task(project_id:UUID,task_id:UUID):
        repository.cancel(str(task_id),str(project_id))
        return {'accepted':True}

    @app.post('/api/projects/{project_id}/tasks/{task_id}/confirm')
    def confirm_task(project_id:UUID,task_id:UUID,payload:TaskConfirmInput):
        row=repository.get_task(str(task_id),str(project_id))
        scope=scoped(project_id,row['dataset_version'])
        metric=metric_by_id(scope,payload.memory_id)
        validate_options(scope,row['dataset_version'],row['request'].get('options') or {},metric)
        repository.confirm_task(str(task_id),str(project_id),metric)
        return {'dispatched':enqueue(task_id)}

    @app.get('/api/projects/{project_id}/artifacts/{artifact_id}')
    def download(project_id:UUID,artifact_id:UUID,inline:bool=False):
        item=repository.artifact(str(artifact_id),str(project_id))
        path=artifact_file(item)
        # 仅服务端生成的 PNG 图表允许内联显示；其余产物一律作为附件下载。
        if inline and item['name'].endswith('.png') and path.suffix=='.png':
            return FileResponse(path,filename=item['name'],media_type='image/png',content_disposition_type='inline')
        return FileResponse(path,filename=item['name'],media_type='application/octet-stream')

    frontend=Path(__file__).resolve().parents[3] / 'frontend/dist'
    if frontend.is_dir():
        app.mount('/',StaticFiles(directory=frontend,html=True),name='frontend')
    return app
