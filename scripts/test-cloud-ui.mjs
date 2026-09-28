import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {apiBase, apiRequest, createApp, deploymentActions, escapeHTML, gpuAvailable} from '../src/lerobot_monitor/cloud/web/app.js';

const require = createRequire(import.meta.url);
const {JSDOM} = require('jsdom');
const html = await readFile(new URL('../src/lerobot_monitor/cloud/web/index.html',import.meta.url),'utf8');
const pause = () => new Promise(resolve => setTimeout(resolve,0));
async function until(predicate) {for(let i=0;i<100;i++){if(predicate())return;await pause();}assert.ok(predicate(),'UI operation did not finish');}

async function fixture(t,mode='manager') {
  const dom = new JSDOM(html,{url:'http://127.0.0.1:8095'});
  const doc = dom.window.document;
  dom.window.HTMLDialogElement.prototype.showModal = function(){this.open=true;};
  dom.window.HTMLDialogElement.prototype.close = function(){this.open=false;};
  const oldFetch = globalThis.fetch, oldFormData = globalThis.FormData;
  globalThis.FormData = dom.window.FormData;
  const calls=[];
  const hosts=[{id:'4090',alias:'8x4090-server',root:'/data/test',status:'connected'},{id:'a6000',alias:'8A6000-server',root:'/data2/test',status:'disconnected'}];
  const models=[{id:'m1',name:'SmolVLA',source_kind:'huggingface',source:'lerobot/smolvla_base',status:'ready'}];
  const gpus=[{index:1,uuid:'GPU-free',name:'RTX 4090',healthy:true,memory_total_mb:24576,memory_used_mb:0},{index:2,uuid:'GPU-busy',name:'RTX 4090',healthy:true,busy:true,memory_total_mb:24576,memory_used_mb:20000},{index:0,uuid:'GPU-broken',name:'RTX 4090',healthy:false}];
  let failure=null;
  globalThis.fetch = async (url,options={}) => {
    calls.push({url,method:options.method || 'GET',headers:options.headers,body:options.body ? JSON.parse(options.body) : undefined});
    if(failure && url.includes(failure.path))return new Response(JSON.stringify({detail:failure.message}),{status:failure.status});
    let result={};
    if(url==='/api/ui-config')result={mode};
    else if(url==='/api/hosts')result=hosts;
    else if(url.endsWith('/health'))result={status:'ok'};
    else if(url.endsWith('/gpus'))result=gpus;
    else if(url.endsWith('/deployments') && options.method==='GET')result=models;
    else if(url.endsWith('/jobs'))result=[];
    else if(url.endsWith('/logs'))result={lines:['first line','second line']};
    return new Response(JSON.stringify(result),{status:200});
  };
  const app=createApp(doc);await app.init();
  t.after(()=>{app.stop();dom.window.close();globalThis.fetch=oldFetch;globalThis.FormData=oldFormData;});
  const click=selector=>{const element=doc.querySelector(selector);assert.ok(element,selector);element.click();};
  const set=(name,value)=>{doc.querySelector(`[name="${name}"]`).value=value;};
  const submit=id=>doc.getElementById(id).dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true}));
  return {dom,doc,app,calls,hosts,models,gpus,click,set,submit,fail:value=>{failure=value;}};
}

test('API paths, unsafe text and availability have explicit boundaries',()=>{
  assert.equal(apiBase('manager','server/unsafe'),'/api/hosts/server%2Funsafe/cloud/api/v1');
  assert.equal(apiBase('cloud',null),'/api/v1');assert.equal(apiBase('manager',null),null);
  assert.equal(escapeHTML('<img src=x onerror="alert(1)">'), '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');
  assert.equal(gpuAvailable({uuid:'GPU-1',healthy:false}),false);
  assert.equal(gpuAvailable({uuid:'GPU-1',busy:true}),false);
  assert.equal(gpuAvailable({uuid:'GPU-1'},[{gpu_uuid:'GPU-1',status:'loading'}]),false);
  assert.deepEqual(deploymentActions({status:'loaded'}),{load:false,unload:true,remove:false});
});

test('offline host selection never connects or initializes a server',async t=>{
  const f=await fixture(t);
  assert.equal(f.doc.getElementById('workspace').hidden,false);
  f.click('[data-host="a6000"]');await until(()=>!f.app.state.refreshing);
  assert.equal(f.doc.getElementById('workspace').hidden,true);
  assert.equal(f.doc.getElementById('server-title').textContent,'8A6000-server');
  assert.equal(f.calls.filter(call=>call.method!=='GET').length,0);
  assert.equal(f.calls.some(call=>call.url.includes('/a6000/cloud/')),false);
});

test('polling preserves model dialog input and load uses explicit available GPU UUID',async t=>{
  const f=await fixture(t);
  f.click('#add-model');f.set('name','Unsaved draft');await f.app.refresh();
  assert.equal(f.doc.querySelector('[name=name]').value,'Unsaved draft');
  f.click('[data-close]');f.click('[data-model-action=load]');
  const select=f.doc.querySelector('[name=gpu_uuid]');
  assert.equal(select.value,'');assert.equal(select.options.length,2);
  assert.equal(select.options[1].value,'GPU-free');
  f.set('gpu_uuid','GPU-free');f.submit('dialog-form');
  await until(()=>!f.doc.getElementById('form-dialog').open);
  const request=f.calls.find(call=>call.url.endsWith('/m1/load'));
  assert.deepEqual(request.body,{gpu_uuid:'GPU-free'});
});

test('local upload uses manager endpoint, no cloud path registration from browser',async t=>{
  const f=await fixture(t);f.click('#add-model');f.set('name','My checkpoint');f.set('source_kind','upload');
  f.doc.querySelector('[name=source_kind]').dispatchEvent(new f.dom.window.Event('change'));
  f.set('source','D:\\models\\checkpoint');f.submit('dialog-form');
  await until(()=>!f.doc.getElementById('form-dialog').open);
  const request=f.calls.find(call=>call.url.endsWith('/upload'));
  assert.deepEqual(request.body,{name:'My checkpoint',path:'D:\\models\\checkpoint'});
  assert.equal(f.calls.some(call=>call.method==='POST'&&call.url.endsWith('/deployments')),false);
});

test('failed request keeps form content and displays actionable server error',async t=>{
  const f=await fixture(t);f.click('#add-model');f.set('name','Example');f.set('source','test/model');
  f.fail({path:'/deployments',status:409,message:'A deployment already uses this source.'});f.submit('dialog-form');
  await until(()=>!f.doc.getElementById('dialog-error').hidden);
  assert.equal(f.doc.getElementById('form-dialog').open,true);
  assert.equal(f.doc.querySelector('[name=name]').value,'Example');
  assert.match(f.doc.getElementById('dialog-error').textContent,/already uses/);
  assert.equal(f.doc.getElementById('dialog-submit').disabled,false);
});

test('direct cloud token remains in memory and only authenticates cloud API requests',async t=>{
  const f=await fixture(t,'cloud');
  assert.equal(f.doc.getElementById('sidebar').hidden,true);
  assert.equal(f.calls.some(call=>call.url.startsWith('/api/v1')),false);
  f.set('token','secret-token');f.submit('auth-form');await until(()=>f.app.state.connected);
  assert.equal(f.doc.querySelector('[name=token]').value,'');
  assert.ok(f.calls.filter(call=>call.url.startsWith('/api/v1')).every(call=>call.headers.Authorization==='Bearer secret-token'));
  assert.equal(f.dom.window.localStorage.length,0);assert.equal(f.dom.window.sessionStorage.length,0);
  assert.equal(f.doc.body.textContent.includes('secret-token'),false);
});

test('runtime installer uses selected host and preserves optional cache location',async t=>{
  const f=await fixture(t);f.click('[data-host-action=runtime]');f.set('wheel_path','D:\\packages\\lerobot.whl');f.set('profile','smolvla');f.set('huggingface_home','/data/hf');f.submit('dialog-form');
  await until(()=>!f.doc.getElementById('form-dialog').open);
  const request=f.calls.find(call=>call.url.endsWith('/4090/runtime'));
  assert.deepEqual(request.body,{wheel_path:'D:\\packages\\lerobot.whl',profile:'smolvla',huggingface_home:'/data/hf'});
});

test('upgrade is shown only for connected server and posts after dialog submission',async t=>{
  const f=await fixture(t);f.click('[data-host-action=upgrade]');
  assert.equal(f.calls.some(call=>call.url.endsWith('/upgrade')),false);
  f.submit('dialog-form');await until(()=>!f.doc.getElementById('form-dialog').open);
  assert.ok(f.calls.some(call=>call.url.endsWith('/4090/upgrade')&&call.method==='POST'));
  f.click('[data-host="a6000"]');await until(()=>!f.app.state.refreshing);
  assert.equal(f.doc.querySelector('[data-host-action=upgrade]'),null);
});

test('busy host retains connected GPU/model workspace and disables competing management actions',async t=>{
  const f=await fixture(t);f.hosts[0].operation_status='busy';await f.app.refresh();
  assert.equal(f.doc.getElementById('workspace').hidden,false);
  assert.equal(f.doc.querySelectorAll('.gpu').length,3);
  assert.equal(f.doc.querySelectorAll('.model').length,1);
  for(const action of ['probe','bootstrap','upgrade','runtime','disconnect'])assert.equal(f.doc.querySelector(`[data-host-action=${action}]`).disabled,true,action);
  assert.equal(f.doc.querySelector('[data-host-action=connect]'),null);
  f.click('#add-model');assert.equal(f.doc.querySelector('option[value=upload]').disabled,true);
  assert.equal(f.doc.querySelector('option[value=huggingface]').disabled,false);
  f.hosts[0].operation_status='idle';await f.app.refresh();
  assert.equal(f.doc.querySelector('option[value=upload]').disabled,false);
  assert.equal(f.doc.querySelector('[data-host-action=disconnect]').disabled,false);
});

test('upload form opened before host becomes busy cannot submit another upload',async t=>{
  const f=await fixture(t);f.click('#add-model');f.set('name','checkpoint');f.set('source_kind','upload');f.set('source','D:\\models\\checkpoint');
  f.hosts[0].operation_status='busy';await f.app.refresh();f.submit('dialog-form');
  await until(()=>!f.doc.getElementById('dialog-error').hidden);
  assert.match(f.doc.getElementById('dialog-error').textContent,/已有管理任务/);
  assert.equal(f.calls.some(call=>call.url.endsWith('/upload')),false);
});

test('model metadata and logs are rendered as text',async t=>{
  const f=await fixture(t);f.models[0].name='<img src=x onerror=alert(1)>';await f.app.refresh();
  assert.equal(f.doc.querySelector('#model-list img'),null);
  assert.ok(f.doc.getElementById('model-list').textContent.includes('<img'));
  f.click('[data-model-action=logs]');await until(()=>f.doc.getElementById('log-content').textContent.includes('first line'));
  assert.equal(f.doc.getElementById('log-content').textContent,'first line\nsecond line');
});

test('API reports structured validation and bounds timeouts',async()=>{
  await assert.rejects(apiRequest('/test',{fetcher:async()=>new Response(JSON.stringify({detail:[{msg:'invalid GPU'}]}),{status:422})}),/invalid GPU/);
  await assert.rejects(apiRequest('/test',{timeout:1,fetcher:async(_path,{signal})=>new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError'))))}),/请求超时/);
});
