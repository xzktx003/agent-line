"use strict";
(() => {
  const $ = id => document.getElementById(`memory-${id}`);
  let sourceFragment = (() => {
    const raw = window.location.hash.slice(1);
    if (!raw) return {value: null, error: ""};
    if (raw.length > 2048) return {value: null, error: "来源链接格式无法核对。"};
    const entries = raw.split("&").map(part => part.split("="));
    const expected = ["source-project", "source-binding", "source-task"];
    if (entries.length !== expected.length || entries.some(parts => parts.length !== 2)
        || new Set(entries.map(parts => parts[0])).size !== expected.length
        || expected.some(name => entries.filter(parts => parts[0] === name).length !== 1)) return {value: null, error: "来源链接格式无法核对。"};
    let values;
    try { values = Object.fromEntries(entries.map(([name, value]) => [decodeURIComponent(name), decodeURIComponent(value)])); }
    catch { return {value: null, error: "来源链接格式无法核对。"}; }
    const segment = value => /^[A-Za-z0-9_-]{1,160}$/.test(value);
    if (Object.keys(values).length !== 3 || !expected.every(name => Object.hasOwn(values, name))
        || !segment(values["source-project"]) || !segment(values["source-binding"])
        || !/^task-[0-9a-f]{32}$/.test(values["source-task"])) return {value: null, error: "来源链接格式无法核对。"};
    return {value: {projectId: values["source-project"], roleBindingId: values["source-binding"], taskId: values["source-task"]}, error: ""};
  })();
  if (window.location.hash) window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  const attempts = new Map(), tombstones = new Set(), controllers = new Set(), retryAttempts = new Set();
  const deletionAttempts = new Map(), deletionTombstones = new Set();
  let activeHandoff = null, batch = null, batchEpoch = null;
  let token = "", epoch = 0, busy = false, bindings = [], selected = null, record = null, checked = false;
  let skillCandidate = null, skillDownloadUrl = "", skillDownloadTimer = 0, deletionUiMessage = "";
  const pages = {source: {page: 0, cursors: [null], next: null}, history: {page: 0, cursors: [null], next: null}};
  const labels = {registered:"已登记",submitting:"正在提交",uncertain:"执行结果未知",accepted:"已接收",cancel_requested:"已请求取消",succeeded:"执行成功",failed:"执行失败",canceled:"已取消",unknown:"尚未确认",pending:"等待核对",passed:"验收通过",rejected:"验收未通过",not_required:"无需验收",not_started:"尚未开始",none:"尚无记忆",not_requested:"尚未请求写回",approved:"已审核，等待写回",intent:"已登记写回意向",committed:"已写回并读回核对",deleting:"删除中，等待索引核对",deleted:"已删除并核对",needs_review:"需要重新核对",review_recorded:"审核已记录，正文尚未登记"};
  const safe = value => String(value ?? "尚无记录").split(token || "\u0000").join("[凭证已隐藏]");
  const state = value => labels[value] || (value ? `未识别状态（${safe(value)}）` : "尚无记录");
  const memoryState = value => value === "failed" ? "记忆写回失败" : state(value);
  const verification = value => ({passed:"验收通过",failed:"验收未通过",pending:"尚待验收",not_required:"无需验收",unknown:"验收尚未确认"})[value] || state(value);
  const dateText = value => {if(value === null || value === undefined)return "尚无记录";const date=new Date(typeof value === "number" ? value*1000 : value);return Number.isNaN(date.getTime()) ? safe(value) : new Intl.DateTimeFormat("zh-CN",{dateStyle:"medium",timeStyle:"short"}).format(date);};
  const key = () => selected ? JSON.stringify([selected.binding.bindingId, selected.taskId]) : "";
  const validEvidenceHash = value => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
  const unfinishedDraft = () => /【请填写[^】]*】/.test($("content").value);
  const deadlinePrefix = '候选写入截止（UTC）：';
  function deadlineOf(content) {
    const lines = String(content ?? '').trim().split('\n');
    const marked = lines.map((line, index) => line.startsWith(deadlinePrefix) ? index : -1).filter(index => index >= 0);
    if (!marked.length) return {state:'not_set', value:null};
    if (marked.length !== 1 || marked[0] !== 0 || !lines.slice(1).join('\n').trim()) return {state:'invalid', value:null};
    const value = lines[0].replace(/\r$/, '').slice(deadlinePrefix.length);
    if (!/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$/.test(value)) return {state:'invalid',value:null};
    const date = new Date(value);
    if (!Number.isFinite(date.getTime()) || date.getUTCFullYear() < 1
        || date.toISOString().replace('.000Z','Z') !== value) return {state:'invalid',value:null};
    return {state:Date.now() >= date.getTime() ? 'expired' : 'active',value};
  }
  const deadlineSupported = () => selected?.binding?.candidateWriteDeadline === 'utc-header-v1';
  const deadlineBlocked = content => {
    const value = deadlineOf(content);
    return value.state === 'invalid' || value.state === 'expired'
      || value.state !== 'not_set' && !deadlineSupported();
  };
  const handoffEligible = () => eligible() && validEvidenceHash(selected?.source?.evidenceSha256);
  const path = () => `/${encodeURIComponent(selected.binding.bindingId)}/${encodeURIComponent(selected.taskId)}`;
  const eligible = () => checked && !record && !tombstones.has(key()) && !deletionTombstones.has(key()) && selected?.sourceVerified === true && ["succeeded","failed","canceled"].includes(selected?.source?.executionStatus) && selected.source.resourcesReleased === true;
  const canReconcile = () => checked && selected && record && !record.deletion && !deletionAttempts.has(key()) && !deletionTombstones.has(key()) && ["pending", "intent", "failed"].includes(record.status);
  const retryKey = () => JSON.stringify([key(), record?.commitVersion]);
  const canRetry = () => checked && selected && record && !record.deletion && !deletionAttempts.has(key()) && !deletionTombstones.has(key()) && record.status === "failed" && record.reason === "official_write_rejected" && Number.isSafeInteger(record.commitVersion) && record.commitVersion > 0 && !retryAttempts.has(retryKey());
  const validHash = value => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
  const canBuildSkill = () => Boolean(token && checked && selected && record?.status === "committed" && !record.deletion
    && !deletionTombstones.has(key()) && !deletionAttempts.has(key())
    && typeof record.content === "string" && record.content.trim() && validHash(record.contentSha256)
    && record.bindingId === selected.binding.bindingId && record.projectId === selected.binding.projectId
    && record.roleAgentId === selected.binding.roleAgentId && record.taskId === selected.taskId);
  function clearSkillCandidate(clearFields = true) {
    if (skillDownloadTimer) clearTimeout(skillDownloadTimer);
    skillDownloadTimer = 0;
    if (skillDownloadUrl) URL.revokeObjectURL(skillDownloadUrl);
    skillDownloadUrl = ""; skillCandidate = null;
    if ($("skill-download-confirm")) $("skill-download-confirm").checked = false;
    if (clearFields && $("skill-form")) $("skill-form").reset();
    if ($("skill-preview")) {$("skill-preview").hidden = true; $("skill-preview").open = false;}
    if ($("skill-json")) $("skill-json").textContent = "";
    if ($("skill-status")) $("skill-status").textContent = "完整填写后生成候选预览。";
  }
  function skillValues() {
    const ids = {name: "name", description: "description", version: "version", applicableWhen: "applicable",
      invalidWhen: "invalid", boundaries: "boundaries", steps: "steps"};
    return Object.fromEntries(Object.entries(ids).map(([name, id]) => [name, $(`skill-${id}`).value]));
  }
  function validSkillValues(values) {
    return /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/.test(values.name) && values.name.length <= 64
      && [[values.description, 1024], [values.version, 160], [values.applicableWhen, 4000],
        [values.invalidWhen, 4000], [values.boundaries, 8000], [values.steps, 32000]].every(([value, limit]) =>
        typeof value === "string" && value.trim().length > 0 && value.length <= limit && !value.includes("\0")
        && !/【请填写[^】]*】/.test(value))
      && !/[\r\n]/.test(values.description) && !/[\r\n]/.test(values.version);
  }
  function skillTarget() {
    return {epoch, bindingId: selected.binding.bindingId, projectId: selected.binding.projectId,
      roleAgentId: selected.binding.roleAgentId, taskId: selected.taskId, sourceContentSha256: record.contentSha256};
  }
  function currentSkillTarget(target) {
    return target.epoch === epoch && token && canBuildSkill() && selected.binding.bindingId === target.bindingId
      && selected.binding.projectId === target.projectId && selected.binding.roleAgentId === target.roleAgentId
      && selected.taskId === target.taskId && record.contentSha256 === target.sourceContentSha256;
  }
  function exactKeys(value, names) {
    return value && typeof value === "object" && !Array.isArray(value)
      && Object.keys(value).sort().join("\0") === [...names].sort().join("\0");
  }
  function validSkillCandidate(value, target, name) {
    const rootKeys = ["kind", "schemaVersion", "installed", "enabled", "projectId", "roleAgentId", "taskId", "sourceContentSha256", "skillContentSha256", "nativeRequest", "requiredAction"];
    const nativeKeys = ["name", "content", "references", "scripts", "config", "enable"];
    return exactKeys(value, rootKeys) && value.kind === "qwenpaw_disabled_skill_candidate" && value.schemaVersion === "1.0"
      && value.installed === false && value.enabled === false && value.projectId === target.projectId
      && value.roleAgentId === target.roleAgentId && value.taskId === target.taskId
      && validHash(value.sourceContentSha256) && value.sourceContentSha256 === target.sourceContentSha256
      && validHash(value.skillContentSha256) && typeof value.requiredAction === "string" && value.requiredAction.trim().length > 0
      && exactKeys(value.nativeRequest, nativeKeys) && value.nativeRequest.name === name
      && typeof value.nativeRequest.content === "string" && value.nativeRequest.content.length > 0
      && value.nativeRequest.content.length <= 200000 && value.nativeRequest.references === null
      && value.nativeRequest.scripts === null && value.nativeRequest.config === null && value.nativeRequest.enable === false;
  }
  const deletionCapability = () => selected?.binding?.memoryDeletion;
  const hasDeletionRecord = () => Boolean(record?.deletion);
  const canRequestDeletion = () => Boolean(token && checked && record?.status === "committed" && !record.deletion
    && validHash(record.contentSha256) && typeof record.content === "string" && record.content.trim()
    && record.bindingId === selected?.binding?.bindingId && record.projectId === selected?.binding?.projectId
    && record.roleAgentId === selected?.binding?.roleAgentId && record.taskId === selected?.taskId
    && deletionCapability()?.configured === true && deletionCapability()?.canRequest === true
    && !deletionAttempts.has(key()) && !deletionTombstones.has(key()));
  const canQueryDeletion = () => Boolean(token && checked && selected && record
    && (hasDeletionRecord() || deletionAttempts.has(key()) || deletionTombstones.has(key())));
  function deletionTarget() {
    const attempt = deletionAttempts.get(key()), row = record?.deletion;
    return {epoch, bindingId: selected.binding.bindingId, projectId: selected.binding.projectId,
      roleAgentId: selected.binding.roleAgentId, taskId: selected.taskId,
      contentSha256: attempt?.expectedContentSha256 || row?.expected_hash || record.contentSha256};
  }
  function currentDeletionTarget(target) {
    return Boolean(target && target.epoch === epoch && token && checked && selected && record
      && selected.binding.bindingId === target.bindingId && selected.binding.projectId === target.projectId
      && selected.binding.roleAgentId === target.roleAgentId && selected.taskId === target.taskId
      && record.bindingId === target.bindingId && record.projectId === target.projectId
      && record.roleAgentId === target.roleAgentId && record.taskId === target.taskId
      && record.contentSha256 === target.contentSha256 && validHash(target.contentSha256));
  }
  function validDeletionResponse(data, target, attempt = deletionAttempts.get(JSON.stringify([target.bindingId, target.taskId]))) {
    const deletion = data?.deletion, receipt = deletion?.receipt;
    const pendingReceipt = deletion?.status === "intent" && (receipt === null || receipt === undefined
      || typeof receipt === "object" && !Array.isArray(receipt) && Object.keys(receipt).length === 0);
    return data && data.bindingId === target.bindingId && data.projectId === target.projectId
      && data.roleAgentId === target.roleAgentId && data.taskId === target.taskId
      && data.contentSha256 === target.contentSha256 && validHash(data.contentSha256)
      && deletion && deletion.role_id === target.roleAgentId && deletion.task_id === target.taskId
      && deletion.expected_hash === target.contentSha256 && typeof deletion.decision_id === "string"
      && deletion.decision_id.length > 0 && ["intent", "deleting", "needs_review", "deleted"].includes(deletion.status)
      && data.status === (deletion.status === "intent" ? "deleting" : deletion.status)
      && (deletion.status !== "deleted" || receipt?.status === "deleted"
        && receipt?.fileAbsent === true && receipt?.indexSourceAbsent === true)
      && (!attempt || deletion.decision_id === attempt.decisionId && deletion.expected_hash === attempt.expectedContentSha256)
      && (pendingReceipt || (receipt && typeof receipt === "object" && !Array.isArray(receipt)
        && receipt.roleAgentId === target.roleAgentId && receipt.taskId === target.taskId
        && receipt.contentHash === target.contentSha256
        && receipt.status === deletion.status
        && [receipt.fileAbsent, receipt.indexSourceAbsent, receipt.roleBlocked].every(value =>
          value === undefined || value === null || typeof value === "boolean")));
  }
  function deletionComplete(data) {
    return data?.status === "deleted" && data?.deletion?.status === "deleted"
      && data.deletion.receipt?.status === "deleted"
      && data.deletion.receipt?.fileAbsent === true && data.deletion.receipt?.indexSourceAbsent === true;
  }
  function deletionMessage(data) {
    const target = deletionTarget();
    if (!currentDeletionTarget(target) || !validDeletionResponse(data, target))
      return "删除记录身份、正文摘要或回执状态不一致，完成情况尚未确认；请查询原删除记录。";
    const deletion = data?.deletion, receipt = deletion?.receipt || {};
    const state = {intent: "删除意向已登记，原生结果尚待核对。", deleting: "删除处理中；不能据此确认索引或缓存已清除。",
      needs_review: "删除状态需人工核对。", deleted: "记录标记已删除。"}[deletion?.status] || "删除状态尚未确认。";
    const file = receipt.fileAbsent === true ? "确认文件已消失" : "尚未确认文件消失";
    const index = receipt.indexSourceAbsent === true ? "确认索引来源已消失" : "尚未确认索引来源消失";
    const blocked = receipt.roleBlocked === true ? "已确认角色被阻止接收新任务"
      : receipt.roleBlocked === false ? "服务当前报告角色未被阻止接收新任务" : "角色阻断状态未知";
    const completed = deletionComplete(data) ? "删除状态、文件消失和索引来源消失均已核对。"
      : deletion?.status === "deleted" ? "尚未获得文件与索引来源双重消失证明，不能显示删除完成。" : "删除尚未完成核对。";
    return `${state}\n${file}；${index}；${blocked}。\n${completed}\n当前原生适配不能确认索引与缓存全部清除；角色可能持续被阻止接收新任务。`;
  }
  function notice(message, tone = "warn") { $("notice").textContent = safe(message); $("notice").className = `notice ${tone}`; $("notice").hidden = false; }
  function controls() {
    batch?.refreshControls(busy || !token);
    const batchSecret = Boolean(token && $("content").value.includes(token));
    const deadline = deadlineOf($("content").value), deadlineInvalid = deadlineBlocked($("content").value);
    $("deadline").disabled = busy || !eligible() || !deadlineSupported();
    $("deadline-apply").disabled = busy || !eligible() || !deadlineSupported();
    $("deadline-state").textContent = !selected ? '' : deadline.state === 'invalid'
      ? '候选写入期限格式无效，请核对正文首行；服务端将拒绝新写入。'
      : deadline.state === 'expired' ? `候选写入期限已到：${dateText(deadline.value)}。不能新写入或重试；已有提交仍可核对，已写入内容不会自动失效。`
      : deadline.state === 'active' ? `候选写入截止：${dateText(deadline.value)}。仅限制新写入，不控制原生召回。`
      : '未设置候选写入期限；这不代表内容永远有效。';
    if (deadline.state !== 'not_set' && !deadlineSupported()) $("deadline-state").textContent += ' 当前服务未声明期限保护，禁止提交此候选。';
    $("content-help").textContent = !record && batchSecret
      ? "正文包含当前审核凭证，请移除后再审核或加入批量待审。"
      : !record && unfinishedDraft() ? "经验草稿尚未填写完整，请补全所有【请填写…】占位后再审核。"
      : "正文将写入所选角色。已有审核正文只读；受控删除请使用下方独立入口。";
    $("batch-help").textContent = batchSecret ? "正文包含当前审核凭证，请先移除后再加入批量待审。" : "暂存不写回；最多加入同一项目的 10 项。修改本页正文不会改变已暂存项，需要移除后重新加入。";
    $("batch-stage").disabled = deadlineInvalid || batchSecret || !batch || busy || !token || !handoffEligible() || batch?.has(key()) || !$("content").value.trim() || unfinishedDraft() || !$("consent").checked;
    $("connect").disabled = busy;
    $("binding").disabled = !token || busy || !bindings.length;
    $("refresh-sources").disabled = !token || busy || !$("binding").value;
    $("refresh-history").disabled = !token || busy;
    $("query").disabled = !token || busy || !selected;
    $("reconciliation").hidden = !token || !canReconcile();
    $("reconcile").disabled = !token || busy || !canReconcile();
    $("retry-area").hidden = !token || !canRetry();
    $("retry").disabled = !token || busy || !canRetry() || deadlineInvalid;
    $("content").disabled = !token || !selected;
    $("content").readOnly = busy || !eligible();
    if ($("build-experience")) $("build-experience").disabled = busy || !handoffEligible();
    if ($("experience-help")) $("experience-help").textContent = selected?.sourceVerified && !validEvidenceHash(selected?.source?.evidenceSha256)
      ? "来源证据摘要缺失或格式无效，不能生成候选；仍可手写并按现有流程审核。候选尚未审核，不具备技能权限。"
      : "候选仅供人工补全与审核，尚未审核，不具备技能权限。";
    const skillAvailable = canBuildSkill();
    $("skill-editor").hidden = !skillAvailable;
    for (const field of $("skill-form").querySelectorAll("input, textarea")) field.disabled = !skillAvailable || busy;
    $("skill-generate").disabled = !skillAvailable || busy || !validSkillValues(skillValues());
    $("skill-download-confirm").disabled = !skillAvailable || busy || !skillCandidate;
    $("skill-download").disabled = !skillAvailable || busy || !skillCandidate || !$("skill-download-confirm").checked;
    const showDeletion = Boolean(token && selected && checked && record
      && (record.status === "committed" && validHash(record.contentSha256) || hasDeletionRecord()
        || deletionAttempts.has(key()) || deletionTombstones.has(key()) && validHash(record.contentSha256)));
    $("deletion-panel").hidden = !showDeletion;
    $("deletion-confirm").disabled = busy || !canRequestDeletion();
    $("deletion-request").disabled = busy || !canRequestDeletion() || !$("deletion-confirm").checked;
    $("deletion-query").disabled = busy || !canQueryDeletion();
    const targetRoot = $("deletion-target"); targetRoot.replaceChildren();
    if (showDeletion) {
      for (const [label, value] of [["项目", selected.binding.projectId], ["角色", selected.binding.roleAgentId],
        ["来源任务", selected.taskId], ["原正文 SHA-256", record.contentSha256]]) {
        const dt=document.createElement("dt"), dd=document.createElement("dd");dt.textContent=label;dd.textContent=String(value ?? "尚无记录");targetRoot.append(dt,dd);
      }
      if (hasDeletionRecord()) $("deletion-state").textContent = deletionMessage(record);
      else if (deletionAttempts.has(key()) || deletionTombstones.has(key())) $("deletion-state").textContent = deletionUiMessage || "本页已发起删除；结果尚未核对。只查询同一删除状态，不再次发送删除请求。";
      else if (deletionCapability()?.configured !== true) $("deletion-state").textContent = "此角色未配置受控删除能力；本页不能发起删除。若已有删除状态，请先查询原记录。";
      else if (deletionCapability()?.canRequest !== true) $("deletion-state").textContent = "当前身份没有发起删除的授权；本页不能发起删除。已有删除状态仍可查询。";
      else $("deletion-state").textContent = "核对目标与原正文摘要后，勾选确认再发起一次删除。";
    }
    $("skill-observe").disabled = !skillAvailable || busy || !skillCandidate;
    $("consent").disabled = !token || busy || !eligible();
    $("submit").disabled = deadlineInvalid || batchSecret || !token || busy || batch?.has(key()) || !eligible() || !$("content").value.trim() || unfinishedDraft() || !$("consent").checked;
    for (const name of ["source", "history"]) {
      const page = pages[name];
      $(`${name}-prev`).disabled = !token || busy || page.page === 0;
      $(`${name}-next`).disabled = !token || busy || !page.next;
      $(`${name}-page`).textContent = `第 ${page.page + 1} 页`;
    }
    for (const button of document.querySelectorAll(".task-row")) button.disabled = busy || !token;
  }
  function clearSelection() { $("deadline").value = ""; clearSkillCandidate(); selected = null; record = null; checked = false; deletionUiMessage = ""; $("deletion-confirm").checked = false; $("deletion-target").replaceChildren(); $("deletion-state").textContent = "尚未核对删除状态。"; $("context").textContent = "尚未选择来源任务"; $("context").className = "result empty"; $("source-detail").replaceChildren(); $("source-detail").hidden = true; $("content").value = ""; $("consent").checked = false; $("decision").textContent = "尚未提交审核。"; }
  function scrubMemoryContent(targetKey = key()) {
    const local = attempts.get(targetKey);
    if (local) {local.content = ""; local.decisionId = ""; attempts.delete(targetKey);}
    clearSkillCandidate(); $("content").value = ""; $("consent").checked = false;
    if (record && targetKey === key()) record = {...record, content: null};
  }
  function discardHandoff(handoff) {
    if (handoff?.value) Object.assign(handoff.value, {projectId: "", roleBindingId: "", taskId: ""});
    if (handoff) {handoff.value = null; handoff.error = "";}
  }
  function disconnect(message = "已退出审核身份，页面内容与凭证已清除。") {
    epoch++; token = ""; busy = false; bindings = [];
    batch?.clear(); batchEpoch = null;
    discardHandoff(activeHandoff); activeHandoff = null; sourceFragment = {value: null, error: ""};
    for (const attempt of attempts.values()) {attempt.content = ""; attempt.decisionId = "";} attempts.clear(); retryAttempts.clear();
    deletionAttempts.clear();
    for (const controller of controllers) controller.abort(); controllers.clear();
    clearSelection(); $("token").value = ""; $("binding").replaceChildren(new Option("连接后选择角色", ""));
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
    $("sources").replaceChildren(); $("history").replaceChildren();
    $("sources-message").textContent = "连接后读取可访问角色及来源任务。";
    $("history-message").textContent = "连接后读取当前身份可访问的审核记录。";
    for (const page of Object.values(pages)) Object.assign(page, {page:0,cursors:[null],next:null});
    $("login").hidden = false; $("logout").hidden = true; $("connection").textContent = "尚未连接"; $("connection").className = "badge";
    notice(message); controls();
  }
  async function request(route, body, allowMissing = false) {
    const generation = epoch, controller = new AbortController(); controllers.add(controller);
    const timer = setTimeout(() => controller.abort(), 25000);
    try {
      const response = await fetch(`/extension/v1/memories${route}`, {method:body ? "POST" : "GET",headers:{Authorization:`Bearer ${token}`,...(body ? {"Content-Type":"application/json"} : {})},body:body ? JSON.stringify(body) : undefined,signal:controller.signal,credentials:"omit",cache:"no-store"});
      if (generation !== epoch) throw new Error("连接已变更。");
      if (response.status === 401) { disconnect("审核凭证无效或已失效，请重新连接。"); throw new Error("连接已失效。"); }
      if (response.status === 404 && route.endsWith("/skill-status")) throw new Error("当前服务尚未提供技能核对能力，或此来源已不可访问；请重新查询来源。");
      if (response.status === 404 && route.endsWith("/skill-candidate")) throw new Error("当前服务尚未提供已审核经验的技能候选导出能力。");
      if (response.status === 404 && route.endsWith("/deletion")) throw new Error("尚未找到可查询的删除记录；本页不会重新发起删除。");
      if (response.status === 404 && route.endsWith("/delete")) throw new Error("服务未找到可用的删除目标；本页不会再次发起删除，可查询同一目标状态。");
      if (response.status === 404 && allowMissing) return null;
      if (!response.ok) {
        if (route.endsWith("/skill-status")) throw new Error("原生技能核对未完成，请重新核对来源和候选后再查询；本页没有创建或启用技能。");
        if (route.endsWith("/skill-candidate")) throw new Error(({403:"当前身份无权导出此候选。",409:"来源正文或记忆状态已变化，请重新查询核对后再生成候选。",422:"候选字段未通过服务端校验，请修正后重新导出。",503:"维护或恢复核对中，暂时无法导出候选。"})[response.status] || "候选导出未完成，请检查字段后重新导出；记忆写回状态未改变。");
        if (route.endsWith("/delete")) throw new Error(({403:"当前身份无权发起删除；本页不会重发，只能查询同一目标状态。",409:"删除目标或正文摘要冲突；本页不会重发，只查询同一目标状态。",422:"删除请求未通过校验；本页不会重发，只查询同一目标状态。",503:"维护或恢复核对中，删除结果未确认；本页不会重发，只查询同一目标状态。"})[response.status] || "删除请求未能核对；本页不会重发，只查询同一目标状态。");
        if (route.endsWith("/deletion")) throw new Error(({403:"当前身份无权查询删除状态。",409:"删除状态与原正文摘要冲突，请核对原审核记录。",503:"维护或恢复核对中，暂时无法查询删除状态。"})[response.status] || "未能查询删除状态，请稍后只读重试。");
        throw new Error(({403:"当前身份无权访问或授权已过期。",404:"未找到记录或无权访问。",409:"来源状态或原审核记录存在冲突，请查询原记录。",422:"请求内容不符合要求。",503:"维护或恢复核对中，暂时无法处理。"})[response.status] || "服务未明确完成请求，请查询原记录。");
      }
      const data = await response.json();
      if (generation !== epoch) throw new Error("连接已变更。");
      if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("服务响应格式无法识别。");
      return data;
    } catch (error) {
      if ((error.name === "AbortError" || error instanceof TypeError) && route.endsWith("/skill-status")) throw new Error("原生技能查询中断，状态尚未确认；可重新核对，没有安装或启用操作。");
      if (error.name === "AbortError" || error instanceof TypeError) throw new Error(route.endsWith("/delete") ? "删除请求结果未确认；本页不会重新发起，只查询同一删除状态。" : route.endsWith("/deletion") ? "删除状态查询中断；稍后只读重试查询，不发送删除请求。" : route.endsWith("/skill-candidate") ? "技能候选导出未完成，请核对字段后重新导出；记忆写回状态未改变。" : route.endsWith("/retry") ? "写回重试结果未知，请查询或核对原记录，勿重复发送本次重试。" : route.endsWith("/reconcile") ? "核对中断；未重复写入记忆，可稍后再次核对原记录。" : body ? "提交结果未知；保留同一审核标识与正文，只查询原记录，不重新发送。" : "查询中断，请稍后重试查询。");
      throw error;
    } finally {clearTimeout(timer);controllers.delete(controller);}
  }
  async function operate(action) {
    if (busy) return;
    const generation = epoch; busy = true; controls();
    try {await action();} catch (error) {if (generation === epoch) notice(error.message,"error");}
    finally {if (generation === epoch) {busy = false;controls();}}
  }
  function downloadSkillCandidate() {
    const entry = skillCandidate;
    if (busy || !entry || !canBuildSkill() || !currentSkillTarget(entry.target)
        || !$("skill-download-confirm").checked || !validSkillCandidate(entry.data, entry.target, entry.name)
        || JSON.stringify(skillValues()) !== JSON.stringify(entry.values)) {
      clearSkillCandidate(false); controls();
      $("skill-status").textContent = "候选身份或字段已变化，请重新生成并核对候选。";
      return;
    }
    try {
      if (skillDownloadUrl) URL.revokeObjectURL(skillDownloadUrl);
      const blob = new Blob([`${JSON.stringify(entry.data, null, 2)}\n`], {type: "application/json;charset=utf-8"});
      skillDownloadUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = skillDownloadUrl; link.download = `agent-line-${entry.name}-candidate.json`;
      document.body.append(link); link.click(); link.remove();
      $("skill-status").textContent = "已发起候选文件下载，请在浏览器中确认保存；本页没有创建或启用原生技能。";
      skillDownloadTimer = setTimeout(() => {
        if (skillDownloadUrl) URL.revokeObjectURL(skillDownloadUrl);
        skillDownloadUrl = ""; skillDownloadTimer = 0;
      }, 1000);
    } catch {
      if (skillDownloadUrl) URL.revokeObjectURL(skillDownloadUrl);
      skillDownloadUrl = "";
      $("skill-status").textContent = "本地下载未完成，请重新生成并下载候选；记忆记录状态未改变。";
    }
  }
  function render() {
    if (!selected) return;
    if (record?.deletion) {attempts.delete(key());tombstones.add(key());}
    const local = attempts.get(key()), binding = selected.binding;
    const root = $("context"); root.replaceChildren(); root.className = "result";
    const rows = [["所属项目",binding.projectId],["角色",binding.roleAgentId],["来源任务",selected.taskId]];
    if (selected.source) rows.push(["任务状态",state(selected.source.executionStatus)],["验收状态",verification(selected.source.verificationStatus)],["执行资源",selected.source.resourcesReleased === true ? "已确认退出" : "尚未确认退出"],["来源运行",selected.source.workflow?.runId]);
    if (record) rows.push(["记忆状态",memoryState(record.status)],["审核人",record.actor],["审核标识",record.decisionId],["审核时间",dateText(record.createdAt)],["正文摘要",record.contentSha256],["来源执行状态",state(record.source?.executionStatus)],["来源验收状态",verification(record.source?.verificationStatus)],["来源证据摘要",record.source?.evidenceSha256]);
    else rows.push(["审核记录",checked ? "尚未查到审核记录" : "尚未完成查询"]);
    const dl = document.createElement("dl");
    for (const [label,value] of rows) {const dt=document.createElement("dt"), dd=document.createElement("dd");dt.textContent=label;dd.textContent=safe(value);dl.append(dt,dd);} root.append(dl);
    if (record) {const details=document.createElement("details"),summary=document.createElement("summary"),pre=document.createElement("pre");details.className="memory-evidence";summary.textContent="查看原始来源与写回事件";pre.textContent=safe(JSON.stringify({source:record.source,reason:record.reason,events:record.events},null,2));details.append(summary,pre);root.append(details);}
    if (record) $("content").value = typeof record.content === "string" ? record.content : "";
    else if (local) $("content").value = local.content;
    else if (tombstones.has(key())) $("content").value = "";
    $("decision").textContent = local ? `本页审核标识：${local.decisionId}\n${record ? "已查询到原记录，以显示的写回状态为准。" : "提交结果尚待核对；不能据此认定远端没有写入。本页不重复发送。"}` : record ? "此任务已有审核记录，正文只读，不提供重复提交。" : tombstones.has(key()) ? "此页面曾发起提交；已清除原正文与审核标识。仅查询原记录，不重复发送。" : checked ? "尚未查到审核记录。核对来源与正文后才可提交。" : "正在查询原审核记录…";
    for (const button of $("sources").querySelectorAll("button")) button.setAttribute("aria-current",String(button.dataset.taskId===selected.taskId));
    controls();
  }
  async function queryDetail() {
    const target = selected && {epoch, bindingId:selected.binding.bindingId, projectId:selected.binding.projectId,
      roleAgentId:selected.binding.roleAgentId, taskId:selected.taskId};
    checked = false; render();
    if (selected.sourceRequested) {
      selected.sourceVerified = false;
      const detail = $("source-detail"); detail.hidden = false; detail.textContent = "正在读取来源任务结果与证据…";
      const generation = epoch;
      let source;
      try {source = await request(`/sources${path()}`);} catch (error) {
        if (generation === epoch) detail.textContent = "来源详情读取失败，尚未核对来源；暂不允许新审核。可重新查询。";
        throw error;
      }
      if (generation !== epoch || !token || !selected || target.bindingId !== selected.binding.bindingId || target.taskId !== selected.taskId) return;
      if (source.taskId !== selected.taskId || source.projectId !== selected.binding.projectId || source.roleAgentId !== selected.binding.roleAgentId || typeof source.outputPreview !== "string" || typeof source.evidencePreview !== "string" || typeof source.outputTruncated !== "boolean" || typeof source.evidenceTruncated !== "boolean") {
        detail.textContent = "来源详情无法核对，暂不允许新审核。";
        throw new Error("来源详情身份或格式不匹配。");
      }
      selected.source = source; selected.sourceVerified = true;
      detail.replaceChildren();
      const summary = document.createElement("summary"); summary.textContent = "来源任务结果与证据"; detail.append(summary);
      for (const [title, value, truncated] of [["任务结果",source.outputPreview,source.outputTruncated],["任务证据",source.evidencePreview,source.evidenceTruncated]]) {
        const heading = document.createElement("h4"), note = document.createElement("p"), pre = document.createElement("pre");
        heading.textContent = title; note.textContent = truncated ? "此预览已截断，并非全量内容；请结合完整任务记录核对。" : "服务端返回的来源内容。";
        pre.textContent = safe(value || "此来源没有提供内容。"); detail.append(heading,note,pre);
      }
    }
    const data = await request(path(),undefined,true);
    if (target.epoch !== epoch || !token || !selected || target.bindingId !== selected.binding.bindingId
        || target.projectId !== selected.binding.projectId || target.roleAgentId !== selected.binding.roleAgentId || target.taskId !== selected.taskId) return;
    if (data && (data.bindingId !== selected.binding.bindingId || data.taskId !== selected.taskId || data.projectId !== selected.binding.projectId || data.roleAgentId !== selected.binding.roleAgentId)) throw new Error("审核记录身份不匹配。");
    const targetKey = key(), attempt = deletionAttempts.get(targetKey);
    if (data?.deletion && !validDeletionResponse(data, {...target, contentSha256:data.contentSha256}, attempt)) throw new Error("删除状态身份或原正文摘要无法核对。");
    if (data?.deletion) deletionTombstones.add(targetKey);
    clearSkillCandidate(); record = data;
    if (deletionTombstones.has(targetKey) || attempt || data?.deletion) {record = data ? {...data, content:null} : data; scrubMemoryContent(targetKey);}
    checked = true; render();
  }
  function select(binding,taskId,source = null) {
    clearSelection(); selected = {binding,taskId,source:null,sourceRequested:source !== null,sourceVerified:false}; $("binding").value=binding.bindingId;
    render(); return queryDetail();
  }
  function addRow(root,title,context,status,onClick,taskId) {
    const li=document.createElement("li"),button=document.createElement("button"),strong=document.createElement("strong"),small=document.createElement("span"),span=document.createElement("span");
    button.type="button";button.className="task-row";if(taskId)button.dataset.taskId=taskId;strong.textContent=safe(title);small.className="task-context";small.textContent=safe(context);span.textContent=status;button.append(strong,small,span);button.addEventListener("click",()=>operate(onClick));li.append(button);root.append(li);
  }
  async function list(name,target = 0) {
    const generation=epoch, page=pages[name], params=new URLSearchParams({limit:"20"});if(page.cursors[target])params.set("after",page.cursors[target]);
    const source=name==="source", root=$(source?"sources":"history"), message=$(source?"sources-message":"history-message");
    message.textContent="正在读取记录…";
    const binding=bindings.find(item=>item.bindingId===$("binding").value);
    if(source&&!binding){root.replaceChildren();message.textContent="当前身份没有可访问的记忆角色。";return;}
    if(source)params.set("bindingId",binding.bindingId);
    try {
      const data=await request(`${source?"/sources":""}?${params}`), rows=data[source?"tasks":"memories"];
      if(!Array.isArray(rows)||(data.nextCursor!==null&&typeof data.nextCursor!=="string"))throw new Error("分页响应无法识别。");
      page.page=target;page.next=data.nextCursor;root.replaceChildren();
      for(const item of rows) {
        if(source)addRow(root,item.taskId,`运行：${safe(item.workflowRunId)}`,`${state(item.executionStatus)} · ${item.resourcesReleased===true?"资源已退出":"资源待核对"}`,()=>select(binding,item.taskId,item),item.taskId);
        else addRow(root,item.taskId,`项目：${safe(item.projectId)} · 审核人：${safe(item.actor)}`,memoryState(item.status),async()=>{const role=bindings.find(value=>value.bindingId===item.bindingId);if(!role)throw new Error("此记录的角色当前不可访问。");if($("binding").value!==role.bindingId){$("binding").value=role.bindingId;Object.assign(pages.source,{page:0,cursors:[null],next:null});await list("source");}await select(role,item.taskId);});
      }
      message.textContent=rows.length?`本页 ${rows.length} 条记录。`:page.next?"本页没有匹配记录，可继续下一页。":"当前没有可访问的记录。";
    } catch(error) {if(generation===epoch){root.replaceChildren();page.next=null;message.textContent="未能读取记录，请重新查询。";}throw error;}
  }
  $("login").addEventListener("submit",event=>{event.preventDefault();if(busy)return;const secret=$("token").value.trim();if(!secret)return;const handoff=sourceFragment;sourceFragment={value:null,error:""};disconnect();activeHandoff=handoff;token=secret;$("token").value="";$("logout").hidden=false;
    const generation=epoch;operate(async()=>{try{const data=await request("/bindings");if(generation!==epoch||!token)return;if(!Array.isArray(data.bindings))throw new Error("角色列表响应无法识别。");bindings=data.bindings;$("binding").replaceChildren(...bindings.map(item=>new Option(`${item.projectId} · ${item.roleAgentId}`,item.bindingId)));$("login").hidden=true;$("connection").textContent="审核身份已连接";$("connection").className="badge ok";
      if (handoff.error) {clearSelection();notice(handoff.error,"error");}
      else if (handoff.value) {
        const target=handoff.value, matches=bindings.filter(item=>item.projectId===target.projectId&&item.roleBindingId===target.roleBindingId);
        if(matches.length!==1){clearSelection();notice("来源角色无法唯一核对，或当前身份无权访问该来源。","error");}
        else {try{await select(matches[0],target.taskId,{});if(generation!==epoch||!token)return;notice("已读取指定来源及原审核记录。","ok");}
          catch {if(generation!==epoch||!token)return;clearSelection();notice("来源任务或原审核记录读取失败，未切换到其他来源。","error");}}
      }
      if(generation!==epoch||!token)return;
      if((handoff.value||handoff.error)&&!selected){
        $("binding").value="";$("sources").replaceChildren();
        $("sources-message").textContent="指定来源未读取。请显式选择当前身份可访问的项目与角色后再查询。";
      }else{await list("source");if(generation!==epoch||!token)return;}
      await list("history");if(generation!==epoch||!token)return;
      if(!handoff.value&&!handoff.error)notice("已连接记忆审核身份。","ok");
      discardHandoff(handoff);if(activeHandoff===handoff)activeHandoff=null;}catch(error){if(generation===epoch&&token)disconnect(error.message);}});
  });
  $("logout").addEventListener("click",()=>disconnect());
  $("binding").addEventListener("change",()=>operate(async()=>{clearSelection();Object.assign(pages.source,{page:0,cursors:[null],next:null});await list("source");}));
  for(const name of ["source","history"]){
    $(name==="source"?"refresh-sources":"refresh-history").addEventListener("click",()=>operate(async()=>{Object.assign(pages[name],{page:0,cursors:[null],next:null});await list(name);}));
    $(`${name}-prev`).addEventListener("click",()=>operate(()=>list(name,pages[name].page-1)));
    $(`${name}-next`).addEventListener("click",()=>operate(()=>{const page=pages[name];page.cursors[page.page+1]=page.next;return list(name,page.page+1);}));
  }
  $("query").addEventListener("click",()=>operate(queryDetail));
  $("deletion-confirm").addEventListener("change",controls);
  $("deletion-request").addEventListener("click",()=>{
    if(busy||!canRequestDeletion()||!$("deletion-confirm").checked)return;
    const target=deletionTarget(), targetKey=key();
    const decisionId=typeof crypto.randomUUID==="function"?crypto.randomUUID():Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,"0")).join("");
    const attempt={decisionId,expectedContentSha256:target.contentSha256};
    deletionAttempts.set(targetKey,attempt);deletionTombstones.add(targetKey);
    $("deletion-confirm").checked=false;deletionUiMessage="删除请求已发起；结果尚未核对。只查询同一删除状态，不再次发送删除请求。";
    scrubMemoryContent(targetKey);controls();
    operate(async()=>{
      try {
        const data=await request(`${path()}/delete`,{decisionId,expectedContentSha256:target.contentSha256});
        if(!currentDeletionTarget(target)||!validDeletionResponse(data,target,attempt))throw new Error("删除回执身份、原正文摘要或决定标识无法核对；不会重发，只查询同一状态。");
        record={...data,content:null};checked=true;deletionUiMessage="";render();
      } catch(error) {
        if(target.epoch===epoch&&token){deletionUiMessage="删除结果尚未核对；本页不会再次发起，只查询同一删除状态。";scrubMemoryContent(targetKey);controls();}
        throw error;
      }
    });
  });
  $("deletion-query").addEventListener("click",()=>{
    if(busy||!canQueryDeletion())return;
    const target=deletionTarget(),targetKey=key(),attempt=deletionAttempts.get(targetKey);
    operate(async()=>{
      try {
        const data=await request(`${path()}/deletion`);
        if(!currentDeletionTarget(target)||!validDeletionResponse(data,target,attempt))throw new Error("删除状态身份、原正文摘要或决定标识无法核对。");
        deletionTombstones.add(targetKey);record={...data,content:null};checked=true;deletionUiMessage="";scrubMemoryContent(targetKey);render();
      } catch(error) {
        if(target.epoch===epoch&&token){deletionUiMessage=error.message;controls();}
        throw error;
      }
    });
  });
  $("skill-form").addEventListener("input",()=>{clearSkillCandidate(false);$("skill-status").textContent="字段已变化，请重新生成候选预览。";controls();});
  $("skill-form").addEventListener("submit",event=>{
    event.preventDefault();
    const values=skillValues();
    if(busy||!canBuildSkill()||!validSkillValues(values)){ $("skill-status").textContent="请完整填写有效的技能名称、说明、版本、条件、操作边界和人工步骤。";return; }
    clearSkillCandidate(false);
    const target=skillTarget(), valuesSnapshot=JSON.stringify(values);
    operate(async()=>{
      try {
        const result=await request(`${path()}/skill-candidate`,{expectedContentSha256:target.sourceContentSha256,...values});
        if(!currentSkillTarget(target)||JSON.stringify(skillValues())!==valuesSnapshot)return;
        if(!validSkillCandidate(result,target,values.name))throw new Error("候选响应身份、摘要或禁用结构无法核对，未提供下载。");
        skillCandidate={data:result,target,name:values.name,values};
        $("skill-json").textContent=JSON.stringify(result,null,2);
        $("skill-preview").hidden=false;$("skill-preview").open=true;
        $("skill-status").textContent="候选已生成，尚未下载。请检查完整内容、来源范围和禁用状态。";
        controls();
      } catch(error) {
        if(target.epoch===epoch&&token)$("skill-status").textContent=error.message;
        throw error;
      }
    });
  });
  $("skill-download-confirm").addEventListener("change",controls);
  $("skill-download").addEventListener("click",downloadSkillCandidate);
  $("skill-observe").addEventListener("click",()=>{
    const entry=skillCandidate;
    if(busy||!entry||!currentSkillTarget(entry.target)
        ||JSON.stringify(skillValues())!==JSON.stringify(entry.values))return;
    operate(async()=>{
      $("skill-status").textContent="正在只读核对所选角色的原生技能…";
      let data;
      try {data=await request(`${path()}/skill-status`,{
        expectedContentSha256:entry.target.sourceContentSha256,...entry.values});}
      catch(error){
        if(skillCandidate===entry&&currentSkillTarget(entry.target))$("skill-status").textContent=error.message;
        throw error;
      }
      if(skillCandidate!==entry||!currentSkillTarget(entry.target)
          ||JSON.stringify(skillValues())!==JSON.stringify(entry.values))return;
      const matches=data.contentMatches===true&&data.actualContentSha256===entry.data.skillContentSha256;
      const unknown=["unknown","not_observed"].includes(data.state)
        &&data.actualContentSha256===null&&data.enabled===null&&data.contentMatches===null;
      const known=(data.state==="disabled_match"&&data.enabled===false&&matches)
        ||(data.state==="enabled_match"&&data.enabled===true&&matches)
        ||(data.state==="content_conflict"&&typeof data.enabled==="boolean"&&data.contentMatches===false
          &&validHash(data.actualContentSha256)&&data.actualContentSha256!==entry.data.skillContentSha256);
      if(data.projectId!==entry.target.projectId||data.roleAgentId!==entry.target.roleAgentId
          ||data.taskId!==entry.target.taskId||data.name!==entry.name
          ||data.sourceContentSha256!==entry.target.sourceContentSha256
          ||data.expectedContentSha256!==entry.data.skillContentSha256
          ||data.safeToCreate!==null||(!unknown&&!known)){
        $("skill-status").textContent="原生技能核对响应无法匹配当前候选，状态尚未确认。";
        throw new Error("原生技能核对身份、摘要或状态无法核对。");
      }
      $("skill-status").textContent={
        disabled_match:"原生技能正文与当前候选一致，当前为禁用；尚不代表权限、安全扫描或执行已验收。",
        enabled_match:"原生技能正文一致，当前已启用；本页没有批准或执行启用，请在原生管理中核对。",
        content_conflict:"同名原生技能正文与当前候选不同，请人工核对；本页不会覆盖。",
        not_observed:"未观察到原生技能；可能存在遗留配置，不能据此判断名称可安全创建。",
        unknown:"原生技能状态未知，请稍后重新核对；本页没有安装或启用。",
      }[data.state];
    });
  });
  if ($("build-experience")) $("build-experience").addEventListener("click",()=>{
    if(busy||!handoffEligible())return;
    if($("content").value.trim()&&!record&&!window.confirm("将用来源元数据候选替换当前未提交正文。是否继续？"))return;
    const source=selected.source, binding=selected.binding;
    const status=source.executionStatus==="failed"?"执行失败":source.executionStatus==="canceled"?"执行已取消":"执行成功";
    $("content").value=[
      `来源项目：${binding.projectId}`,
      `来源角色绑定：${binding.roleBindingId}`,
      `原生角色 ID：${binding.roleAgentId}`,
      `来源任务标识：${selected.taskId}`,
      `来源执行结果：${status}`,
      `来源验收结果：${verification(source.verificationStatus)}`,
      `来源证据 SHA-256：${source.evidenceSha256}`,
      "经验结论：【请填写经验结论】",
      "适用条件：【请填写适用条件】",
      "失效条件：【请填写失效条件】",
      "适用版本：【请填写适用版本】",
      "操作边界：【请填写操作边界】",
    ].join("\n");
    $("consent").checked=false;
    controls();
  });
  $("reconcile").addEventListener("click",()=>{
    if(busy || !canReconcile())return;
    const target=selected;
    operate(async()=>{
      notice("正在核对原记忆写回结果…");
      const data=await request(`${path()}/reconcile`,{});
      if(data.bindingId!==target.binding.bindingId || data.taskId!==target.taskId || data.projectId!==target.binding.projectId || data.roleAgentId!==target.binding.roleAgentId || data.decisionId!==record.decisionId || data.contentSha256!==record.contentSha256)throw new Error("核对回执身份不匹配，请查询原审核记录。");
      clearSkillCandidate();record=data;render();
      notice(data.status==="committed"?"已核对：原记忆正文与提交一致，未重复写入。":data.status==="needs_review"?"原记忆与审核记录不一致，需要人工核对。":data.status==="failed"?"原写回曾被拒绝，尚未确认正文写入。可单独重试记忆。":"原写回结果仍待核对，保留原提交记录。",data.status==="committed"?"ok":"warn");
    });
  });
  $("retry").addEventListener("click",()=>{
    if(busy || !canRetry())return;
    if (deadlineBlocked($("content").value)) {controls(); notice('候选写入期限已到或格式无法核对；仍可核对原提交。'); return;}
    const target=selected, original=record, expectedVersion=record.commitVersion, route=path();
    retryAttempts.add(retryKey());
    operate(async()=>{
      render();notice("正在重试同一份已审核记忆，编码任务保持原结果…");
      const data=await request(`${route}/retry`,{expectedVersion});
      if(data.bindingId!==target.binding.bindingId || data.taskId!==target.taskId || data.projectId!==target.binding.projectId || data.roleAgentId!==target.binding.roleAgentId || data.decisionId!==original.decisionId || data.contentSha256!==original.contentSha256 || !Number.isSafeInteger(data.commitVersion) || data.commitVersion<=expectedVersion || !["committed","failed","pending","intent","needs_review"].includes(data.status))throw new Error("重试回执无法核对，请查询原审核记录，勿重复发送本次重试。");
      clearSkillCandidate();record=data;render();
      notice(data.status==="committed"?"记忆已写回并读回核对，编码任务未重新执行。":data.status==="failed"?"本次记忆写回仍被明确拒绝。处理原因后可再次单独重试。":data.status==="needs_review"?"记忆正文存在冲突，需要人工核对。":"重试结果仍待核对，请查询或核对原记录。",data.status==="committed"?"ok":"warn");
    });
  });
  $("deadline-apply").addEventListener("click", () => {
    if (busy || !eligible() || !deadlineSupported()) return;
    const value = $("deadline").value;
    const date = value ? new Date(value) : null;
    if (date && (!Number.isFinite(date.getTime()) || date.getTime() <= Date.now() || date.getUTCFullYear() < 1 || date.getUTCFullYear() > 9999)) {
      notice('请选择有效的未来时间；未修改正文。'); return;
    }
    const lines = $("content").value.trim().split('\n');
    if (deadlineOf($("content").value).state === 'invalid') {
      notice('正文期限格式无法核对，请先手动修正或移除期限行。'); return;
    }
    if (lines[0].startsWith(deadlinePrefix)) lines.shift();
    const content = lines.join('\n');
    if (date && !content.trim()) {notice('请先填写记忆正文，再设置写入期限。'); return;}
    $("content").value = date ? `${deadlinePrefix}${date.toISOString().replace('.000Z','Z')}\n${content}` : content;
    $("consent").checked = false; controls();
  });
  $("content").addEventListener("input",controls);$("consent").addEventListener("change",controls);
  $("review-form").addEventListener("submit",event=>{event.preventDefault();if(busy||batch?.has(key())||!eligible()||unfinishedDraft()||!$("consent").checked||!$("content").value.trim())return;
    if (deadlineBlocked($("content").value)) {controls(); notice('候选写入期限已到、格式无效或服务不支持，请核对正文。'); return;}
    if(token&&$("content").value.includes(token)){notice("正文包含当前审核凭证，请先移除后再审核。");return;}
    const decisionId=typeof crypto.randomUUID==="function"?crypto.randomUUID():Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,"0")).join("");
    const generation=epoch;const payload={decisionId,content:$("content").value};
    if(selected.sourceVerified&&validEvidenceHash(selected.source?.evidenceSha256))payload.expectedEvidenceSha256=selected.source.evidenceSha256;
    const local={...payload};attempts.set(key(),local);tombstones.add(key());
    operate(async()=>{render();try{const data=await request(`${path()}/review`,payload);if(data.bindingId!==selected.binding.bindingId||data.taskId!==selected.taskId||data.decisionId!==decisionId||data.projectId!==selected.binding.projectId||data.roleAgentId!==selected.binding.roleAgentId)throw new Error("提交回执身份不匹配，只能查询原记录。");clearSkillCandidate();record=data;checked=true;render();notice(data.status==="committed"?"记忆已写回并读回核对；检索效果需另行验证。":data.status==="failed"?"审核已记录，但记忆写回被明确拒绝。可单独重试记忆。":"审核已记录，写回仍需核对。",data.status==="committed"?"ok":"warn");}catch(error){if(generation===epoch&&token)render();throw error;}});
  });
  batch = window.AgentLineMemoryBatch?.create({
    container: $('batch'), request, safe,
    isCurrent: entry => Boolean(token) && batchEpoch === epoch && bindings.some(binding =>
      binding.bindingId === entry.bindingId && binding.projectId === entry.projectId && binding.roleAgentId === entry.roleAgentId),
    onAttempt: (entry, payload) => {
      const deadline = deadlineOf(payload.content);
      const binding = bindings.find(item => item.bindingId === entry.bindingId && item.projectId === entry.projectId && item.roleAgentId === entry.roleAgentId);
      if (deadline.state === 'invalid' || deadline.state === 'expired'
          || deadline.state !== 'not_set' && binding?.candidateWriteDeadline !== 'utc-header-v1') {
        const error = new Error('批量候选写入期限已到、格式无效或服务不支持，尚未发起此项提交');
        error.code = 'candidate_deadline'; throw error;
      }
      if (!token || batchEpoch !== epoch || entry.key !== JSON.stringify([entry.bindingId, entry.taskId])
          || typeof payload.content !== 'string' || payload.content.includes(token)
          || attempts.has(entry.key) || tombstones.has(entry.key) || deletionAttempts.has(entry.key) || deletionTombstones.has(entry.key)) {
        throw new Error('审核身份或本页防重记录已变化');
      }
      attempts.set(entry.key, {...payload}); tombstones.add(entry.key);
    },
    onBusy: value => {busy = value; controls(); if (!value && selected) render();},
    onOpen: entry => {
      if (busy || !token || batchEpoch !== epoch) return;
      const binding = bindings.find(item => item.bindingId === entry.bindingId && item.projectId === entry.projectId && item.roleAgentId === entry.roleAgentId);
      if (binding) operate(() => select(binding, entry.taskId, {}));
    },
  });
  $('batch').addEventListener('click', controls);
  $('batch').addEventListener('change', controls);
  $('batch-stage').addEventListener('click', () => {
    if (deadlineBlocked($("content").value)) {controls(); notice('候选写入期限已到或无法核对，未加入批量待审。'); return;}
    if (!batch || busy || !token || $("content").value.includes(token) || !handoffEligible() || batch.has(key()) || !validEvidenceHash(selected.source.evidenceSha256)
        || !bindings.some(binding => binding.bindingId === selected.binding.bindingId && binding.projectId === selected.binding.projectId && binding.roleAgentId === selected.binding.roleAgentId)
        || !$("consent").checked || !$("content").value.trim() || unfinishedDraft() || deadlineBlocked($("content").value)) return;
    try {
      batchEpoch = epoch;
      batch.stage({key:key(), bindingId:selected.binding.bindingId, projectId:selected.binding.projectId,
        roleAgentId:selected.binding.roleAgentId, taskId:selected.taskId, content:$("content").value,
        expectedEvidenceSha256:selected.source.evidenceSha256});
      $('consent').checked = false;
      notice('已加入本页批量待审区，尚未写回。请继续核对来源，最后展开待审区统一确认。');
    } catch {notice('无法加入待审区：请核对同项目、重复任务、正文大小或来源身份。', 'error');}
    controls();
  });
  controls();
})();
