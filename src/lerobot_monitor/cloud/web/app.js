const labels = {connected:'已连接',disconnected:'未连接',connecting:'连接中',ready:'权重就绪',loaded:'已加载',loading:'加载中',unloading:'卸载中',queued:'排队中',running:'进行中',pending:'等待中',downloading:'下载中',uploading:'上传中',failed:'失败',error:'异常',success:'已完成',succeeded:'已完成',completed:'已完成',cancelled:'已取消',offline:'离线',healthy:'可用',busy:'占用中',unavailable:'不可用',stopped:'已停止',deployed:'权重就绪',registered:'已登记'};
export const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const collection = (value, key) => Array.isArray(value) ? value : (Array.isArray(value?.[key]) ? value[key] : []);
export const statusText = value => labels[value] || String(value || '未知');
export function statusClass(value) {
  if (['connected','ready','loaded','success','succeeded','completed','healthy','deployed','registered'].includes(value)) return 'good';
  if (['failed','error','unavailable'].includes(value)) return 'bad';
  if (['loading','unloading','running','queued','pending','connecting','downloading','uploading','busy'].includes(value)) return 'warn';
  return 'neutral';
}
export function gpuAvailable(gpu, deployments = []) {
  return Boolean(gpu.uuid) && gpu.healthy !== false && !gpu.error && !gpu.busy && gpu.available !== false && !deployments.some(model => model.gpu_uuid === gpu.uuid && ['loaded','loading','unloading'].includes(model.status));
}
export function deploymentActions(model) {
  const busy = ['loading','unloading','queued','running','downloading','uploading','deploying'].includes(model.status);
  return {load:!busy && ['ready','registered','deployed','unloaded','stopped','error','failed'].includes(model.status),unload:model.status === 'loaded',remove:!busy && model.status !== 'loaded'};
}
export function apiBase(mode, hostId) {
  return mode === 'cloud' ? '/api/v1' : (hostId ? `/api/hosts/${encodeURIComponent(hostId)}/cloud/api/v1` : null);
}
export async function apiRequest(path, {method = 'GET', body, token = '', timeout = 30000, fetcher = fetch} = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const headers = {'Accept':'application/json'};
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (token) headers.Authorization = `Bearer ${token}`;
    const response = await fetcher(path, {method, headers, body:body === undefined ? undefined : JSON.stringify(body),signal:controller.signal,cache:'no-store'});
    const text = await response.text();
    let result;
    try { result = text ? JSON.parse(text) : {}; } catch { result = {detail:text.slice(0,500)}; }
    if (!response.ok) {
      const detail = typeof result.detail === 'string' ? result.detail : JSON.stringify(result.detail || result.error || `请求失败 (${response.status})`);
      const error = new Error(detail); error.status = response.status; throw error;
    }
    return result;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('请求超时。请检查服务器连接；任务可能仍在远端执行，可刷新查看。');
    throw error;
  } finally { clearTimeout(timer); }
}

export function createApp(doc = document) {
  const $ = id => doc.getElementById(id);
  const state = {mode:'manager',hosts:[],hostId:null,token:'',gpus:[],models:[],jobs:[],localJobs:[],health:null,connected:false,busy:false,generation:0,refreshing:false,pendingRefresh:false,dialogHandler:null,timer:null};
  const esc = escapeHTML;
  const host = () => state.hosts.find(item => item.id === state.hostId);
  const hostBusy = id => state.hosts.find(item => item.id === id)?.operation_status === 'busy';
  function requireHostIdle(id) { if (hostBusy(id)) throw new Error('此服务器已有管理任务进行中，请等待当前任务完成。'); }
  const request = (path, options = {}) => apiRequest(path, {...options,token:state.mode === 'cloud' && path.startsWith('/api/v1') ? state.token : ''});
  const cloud = (suffix, options) => {
    const base = apiBase(state.mode,state.hostId);
    if (!base) throw new Error('请先选择服务器。');
    return request(base + suffix,options);
  };
  const badge = value => `<span class="badge ${statusClass(value)}">${esc(statusText(value))}</span>`;
  // Preserve focused controls when a poll returns an unchanged snapshot.
  const renderHTML = (id, html) => {if ($(id).innerHTML !== html) $(id).innerHTML = html;};
  function notice(message = '', error = false) { $('notice').hidden = !message; $('notice').textContent = message; $('notice').className = `notice${error ? ' error' : ''}`; }
  function renderHosts() {
    $('sidebar').hidden = state.mode === 'cloud';
    doc.querySelector('.shell').classList.toggle('cloud-mode',state.mode === 'cloud');
    $('mode-label').textContent = state.mode === 'cloud' ? '云端服务' : '本地管理';
    renderHTML('host-list',state.hosts.length ? state.hosts.map(item => `<button class="host-select" data-host="${esc(item.id)}" aria-current="${item.id === state.hostId}"><span class="host-name mono">${esc(item.alias)}</span><span class="host-sub">${badge(item.status || 'disconnected')}</span></button>`).join('') : '<p class="empty">尚未添加服务器。<br>使用上方 + 添加 SSH 别名。</p>');
    const selected = host();
    $('server-title').textContent = state.mode === 'cloud' ? '云端模型' : selected?.alias || '云端模型';
    $('server-description').textContent = state.mode === 'cloud' ? '管理此服务器上的模型、显存和部署任务。' : selected ? '模型与推理在此服务器上运行。' : '添加服务器，开始管理远端模型。';
    $('host-panel').hidden = state.mode === 'cloud' || !selected;
    if (selected) {
      const connected = selected.status === 'connected';
      const operationBusy = hostBusy(selected.id), disabled = state.busy || operationBusy;
      renderHTML('host-panel',`<div class="host-detail"><div class="host-meta">${badge(selected.status || 'disconnected')}${operationBusy?' <span class="badge warn">管理任务进行中</span>':''}<p class="mono muted small">${esc(selected.root)} · ${esc(selected.port || 8091)}</p></div><div class="actions"><button data-host-action="probe" ${disabled?'disabled':''}>检测</button><button data-host-action="bootstrap" ${disabled?'disabled':''}>初始化服务</button>${connected?`<button data-host-action="upgrade" ${disabled?'disabled':''}>升级服务</button>`:''}<button data-host-action="runtime" ${disabled?'disabled':''}>推理环境</button><button class="${connected?'secondary':'primary'}" data-host-action="${connected?'disconnect':'connect'}" ${disabled?'disabled':''}>${connected?'断开':'连接'}</button></div></div>${selected.error?`<p class="host-error">${esc(selected.error)}</p>`:''}<p class="host-help">${operationBusy ? '管理任务进行中；保持 SSH 连接，完成后可断开或提交新任务。进度见最近任务。' : connected ? 'SSH 隧道已连接。模型加载后可在云端保持驻留。' : '首次使用先检测服务器，再初始化服务；已有服务可直接连接。'}</p>`);
      const uploadOption = doc.querySelector('#model-source-kind option[value="upload"]');
      if (uploadOption) uploadOption.disabled = operationBusy;
    }
  }
  function renderWorkspace() {
    $('workspace').hidden = !state.connected;
    $('auth-panel').hidden = state.mode !== 'cloud' || state.connected;
    const runtime = state.health?.runtime;
    $('runtime-status').textContent = runtime?.configured ? `推理环境已配置${runtime.profile ? ` · ${runtime.profile}` : ''}` : runtime?.configured === false ? `推理环境尚未配置。${state.mode === 'manager' ? '点击「推理环境」安装对应模型依赖。' : '请通过本地管理页面配置模型运行环境。'}` : '推理环境状态未提供；加载模型时会检查依赖。';
    const available = state.gpus.filter(gpu => gpuAvailable(gpu,state.models)).length;
    $('gpu-summary').textContent = `${available} / ${state.gpus.length} 可用`;
    renderHTML('gpu-list',state.gpus.length ? state.gpus.map(gpu => {
      const free = gpuAvailable(gpu,state.models), healthy = gpu.healthy !== false && !gpu.error;
      const total = Number(gpu.memory_total_mb || 0), used = Number(gpu.memory_used_mb || 0);
      const percent = Math.min(100,Math.max(0,total ? used / total * 100 : 0));
      return `<article class="gpu ${healthy?'':'bad'}"><p class="gpu-title" title="${esc(gpu.name)}">${esc(gpu.name || '未知 GPU')}</p><div class="gpu-top"><span class="mono small">GPU ${esc(gpu.index)}</span>${badge(!healthy ? 'unavailable' : free ? 'healthy' : 'busy')}</div><div class="gpu-memory"><span class="mono">${(used/1024).toFixed(1)} / ${(total/1024).toFixed(1)} GiB</span><span>已用</span></div><div class="meter" role="meter" aria-label="GPU ${esc(gpu.index)} 显存使用率" aria-valuenow="${Math.round(percent)}" aria-valuemin="0" aria-valuemax="100"><span style="width:${percent}%"></span></div><p class="gpu-id mono">${esc(gpu.uuid || gpu.error || '无法读取设备')}</p></article>`;
    }).join('') : '<p class="empty">没有可用的 GPU 信息。请检查服务器驱动和设备状态。</p>');
    $('model-count').textContent = state.models.length;
    renderHTML('model-list',state.models.length ? state.models.map(model => {
      const actions = deploymentActions(model);
      const active = Number(model.active_sessions || 0) > 0;
      return `<article class="model"><div><div class="model-title"><h3>${esc(model.name || model.id)}</h3>${badge(model.status)}</div><p class="model-source mono">${esc(model.source_kind === 'huggingface' ? 'HF' : 'PATH')} · ${esc(model.source || model.path || '')}</p>${model.revision?`<p class="model-note mono">revision · ${esc(model.revision)}</p>`:''}${model.gpu_uuid?`<p class="model-note mono">${esc(model.gpu_uuid)}${active ? ' · 推理会话使用中' : ''}</p>`:''}${model.error?`<p class="model-error">${esc(model.error)}</p>`:''}</div><div class="actions">${model.status === 'loaded'?`<button data-model-action="unload" data-id="${esc(model.id)}" ${active||state.busy?'disabled':''}>释放显存</button>`:`<button data-model-action="load" data-id="${esc(model.id)}" ${!actions.load||!available||state.busy?'disabled':''}>加载到 GPU</button>`}<button class="secondary" data-model-action="logs" data-id="${esc(model.id)}">日志</button><button class="secondary" data-model-action="remove" data-id="${esc(model.id)}" ${!actions.remove||state.busy?'disabled':''}>移除</button></div></article>`;
    }).join('') : '<div class="empty"><strong>还没有部署模型</strong>从 Hugging Face 下载权重，或登记已有模型目录。<br>部署完成后再选择 GPU 加载。</div>');
    $('add-model').disabled = state.busy;
  }
  function renderJobs() {
    const jobs = [...state.localJobs.map(job => ({...job,local:true})),...state.jobs].sort((a,b) => String(b.created_at || '').localeCompare(String(a.created_at || ''))).slice(0,12);
    renderHTML('job-list',jobs.length ? jobs.map(job => {
      const date = job.updated_at || job.created_at;
      const dt = date ? new Date(typeof date === 'number' ? date*1000 : date) : null;
      const label = dt && !Number.isNaN(dt.getTime()) ? dt.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '';
      const kinds = {bootstrap:'初始化服务',upgrade:'升级服务',probe:'检测服务器',upload:'上传模型',download:'下载模型',deploy:'部署模型',load:'加载模型',unload:'卸载模型',runtime:'配置推理环境',install_runtime:'配置推理环境'};
      return `<article class="job">${badge(job.status)}<div><p class="job-name">${esc(kinds[job.kind] || job.kind || '任务')}${job.local?' · 本地管理':''}</p><p class="job-detail">${esc(job.error || job.message || job.id)}</p></div><time>${esc(label)}</time></article>`;
    }).join('') : '<p class="empty">暂无任务。部署、上传和加载的进度会显示在这里。</p>');
  }
  async function refresh({manual = false} = {}) {
    if (state.refreshing) { state.pendingRefresh = true; return; }
    state.refreshing = true;
    const generation = state.generation;
    $('refresh').disabled = true;
    try {
      if (state.mode === 'manager') {
        const [hosts,jobs] = await Promise.all([request('/api/hosts'),request('/api/jobs')]);
        if (generation !== state.generation) return;
        state.hosts = collection(hosts,'hosts'); state.localJobs = collection(jobs,'jobs');
        if (!state.hostId && state.hosts.length) state.hostId = state.hosts[0].id;
        renderHosts();
      }
      const canFetch = state.mode === 'cloud' ? Boolean(state.token) : host()?.status === 'connected';
      if (canFetch) {
        const [health,gpus,models,jobs] = await Promise.all([cloud('/health'),cloud('/gpus'),cloud('/deployments'),cloud('/jobs')]);
        if (generation !== state.generation) return;
        state.health = health; state.gpus = collection(gpus,'gpus'); state.models = collection(models,'deployments'); state.jobs = collection(jobs,'jobs'); state.connected = true;
      } else { state.connected = false; state.gpus = []; state.models = []; state.jobs = []; }
      if (manual) notice('状态已更新。');
      $('updated-at').textContent = `${new Date().toLocaleTimeString('zh-CN',{hour12:false})} 更新`;
    } catch(error) {
      if (generation === state.generation) { state.connected = false; state.jobs = []; notice(error.message,true); }
    } finally {
      state.refreshing = false; $('refresh').disabled = false; renderHosts(); renderWorkspace(); renderJobs();
      if (state.pendingRefresh) { state.pendingRefresh = false; void refresh(); }
    }
  }
  function openDialog(title,body,label,handler) {
    $('dialog-title').textContent = title; $('dialog-body').innerHTML = body; $('dialog-submit').textContent = label; $('dialog-submit').disabled = false; $('dialog-error').hidden = true; state.dialogHandler = handler; $('form-dialog').showModal();
  }
  function addHost() {
    openDialog('添加服务器','<p class="dialog-description">使用本机已有的 SSH 别名，不需要在网页中输入 SSH 密钥。</p><label class="field">SSH 别名<input name="alias" required placeholder="8x4090-server" autocomplete="off"></label><label class="field">服务器数据目录<input name="root" required placeholder="/data/zhuyutian/lerobot-monitor"><small>在独立目录保存服务环境、模型和任务记录。</small></label><div class="field-row"><label class="field">服务端口<input name="port" type="number" min="1024" max="65535" value="8091" required></label><label class="field">Python 命令<input name="python" value="python3.12" required></label></div>','添加服务器',async data => {
      await request('/api/hosts',{method:'POST',body:{alias:data.get('alias').trim(),root:data.get('root').trim(),port:Number(data.get('port')),python:data.get('python').trim()}});
      notice('服务器已添加。点击检测或连接继续。');
    });
  }
  function addModel() {
    const base = apiBase(state.mode,state.hostId), selectedHost = state.hostId;
    openDialog('添加模型',`<label class="field">显示名称<input name="name" required placeholder="例如：SmolVLA base" maxlength="160"></label><label class="field">模型来源<select name="source_kind" id="model-source-kind"><option value="huggingface">Hugging Face</option><option value="path">服务器已有目录</option>${state.mode === 'manager'?`<option value="upload" ${hostBusy(selectedHost)?'disabled':''}>本地目录上传</option>`:''}</select></label><label class="field"><span id="source-label">模型仓库 ID</span><input name="source" id="model-source" required placeholder="lerobot/smolvla_base" autocomplete="off"><small id="source-help">由云端下载模型权重，不占用 GPU 显存。</small></label><label class="field" id="revision-field">版本 / revision（可选）<input name="revision" placeholder="main、标签或 commit"><small>留空使用仓库默认版本。</small></label>`,'部署模型',async data => {
      const sourceKind = data.get('source_kind');
      if (sourceKind === 'upload') requireHostIdle(selectedHost);
      if (sourceKind === 'upload') await request(`/api/hosts/${encodeURIComponent(selectedHost)}/upload`,{method:'POST',body:{name:data.get('name').trim(),path:data.get('source').trim()}});
      else await request(base+'/deployments',{method:'POST',body:{name:data.get('name').trim(),source_kind:sourceKind,source:data.get('source').trim(),revision:sourceKind === 'huggingface' ? data.get('revision').trim() || undefined : undefined}});
      notice('部署请求已提交。请在最近任务中查看进度，完成后选择 GPU 加载。');
    });
    $('model-source-kind').addEventListener('change',event => {
      const type = event.target.value, field = $('model-source');
      $('revision-field').hidden = type !== 'huggingface';
      $('source-label').textContent = type === 'huggingface' ? '模型仓库 ID' : type === 'upload' ? '本机模型目录' : '服务器模型目录';
      field.placeholder = type === 'huggingface' ? 'lerobot/smolvla_base' : type === 'upload' ? 'D:\\models\\my-checkpoint' : '/data/models/my-checkpoint';
      $('source-help').textContent = type === 'huggingface' ? '由云端下载模型权重，不占用 GPU 显存。' : type === 'upload' ? '输入运行本地管理服务的电脑上的目录；上传后会校验文件。' : '登记服务器上已存在的模型；移除登记不会删除原始目录。';
    });
  }
  function runtimeDialog() {
    const selected = host(); if (!selected) return;
    openDialog('配置推理环境','<p class="dialog-description">为此服务器建立独立的模型运行环境。准备与模型兼容的 LeRobot wheel 安装包，完成后再加载模型。</p><label class="field">本机 LeRobot 安装包<input name="wheel_path" required placeholder="D:\\packages\\lerobot-0.4.4-py3-none-any.whl"><small>填写运行本地管理服务的电脑上的 .whl 文件路径。</small></label><label class="field">模型类型<select name="profile"><option value="act">ACT</option><option value="smolvla">SmolVLA</option><option value="pi">π0 / π0.5</option></select></label><label class="field">服务器 Hugging Face 缓存目录（可选）<input name="huggingface_home" placeholder="/data/zhuyutian/huggingface"><small>可复用已下载的模型文件，留空使用默认缓存。</small></label>','安装推理环境',async data => {
      requireHostIdle(selected.id);
      await request(`/api/hosts/${encodeURIComponent(selected.id)}/runtime`,{method:'POST',body:{wheel_path:data.get('wheel_path').trim(),profile:data.get('profile'),huggingface_home:data.get('huggingface_home').trim() || undefined}});
      notice('推理环境安装已提交。请在最近任务中查看进度。');
    });
  }
  async function hostAction(action) {
    if (state.busy || hostBusy(state.hostId)) return;
    if (action === 'runtime') { runtimeDialog(); return; }
    const selected = host(); if (!selected) return;
    if (action === 'upgrade') {
      openDialog('升级云端服务','<p class="dialog-description">将当前本地版本安装到此服务器，验证后切换服务。有活动推理会话时，服务会拒绝升级。升级失败时保留原版本。</p>','提交升级',async () => {
        requireHostIdle(selected.id);
        await request(`/api/hosts/${encodeURIComponent(selected.id)}/upgrade`,{method:'POST',body:{}}); notice('升级任务已提交。请在最近任务中查看进度。');
      }); return;
    }
    state.busy = true; renderHosts();
    try {
      const result = await request(`/api/hosts/${encodeURIComponent(selected.id)}/${action}`,{method:'POST',body:{},timeout:60000});
      if (action === 'probe') {
        const detail = typeof result.message === 'string' ? result.message : 'SSH 检测完成。';
        notice(detail);
      } else notice({bootstrap:'初始化任务已提交。完成后点击连接。',connect:'连接请求完成。',disconnect:'SSH 隧道已断开，云端任务继续运行。'}[action] || '操作已完成。');
    } catch(error) {notice(error.message,true);} finally {state.busy = false; await refresh();}
  }
  async function modelAction(action,id) {
    const model = state.models.find(item => item.id === id); if (!model) return;
    const base = apiBase(state.mode,state.hostId)+`/deployments/${encodeURIComponent(id)}`;
    if (action === 'logs') {
      $('log-title').textContent = `${model.name || model.id} · 日志`; $('log-content').textContent = '正在读取日志…'; $('log-dialog').showModal();
      try { const result = await request(base+'/logs'); $('log-content').textContent = result.text || result.lines?.join('\n') || '暂无日志。'; } catch(error) { $('log-content').textContent = error.message; } return;
    }
    if (action === 'load') {
      const gpus = state.gpus.filter(gpu => gpuAvailable(gpu,state.models));
      openDialog(`加载 ${model.name || model.id}`,`<p class="dialog-description">加载权重到单张 GPU。首次加载可能需要数分钟；任务进度会持续更新。</p><label class="field">选择 GPU<select name="gpu_uuid" required><option value="">请选择 GPU</option>${gpus.map(gpu => `<option value="${esc(gpu.uuid)}">GPU ${esc(gpu.index)} · ${esc(gpu.name)} · 剩余 ${((Number(gpu.memory_total_mb || 0)-Number(gpu.memory_used_mb || 0))/1024).toFixed(1)} GiB</option>`).join('')}</select></label><p class="field-help">模型大小需要适合此卡的可用显存。空闲 15 分钟后自动卸载权重。</p>`,'加载模型',async data => {
        if (!data.get('gpu_uuid')) throw new Error('请选择 GPU。');
        await request(base+'/load',{method:'POST',body:{gpu_uuid:data.get('gpu_uuid')}}); notice('模型加载任务已提交。');
      });
    } else if (action === 'unload') {
      openDialog('释放 GPU 显存',`<p class="dialog-description">卸载 ${esc(model.name || model.id)} 的驻留权重。模型文件保留，可以再次加载。</p>`,'释放显存',async () => { await request(base+'/unload',{method:'POST',body:{}}); notice('卸载请求已提交。'); });
    } else if (action === 'remove') {
      openDialog('移除模型部署',`<p class="dialog-description">从此服务器的部署列表移除 ${esc(model.name || model.id)}。默认保留磁盘上的模型权重。</p>${model.source_kind === 'huggingface' ? '<label class="check-label"><input type="checkbox" name="delete_files"><span>同时删除此服务管理的模型权重</span></label>' : '<p class="field-help">外部模型目录会保留。</p>'}`,'移除部署',async data => { await request(base+`?delete_files=${data.get('delete_files') === 'on'}`,{method:'DELETE'}); notice('模型部署已移除。'); });
    }
  }
  async function init() {
    $('refresh').addEventListener('click',() => refresh({manual:true})); $('add-host').addEventListener('click',addHost); $('add-model').addEventListener('click',addModel);
    $('host-list').addEventListener('click',event => {const button = event.target.closest('[data-host]'); if (!button || state.busy || button.dataset.host === state.hostId) return; state.hostId = button.dataset.host; state.generation++; state.connected = false; state.gpus = []; state.models = []; state.jobs = []; notice(); renderHosts(); renderWorkspace(); void refresh();});
    $('host-panel').addEventListener('click',event => {const button = event.target.closest('[data-host-action]'); if (button && !button.disabled) void hostAction(button.dataset.hostAction);});
    $('model-list').addEventListener('click',event => {const button = event.target.closest('[data-model-action]'); if (button && !button.disabled) void modelAction(button.dataset.modelAction,button.dataset.id);});
    $('auth-form').addEventListener('submit',event => {event.preventDefault(); state.token = new FormData(event.currentTarget).get('token').trim(); event.currentTarget.reset(); notice(); void refresh();});
    doc.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click',() => $('form-dialog').close()));
    $('close-logs').addEventListener('click',() => $('log-dialog').close());
    $('dialog-form').addEventListener('submit',async event => {
      event.preventDefault(); if (!state.dialogHandler || $('dialog-submit').disabled) return;
      const data = new FormData(event.currentTarget); $('dialog-submit').disabled = true; $('dialog-error').hidden = true;
      try {await state.dialogHandler(data); $('form-dialog').close(); await refresh();} catch(error) {$('dialog-error').textContent = error.message; $('dialog-error').hidden = false;} finally {$('dialog-submit').disabled = false;}
    });
    try { const config = await request('/api/ui-config'); state.mode = config.mode === 'cloud' ? 'cloud' : 'manager'; renderHosts(); await refresh(); }
    catch(error) {notice(`无法读取管理服务配置：${error.message}`,true);}
    state.timer = setInterval(() => {if (!doc.hidden) void refresh();},5000);
  }
  return {init,refresh,state,stop:() => clearInterval(state.timer)};
}

if (typeof document !== 'undefined') void createApp().init();
