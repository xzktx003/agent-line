"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  let credential = "", generation = 0, selected = null, next = null, page = 0, loading = false;
  let cursors = [null], confirmation = null, detailSequence = 0;
  const decisions = new Map(); // Page lifetime only; retained across disconnect to prevent replay.
  const labels = {waiting:"等待人工决定",expired:"审批已过期",decision_recorded:"决定已记录",intent:"提交意向已记录",pending:"等待处理",uncertain:"提交结果未知，需核对",submitted:"已提交，等待核对",delivered:"已送达",confirmed:"已确认",completed:"执行已结束",paused:"等待恢复",running:"执行中",failed:"执行失败",rejected:"恢复提交被服务拒绝",accepted:"恢复请求已接收",not_submitted:"尚未提交恢复",not_started:"尚未提交"};
  const text = (value) => String(value ?? "尚无记录").split(credential || "\u0000").join("[凭证已隐藏]");
  const status = (value) => labels[value] || (value ? `未识别状态（${text(value)}）` : "尚无记录");
  function confirmationText(record) {
    const value=record?.confirmation;
    if(!value||typeof value!=="object"||Array.isArray(value))return "未提供";
    if(value.state==="configured") {
      if(typeof value.text!=="string"||!value.text.trim()||Array.from(value.text).length>2000||value.text.includes("\u0000"))return "确认事项待核对";
      return text(value.text);
    }
    return ({unconfigured:"未配置",unavailable:"当前不可用",unknown:"状态待核对"})[value.state]||"未提供";
  }
  const dateText=(value)=>{if(value===null||value===undefined)return "尚无记录";const date=new Date(typeof value==="number"?value*1000:value);return Number.isNaN(date.getTime())?text(value):new Intl.DateTimeFormat("zh-CN",{dateStyle:"medium",timeStyle:"short"}).format(date);};
  function notice(message, tone="warn") { const n=$("approval-notice"); n.textContent=text(message); n.className=`notice ${tone}`;n.hidden=false; }
  function controls() {
    $("approval-refresh").disabled=!credential||loading;
    $("approval-prev").disabled=!credential||loading||page===0;
    $("approval-next").disabled=!credential||loading||!next;
    $("approval-query").disabled=!credential||loading||!selected;
    const canDecide=!!credential&&!loading&&selected?.status==="waiting"&&selected.boundPauseObserved===true&&!selected.decision&&!decisions.has(selected.approvalId);
    $("approval-approve").disabled=!canDecide;$("approval-reject").disabled=!canDecide;
    nativeControls();
    $("approval-page").textContent=`第 ${page+1} 页`;
  }
  function disconnect() {
    generation++;credential="";selected=null;next=null;page=0;cursors=[null];confirmation=null;loading=false;
    $("approval-token").value="";$("approval-login").hidden=false;$("approval-logout").hidden=true;
    $("approval-connection").textContent="尚未连接";$("approval-connection").className="badge";
    $("approval-list").replaceChildren();$("approval-list-message").textContent="连接后读取当前审批身份可访问的项目记录。";
    $("approval-detail").replaceChildren();$("approval-detail").textContent="尚未选择审批记录";
    $("approval-decision-state").textContent="本页已提交过的决定不会重新发送。重新连接后请查询原记录。";
    $("approval-confirm").close();clearNative(true);controls();
  }
  async function request(path, body, base="/extension/v1/approvals") {
    const epoch=generation, secret=credential;
    const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),20000);
    try {
      const response=await fetch(`${base}${path}`,{method:body?"POST":"GET",headers:{Authorization:`Bearer ${secret}`,...(body?{"Content-Type":"application/json"}:{})},body:body?JSON.stringify(body):undefined,signal:controller.signal,cache:"no-store",credentials:"omit"});
      if(epoch!==generation)throw new Error("审批连接已变更，请重新查询原记录。");
      if(!response.ok) {
        if(response.status===401) disconnect();
        throw new Error(({401:"审批凭证无效或已失效。",403:"当前审批身份无权操作或凭证已过期。",404:"未找到记录或无权访问所属项目。",409:"该记录已有不可变决定或状态冲突，请查询原记录。",422:"审批参数不符合要求。",503:"上游暂不可核验，请查询原记录。"})[response.status]||"服务未明确完成请求，请查询原记录。");
      }
      let data;
      try { data=await response.json(); } catch { throw new Error("审批响应无法识别，请查询原记录。"); }
      if(!data||typeof data!=="object"||Array.isArray(data))throw new Error("审批响应无法识别，请查询原记录。");
      if(epoch!==generation)throw new Error("审批连接已变更，请重新查询原记录。");
      return data;
    } catch(error) {
      if(error.name==="AbortError"||error instanceof TypeError)throw new Error(body?"提交结果未知。保留本页与决定标识，只查询原记录，不重复提交。":"查询中断，请稍后查询原记录。");
      throw error;
    } finally {clearTimeout(timer);}
  }
  function render(record) {
    selected=record;
    for(const button of $("approval-list").querySelectorAll("button")){const active=button.dataset.approvalId===record.approvalId;button.setAttribute("aria-current",String(active));if(active)button.querySelector(".approval-row-status").textContent=status(record.status);}
    const root=$("approval-detail");root.replaceChildren();root.className="result";
    const dl=document.createElement("dl");
    const decision=record.decision;
    const rows=[["审批标识",record.approvalId],["所属项目",record.projectId],["工作流",record.workflowId],["原始运行",record.runId],["原始节点",record.blockId],["确认事项",confirmationText(record)],["暂停上下文",record.contextId],["节点激活",record.activationId],["执行尝试",record.attemptId],["版本",record.revision],["有效期至",dateText(record.expiresAt)],["审批状态",status(record.status)],["当前暂停点",record.boundPauseObserved===true?"已核对原暂停点":record.runStatus==="completed"?"该运行已结束":"当前未匹配暂停点"],["决定",decision?(decision.decision==="approve"?"批准":decision.decision==="reject"?"拒绝":"未识别决定"):"尚无决定"],["恢复提交",status(record.deliveryStatus)],["运行状态",status(record.runStatus)]];
    if(decision?.decisionId)rows.push(["决定标识",decision.decisionId],["决定人",decision.decidedBy],["决定时间",dateText(record.decidedAt)]);
    for(const [name,value] of rows){const dt=document.createElement("dt"),dd=document.createElement("dd");dt.textContent=name;dd.textContent=text(value);dl.append(dt,dd);}root.append(dl);
    const local=decisions.get(record.approvalId);
    if(local&&record.decision?.decisionId===local.decisionId)local.status=["uncertain","intent"].includes(record.deliveryStatus)?"unknown":"received";
    $("approval-decision-state").textContent=local?`本页决定：${local.decision==="approve"?"批准":"拒绝"}；决定标识：${local.decisionId}\n${local.status==="unknown"?"提交结果未知，仅允许查询原记录。":local.status==="submitting"?"正在提交，请勿关闭页面。":"提交已处理；以原记录的恢复与运行状态为准。"}`:decision?"此记录已有决定，不再提供重复提交。":"尚未提交决定。核对原暂停点后才可操作。";
    controls();
  }
  async function query() {if(!selected)return;const id=selected.approvalId,sequence=++detailSequence;loading=true;controls();try{const data=await request(`/${encodeURIComponent(id)}`);if(sequence!==detailSequence||selected?.approvalId!==id)return;if(data.approvalId!==id)throw new Error("审批记录身份不匹配。");render(data);notice((decisions.get(id)?.status==="unknown"||["uncertain","intent"].includes(data.deliveryStatus))?"原记录仍需核对；保留同一决定标识，只查询、不重发。":"已查询原审批记录。",(decisions.get(id)?.status==="unknown"||["uncertain","intent"].includes(data.deliveryStatus))?"warn":"ok");}catch(e){if(sequence===detailSequence)notice(e.message,"error");}finally{if(sequence===detailSequence){loading=false;controls();}}}
  async function list(targetPage=0) {
    loading=true;controls();$("approval-list-message").textContent="正在读取授权审批记录…";
    try {
      const query=new URLSearchParams({limit:"20"});if(cursors[targetPage])query.set("after",cursors[targetPage]);
      const data=await request(`?${query}`);if(!Array.isArray(data.approvals))throw new Error("审批列表响应无法识别。");
      page=targetPage;next=data.nextCursor||null;const root=$("approval-list");root.replaceChildren();
      for(const record of data.approvals){const item=document.createElement("li"),button=document.createElement("button"),title=document.createElement("strong"),context=document.createElement("span"),state=document.createElement("span");button.className="task-row";button.type="button";button.dataset.approvalId=record.approvalId;state.className="approval-row-status";title.textContent=text(record.approvalId);context.className="task-context";context.textContent=`项目：${text(record.projectId)} · 节点：${text(record.blockId)}`;state.textContent=status(record.status);button.append(title,context,state);button.addEventListener("click",()=>{selected=record;queryDetail();});item.append(button);root.append(item);}
      $("approval-list-message").textContent=data.approvals.length?`本页 ${data.approvals.length} 条审批记录；仅显示授权项目。`:"当前没有可访问的审批记录。";
    }catch(e){$("approval-list").replaceChildren();next=null;$("approval-list-message").textContent="未能读取审批记录。";notice(e.message,"error");throw e;}finally{loading=false;controls();}
  }
  const queryDetail=()=>query();
  $("approval-login").addEventListener("submit",async event=>{event.preventDefault();if(loading)return;generation++;credential=$("approval-token").value.trim();$("approval-token").value="";if(!credential)return;try{await list();$("approval-login").hidden=true;$("approval-logout").hidden=false;$("approval-connection").textContent="审批身份已连接";$("approval-connection").className="badge ok";notice("已连接人工审批身份。","ok");}catch{disconnect();}});
  $("approval-logout").addEventListener("click",disconnect);
  $("approval-refresh").addEventListener("click",()=>{cursors=[null];list().catch(()=>{});});
  $("approval-next").addEventListener("click",()=>{if(!loading&&next){cursors[page+1]=next;list(page+1).catch(()=>{});}});
  $("approval-prev").addEventListener("click",()=>{if(!loading&&page>0)list(page-1).catch(()=>{});});
  $("approval-query").addEventListener("click",query);
  for(const choice of ["approve","reject"]){$("approval-"+choice).addEventListener("click",()=>{if(!selected||decisions.has(selected.approvalId))return;confirmation={approvalId:selected.approvalId,decision:choice};$("approval-confirm-title").textContent=choice==="approve"?"确认批准此节点？":"确认拒绝此节点？";$("approval-confirm-context").textContent=`原始运行：${text(selected.runId)}\n节点：${text(selected.blockId)}\n确认事项：${confirmationText(selected)}`;$("approval-confirm-submit").textContent=choice==="approve"?"确认批准":"确认拒绝";$("approval-confirm-submit").className=choice==="reject"?"danger":"";$("approval-confirm").showModal();});}
  $("approval-confirm-back").addEventListener("click",()=>{$("approval-confirm").close();confirmation=null;});
  $("approval-confirm-submit").addEventListener("click",async()=>{
    if(!confirmation||!credential||!selected||selected.approvalId!==confirmation.approvalId||decisions.has(confirmation.approvalId))return;
    const record={...confirmation,decisionId:typeof crypto.randomUUID==="function"?crypto.randomUUID():Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,"0")).join(""),status:"submitting"};decisions.set(record.approvalId,record);confirmation=null;$("approval-confirm").close();loading=true;render(selected);
    try{const data=await request(`/${encodeURIComponent(record.approvalId)}/decision`,{decisionId:record.decisionId,decision:record.decision});record.status="received";if(data.approvalId!==record.approvalId)throw new Error("提交回执身份不匹配，请查询原记录。");render(data);notice(data.deliveryStatus==="accepted"?"恢复请求已接收，请查询运行结果。":"决定已记录；恢复提交仍需核对，只查询原记录。",data.deliveryStatus==="accepted"?"ok":"warn");}
    catch(e){record.status="unknown";if(selected?.approvalId===record.approvalId)render(selected);notice(e.message,"error");}
    finally{loading=false;controls();}
  });
  let nativeSequence=0, nativeSelected=null, nativeBusy=false, nativeConfirmation=null;
  const nativeDecisions=new Map();
  const identityKey=i=>JSON.stringify([i?.upstreamTaskId,i?.codexThreadId,i?.codexTurnId]);
  const validIdentity=i=>i && ['upstreamTaskId','codexThreadId','codexTurnId'].every(k=>typeof i[k]==="string"&&i[k].length>0);
  const nativeKey=r=>JSON.stringify([r.taskId,r.requestId,identityKey(r.identity)]);
  const nativeBase=task=>`/extension/v1/node-tasks/${encodeURIComponent(task)}/execution-approvals`;
  function nativeActionable(){return !!credential&&!nativeBusy&&nativeSelected?.taskId===$("native-task").value.trim()&&nativeSelected?.purpose==="execution_permission"&&nativeSelected.bound===true&&nativeSelected.status==="waiting"&&typeof nativeSelected.expiresAt==="number"&&Number.isFinite(nativeSelected.expiresAt)&&nativeSelected.expiresAt>Date.now()/1000&&!nativeSelected.decision&&!nativeDecisions.has(nativeKey(nativeSelected));}
  function nativeControls(){
    $("native-query").disabled=!credential;
    $("native-approve").disabled=!nativeActionable();$("native-reject").disabled=!nativeActionable();
  }
  function clearNative(disconnected=false){
    nativeSequence++;nativeSelected=null;nativeBusy=false;nativeConfirmation=null;$("native-confirm").close();
    $("native-list").replaceChildren();$("native-detail").textContent="尚未选择执行权限请求";$("native-decision-state").textContent="";
    $("native-message").textContent=disconnected?"审批连接已断开；重新连接后只查询原记录。":"请查询当前任务的执行权限请求。";
    if(disconnected)$("native-task").value="";nativeControls();
  }
  function nativeRender(record){
    nativeSelected=record;const root=$("native-detail");root.replaceChildren();root.className="result";
    const states={waiting:"等待人工决定",expired:"请求已过期",unbound:"无法确定请求归属",not_observed:"未再观察到请求，不能推断已批准",decision_recorded:"决定已记录"};
    const delivery={not_submitted:"尚未提交",intent:"提交意向已记录，结果待核对",uncertain:"提交结果未知",accepted:"权限回应已接收",unavailable:"暂时无法核对"};
    const dl=document.createElement("dl");
    for(const [name,value] of [["所属项目",record.projectId],["原始运行",record.runId],["原始节点",record.nodeId],["节点执行",record.nodeExecutionId],["本次尝试",record.attemptId],["发布版本",record.revision],["父角色",record.roleAgentId],["节点任务",record.taskId],["权限请求",record.requestId],["目标角色",record.targetAgentId],["原生任务",record.identity.upstreamTaskId],["会话线程",record.identity.codexThreadId],["执行轮次",record.identity.codexTurnId],["请求内容",record.summary],["权限类型",record.method],["有效期至",dateText(record.expiresAt)],["请求状态",states[record.status]||"未知状态"],["决定",record.decision?(record.decision.decision==="approve"?"批准":"拒绝"):"尚无决定"],["权限回应",delivery[record.deliveryStatus]||"未知状态"]]){const dt=document.createElement("dt"),dd=document.createElement("dd");dt.textContent=name;dd.textContent=text(value);dl.append(dt,dd);}root.append(dl);
    const local=nativeDecisions.get(nativeKey(record));
    $("native-decision-state").textContent=local?`本页决定标识：${local.decisionId}。已提交过此请求，只查询、不重发。`:(record.decision?"此请求已有决定，只查询原记录。":"核对当前请求身份后，才能回应本次权限。");nativeControls();
  }
  function nativeValid(record,task,identity){return record&&typeof record.requestId==="string"&&record.requestId.length>0&&record.taskId===task&&record.purpose==="execution_permission"&&validIdentity(record.identity)&&identityKey(record.identity)===identityKey(identity);}
  async function nativeQuery(){
    if(!credential)return;const task=$("native-task").value.trim();if(!task)return;
    clearNative();const sequence=nativeSequence,epoch=generation;nativeBusy=true;nativeControls();
    try{const data=await request("",undefined,nativeBase(task));if(sequence!==nativeSequence||epoch!==generation)return;
      if(data.taskId!==task||!validIdentity(data.identity)||!Array.isArray(data.approvals)||!data.approvals.every(r=>nativeValid(r,task,data.identity))||new Set(data.approvals.map(r=>r.requestId)).size!==data.approvals.length)throw new Error("执行权限响应身份不匹配，请重新核对原任务。");
      $("native-message").textContent=data.approvals.length?`本次查询 ${data.approvals.length} 个执行权限请求；与流程节点审批分别处理。`:"未观察到权限请求；不能据此推断已批准。";
      for(const record of data.approvals){const li=document.createElement("li"),button=document.createElement("button");button.type="button";button.className="task-row";button.textContent=text(record.requestId)+" · "+text(record.summary||"执行权限请求");button.addEventListener("click",()=>{nativeSequence++;nativeBusy=false;nativeConfirmation=null;$("native-confirm").close();nativeRender(record);});li.append(button);$("native-list").append(li);}
      if(data.approvals.length)nativeRender(data.approvals[0]);
    }catch(e){if(sequence===nativeSequence&&epoch===generation)$("native-message").textContent=text(e.message)+" 未确认批准。";}
    finally{if(sequence===nativeSequence&&epoch===generation){nativeBusy=false;nativeControls();}}
  }
  $("native-task").addEventListener("input",()=>clearNative());
  $("native-query-form").addEventListener("submit",e=>{e.preventDefault();nativeQuery();});
  for(const decision of ["approve","reject"])$("native-"+decision).addEventListener("click",()=>{
    if(!nativeActionable())return;nativeConfirmation={record:nativeSelected,key:nativeKey(nativeSelected),sequence:nativeSequence,epoch:generation,decision};
    $("native-confirm-title").textContent=decision==="approve"?"批准当前执行权限？":"拒绝当前执行权限？";
    $("native-confirm-context").textContent=`项目：${text(nativeSelected.projectId)}\n原运行：${text(nativeSelected.runId)}\n节点：${text(nativeSelected.nodeId)}\n本次尝试：${text(nativeSelected.attemptId)}\n任务：${text(nativeSelected.taskId)}\n请求：${text(nativeSelected.requestId)}\n会话线程：${text(nativeSelected.identity.codexThreadId)}\n执行轮次：${text(nativeSelected.identity.codexTurnId)}`;$("native-confirm").showModal();
  });
  $("native-confirm-back").addEventListener("click",()=>{$("native-confirm").close();nativeConfirmation=null;});
  $("native-confirm-submit").addEventListener("click",async()=>{
    const c=nativeConfirmation;if(!c||!nativeActionable()||c.sequence!==nativeSequence||c.epoch!==generation||nativeKey(nativeSelected)!==c.key)return;
    const operation={decisionId:typeof crypto.randomUUID==="function"?crypto.randomUUID():Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,"0")).join(""),decision:c.decision};nativeDecisions.set(c.key,operation);nativeConfirmation=null;$("native-confirm").close();nativeBusy=true;nativeRender(c.record);
    try{const data=await request(`/${encodeURIComponent(c.record.requestId)}/decision`,{...operation,expectedIdentity:c.record.identity},nativeBase(c.record.taskId));
      if(c.sequence!==nativeSequence||c.epoch!==generation)return;
      if(!nativeValid(data,c.record.taskId,c.record.identity)||data.requestId!==c.record.requestId||data.decision?.decisionId!==operation.decisionId||data.decision?.decision!==operation.decision)throw new Error("权限回应回执不匹配；只查询原记录，不重发。");
      nativeRender(data);$("native-message").textContent=data.deliveryStatus==="accepted"?"权限回应已接收；不代表执行、测试或流程成功。":"权限回应结果待核对，只查询原记录，不重发。";
    }catch(e){if(c.sequence===nativeSequence&&c.epoch===generation)$("native-message").textContent=text(e.message)+" 权限回应未确认，只查询原记录，不重发。";}
    finally{if(c.sequence===nativeSequence&&c.epoch===generation){nativeBusy=false;nativeControls();}}
  });
  const hashTask=new URLSearchParams(location.hash.slice(1)).get("execution-task");if(hashTask&&/^[A-Za-z0-9_-]{1,160}$/.test(hashTask))$("native-task").value=hashTask;
  controls();
})();
