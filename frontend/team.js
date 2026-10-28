'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const base = '/extension/v1/authoring';
  let token = '', epoch = 0, project = '', busy = false, current = null, draft = null, dirty = false;
  let pendingOperation = null, sequence = 0;
  const enc = encodeURIComponent;
  const activeRequests = new Set();
  const note = (message, kind = '') => { const node = $('team-notice'); node.hidden = !message; node.textContent = message; node.className = 'notice' + (kind ? ' ' + kind : ''); };
  const elem = (tag, text, cls) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = String(text); if (cls) node.className = cls; return node; };
  const isText = value => typeof value === 'string' && value.length > 0 && value.length <= 512;
  const fail = () => { throw new Error('服务响应无法核验，页面已停止展示该数据。'); };
  const lexical = (a,b) => a < b ? -1 : a > b ? 1 : 0;
  const compareEdge = (a,b) => lexical(a.sourceAgentId,b.sourceAgentId) || lexical(a.targetAgentId,b.targetAgentId);
  const sameEdges = (a,b) => a.length === b.length && a.every((edge,i) => edge.sourceAgentId === b[i].sourceAgentId && edge.targetAgentId === b[i].targetAgentId);
  function validateEdges(edges) {
    if (!Array.isArray(edges) || edges.length > 200) fail();
    const keys = new Set(); let previous = null;
    for (const edge of edges) {
      if (!edge || !isText(edge.sourceAgentId) || !isText(edge.targetAgentId) || edge.sourceAgentId === edge.targetAgentId) fail();
      const key = edge.sourceAgentId + '\u0000' + edge.targetAgentId;
      if (keys.has(key) || (previous && compareEdge(previous, edge) >= 0)) fail();
      keys.add(key); previous = edge;
    }
    return edges.map(edge => ({sourceAgentId:edge.sourceAgentId,targetAgentId:edge.targetAgentId}));
  }
  function validateTeam(data, expectedProject = project) {
    if (!data || data.projectId !== expectedProject || !Number.isSafeInteger(data.revision) || data.revision < 0 || !Array.isArray(data.roles) || data.roles.length > 100) fail();
    const roleIds = new Set();
    const roles = data.roles.map(role => {
      if (!role || !isText(role.roleAgentId) || roleIds.has(role.roleAgentId) || typeof role.enabled !== 'boolean' || !Array.isArray(role.bindingIds) || role.bindingIds.some(id => !isText(id)) || new Set(role.bindingIds).size !== role.bindingIds.length || (role.roleRevision !== undefined && role.roleRevision !== null && !isText(role.roleRevision))) fail();
      roleIds.add(role.roleAgentId); return {roleAgentId:role.roleAgentId,enabled:role.enabled,bindingIds:[...role.bindingIds],roleRevision:role.roleRevision};
    });
    const edges = validateEdges(data.edges);
    const orphanAgentIds = data.orphanAgentIds;
    if (!Array.isArray(orphanAgentIds) || orphanAgentIds.some(id => !isText(id)) || new Set(orphanAgentIds).size !== orphanAgentIds.length) fail();
    if (typeof data.tasksAvailable !== 'boolean' || typeof data.tasksTruncated !== 'boolean' || !Array.isArray(data.activeTasks) || data.activeTasks.length > 500) fail();
    const tasks = data.activeTasks.map(task => {
      if (!task || !isText(task.taskId) || (task.roleAgentId !== undefined && task.roleAgentId !== null && !isText(task.roleAgentId)) || !isText(task.executionStatus) || !isText(task.verificationStatus) || !isText(task.memoryStatus) || typeof task.resourcesReleased !== 'boolean' || (task.workflowRunId !== undefined && task.workflowRunId !== null && !isText(task.workflowRunId))) fail();
      if (task.executionIdentity !== undefined && task.executionIdentity !== null && (typeof task.executionIdentity !== 'object' || Array.isArray(task.executionIdentity))) fail();
      return {...task,roleAgentId:task.roleAgentId??null,workflowRunId:task.workflowRunId??null};
    });
    return {projectId:expectedProject,revision:data.revision,roles,edges,orphanAgentIds:[...orphanAgentIds],activeTasks:tasks,tasksAvailable:data.tasksAvailable,tasksTruncated:data.tasksTruncated};
  }
  async function request(path, options = {}) {
    if (!token) throw new Error('请先连接服务。');
    const captured = epoch, controller = new AbortController(); activeRequests.add(controller);
    const timer = setTimeout(() => controller.abort(), 25000);
    try {
      const response = await fetch(path,{...options,credentials:'omit',cache:'no-store',signal:controller.signal,headers:{Authorization:'Bearer '+token,...(options.body?{'Content-Type':'application/json'}:{})}});
      if (captured !== epoch) throw new Error('STALE');
      if (response.status === 401) { expireSession(); throw new Error('登录已失效，请重新连接服务。'); }
      if (!response.ok) { const error = new Error(response.status === 403 ? '当前凭证无权执行此操作。' : response.status === 409 ? '团队版本已变化。请核对当前团队后再编辑。' : response.status === 503 ? '服务暂不可用，修改仍保留在本页。' : '请求未完成，请核对服务配置。'); error.status=response.status; throw error; }
      const payload = await response.json().catch(() => null);
      if (captured !== epoch || !payload || typeof payload !== 'object' || Array.isArray(payload)) throw new Error('服务响应无法核验，页面已停止展示该数据。');
      return payload;
    } finally { clearTimeout(timer); activeRequests.delete(controller); }
  }
  function abortAll() { for (const request of activeRequests) request.abort(); activeRequests.clear(); }
  function clearTeam(){project='';current=null;draft=null;dirty=false;$('team-project').replaceChildren(new Option('请先连接服务',''));$('team-equivalent').replaceChildren();$('edge-staging').replaceChildren();$('role-tasks').replaceChildren();$('team-graph').replaceChildren();$('edge-source').replaceChildren();$('edge-target').replaceChildren();$('team-title').textContent='项目团队';$('team-state').textContent='';$('team-help').textContent='从已有角色中选择关系。保存只更新组织关系，不会创建角色、触发工作流或改变角色权限。';$('task-state').textContent='';$('operation-info').textContent='';$('operation-info').hidden=true;$('team-content').hidden=true;}
  function expireSession() { epoch++; abortAll(); token=''; pendingOperation=null; $('team-token').value=''; $('team-connection').textContent='尚未连接'; $('team-actions').hidden=true; $('team-login').hidden=false; $('team-project').disabled=true; clearTeam(); busy=false; controls(); }
  function controls() {
    const connected=!!token, locked=busy || !!pendingOperation;
    $('team-connect').disabled=connected || busy; $('team-project').disabled=!connected || busy;
    $('team-refresh').disabled=!project || busy || (!!pendingOperation && !pendingOperation.conflict);
    $('team-logout').disabled=!connected;
    $('edge-source').disabled=!current || locked; $('edge-target').disabled=!current || locked;
    $('edge-add').disabled=!current || locked || !current.roles.some(role=>role.enabled) || current.roles.length<2;
    $('team-save').disabled=!current || !dirty || locked;
    $('team-observe').hidden=!pendingOperation; $('team-observe').disabled=!pendingOperation || busy || !connected;
    $('operation-info').hidden=!pendingOperation; $('operation-info').textContent=pendingOperation?`待核对操作 ${pendingOperation.operationId} · ${pendingOperation.receiptKnown?'已收到回执，仅核对当前团队':pendingOperation.conflict?'版本冲突，可刷新团队解除保存锁定':'结果未知，仅查询原操作'}`:'';
    $('edge-staging').querySelectorAll('button').forEach(button=>{button.disabled=locked;});
  }
  function changeAllowed() { return !dirty || confirm('还有未保存的团队关系。确定放弃这些编辑吗？'); }
  function setOptions(select, roles) {
    select.replaceChildren(new Option('选择已有角色',''));
    for (const role of roles) select.add(new Option(role.roleAgentId+(role.enabled?'':' · 已停用'),role.roleAgentId));
  }
  function drawGraph() {
    const svg=$('team-graph'); svg.replaceChildren();
    const NS='http://www.w3.org/2000/svg', roles=current.roles, colWidth=220, rowHeight=100;
    const available=Math.max(260,Math.floor(svg.parentElement.clientWidth||320));
    const cols=Math.max(1,Math.min(3,Math.floor((available-40)/colWidth))), rows=Math.ceil(roles.length/cols), height=Math.max(130,rows*rowHeight+25);
    svg.setAttribute('viewBox',`0 0 ${available} ${height}`);
    svg.setAttribute('aria-label',`包含 ${roles.length} 个正式角色和 ${draft.length} 条${dirty?'待保存预览':''}关系`);
    const startX=Math.max(0,(available-cols*colWidth)/2+20), position=new Map();
    roles.forEach((role,i)=>position.set(role.roleAgentId,{x:startX+(i%cols)*colWidth,y:12+Math.floor(i/cols)*rowHeight}));
    const edgeKeys=new Set(draft.map(edge=>edge.sourceAgentId+'\u0000'+edge.targetAgentId));
    for(const [i,edge] of draft.entries()){
      const a=position.get(edge.sourceAgentId),b=position.get(edge.targetAgentId);if(!a||!b)continue;
      const dx=(b.x+90)-(a.x+90),dy=(b.y+27)-(a.y+27),reverse=edgeKeys.has(edge.targetAgentId+'\u0000'+edge.sourceAgentId),path=document.createElementNS(NS,'path');let d;
      if(Math.abs(dx)>=Math.abs(dy)){
        const direction=dx>=0?1:-1,fromX=a.x+(direction>0?180:0),toX=b.x+(direction>0?0:180),lane=reverse?(direction>0?-18:18):((i%3)-1)*5;
        d=`M ${fromX} ${a.y+27} C ${fromX+direction*36} ${a.y+27+lane}, ${toX-direction*36} ${b.y+27+lane}, ${toX} ${b.y+27}`;
      }else{
        const direction=dy>=0?1:-1,fromY=a.y+(direction>0?54:0),toY=b.y+(direction>0?0:54),lane=reverse?(direction>0?-18:18):((i%3)-1)*5;
        d=`M ${a.x+90} ${fromY} C ${a.x+90+lane} ${fromY+direction*30}, ${b.x+90+lane} ${toY-direction*30}, ${b.x+90} ${toY}`;
      }
      path.setAttribute('d',d);path.setAttribute('class','team-edge'+(roles.some(r=>r.roleAgentId===edge.sourceAgentId)&&roles.some(r=>r.roleAgentId===edge.targetAgentId)?'':' stale')+(!sameEdges(draft,current.edges)?' staged':''));path.setAttribute('marker-end','url(#team-arrow)');svg.append(path);
    }
    const defs=document.createElementNS(NS,'defs'), marker=document.createElementNS(NS,'marker'); marker.id='team-arrow'; marker.setAttribute('markerWidth','8');marker.setAttribute('markerHeight','8');marker.setAttribute('refX','7');marker.setAttribute('refY','4');marker.setAttribute('orient','auto');marker.setAttribute('markerUnits','strokeWidth');const arrow=document.createElementNS(NS,'path');arrow.setAttribute('d','M0,0 L8,4 L0,8 Z');arrow.setAttribute('class','team-arrow');marker.append(arrow);defs.append(marker);svg.prepend(defs);
    for(const role of roles){
      const p=position.get(role.roleAgentId),group=document.createElementNS(NS,'g');group.setAttribute('class','team-node'+(role.enabled?'':' disabled'));
      const rect=document.createElementNS(NS,'rect');rect.setAttribute('x',p.x);rect.setAttribute('y',p.y);rect.setAttribute('width','180');rect.setAttribute('height','54');rect.setAttribute('rx','8');
      const label=document.createElementNS(NS,'text');label.setAttribute('x',p.x+12);label.setAttribute('y',p.y+23);let shown='',used=0;for(const char of role.roleAgentId){const size=/[^\u0000-\u00ff]/.test(char)?13:7;if(used+size>156){shown+='…';break;}shown+=char;used+=size;}label.textContent=shown;const fullTitle=document.createElementNS(NS,'title');fullTitle.textContent=role.roleAgentId;label.append(fullTitle);
      const subtitle=document.createElementNS(NS,'text');subtitle.setAttribute('x',p.x+12);subtitle.setAttribute('y',p.y+42);subtitle.textContent=role.enabled?'已启用 · 正式角色':'已停用';group.append(rect,label,subtitle);svg.append(group);
    }
  }
  function render() {
    $('team-content').hidden=!current; if(!current) {controls();return;}
    $('team-title').textContent='项目团队 · '+project;
    const ids=new Set(current.roles.map(role=>role.roleAgentId));
    const staleEdges=current.edges.filter(edge=>!ids.has(edge.sourceAgentId)||!ids.has(edge.targetAgentId));
    $('team-state').textContent=`团队版本 ${current.revision} · ${current.roles.length} 个正式角色 · ${draft.length} 条${dirty?'待保存预览':''}关系`+(current.orphanAgentIds.length?` · ${current.orphanAgentIds.length} 个孤立角色标识待核对`: '');
    $('team-help').textContent=staleEdges.length?`发现 ${staleEdges.length} 条引用已不存在角色的关系。请移除失效关系后保存；保存只接受当前项目已有正式角色。`: '选择已有角色添加组织关系。页面只更新组织关系元数据，不创建角色、触发工作流或更改角色权限。';
    setOptions($('edge-source'),current.roles); setOptions($('edge-target'),current.roles); drawGraph();
    const equivalent=$('team-equivalent'); equivalent.replaceChildren();
    for(const role of current.roles){const li=elem('li');const name=elem('strong',role.roleAgentId);const state=elem('span',role.enabled?'已启用':'已停用','role-status');const bindings=elem('small',role.bindingIds.length?'绑定：'+role.bindingIds.join('、'):'无角色绑定');li.append(name,state,bindings);equivalent.append(li);}
    for(const orphanId of current.orphanAgentIds){const li=elem('li',`孤立角色标识：${orphanId} · 需复核`,'edge-row stale');equivalent.append(li);}
    const outgoing=new Map(); current.edges.forEach(edge=>{const list=outgoing.get(edge.sourceAgentId)||[];list.push(edge.targetAgentId);outgoing.set(edge.sourceAgentId,list);});
    for(const edge of current.edges){const li=elem('li',`${edge.sourceAgentId} → ${edge.targetAgentId}`,'edge-row'+(ids.has(edge.sourceAgentId)&&ids.has(edge.targetAgentId)?'':' stale')); if(!ids.has(edge.sourceAgentId)||!ids.has(edge.targetAgentId)){li.append(elem('span','需复核：角色已不存在'));const remove=elem('button','移除失效关系','secondary');remove.type='button';remove.addEventListener('click',()=>removeEdge(edge));li.append(remove);} equivalent.append(li);}
    const staging=$('edge-staging');staging.replaceChildren();
    if(!draft.length)staging.append(elem('li','没有待保存关系。','muted'));
    for(const edge of draft){const li=elem('li',`${edge.sourceAgentId} → ${edge.targetAgentId}`);const remove=elem('button','移除','secondary');remove.type='button';remove.addEventListener('click',()=>removeEdge(edge));li.append(remove);staging.append(li);}
    renderTasks(); controls();
  }
  function renderTasks(){const container=$('role-tasks');container.replaceChildren();
    if(!current.tasksAvailable){$('task-state').textContent='当前服务未提供活动任务查询；角色任务状态暂不可用。';return;}
    $('task-state').textContent=current.tasksTruncated?'活动任务列表已截断；以下内容不代表完整任务数。':'显示服务返回的当前活动任务。';
    for(const role of current.roles){const section=elem('section',undefined,'role-task');section.append(elem('h4',role.roleAgentId));const tasks=current.activeTasks.filter(task=>task.roleAgentId===role.roleAgentId);if(!tasks.length)section.append(elem('p','当前没有服务返回的活动任务。','muted'));
      for(const task of tasks){const dl=elem('dl');const row=(label,value)=>{dl.append(elem('dt',label),elem('dd',value));};row('任务标识',task.taskId);if(task.workflowRunId)row('流程运行标识',task.workflowRunId);row('执行状态',executionLabel(task.executionStatus));row('验收状态',verificationLabel(task.verificationStatus));row('记忆状态',memoryLabel(task.memoryStatus));row('资源释放',task.resourcesReleased?'已释放':'未确认释放');
        if(task.executionIdentity){const parts=[];for(const key of ['upstreamTaskId','codexThreadId','codexTurnId'])if(isText(task.executionIdentity[key]))parts.push(`${key}：${task.executionIdentity[key]}`);if(parts.length)row('原生身份摘要',parts.join(' · '));}
        section.append(dl);}
      container.append(section);
    }
    const unassigned=current.activeTasks.filter(task=>!task.roleAgentId||!current.roles.some(role=>role.roleAgentId===task.roleAgentId));
    if(unassigned.length){const section=elem('section',undefined,'role-task');section.append(elem('h4','未分配或未知角色'));for(const task of unassigned){const p=elem('p',`任务 ${task.taskId} · ${task.roleAgentId||'角色标识缺失'} · ${executionLabel(task.executionStatus)}`,'field-help');section.append(p);}container.append(section);}
  }
  const executionLabels={needs_review:'需要人工核对',recovering:'恢复核对中',registered:'已登记',pending:'等待处理',accepted:'已接收',intent:'提交意向已保存',uncertain:'提交结果待核对',rejected:'提交被拒绝',running:'执行中',queued:'排队中',completed:'执行已结束',succeeded:'执行成功',failed:'执行失败',paused:'等待恢复',run_canceled:'原生运行已取消',canceled:'已取消',cancelled:'已取消',cancel_requested:'已申请取消，待确认停止',unknown:'状态未知，需核对',confirmed:'已确认',unsupported:'不支持',passed:'验证通过',verified:'已验证',not_requested:'未请求回写',written:'已写入',committed:'已写入',candidate:'等待审核',skipped:'已跳过',blocked:'已阻止',submitting:'提交中',submitted:'已提交',waiting_approval:'等待审批',waiting_input:'等待输入',preparing:'准备中',succeeded_unverified:'执行结束，尚未验证',available:'可用'};
  const verificationLabels={pending:'等待验证',passed:'验证通过',failed:'验证失败',canceled:'已取消验收',not_required:'无需验收'};
  const memoryLabels={not_requested:'未请求回写',pending:'记忆等待核对',committed:'已写入',failed:'记忆写回失败',needs_review:'需要审核',deleting:'删除处理中',deleted:'已删除'};
  const mapLabel=(map,value)=>Object.hasOwn(map,value)?map[value]:'未识别状态';
  const executionLabel=value=>mapLabel(executionLabels,value),verificationLabel=value=>mapLabel(verificationLabels,value),memoryLabel=value=>mapLabel(memoryLabels,value);
  function removeEdge(edge){if(busy||pendingOperation)return;draft=draft.filter(item=>item.sourceAgentId!==edge.sourceAgentId||item.targetAgentId!==edge.targetAgentId);draft.sort(compareEdge);dirty=!sameEdges(draft,current.edges);render();}
  function apiError(error){if(error.message==='STALE')return;if(error.name==='AbortError')note('请求中断。团队草稿仍保留在本页。','error');else if(error.status===403||error.status===409||error.status===503||error.status===404)note(error.message,'error');else note('请求未完成或服务响应无法核验。团队草稿仍保留，请刷新核对。','error');}
  async function loadProjects(){const captured=epoch;busy=true;controls();try{const data=await request(base+'/projects');if(captured!==epoch)return;if(!Array.isArray(data.projects))fail();const projects=data.projects.map(item=>{if(!item||!isText(item.id)||!isText(item.name))fail();return {id:item.id,name:item.name};});$('team-project').replaceChildren(new Option('请选择项目',''));for(const item of projects)$('team-project').add(new Option(item.name,item.id));$('team-connection').textContent='服务已连接';$('team-actions').hidden=false;$('team-login').hidden=true;note(projects.length?'请选择项目读取团队。':'此凭证没有可访问的项目。');}catch(error){if(captured===epoch&&error.message!=='STALE'){if(error.status===401)expireSession();apiError(error);}}finally{if(captured===epoch){busy=false;controls();}}}
  async function loadTeam(selected=project){if(!selected)return false;const captured=epoch;busy=true;controls();note('正在读取团队和当前任务…');try{const data=await request(base+'/projects/'+enc(selected)+'/team');if(captured!==epoch||selected!==project)return false;const next=validateTeam(data,selected);current=next;draft=next.edges.map(edge=>({...edge}));dirty=false;render();note('团队关系与任务状态已读取。');return true;}catch(error){if(captured===epoch&&error.message!=='STALE')apiError(error);return false;}finally{if(captured===epoch){busy=false;controls();}}}
  $('team-login').addEventListener('submit',event=>{event.preventDefault();if(busy||token)return;token=$('team-token').value.trim();$('team-token').value='';if(token){epoch++;loadProjects();}});
  $('team-project').addEventListener('change',()=>{const next=$('team-project').value;if(pendingOperation){$('team-project').value=project;note('仍有待核对操作，请先查询原操作或按提示刷新团队。','error');return;}if(!changeAllowed()){$('team-project').value=project;return;}epoch++;abortAll();project=next;current=null;draft=null;dirty=false;render();if(project)loadTeam(project);else note('请选择项目。');});
  $('team-refresh').addEventListener('click',()=>{if(!project||busy)return;if(!changeAllowed())return;const capturedProject=project,previousDraft=draft?.map(edge=>({...edge})),hadPending=!!pendingOperation;epoch++;abortAll();const captured=epoch;busy=true;controls();note('正在读取当前团队…');loadTeam(capturedProject).then(loaded=>{if(captured!==epoch||loaded!==true||project!==capturedProject||!current)return;if(hadPending){draft=previousDraft||current.edges.map(edge=>({...edge}));dirty=!sameEdges(draft,current.edges);pendingOperation=null;render();note('当前团队已刷新。页面保留本地关系供核对；再次保存前请复核。');}});});
  $('team-logout').addEventListener('click',()=>{if(!changeAllowed())return;if(pendingOperation&&!confirm('保存结果尚未核对，退出会清除本页操作编号；重新连接后可读取当前团队。确定退出吗？'))return;epoch++;abortAll();token='';pendingOperation=null;$('team-token').value='';$('team-connection').textContent='尚未连接';$('team-actions').hidden=true;$('team-login').hidden=false;$('team-project').disabled=true;clearTeam();busy=false;controls();note('已断开服务连接，并清空页面中的团队数据。');});
  $('edge-form').addEventListener('submit',event=>{event.preventDefault();if(!current||busy||pendingOperation)return;const source=$('edge-source').value,target=$('edge-target').value,ids=new Set(current.roles.map(role=>role.roleAgentId));if(!ids.has(source)||!ids.has(target)||source===target){note('请选择两个不同的正式角色。','error');return;}if(draft.some(edge=>edge.sourceAgentId===source&&edge.targetAgentId===target)){note('这条关系已经存在。','error');return;}if(draft.length>=200){note('团队关系已达到 200 条上限。','error');return;}draft.push({sourceAgentId:source,targetAgentId:target});draft.sort(compareEdge);dirty=!sameEdges(draft,current.edges);render();});
  $('team-save').addEventListener('click',saveTeam);
  async function saveTeam(){if(!current||!dirty||busy||pendingOperation)return;const captured=epoch;const roles=new Set(current.roles.map(role=>role.roleAgentId));const baseline=new Set(current.edges.map(edge=>edge.sourceAgentId+'\u0000'+edge.targetAgentId));if(draft.some(edge=>(!roles.has(edge.sourceAgentId)||!roles.has(edge.targetAgentId))&&!baseline.has(edge.sourceAgentId+'\u0000'+edge.targetAgentId))){note('只能保留已有失效关系，不能新增指向未知角色的关系。','error');return;}const operationId=globalThis.crypto?.randomUUID?crypto.randomUUID():uuidFallback();const operation={operationId,projectId:project,expectedRevision:current.revision,edges:draft.map(edge=>({...edge}))};pendingOperation=operation;busy=true;controls();note('正在保存团队关系…');
    try{const receipt=await request(base+'/projects/'+enc(project)+'/team',{method:'POST',body:JSON.stringify({operationId:operation.operationId,expectedRevision:operation.expectedRevision,edges:operation.edges})});validateReceipt(receipt,operation);pendingOperation={...operation,receiptKnown:true};note('已收到保存回执，正在读取当前团队核对…');await reconcile(operation,receipt);}
    catch(error){if(captured===epoch&&error.message!=='STALE'){if(error.status===409){note('团队版本已变化。草稿保留；保存已锁定，请刷新团队核对最新版本。','error');pendingOperation={...operation,conflict:true};}else if(error.status===403){note('当前凭证无权执行此操作。团队草稿保留。','error');pendingOperation=null;}else{note('保存结果待核对。为避免重复提交，保存已锁定；请只查询原操作。','error');pendingOperation={...operation,receiptKnown:false};}}}
    finally{if(captured===epoch){busy=false;controls();}}
  }
  function validateReceipt(receipt,operation){if(receipt.projectId!==operation.projectId||receipt.operationId!==operation.operationId||!Number.isSafeInteger(receipt.revision)||receipt.revision!==operation.expectedRevision+1||!Array.isArray(receipt.edges))fail();const edges=validateEdges(receipt.edges);if(!sameEdges(edges,operation.edges))fail();return {...receipt,edges};}
  function uuidFallback(){const bytes=globalThis.crypto?.getRandomValues?crypto.getRandomValues(new Uint8Array(16)):Uint8Array.from({length:16},()=>Math.floor(Math.random()*256));bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;const hex=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;}
  async function reconcile(operation,receipt){const captured=epoch,selectedProject=project,saveVersion=operation.operationId,localDraft=draft?.map(edge=>({...edge}));const data=await request(base+'/projects/'+enc(operation.projectId)+'/team');if(captured!==epoch||project!==selectedProject||selectedProject!==operation.projectId||pendingOperation?.operationId!==saveVersion||!pendingOperation.receiptKnown)return;const next=validateTeam(data,operation.projectId);if(next.revision<receipt.revision||!sameEdges(next.edges,receipt.edges)){note('保存回执与当前团队不一致。请刷新核对版本；本地关系已保留。','error');current=next;draft=localDraft||[];dirty=!sameEdges(draft,current.edges);pendingOperation={...operation,receiptKnown:true,conflict:true};render();return;}current=next;draft=next.edges.map(edge=>({...edge}));dirty=false;pendingOperation=null;render();note(next.revision===receipt.revision?'团队关系已保存并与回执核对一致。':'操作回执有效；当前团队包含后续已保存版本。');}
  // An ambiguous save may only be observed through its original operation id.
  async function observeOperation(){if(!pendingOperation||busy||!token)return;const captured=epoch,operation=pendingOperation;busy=true;controls();note('正在只读查询原保存操作…');try{const receipt=await request(base+'/projects/'+enc(operation.projectId)+'/team/operations/'+enc(operation.operationId));if(captured!==epoch||receipt.projectId!==operation.projectId||receipt.operationId!==operation.operationId)fail();const checked=validateReceipt(receipt,operation);pendingOperation={...operation,receiptKnown:true};await reconcile(operation,checked);}catch(error){if(captured===epoch&&error.status===404){note('原操作暂不可查询（404）。保存仍锁定，只允许再次查询原操作。','error');}else if(captured===epoch&&error.message!=='STALE')apiError(error);}finally{if(captured===epoch){busy=false;controls();}}}
  $('team-observe').addEventListener('click',observeOperation);
  window.addEventListener('resize',()=>{if(current)drawGraph();});
})();
