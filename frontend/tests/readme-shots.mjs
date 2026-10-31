// README 截图：真实模型模式下从新建项目走完两次归因调查，生成数据、记忆、任务、报告与人在回路截图。
// 前提：全栈运行在 http://127.0.0.1:18080 且模型为 MiMo；Olist 原始数据已由 scripts/fetch_olist.py 下载。
// 调查一给定指标与期间，展示完整报告；调查二不给指标与期间，主 Agent 应调用 ask_user 暂停等待答复。
// 每次调查约 6–15 次模型调用；调查一不足 2 个子 Agent 分支时重跑，至多 3 次。
// 截图目录由 DATALAB_SHOT_DIR 指定，默认 artifacts/screenshots。
import {chromium} from '@playwright/test';
import {fileURLToPath} from 'node:url';
import {resolve,dirname} from 'node:path';
import {mkdirSync} from 'node:fs';

const BASE=process.env.DATALAB_BASE||'http://127.0.0.1:18080';
const HEADERS={'X-Datalab-Client':'local-ui'};
const TERMINAL=['SUCCEEDED','FAILED','CANCELLED'];
const TIMEOUT=15*60*1000;
const QUESTION='2018 年 3 月送达延迟率环比为什么上升？';
const VAGUE='最近履约体验好像变差了，问题主要出在哪里？';
const shots=process.env.DATALAB_SHOT_DIR||resolve(dirname(fileURLToPath(import.meta.url)),'../../artifacts/screenshots');
mkdirSync(shots,{recursive:true});
const log=line=>process.stdout.write(line+'\n');

const browser=await chromium.launch({channel:'msedge',headless:true});
const page=await browser.newPage({viewport:{width:1440,height:960}});
const errors=[];page.on('pageerror',e=>errors.push(String(e)));
// 侧栏高度跟随视口：先把视口拉到整页高度再截图，避免侧栏在半途截断。clip 给出时只截页面的一段。
async function shot(name,clip){
 await page.evaluate(()=>document.activeElement instanceof HTMLElement&&document.activeElement.blur());
 const size=page.viewportSize();const height=await page.evaluate(()=>document.documentElement.scrollHeight);
 await page.setViewportSize({width:size.width,height});
 const box=clip?await clip():undefined;
 await page.screenshot({path:resolve(shots,name+'.png'),...(box?{clip:box}:{})});
 await page.setViewportSize(size);log('截图 '+name+'.png');
}
const card=title=>page.locator('section.card',{has:page.getByRole('heading',{name:title,exact:true})});
const bottomOf=async title=>{const box=await card(title).boundingBox();return box.y+box.height;};
const topOf=async title=>(await card(title).boundingBox()).y;
async function status(projectId,taskId){const response=await fetch(`${BASE}/api/projects/${projectId}/tasks/${taskId}`,{headers:HEADERS});return response.json();}
async function openInvestigationForm(question){
 await page.getByRole('button',{name:/分析任务/}).click();
 await page.getByRole('tab',{name:'归因调查'}).click();
 await page.getByLabel('归因问题').fill(question);
 await page.getByLabel('调查口径').selectOption({index:1});
}
async function fillPeriods(){
 await page.getByLabel('调查指标').selectOption('late_delivery_rate');
 await page.getByLabel('本期起点').fill('2018-03-01');
 await page.getByLabel('本期终点').fill('2018-04-01');
 await page.getByLabel('对比期起点').fill('2018-02-01');
 await page.getByLabel('对比期终点').fill('2018-03-01');
}
// 提交后轮询到终态；遇到等待确认时先截图（只截第一次），再按界面上第一个候选答复。
async function runInvestigation(pendingShot){
 await page.getByRole('button',{name:'提交归因调查'}).click();
 await page.waitForURL(/#report\//);
 const taskId=page.url().split('#report/')[1];
 const projectId=await page.evaluate(()=>localStorage.getItem('datalab-project'));
 log('调查任务 '+taskId+' 已创建');
 const started=Date.now();let detail=null;let paused=0;
 while(Date.now()-started<TIMEOUT){
  detail=await status(projectId,taskId);
  log(`[${Math.round((Date.now()-started)/1000)} s] ${detail.state}`);
  if(TERMINAL.includes(detail.state))break;
  const pending=detail.investigation?.pending;
  if(detail.state==='WAITING_CONFIRMATION'&&pending){
   log('等待确认：'+pending.question+' 选项 '+JSON.stringify(pending.options||[]));
   await page.getByRole('heading',{name:'等待确认'}).waitFor();
   if(pendingShot&&!paused)await shot(pendingShot,async()=>({x:0,y:0,width:1440,height:await bottomOf('等待确认')+24}));
   paused++;
   if(pending.type==='metric'){page.once('dialog',dialog=>dialog.accept());await page.getByRole('button',{name:'确认口径并继续'}).click();}
   else if((pending.options||[]).length)await page.getByRole('button',{name:pending.options[0],exact:true}).click();
   else{await page.getByLabel('自由答复').fill('按已确认口径，看最近一个完整月与上月');await page.getByRole('button',{name:'提交答复'}).click();}
   await page.getByRole('heading',{name:'等待确认'}).waitFor({state:'detached',timeout:60000});
  }
  await new Promise(done=>setTimeout(done,10000));
 }
 if(!detail||!TERMINAL.includes(detail.state))throw new Error('超时未达到终态（最后状态 '+(detail?.state||'未知')+'）');
 log(`终态 ${detail.state}；模型调用 ${detail.investigation?.usage?.model_calls??'?'} 次；暂停答复 ${paused} 次`);
 return detail;
}
try{
 await page.goto(BASE);
 await page.getByLabel('新建项目').fill('2018 年 3 月经营复盘');
 await page.getByRole('button',{name:'创建项目'}).click();
 await page.getByLabel('数据语义家族').fill('Olist 巴西电商');
 await page.getByRole('button',{name:'导入 Olist 多表数据'}).click();
 await page.getByText('Olist 多表数据已导入',{exact:false}).waitFor({timeout:180000});
 await page.getByText('笔规范化订单').waitFor();
 await shot('workbench');
 // 业务口径：保存候选后必须明确确认，调查表单只列出已确认口径。
 await page.getByRole('button',{name:/项目记忆/}).click();
 await page.getByLabel('指标名称').fill('经营指标口径');
 await page.getByLabel('业务定义').fill('只纳入 delivered 订单；订单金额取该单全部支付之和；延迟指实际送达日期晚于预计送达日期');
 await page.getByRole('button',{name:'保存为待确认候选'}).click();
 page.once('dialog',dialog=>dialog.accept());
 await page.getByRole('button',{name:'明确确认'}).click();
 await page.getByText('已确认',{exact:true}).first().waitFor();
 await shot('memory');

 // 调查一：指标与期间明确，展示完整报告。
 // 分派几个子 Agent 由模型决定；截图要展示并行分派，不足 2 个分支时重跑，至多 3 次。
 let first=null;
 for(let round=1;round<=3;round++){
  await openInvestigationForm(QUESTION);
  await fillPeriods();
  first=await runInvestigation(null);
  const branches=first.investigation?.branches?.length||0;
  log(`调查一第 ${round} 次：${first.state}，子 Agent 分支 ${branches} 个`);
  if(first.state==='SUCCEEDED'&&branches>=2)break;
 }
 if(first.state!=='SUCCEEDED')throw new Error('调查一未成功：'+first.state);
 await page.reload();
 await page.getByRole('heading',{name:'结论摘要'}).waitFor({timeout:60000});
 await page.getByRole('heading',{name:'轨迹时间线'}).waitFor();
 await shot('report',async()=>({x:0,y:0,width:1440,height:await bottomOf('指标树')+24}));
 await shot('report-drilldown',async()=>{const top=await topOf('贡献度表');return {x:222,y:top-24,width:1440-222,height:await bottomOf('贡献度表')-top+48};});
 await shot('report-trace',async()=>{const top=await topOf('核验');return {x:222,y:top-24,width:1440-222,height:await bottomOf('轨迹时间线')-top+48};});

 // 调查二：不给指标与期间，主 Agent 应先向用户澄清（人在回路）。
 await openInvestigationForm(VAGUE);
 const second=await runInvestigation('pending');
 log('调查二终态 '+second.state);

 // 任务页：表单填好调查一的问题（不再提交），任务记录里能看到两次调查。刷新以清掉上一步的提示条。
 await page.reload();
 await openInvestigationForm(QUESTION);
 await fillPeriods();
 await page.getByRole('cell',{name:VAGUE}).waitFor();
 await shot('tasks');
 if(errors.length)throw new Error(errors.join(';'));
 log('完成，截图目录 '+shots);
}finally{await browser.close();}
