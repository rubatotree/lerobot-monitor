/* Browser verification for the independent manager; remote APIs are mocked. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); }
catch { ({chromium} = require(process.env.CODEX_PLAYWRIGHT_PATH || 'C:/Users/Admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright')); }

async function main() {
  const origin = process.env.CLOUD_UI_URL || 'http://127.0.0.1:8095';
  const output = path.resolve('.tmp_cloud_ui'); await fs.mkdir(output,{recursive:true});
  const browser = await chromium.launch({headless:true,channel:process.env.CLOUD_UI_BROWSER || 'chrome'});
  try {
    const page = await browser.newPage();
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    const requests=[];
    const hosts=[{id:'4090',alias:'8x4090-server',root:'/data/zhuyutian/lerobot-monitor',port:8091,status:'connected'},{id:'a6000',alias:'8A6000-server',root:'/data2/zhuyutian/lerobot-monitor',port:8091,status:'disconnected'}];
    const gpus=Array.from({length:8},(_,index)=>({index,uuid:`GPU-${index}-example-382ae1a6`,name:'NVIDIA GeForce RTX 4090',memory_total_mb:24564,memory_used_mb:index===3?18040:0,healthy:index!==0,busy:index===3}));
    const deployments=[{id:'smolvla',name:'SmolVLA · pick and place',source_kind:'huggingface',source:'lerobot/smolvla_base',revision:'main',status:'loaded',gpu_uuid:gpus[3].uuid},{id:'pi',name:'π0.5 base',source_kind:'huggingface',source:'lerobot/pi05_base',status:'ready'},{id:'act',name:'ACT · local checkpoint',source_kind:'path',source:'/data/zhuyutian/models/act/pretrained_model',status:'failed',error:'推理环境缺少依赖，请先配置模型运行环境。'}];
    await page.route('**/api/**',async route=>{
      const req=route.request(),url=new URL(req.url());requests.push({path:url.pathname,method:req.method()});let body={};
      if(url.pathname==='/api/ui-config')body={mode:'manager'};
      else if(url.pathname==='/api/hosts')body=hosts;
      else if(url.pathname.endsWith('/gpus'))body=gpus;
      else if(url.pathname.endsWith('/health'))body={status:'ok',version:'1'};
      else if(url.pathname.endsWith('/deployments'))body=deployments;
      else if(url.pathname.endsWith('/jobs'))body=url.pathname==='/api/jobs'?[]:[{id:'j1',kind:'load',status:'succeeded',message:'SmolVLA loaded on GPU 3',created_at:'2026-09-28T04:15:00Z'},{id:'j2',kind:'deploy',status:'running',message:'Downloading π0.5 model weights',created_at:'2026-09-28T04:20:00Z'}];
      await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
    });
    for(const width of [390,768,1440,1920]) {
      await page.setViewportSize({width,height:1000});await page.goto(origin);await page.locator('.gpu').first().waitFor();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`horizontal overflow at ${width}`);
      await page.screenshot({path:path.join(output,`manager-${width}.png`),fullPage:true});
    }
    await page.setViewportSize({width:390,height:844});await page.getByRole('button',{name:'+ 添加模型',exact:true}).click();
    await page.getByLabel('显示名称').fill('Kept while polling');
    await page.getByRole('button',{name:'刷新状态',exact:true}).evaluate(button=>button.click());
    await page.waitForFunction(()=>!document.getElementById('refresh').disabled);
    assert.equal(await page.getByLabel('显示名称').inputValue(),'Kept while polling');
    await page.screenshot({path:path.join(output,'add-model-390.png'),fullPage:true});
    await page.getByRole('button',{name:'取消',exact:true}).click();
    await page.getByRole('button',{name:/8A6000-server/}).click();
    assert.equal(await page.locator('#workspace').isHidden(),true);
    assert.equal(requests.some(req=>req.method!=='GET'),false,'review must not mutate live servers');
    assert.deepEqual(errors,[]);
    await page.close();
    // Real backend smoke uses GET only and never activates a host action.
    const actual=await browser.newPage({viewport:{width:1440,height:1000}});
    await actual.goto(origin);await actual.locator('#server-title').waitFor();
    await actual.waitForFunction(()=>!document.getElementById('refresh').disabled);
    await actual.screenshot({path:path.join(output,'manager-actual.png'),fullPage:true});
    console.log(`PASS: four responsive sizes, stable dialog, offline selection, no browser errors. Screenshots: ${output}`);
  } finally {await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
