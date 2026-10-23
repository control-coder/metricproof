"""真实 PostgreSQL、Redis/Celery 和 Docker 联动；角色明确使用固定演示。"""
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch
from uuid import uuid4


@unittest.skipUnless(os.environ.get('DATALAB_APP_TESTS') == '1','需显式启用真实应用集成')
class AppIntegrationTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from datalab.api.app import create_app
        from datalab.settings import Settings
        from datalab.storage.repository import Repository
        # 使用临时模型配置并显式切到固定演示，既不依赖也不改写本机 .env。
        import tempfile
        scratch=(Path(__file__).resolve().parents[2]/'.tmp').resolve()
        scratch.mkdir(exist_ok=True)
        folder=tempfile.TemporaryDirectory(dir=scratch,prefix='app-env-')
        self.addCleanup(folder.cleanup)
        self.env_file=Path(folder.name)/'.env'
        self.env_file.write_text((Path(__file__).resolve().parents[2]/'.env.example').read_text(encoding='utf-8')
                                 .replace('DATALAB_MODEL_PROVIDER=mimo','DATALAB_MODEL_PROVIDER=guided'),encoding='utf-8')
        environment=patch.dict(os.environ,{'DATALAB_ENV_FILE':str(self.env_file),'DATALAB_MIMO_API_KEY':'','DATALAB_DEEPSEEK_API_KEY':''})
        environment.start()
        self.addCleanup(environment.stop)
        self.settings=Settings.load()
        self.repository=Repository(self.settings.database_url)
        self.client=TestClient(create_app(self.settings),headers={'X-Datalab-Client':'local-ui'})
        self.addCleanup(self.client.close)
        response=self.client.post('/api/projects',json={'name':'集成-test-'+uuid4().hex[:8]})
        self.assertEqual(response.status_code,201,response.text)
        self.project=response.json()['project_id']
        self.addCleanup(self.cleanup_project)
        self.base='/api/projects/'+self.project
        data=(Path(__file__).resolve().parents[2]/'examples/orders.csv').read_bytes()
        response=self.client.post(self.base+'/datasets',files={'file':('orders.csv',data,'text/csv')},
            data={'dataset_family':'orders','currency':'CNY','timezone':'Asia/Shanghai','mapping':'{}'})
        self.assertEqual(response.status_code,201,response.text)
        self.dataset=response.json()['dataset_version']
        self.assertEqual(response.json()['field_semantics_candidates'],6)

    def cleanup_project(self):
        with self.repository.connection() as connection:
            connection.execute('DELETE FROM datalab_artifacts WHERE task_id IN (SELECT task_id FROM datalab_tasks WHERE project_id=%s)',(self.project,))
            for table in ('datalab_tasks','datalab_memories','datalab_datasets','datalab_projects'):
                connection.execute(f'DELETE FROM {table} WHERE project_id=%s',(self.project,))

    def wait_task(self,identifier,states):
        until=time.monotonic()+45
        while time.monotonic()<until:
            response=self.client.get(self.base+'/tasks/'+identifier)
            self.assertEqual(response.status_code,200,response.text)
            value=response.json()
            if value['state'] in states:
                return value
            if value['state'] in {'FAILED','CANCELLED'}:
                self.fail(str(value))
            time.sleep(.2)
        self.fail('任务未在时限内进入预期状态')

    def confirm_rule(self):
        response=self.client.post(self.base+'/memories',json={'dataset_version':self.dataset,'name':'成交金额',
            'definition':'仅计算已支付且未退款订单','included_statuses':['paid']})
        self.assertEqual(response.status_code,201,response.text)
        memory_id=response.json()['memory_id']
        response=self.client.post(self.base+'/memories/'+memory_id+'/confirm',
            json={'dataset_version':self.dataset,'expected_current_id':None})
        self.assertEqual(response.status_code,200,response.text)
        return memory_id

    def test_confirmation_queue_report_download_and_refresh(self):
        response=self.client.post(self.base+'/tasks',json={'dataset_version':self.dataset,'question':'按渠道汇总金额',
                                                           'options':{'kind':'ranking','group_by':'channel'}})
        self.assertEqual(response.status_code,202,response.text)
        task_id=response.json()['task_id']
        waiting=self.wait_task(task_id,{'WAITING_CONFIRMATION'})
        self.assertEqual(waiting['snapshot']['budget']['calls'],0)
        memory_id=self.confirm_rule()
        response=self.client.post(self.base+'/tasks/'+task_id+'/confirm',json={'memory_id':memory_id})
        self.assertEqual(response.status_code,200,response.text)
        complete=self.wait_task(task_id,{'SUCCEEDED'})
        self.assertEqual(complete['snapshot']['verifications'][-1]['reference']['total']['amount_cents'],35000)
        self.assertEqual(complete['snapshot']['budget']['tokens'],0)
        # trace 随任务快照落库：等待确认阶段没有调用，恢复后依次是三个角色；只有字段路径与指纹，没有内容。
        trace=complete['snapshot']['trace']
        self.assertEqual([item['role'] for item in trace],['Planner','Analyst','Reviewer'])
        self.assertTrue(all(item['outcome']=='ok' and len(item['handoff_sha256'])==64 for item in trace))
        self.assertNotIn('profile.dataset_family',trace[0]['handoff_fields'])
        self.assertEqual(complete['request']['mode'],'guided-demo')
        artifact=next(item for item in complete['artifacts'] if item['name']=='结果表.csv')
        download=self.client.get(self.base+'/artifacts/'+artifact['artifact_id'])
        self.assertEqual(download.status_code,200)
        self.assertIn('22000',download.content.decode('utf-8-sig'))
        # 两条路径共用的派生产物：模板说明标明口径版本与期间，图表可内联查看与下载。
        narrative=complete['narrative']
        self.assertIn(complete['snapshot']['metric']['version'],narrative['text'])
        self.assertIn('自然流量',narrative['text'])
        charts=sorted(item['name'] for item in complete['artifacts'] if item['name'].endswith('.png'))
        self.assertEqual(charts,['图表-1.png','图表-2.png'])
        chart=next(item for item in complete['artifacts'] if item['name']=='图表-1.png')
        inline=self.client.get(self.base+'/artifacts/'+chart['artifact_id']+'?inline=true')
        self.assertEqual(inline.headers['content-type'],'image/png')
        self.assertTrue(inline.content.startswith(b'\x89PNG'))
        self.assertEqual(self.client.get(self.base+'/artifacts/'+artifact['artifact_id']+'?inline=true').headers['content-type'],
                         'application/octet-stream')
        fields=[item for item in self.client.get(self.base+'/memories',params={'dataset_version':self.dataset}).json()
                if item['kind']=='field_semantics']
        self.assertEqual(sorted(item['name'] for item in fields),sorted(['order_id','payment_time','amount','status','channel','category']))
        self.assertTrue(all(item['status']=='pending' for item in fields))
        from datalab.api.app import create_app
        from fastapi.testclient import TestClient
        with TestClient(create_app(self.settings)) as refreshed:
            self.assertEqual(refreshed.get(self.base+'/tasks/'+task_id).json()['state'],'SUCCEEDED')
        # 重复分发不增加执行尝试，也不能覆盖结果。
        from celery import Celery
        publisher=Celery('test',broker=self.settings.broker_url)
        publisher.send_task('datalab.execute',args=[task_id])
        publisher.close()
        time.sleep(.5)
        self.assertEqual(len(self.client.get(self.base+'/tasks/'+task_id).json()['snapshot']['attempts']),1)

    def test_model_switch_and_missing_key(self):
        self.assertEqual(self.client.get('/api/model').json()['active'],'guided')
        response=self.client.put('/api/model',json={'provider':'deepseek'},headers={'X-Datalab-Client':''})
        self.assertEqual(response.status_code,403)
        response=self.client.put('/api/model',json={'provider':'deepseek'})
        self.assertEqual(response.status_code,200,response.text)
        state=response.json()
        self.assertEqual(state['active'],'deepseek')
        self.assertFalse(any(item['configured'] for item in state['providers']))
        self.assertNotIn('api_key',response.text)
        self.assertEqual(self.client.get('/api/health').json()['provider'],'deepseek')
        # 未配置密钥时拒绝创建模型任务，不静默降级为固定演示。
        response=self.client.post(self.base+'/tasks',json={'dataset_version':self.dataset,'question':'按渠道汇总金额'})
        self.assertEqual(response.status_code,400)
        self.assertIn('API Key',response.json()['detail'])
        self.assertEqual(self.client.put('/api/model',json={'provider':'gpt'}).status_code,422)
        self.client.put('/api/model',json={'provider':'mimo'})
        with patch.dict(os.environ,{'DATALAB_MIMO_API_KEY':'test-key-not-real'}):
            response=self.client.post(self.base+'/tasks',json={'dataset_version':self.dataset,'question':'按渠道汇总金额'})
        self.assertEqual(response.status_code,202,response.text)
        # 未确认口径时任务停在确认点，不会发出模型请求；记录创建时的模型供应商。
        waiting=self.wait_task(response.json()['task_id'],{'WAITING_CONFIRMATION'})
        self.assertEqual(waiting['request']['model'],{'provider':'mimo','model':'mimo-v2.6-flash'})
        self.assertEqual((waiting['snapshot']['budget']['calls'],waiting['snapshot']['budget']['max_tokens']),(0,150000))

    def test_project_scope_and_cross_site_write(self):
        other=str(uuid4())
        response=self.client.post('/api/projects/'+other+'/tasks',json={'dataset_version':self.dataset,'question':'不应访问'})
        self.assertEqual(response.status_code,404)
        response=self.client.post('/api/projects',json={'name':'不应创建'},headers={'Origin':'https://evil.example'})
        self.assertEqual(response.status_code,403)
        response=self.client.post('/api/projects',json={'name':'不应创建'},headers={'X-Datalab-Client':''})
        self.assertEqual(response.status_code,403)

    def test_invalid_csv_and_plan_rejected(self):
        response=self.client.post(self.base+'/datasets',files={'file':('bad.csv',b'bad')},
            data={'dataset_family':'orders','currency':'CNY','timezone':'Asia/Shanghai'})
        self.assertEqual(response.status_code,400)
        response=self.client.post(self.base+'/tasks',json={'dataset_version':self.dataset,'question':'对比',
                                                          'options':{'kind':'comparison'}})
        self.assertEqual(response.status_code,400)

    def test_olist_import_reads_only_the_fixed_directory(self):
        import dataclasses
        import random
        import tempfile
        from fastapi.testclient import TestClient
        from olist_bundle import bundle,write
        from datalab.api.app import create_app
        scratch=(Path(__file__).resolve().parents[2]/'.tmp').resolve()
        folder=tempfile.TemporaryDirectory(dir=scratch,prefix='app-olist-')
        self.addCleanup(folder.cleanup)
        settings=dataclasses.replace(self.settings,artifact_root=Path(folder.name),olist_raw=Path(folder.name)/'olist/raw')
        with TestClient(create_app(settings),headers={'X-Datalab-Client':'local-ui'}) as client:
            response=client.post(self.base+'/datasets/olist',json={'dataset_family':'olist'})
            self.assertEqual(response.status_code,400)
            self.assertIn({'table':'orders','reason':'文件缺失'},response.json()['issues'])
            write(Path(folder.name)/'olist/raw',bundle(random.Random(3),30))
            # 请求不能指定路径或表。
            self.assertEqual(client.post(self.base+'/datasets/olist',json={'dataset_family':'olist','path':'C:/'}).status_code,422)
            self.assertEqual(client.post('/api/projects/'+str(uuid4())+'/datasets/olist',json={'dataset_family':'olist'}).status_code,404)
            response=client.post(self.base+'/datasets/olist',json={'dataset_family':'olist'})
            self.assertEqual(response.status_code,201,response.text)
            value=response.json()
            self.assertEqual((value['schema'],value['row_count'],value['currency']),('olist',30,'BRL'))
            self.assertIn(value['dataset_version'],[item['dataset_version'] for item in client.get(self.base+'/datasets').json()])

    def test_olist_task_with_confirmed_amount_source_end_to_end(self):
        # 需要先运行 scripts/fetch_olist.py；导入真实 Olist 数据（约 9 秒），测试结束删除本次数据版本目录。
        if not (self.settings.olist_raw/'olist_orders_dataset.csv').is_file():
            self.skipTest('本机没有下载 Olist 原始数据')
        response=self.client.post(self.base+'/datasets/olist',json={'dataset_family':'olist-brazil'})
        self.assertEqual(response.status_code,201,response.text)
        version=response.json()['dataset_version']
        self.addCleanup(self.remove_dataset,version)
        rule={'dataset_version':version,'name':'已送达订单','definition':'只计已送达订单，金额取支付合计',
              'included_statuses':['delivered']}
        # 单表状态不适用于 Olist 口径；金额来源只能是两种之一。
        self.assertEqual(self.client.post(self.base+'/memories',json={**rule,'included_statuses':['paid']}).status_code,400)
        self.assertEqual(self.client.post(self.base+'/memories',json={**rule,'amount_source':'both'}).status_code,422)
        plain=self.client.post(self.base+'/memories',json={**rule,'name':'仅状态'}).json()['memory_id']
        self.client.post(self.base+'/memories/'+plain+'/confirm',json={'dataset_version':version,'expected_current_id':None})
        response=self.client.post(self.base+'/memories',json={**rule,'amount_source':'payments'})
        self.assertEqual(response.status_code,201,response.text)
        memory_id=response.json()['memory_id']
        self.client.post(self.base+'/memories/'+memory_id+'/confirm',json={'dataset_version':version,'expected_current_id':None})
        task={'dataset_version':version,'question':'各州订单金额排行','options':{'kind':'ranking','group_by':'customer_state'}}
        # 没有确认金额来源的口径不能用于订单金额分析；单表分组与无意义组合在创建时拒绝。
        self.assertEqual(self.client.post(self.base+'/tasks',json={**task,'memory_id':plain}).status_code,400)
        self.assertEqual(self.client.post(self.base+'/tasks',json={**task,'options':{'kind':'ranking','group_by':'channel'}}).status_code,400)
        self.assertEqual(self.client.post(self.base+'/tasks',json={**task,'options':{'kind':'trend','group_by':'customer_state'}}).status_code,400)
        # 单表数据上的 Olist 类型同样拒绝。
        self.assertEqual(self.client.post(self.base+'/tasks',json={'dataset_version':self.dataset,'question':'复购',
                                                                   'options':{'kind':'repeat_rate'}}).status_code,400)
        response=self.client.post(self.base+'/tasks',json={**task,'memory_id':memory_id})
        self.assertEqual(response.status_code,202,response.text)
        complete=self.wait_task(response.json()['task_id'],{'SUCCEEDED'})
        plan=complete['snapshot']['plan']
        self.assertEqual((plan['schema'],plan['amount_source']),('olist','payments'))
        self.assertTrue(all(item['passed'] for item in complete['snapshot']['verifications'][-1]['checks']))
        self.assertEqual(complete['snapshot']['verifications'][-1]['reference']['total']['order_count'],96478)
        response=self.client.post(self.base+'/tasks',json={'dataset_version':version,'question':'复购率',
                                                          'options':{'kind':'repeat_rate'},'memory_id':plain})
        self.assertEqual(response.status_code,202,response.text)
        complete=self.wait_task(response.json()['task_id'],{'SUCCEEDED'})
        names=[item['name'] for item in complete['artifacts']]
        self.assertIn('结果表.csv',names)
        # 核验通过后同样派生说明、图表与报告结论（复购率引用 total.repeat_rate）。
        self.assertIn('复购率',complete['narrative']['text'])
        self.assertIn(complete['snapshot']['metric']['version'],complete['narrative']['text'])
        self.assertIn('图表-1.png',names)
        report=next(item for item in complete['artifacts'] if item['name']=='核验报告.json')
        content=self.client.get(self.base+'/artifacts/'+report['artifact_id']).json()
        self.assertEqual([claim['result_pointer'] for claim in content['claims']],['/total/repeat_rate'])

    def remove_dataset(self,version):
        folder=(self.settings.artifact_root/'datasets'/version).resolve()
        if folder.parent==(self.settings.artifact_root/'datasets').resolve() and (folder/'metadata.json').is_file():
            for path in folder.iterdir():
                path.unlink()
            folder.rmdir()

    def test_single_claim_cancel_and_stale_lease(self):
        from datalab.orchestration.workflow import Task
        task=Task(self.project,self.dataset,'不派发的领取测试')
        self.repository.create_task(task,{'options':{},'mode':'guided-demo'})
        first=self.repository.claim(task.task_id)
        self.assertIsNotNone(first)
        self.assertIsNone(self.repository.claim(task.task_id))
        self.repository.cancel(task.task_id,self.project)
        self.assertTrue(self.repository.heartbeat(task.task_id,first[0]))
        with self.repository.connection() as connection:
            connection.execute("UPDATE datalab_tasks SET lease_until=now()-interval '1 minute' WHERE task_id=%s",(task.task_id,))
        self.repository.pending_ids()
        # 租约过期时已有取消请求：按取消收尾，而不是恢复或记为失败。
        self.assertEqual(self.repository.get_task(task.task_id)['state'],'CANCELLED')
        with self.assertRaises(RuntimeError):
            self.repository.save(task,first[0])

    def test_cancel_before_final_save_wins(self):
        from datalab.orchestration.workflow import State, Task
        task=Task(self.project,self.dataset,'并发取消边界')
        self.repository.create_task(task,{'options':{},'mode':'guided-demo'})
        token,_=self.repository.claim(task.task_id)
        self.repository.cancel(task.task_id,self.project)
        task.state=State.SUCCEEDED
        self.repository.save(task,token,release=True)
        row=self.repository.get_task(task.task_id,self.project)
        self.assertEqual(row['state'],'CANCELLED')
        self.assertEqual(row['snapshot']['state'],'CANCELLED')
    def test_memory_conflict_is_http_409(self):
        self.confirm_rule()
        pending=self.client.post(self.base+'/memories',json={'dataset_version':self.dataset,'name':'成交金额',
            'definition':'修订口径','included_statuses':['paid','refunded']}).json()['memory_id']
        response=self.client.post(self.base+'/memories/'+pending+'/confirm',
                                  json={'dataset_version':self.dataset,'expected_current_id':None})
        self.assertEqual(response.status_code,409)

    def expire_lease(self,task_id):
        with self.repository.connection() as connection:
            connection.execute("UPDATE datalab_tasks SET lease_until=now()-interval '1 minute' WHERE task_id=%s",(task_id,))

    def new_task(self,question='围栏与恢复'):
        from datalab.orchestration.workflow import Task
        task=Task(self.project,self.dataset,question)
        self.repository.create_task(task,{'options':{},'mode':'guided-demo'})
        return task

    def test_lease_expiry_requeues_from_checkpoint_once_then_fails(self):
        from datalab.orchestration.workflow import State
        task=self.new_task()
        first,row=self.repository.claim(task.task_id)
        self.assertEqual(row['fence'],1)
        task.move(State.PROFILING,'检查点')
        self.repository.save(task,first)
        self.expire_lease(task.task_id)
        self.assertIn(task.task_id,self.repository.pending_ids())
        requeued=self.repository.get_task(task.task_id)
        self.assertEqual((requeued['state'],requeued['lease_id'],requeued['snapshot']['state'],requeued['snapshot']['recoveries']),
                         ('QUEUED',None,'PROFILING',1))
        self.assertIn('从最近检查点恢复',requeued['snapshot']['timeline'][-1]['note'])
        with self.assertRaises(RuntimeError):
            self.repository.save(task,first)
        # 第二次领取拿到更大的 fence；再次过期时恢复次数已用尽，按原规则失败并保留证据。
        second,row=self.repository.claim(task.task_id)
        self.assertEqual(row['fence'],2)
        self.expire_lease(task.task_id)
        self.repository.pending_ids()
        failed=self.repository.get_task(task.task_id)
        self.assertEqual((failed['state'],failed['snapshot']['failure']),('FAILED','WORKER_LEASE_EXPIRED'))

    def test_artifact_index_rejects_stale_writers_and_only_moves_forward(self):
        task=self.new_task()
        run_id=str(uuid4())
        old,_=self.repository.claim(task.task_id)
        self.assertTrue(self.repository.add_artifact(task.task_id,run_id,'报告','runs/x/report.1.json',old))
        self.assertFalse(self.repository.add_artifact(task.task_id,run_id,'报告','runs/x/report.1.json',old))
        self.expire_lease(task.task_id)
        self.repository.pending_ids()
        new,row=self.repository.claim(task.task_id)
        self.assertEqual(row['fence'],2)
        self.assertTrue(self.repository.add_artifact(task.task_id,run_id,'报告','runs/x/report.2.json',new))
        # 旧 worker 迟到的登记被拒绝，索引仍指向新领取者的文件。
        with self.assertRaises(RuntimeError):
            self.repository.add_artifact(task.task_id,run_id,'报告','runs/x/report.late.json',old)
        with self.repository.connection() as connection:
            rows=connection.execute('SELECT relative_path,fence FROM datalab_artifacts WHERE task_id=%s',(task.task_id,)).fetchall()
        self.assertEqual([(item['relative_path'],item['fence']) for item in rows],[('runs/x/report.2.json',2)])

    def test_crashed_worker_task_is_recovered_end_to_end(self):
        from datalab.execution.runner import DockerRunner
        from datalab.orchestration import worker
        from datalab.orchestration.workflow import MetricSnapshot, Task
        meta=self.repository.dataset(self.project,self.dataset)
        metric=MetricSnapshot('recovery-test:v1',('paid',),'trusted-test',self.project,meta['dataset_family'],meta['schema_signature'])
        task=Task(self.project,self.dataset,'恢复演练',metric=metric)
        self.repository.create_task(task,{'options':{'kind':'ranking','group_by':'channel'},'mode':'guided-demo'})

        class Crash(BaseException):
            pass
        # 执行阶段进程消失：租约不释放，检查点停在 EXECUTING。
        with patch.object(DockerRunner,'execute_python',side_effect=Crash('worker killed')):
            with self.assertRaises(Crash):
                worker.process_task(task.task_id)
        stuck=self.repository.get_task(task.task_id)
        self.assertEqual((stuck['state'],stuck['snapshot']['state']),('EXECUTING','EXECUTING'))
        self.assertIsNotNone(stuck['lease_id'])
        self.expire_lease(task.task_id)
        self.repository.pending_ids()
        worker.process_task(task.task_id)
        done=self.repository.get_task(task.task_id)
        self.assertEqual(done['state'],'SUCCEEDED',done['snapshot'].get('failure'))
        self.assertEqual((done['fence'],done['snapshot']['recoveries']),(2,1))
        self.assertEqual(len(done['snapshot']['attempts']),1)
        self.assertTrue(any('从最近检查点恢复' in item['note'] for item in done['snapshot']['timeline']))
        with self.repository.connection() as connection:
            rows=connection.execute('SELECT name,relative_path,fence FROM datalab_artifacts WHERE task_id=%s',(task.task_id,)).fetchall()
        self.assertIn('核验报告.json',{item['name'] for item in rows})
        self.assertEqual({item['fence'] for item in rows},{2})
        report=next(item for item in rows if item['name']=='核验报告.json')
        self.assertTrue(report['relative_path'].endswith('report.2.json'))

    def test_idempotency_key_replay_conflict_and_race(self):
        from concurrent.futures import ThreadPoolExecutor
        from datalab.orchestration.workflow import Task
        payload={'dataset_version':self.dataset,'question':'按渠道汇总金额','options':{'kind':'ranking','group_by':'channel'}}
        key='idem-'+uuid4().hex
        first=self.client.post(self.base+'/tasks',json=payload,headers={'Idempotency-Key':key})
        self.assertEqual(first.status_code,202,first.text)
        again=self.client.post(self.base+'/tasks',json=payload,headers={'Idempotency-Key':key})
        self.assertEqual(again.status_code,202,again.text)
        self.assertEqual(again.json()['task_id'],first.json()['task_id'])
        self.assertTrue(again.json()['replayed'])
        self.assertNotIn('replayed',first.json())
        with self.repository.connection() as connection:
            count=connection.execute('SELECT count(*) AS n FROM datalab_tasks WHERE project_id=%s AND idempotency_key=%s',(self.project,key)).fetchone()['n']
        self.assertEqual(count,1)
        # 同一个键换了内容：拒绝，而不是悄悄返回另一个请求的任务。
        changed=self.client.post(self.base+'/tasks',json={**payload,'question':'换一个问题'},headers={'Idempotency-Key':key})
        self.assertEqual(changed.status_code,422,changed.text)
        # 不带键的请求互不影响；非法键被拒绝。
        one=self.client.post(self.base+'/tasks',json=payload).json()['task_id']
        two=self.client.post(self.base+'/tasks',json=payload).json()['task_id']
        self.assertNotEqual(one,two)
        self.assertEqual(self.client.post(self.base+'/tasks',json=payload,headers={'Idempotency-Key':'has space'}).status_code,422)
        # 并发重试同一个键：只创建一个任务，其余拿到同一个 task_id。
        race='race-'+uuid4().hex

        def submit(_):
            task=Task(self.project,self.dataset,'并发幂等')
            return self.repository.create_task(task,{'options':{},'mode':'guided-demo'},idempotency_key=race,request_hash='h')
        with ThreadPoolExecutor(8) as pool:
            results=list(pool.map(submit,range(8)))
        self.assertEqual(len({item[0] for item in results}),1)
        self.assertEqual(sum(1 for item in results if item[1]),1)
