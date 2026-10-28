"""数据库为阶段真相；重复队列投递通过租约和写入令牌隔离。"""
from contextlib import contextmanager
from dataclasses import asdict
from importlib.resources import files
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from datalab.contracts import AnalysisPlan, ExecutionManifest, VerificationReport
from datalab.orchestration.workflow import MetricSnapshot, State, Task
from datalab.roles.runtime import CallBudget


def encode_task(task: Task) -> dict:
    payload = asdict(task)
    payload['state'] = task.state.value
    # monotonic 起点不能跨进程复用；只保存已消耗的有效时间。
    budget = payload['budget']
    budget['spent_seconds'] = task.budget.seconds - task.budget.remaining_time()
    budget['started'] = None
    return payload


def decode_task(payload: dict) -> Task:
    payload = dict(payload)
    payload['state'] = State(payload['state'])
    payload['metric'] = MetricSnapshot(**payload['metric']) if payload['metric'] else None
    payload['plan'] = AnalysisPlan(**payload['plan']) if payload['plan'] else None
    payload['attempts'] = [ExecutionManifest(**item) for item in payload['attempts']]
    payload['verifications'] = [VerificationReport(**item) for item in payload['verifications']]
    payload['budget'] = CallBudget(**payload['budget'])
    return Task(**payload)


# 租约过期后最多从检查点恢复的次数；用尽仍失败则按原规则标记失败并保留证据。
MAX_RECOVERIES = 1
# 这些快照状态都已有持久化的检查点，可由新 worker 接续；等待确认与终态从不持有租约。
RECOVERABLE = (State.QUEUED, State.PROFILING, State.PLANNING, State.EXECUTING, State.VERIFYING)


class IdempotencyConflict(ValueError):
    """同一个 Idempotency-Key 被用于内容不同的请求。"""


class Repository:
    def __init__(self, database_url):
        self._dsn = database_url

    @contextmanager
    def connection(self):
        with psycopg.connect(self._dsn, connect_timeout=5, row_factory=dict_row) as connection:
            connection.execute("SET LOCAL statement_timeout='10s'")
            yield connection

    def initialize(self):
        with self.connection() as connection:
            connection.execute(files('datalab.storage').joinpath('schema.sql').read_text(encoding='utf-8'))

    def projects(self):
        with self.connection() as connection:
            return connection.execute('SELECT * FROM datalab_projects ORDER BY created_at').fetchall()

    def create_project(self, name):
        identifier = str(uuid4())
        with self.connection() as connection:
            connection.execute('INSERT INTO datalab_projects(project_id,name) VALUES(%s,%s)', (identifier, name))
        return {'project_id': identifier, 'name': name}

    def require_project(self, identifier):
        with self.connection() as connection:
            if not connection.execute('SELECT 1 FROM datalab_projects WHERE project_id=%s', (identifier,)).fetchone():
                raise KeyError('项目不存在')

    def add_dataset(self, metadata):
        with self.connection() as connection:
            connection.execute('INSERT INTO datalab_datasets VALUES(%s,%s,%s,now())',
                               (metadata.dataset_version, metadata.project_id, Jsonb(asdict(metadata))))

    def datasets(self, project_id):
        with self.connection() as connection:
            return [row['metadata'] for row in connection.execute(
                'SELECT metadata FROM datalab_datasets WHERE project_id=%s ORDER BY created_at', (project_id,))]

    def dataset(self, project_id, version):
        with self.connection() as connection:
            row = connection.execute('SELECT metadata FROM datalab_datasets WHERE project_id=%s AND dataset_version=%s',
                                     (project_id, version)).fetchone()
        if not row:
            raise KeyError('当前项目不存在该数据版本')
        return row['metadata']

    def create_task(self, task, request, *, idempotency_key=None, request_hash=None):
        """返回 (task_id, created)。同一项目内相同 key 且内容相同的重放返回原任务；内容不同则拒绝。"""
        self.dataset(task.project_id, task.dataset_version)
        with self.connection() as connection:
            row = connection.execute(
                'INSERT INTO datalab_tasks(task_id,project_id,dataset_version,state,request,snapshot,idempotency_key,request_hash) '
                'VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(project_id,idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING RETURNING task_id',
                (task.task_id, task.project_id, task.dataset_version, task.state.value, Jsonb(request), Jsonb(encode_task(task)),
                 idempotency_key, request_hash)).fetchone()
            if row:
                return str(row['task_id']), True
            # 冲突时另一个事务已经提交（ON CONFLICT 会等待它），这里一定读得到。
            existing = connection.execute('SELECT task_id,request_hash FROM datalab_tasks WHERE project_id=%s AND idempotency_key=%s',
                                          (task.project_id, idempotency_key)).fetchone()
        if existing['request_hash'] != request_hash:
            raise IdempotencyConflict('Idempotency-Key 已用于内容不同的请求')
        return str(existing['task_id']), False

    def get_task(self, task_id, project_id=None):
        with self.connection() as connection:
            row = connection.execute('SELECT * FROM datalab_tasks WHERE task_id=%s', (task_id,)).fetchone()
        if not row or (project_id is not None and str(row['project_id']) != str(project_id)):
            raise KeyError('当前项目不存在该任务')
        return row

    def tasks(self, project_id):
        with self.connection() as connection:
            rows = connection.execute('SELECT task_id,state,request,snapshot,created_at,updated_at FROM datalab_tasks WHERE project_id=%s ORDER BY created_at DESC LIMIT 100', (project_id,)).fetchall()
        for row in rows:
            # 列表行的展示字段：任务类型（调查/统计）与问题；request 本身不交给列表页。
            row['type'] = 'investigation' if (row.pop('request') or {}).get('type') == 'investigation' else 'analysis'
            row['question'] = row['snapshot'].get('question')
        return rows

    def claim(self, task_id):
        """领取任务：租约 UUID 标识持有者，fence 每次领取加一，供产物索引判断新旧。"""
        token = str(uuid4())
        with self.connection() as connection:
            row = connection.execute(
                "UPDATE datalab_tasks SET lease_id=%s,lease_until=now()+interval '30 seconds',fence=fence+1 WHERE task_id=%s AND state='QUEUED' AND lease_id IS NULL RETURNING *",
                (token, task_id)).fetchone()
        return (token, row) if row else None

    def heartbeat(self, task_id, token):
        with self.connection() as connection:
            row = connection.execute(
                "UPDATE datalab_tasks SET lease_until=now()+interval '30 seconds' WHERE task_id=%s AND lease_id=%s RETURNING cancel_requested",
                (task_id, token)).fetchone()
        return None if not row else row['cancel_requested']

    def save(self, task, token, *, release=False):
        with self.connection() as connection:
            current = connection.execute(
                "SELECT cancel_requested FROM datalab_tasks WHERE task_id=%s AND lease_id=%s FOR UPDATE",
                (task.task_id, token)).fetchone()
            if not current:
                raise RuntimeError("任务租约失效，禁止旧 worker 覆盖结果")
            if release and current["cancel_requested"] and task.state != State.CANCELLED:
                # 行锁保证最终提交不会覆盖已经确认的取消请求。
                task.timeline.append({"from":task.state.value,"to":"CANCELLED","note":"最终提交前收到用户取消"})
                task.state = State.CANCELLED
                task.failure = None
            row = connection.execute(
                'UPDATE datalab_tasks SET state=%s,snapshot=%s,updated_at=now(),lease_id=CASE WHEN %s THEN NULL ELSE lease_id END,lease_until=CASE WHEN %s THEN NULL ELSE lease_until END WHERE task_id=%s AND lease_id=%s RETURNING task_id',
                (task.state.value, Jsonb(encode_task(task)), release, release, task.task_id, token)).fetchone()
        if not row:
            raise RuntimeError('任务租约失效，禁止旧 worker 覆盖结果')

    def cancel(self, task_id, project_id):
        with self.connection() as connection:
            row = connection.execute('SELECT * FROM datalab_tasks WHERE task_id=%s AND project_id=%s FOR UPDATE', (task_id,project_id)).fetchone()
            if not row:
                raise KeyError('任务不存在')
            if row['state'] in ('SUCCEEDED','FAILED','CANCELLED'):
                return
            task = decode_task(row['snapshot'])
            if row['lease_id'] is None:
                task.move(State.CANCELLED, '用户取消未执行任务')
            connection.execute('UPDATE datalab_tasks SET cancel_requested=true,state=%s,snapshot=%s,updated_at=now() WHERE task_id=%s',
                (task.state.value if row['lease_id'] is None else row['state'], Jsonb(encode_task(task)), task_id))

    def confirm_task(self, task_id, project_id, metric):
        with self.connection() as connection:
            row = connection.execute('SELECT * FROM datalab_tasks WHERE task_id=%s AND project_id=%s FOR UPDATE', (task_id,project_id)).fetchone()
            if not row or row['state'] != 'WAITING_CONFIRMATION' or row['lease_id'] is not None:
                raise ValueError('任务不在可确认的等待阶段')
            task = decode_task(row['snapshot'])
            task.metric = metric
            # 数据库可派发状态与领域恢复快照分开；worker 调用 resume。
            connection.execute("UPDATE datalab_tasks SET state='QUEUED',snapshot=%s,cancel_requested=false,updated_at=now() WHERE task_id=%s",
                               (Jsonb(encode_task(task)), task_id))

    def answer_investigation(self, task_id, project_id, *, question, answer, metric=None):
        """调查任务的澄清回答：追加一轮 {question, answer}，可选替换口径，重新排队。

        与 confirm_task 同一风格：行锁、只允许无租约的 WAITING_CONFIRMATION；
        澄清轮数超过 MAX_CLARIFICATION_ROUNDS 时拒绝（计划的人机往返上限）。
        """
        from datalab.orchestration.investigation_task import MAX_CLARIFICATION_ROUNDS
        with self.connection() as connection:
            row = connection.execute('SELECT * FROM datalab_tasks WHERE task_id=%s AND project_id=%s FOR UPDATE', (task_id,project_id)).fetchone()
            if not row or row['state'] != 'WAITING_CONFIRMATION' or row['lease_id'] is not None:
                raise ValueError('任务不在可确认的等待阶段')
            request = dict(row['request'])
            if request.get('type') != 'investigation':
                raise ValueError('只有调查任务支持澄清回答')
            task = decode_task(row['snapshot'])
            if metric is not None:
                task.metric = metric
            task.confirmation = None
            investigation = dict(request.get('investigation') or {})
            clarifications = list(investigation.get('clarifications') or [])
            if len(clarifications) >= MAX_CLARIFICATION_ROUNDS:
                raise ValueError('澄清轮数已达上限')
            clarifications.append({'question': question, 'answer': answer})
            investigation['clarifications'] = clarifications
            request['investigation'] = investigation
            connection.execute("UPDATE datalab_tasks SET state='QUEUED',request=%s,snapshot=%s,cancel_requested=false,updated_at=now() WHERE task_id=%s",
                               (Jsonb(request), Jsonb(encode_task(task)), task_id))

    def pending_ids(self):
        with self.connection() as connection:
            stale = connection.execute('SELECT task_id,snapshot,cancel_requested FROM datalab_tasks WHERE lease_until<now() FOR UPDATE SKIP LOCKED').fetchall()
            for row in stale:
                task = decode_task(row['snapshot'])
                if row['cancel_requested']:
                    task.timeline.append({'from': task.state.value, 'to': 'CANCELLED', 'note': '租约过期时已有取消请求'})
                    task.state, task.failure = State.CANCELLED, None
                    target = 'CANCELLED'
                elif task.state in RECOVERABLE and task.recoveries < MAX_RECOVERIES:
                    # 重新排队，领域状态保持检查点时的样子；下一个 worker 用 recover 接续。
                    task.recoveries += 1
                    task.timeline.append({'from': task.state.value, 'to': task.state.value,
                                          'note': f'租约过期，从最近检查点恢复（第 {task.recoveries} 次）'})
                    target = 'QUEUED'
                else:
                    task.state, task.failure = State.FAILED, 'WORKER_LEASE_EXPIRED'
                    target = 'FAILED'
                connection.execute('UPDATE datalab_tasks SET state=%s,snapshot=%s,lease_id=NULL,lease_until=NULL,updated_at=now() WHERE task_id=%s',
                                   (target, Jsonb(encode_task(task)), row['task_id']))
            return [str(row['task_id']) for row in connection.execute("SELECT task_id FROM datalab_tasks WHERE state='QUEUED' AND lease_id IS NULL ORDER BY created_at LIMIT 100")]

    def add_artifact(self, task_id, run_id, name, relative_path, token):
        """登记产物，受围栏约束：只有仍持有租约的 worker 能写，且只会被更高的 fence 覆盖。

        返回是否发生了写入；同一次领取的重复登记是幂等的空操作。租约已失效（旧 worker 迟到）抛出 RuntimeError。
        """
        with self.connection() as connection:
            row = connection.execute(
                'INSERT INTO datalab_artifacts(artifact_id,task_id,run_id,name,relative_path,fence) '
                'SELECT %s,%s,%s,%s,%s,t.fence FROM datalab_tasks t WHERE t.task_id=%s AND t.lease_id=%s '
                'ON CONFLICT(task_id,run_id,name) DO UPDATE SET relative_path=EXCLUDED.relative_path,fence=EXCLUDED.fence '
                'WHERE datalab_artifacts.fence<EXCLUDED.fence RETURNING artifact_id',
                (str(uuid4()), task_id, run_id, name, relative_path, task_id, token)).fetchone()
            if row:
                return True
            if not connection.execute('SELECT 1 FROM datalab_tasks WHERE task_id=%s AND lease_id=%s', (task_id, token)).fetchone():
                raise RuntimeError('任务租约失效，禁止旧 worker 登记产物')
        return False

    def artifacts(self, task_id, project_id, *, with_fence=False):
        self.get_task(task_id,project_id)
        columns = 'artifact_id,run_id,name,fence' if with_fence else 'artifact_id,run_id,name'
        with self.connection() as connection:
            return connection.execute(f'SELECT {columns} FROM datalab_artifacts WHERE task_id=%s ORDER BY name', (task_id,)).fetchall()

    def artifact(self, artifact_id, project_id):
        with self.connection() as connection:
            row = connection.execute('SELECT a.* FROM datalab_artifacts a JOIN datalab_tasks t USING(task_id) WHERE a.artifact_id=%s AND t.project_id=%s', (artifact_id,project_id)).fetchone()
        if not row:
            raise KeyError('当前项目不存在该产物')
        return row
