// 归因调查（A9）浏览器流程：真实模型模式 + Olist 数据 + 已确认口径。
// 前提：全栈运行在 http://127.0.0.1:18080（模型模式），已有 Olist 数据集与一条已确认口径。
// 项目 / 数据集 / 口径可用环境变量覆盖；默认通过 API 取第一个项目、第一个 Olist 数据集、
// 该数据集的第一条已确认 metric 口径。不在这里启动任何后端服务。
import {chromium} from '@playwright/test';
import {fileURLToPath} from 'node:url';
import {resolve,dirname} from 'node:path';
import {mkdirSync} from 'node:fs';

const BASE=process.env.DATALAB_BASE||'http://127.0.0.1:18080';
const HEADERS={'X-Datalab-Client':'local-ui','Content-Type':'application/json'};
const TERMINAL=['SUCCEEDED','FAILED','CANCELLED'];
// 调查预算 600 秒 + 排队与读取时间，上限 12 分钟。
const TIMEOUT=12*60*1000;
const shots=resolve(dirname(fileURLToPath(import.meta.url)),'../../artifacts/screenshots');
mkdirSync(shots,{recursive:true});
const log=line=>process.stdout.write(line+'\n');

async function api(path,options={}){
 const response=await fetch(BASE+'/api'+path,{...options,headers:{...HEADERS,...options.headers}});
 const data=await response.json();
 if(!response.ok)throw new Error(path+' → '+response.status+' '+JSON.stringify(data));
 return data;
}

// 选取项目、Olist 数据集与已确认口径；环境变量 DATALAB_PROJECT / DATALAB_DATASET / DATALAB_MEMORY 可覆盖。
async function pickScope(){
 const projects=await api('/projects');
 const projectId=process.env.DATALAB_PROJECT||projects[0]?.project_id;
 if(!projectId)throw new Error('没有可用项目');
 const datasets=await api(`/projects/${projectId}/datasets`);
 const olist=datasets.filter(item=>item.schema==='olist'||item.schema_signature?.includes('olist'));
 const datasetVersion=process.env.DATALAB_DATASET||(olist[0]||datasets[0])?.dataset_version;
 if(!datasetVersion)throw new Error('没有可用数据集（需要 Olist 多表数据）');
 const memories=await api(`/projects/${projectId}/memories?dataset_version=${datasetVersion}`);
 const confirmed=memories.filter(item=>item.kind==='metric'&&item.status==='confirmed');
 const memoryId=process.env.DATALAB_MEMORY||confirmed[0]?.memory_id;
 if(!memoryId)throw new Error('没有已确认的 metric 口径');
 return {projectId,datasetVersion,memoryId};
}

const browser=await chromium.launch({channel:'msedge',headless:true});
const page=await browser.newPage({viewport:{width:1440,height:960}});
const errors=[];page.on('pageerror',e=>errors.push(String(e)));
try{
 const scope=await pickScope();
 log(`范围：项目 ${scope.projectId} · 数据集 ${scope.datasetVersion.slice(0,8)} · 口径 ${scope.memoryId.slice(0,8)}`);
 await page.goto(BASE);
 // 页面按 localStorage 记忆项目与数据版本；换成本次范围，保证表单选项来自同一数据集。
 await page.evaluate(({projectId,datasetVersion})=>{localStorage.setItem('datalab-project',projectId);localStorage.setItem('datalab-dataset',datasetVersion);},scope);
 await page.reload();
 await page.getByRole('button',{name:/分析任务/}).click();
 await page.getByRole('tab',{name:'归因调查'}).click();
 await page.getByLabel('归因问题').fill('2018 年 3 月 GMV 相比 2 月（环比）为什么变化？');
 await page.getByLabel('调查口径').selectOption(scope.memoryId);
 const metricSelect=page.getByLabel('调查指标');
 if(await metricSelect.locator('option[value="gmv"]').count())await metricSelect.selectOption('gmv');
 await page.getByLabel('本期起点').fill('2018-03-01');
 await page.getByLabel('本期终点').fill('2018-04-01');
 await page.getByLabel('对比期起点').fill('2018-02-01');
 await page.getByLabel('对比期终点').fill('2018-03-01');
 await page.getByRole('button',{name:'提交归因调查'}).click();
 // 提交后跳到报告页；WAITING_CONFIRMATION 不算终态，人在回路期间继续等待（本流程不代答）。
 await page.waitForURL(/#report\//);
 const taskId=page.url().split('#report/')[1];
 log('任务 '+taskId+' 已创建，开始轮询（每 15 秒报告一次）');
 const started=Date.now();
 let detail=null;
 while(Date.now()-started<TIMEOUT){
  detail=await api(`/projects/${scope.projectId}/tasks/${taskId}`);
  const state=detail.state;
  log(`[${new Date().toISOString()}] 状态 ${state}（已等待 ${Math.round((Date.now()-started)/1000)} s）`);
  if(TERMINAL.includes(state))break;
  await new Promise(done=>setTimeout(done,15000));
 }
 if(!detail||!TERMINAL.includes(detail.state))throw new Error('12 分钟内未达到终态（最后状态 '+(detail?.state||'未知')+'）');
 log('终态 '+detail.state+'，加载报告视图');
 await page.reload();
 // 报告视图：摘要、至少一个核验等级徽标、轨迹时间线。
 await page.getByRole('heading',{name:'结论摘要'}).waitFor({timeout:60000});
 const badges=page.locator('.badge[class*="level-"]');
 if(await badges.count()<1)throw new Error('报告页没有核验等级徽标');
 await page.getByRole('heading',{name:'轨迹时间线'}).waitFor();
 const size=page.viewportSize();
 const height=await page.evaluate(()=>document.documentElement.scrollHeight);
 await page.setViewportSize({width:size.width,height});
 await page.screenshot({path:resolve(shots,'investigation-report.png')});
 await page.setViewportSize(size);
 if(errors.length)throw new Error(errors.join(';'));
 log('归因调查报告页通过：摘要、核验徽标、轨迹时间线齐全，截图已写入 artifacts/screenshots/investigation-report.png');
}finally{await browser.close();}
