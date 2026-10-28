"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  document.addEventListener("invalid", event => {
    for (let section = event.target.closest("details"); section; section = section.parentElement?.closest("details")) section.open = true;
  }, true);
  let token = "";
  let epoch = 0;
  let healthGeneration = 0;
  let executionBlockReason = "";
  let selectedTask = null, selectedTaskData = null, cancelTarget = null, taskGeneration = 0;
  const pendingTaskCancels = new Set();
  let selectedRun = null, controlBusy = false, stopRunTarget = null, pauseRunTarget = null;
  let pageDiscarded = false;
  const pendingControls = new Map();
  const pendingStartAttempts = new Set();
  const workflowBindings = new Map();
  let runDirectoryGeneration = 0, runDirectoryLoading = false, runDirectoryPage = 0, runDirectoryNext = null;
  let runDirectoryCursors = [null], runDirectoryPages = [], runDirectorySeen = new Set(), runQueryGeneration = 0;
  let taskCursors = [null], taskPage = 0, taskNext = null, listGeneration = 0;
  let listLoading = false, taskFiltersDirty = false;
  const safeText = (value) => {
    const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
    return token ? String(text ?? "").split(token).join("[凭证已隐藏]") : String(text ?? "");
  };
  function linkedRunObservation() {
    return window.AgentLineRunObservation?.linkedObservation(window.location.search) || null;
  }
  function rememberObservation(bindingId, runId) {
    try {
      const next = window.AgentLineRunObservation?.observationSearch(window.location.search, bindingId, runId);
      if (!next || next === window.location.search) return;
      window.history.replaceState(null, '', `${window.location.pathname}${next}${window.location.hash}`);
    } catch {
      return;
    }
  }
  async function restoreLinkedObservation() {
    const link = linkedRunObservation();
    if (pageDiscarded || !token || !link) return;
    if (byId('query-binding').value.trim() !== link.bindingId || byId('run-id').value.trim() !== link.runId) return;
    try { byId('run-form').requestSubmit(); }
    catch { notice('原运行标识已保留。请点击“查询进度”恢复观察，不会启动新运行。', 'warn'); }
  }
  function prefillRunForQuery({bindingId, runId}) {
    if (!token || !/^[A-Za-z0-9_-]{1,160}$/.test(bindingId) || !/^[A-Za-z0-9_-]{1,160}$/.test(runId)) return;
    runQueryGeneration += 1;
    clearRunControl();
    byId('stop-run-dialog').close(); byId('pause-run-dialog').close();
    byId('query-binding').value = bindingId;
    byId('run-id').value = runId;
    rememberObservation(bindingId, runId);
    runInputReuse?.clear();
    empty('run-result', '已选择运行记录', '点击查询运行以读取状态；尚未发起查询。');
    byId('run-form').scrollIntoView({behavior: 'auto', block: 'nearest'});
  }
  const workItems = window.AgentLineWorkItems?.create({request, openTasks: viewBusinessTasks, openRun: prefillRunForQuery});
  const triggerInspection = window.AgentLineTriggerInspection?.create({request, openRun: prefillRunForQuery});
  const runInputReuse = window.AgentLineRunInputReuse?.create({request,safeText,
    scope: () => {
      const bindingId = byId('query-binding').value.trim(), runId = byId('run-id').value.trim();
      const binding = workflowBindings.get(bindingId);
      if (!token || !binding || ![bindingId,runId,binding.projectId,binding.workflowId].every(value => typeof value === 'string' && /^[A-Za-z0-9_-]{1,160}$/.test(value))) return null;
      return {epoch,generation:runQueryGeneration,bindingId,runId,projectId:binding.projectId,workflowId:binding.workflowId};
    },
    targetSignature: () => JSON.stringify([byId('binding').value,byId('trigger-key').value,
      byId('workflow-input').value,byId('work-item-start-selection').textContent]),
    canApply: () => Boolean(token) && pendingStartAttempts.size === 0
      && byId('start-form').querySelector('button[type=submit]').getAttribute('aria-busy') !== 'true',
    apply: ({bindingId,input}) => {
      newKey(); workItems?.clearStartSelection();
      byId('binding').value = bindingId; byId('workflow-input').value = JSON.stringify(input,null,2);
      byId('start-feedback').textContent = '已按原输入准备全新运行，尚未提交。请核对输入并重新选择业务任务关联。';
      updateStartAvailability(); byId('start-form').scrollIntoView({block:'start'});
    }});
  let serviceCapabilities;
  const runView = window.AgentLineRunView?.create({request, redact: safeText, identity: () => {
    const bindingId = byId('query-binding').value.trim(), runId = byId('run-id').value.trim();
    if (!token || !/^[A-Za-z0-9_-]{1,160}$/.test(bindingId) || !/^[A-Za-z0-9_-]{1,160}$/.test(runId)) return null;
    const scope = workflowBindings.get(bindingId);
    return {bindingId, runId, ...(scope?.projectId ? {projectId: scope.projectId} : {})};
  }, openTask: taskId => {
    const decide = window.AgentLineRunView?.nodeDetailAction;
    const decision = typeof decide === 'function' ? decide(taskId, Boolean(token)) : {action: 'ignore'};
    if (decision.action !== 'query_existing_detail' || decision.canvasToolBodies !== false) return;
    byId('task-id').value = decision.taskId;
    byId('task-form').requestSubmit();
    byId('task-form').scrollIntoView({block:'start'});
  }});
  const labels = {
    needs_review: "需要人工核对", recovering: "恢复核对中", registered: "已登记", pending: "等待处理", accepted: "已接收", intent: "提交意向已保存",
    uncertain: "提交结果待核对", rejected: "提交被拒绝", running: "执行中", queued: "排队中",
    completed: "执行已结束", succeeded: "执行成功", failed: "执行失败", paused: "等待恢复",
    run_canceled: "原生运行已取消", canceled: "已取消", cancelled: "已取消", cancel_requested: "已申请取消，待确认停止",
    unknown: "状态未知，需核对", confirmed: "已确认", unsupported: "不支持",
    passed: "验证通过", verified: "已验证", not_requested: "未请求回写", written: "已写入",
    committed: "已写入", candidate: "等待审核", skipped: "已跳过", blocked: "已阻塞",
    submitting: "提交中", submitted: "已提交", waiting_approval: "等待审批", waiting_input: "等待输入",
    preparing: "准备中", succeeded_unverified: "执行结束，尚未验证", available: "可用",
  };
  const unknownStatus = (value) => value ? `未识别状态（${value}）` : "尚无记录";
  const statusLabel = (value) => Object.hasOwn(labels, value) ? labels[value] : unknownStatus(value);
  const verificationLabels = {pending: "等待验证", passed: "验证通过", failed: "验证失败", canceled: "已取消验收"};
  const memoryLabels = {not_requested: "未请求回写", pending: "记忆等待核对", committed: "已写入",
    failed: "记忆写回失败", needs_review: "需要审核", deleting: "删除处理中", deleted: "已删除"};
  const verificationLabel = (value) => Object.hasOwn(verificationLabels, value) ? verificationLabels[value] : unknownStatus(value);
  const memoryLabel = (value) => Object.hasOwn(memoryLabels, value) ? memoryLabels[value] : unknownStatus(value);
  function observationLabel(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)
        || typeof value.updatedAt !== "number" || !Number.isFinite(value.updatedAt) || value.updatedAt < 0) return "核对状态待确认";
    return value.status === "unknown" ? "核对结果未知"
      : value.status === "observed" ? "已取得原执行记录" : "核对状态待确认";
  }
  function executionLimitsMessage(task) {
    if (!Object.hasOwn(task, "executionLimits")) return "";
    const value = task.executionLimits;
    const valid = value && typeof value === "object" && !Array.isArray(value)
      && Number.isSafeInteger(value.attemptsUsed) && value.attemptsUsed >= 0
      && Number.isSafeInteger(value.maxBusinessAttempts) && value.maxBusinessAttempts >= 1 && value.maxBusinessAttempts <= 100
      && Number.isSafeInteger(value.limitSeconds) && value.limitSeconds >= 1 && value.limitSeconds <= 86400
      && typeof value.admissionAllowed === "boolean"
      && (value.deadlineAt === null || (typeof value.deadlineAt === "number" && Number.isFinite(value.deadlineAt) && value.deadlineAt > 0))
      && value.usage && value.usage.tokens === null && value.usage.modelCalls === null && value.usage.cost === null;
    if (!valid) return "执行限制与用量暂时无法核对，不能视为剩余额度充足。";
    return `本节点在此运行中已提交 ${value.attemptsUsed}/${value.maxBusinessAttempts} 次业务尝试；本任务时限 ${value.limitSeconds} 秒，从提交开始计算。token、模型调用次数和费用未知。超时将请求停止，实际退出须等待回执。`;
  }
  function runBudgetMessage(task) {
    if (!Object.hasOwn(task, "runBudget")) return "";
    const value = task.runBudget;
    const valid = value && typeof value === "object" && !Array.isArray(value)
      && value.scope?.projectId === task.projectId && value.scope?.workflowId === task.workflow?.id
      && value.scope?.runId === task.workflow?.runId
      && Number.isSafeInteger(value.submissionsUsed) && value.submissionsUsed >= 0
      && Number.isSafeInteger(value.maxSubmissions) && value.maxSubmissions >= 1 && value.maxSubmissions <= 10000
      && Number.isSafeInteger(value.maxExecutionSeconds) && value.maxExecutionSeconds >= 1 && value.maxExecutionSeconds <= 604800
      && typeof value.admissionAllowed === "boolean"
      && (value.deadlineAt === null || (typeof value.deadlineAt === "number" && Number.isFinite(value.deadlineAt) && value.deadlineAt > 0))
      && value.usage && value.usage.tokens === null && value.usage.modelCalls === null && value.usage.cost === null;
    if (!valid) return "运行额度暂时无法核对，不能视为仍有可用额度。";
    return `本次运行共已提交 ${value.submissionsUsed}/${value.maxSubmissions} 次节点执行，重试、循环和同一运行中的嵌套调用共用此额度；执行阶段时限 ${value.maxExecutionSeconds} 秒，从首次提交开始，后续节点不会重置。token、模型调用次数和费用未知。达到时限将请求停止，实际退出须等待回执。`;
  }
  function renderRunBudget(data, bindingId, runId, binding) {
    byId('run-budget-summary')?.remove();
    if (!Object.hasOwn(data, 'budget')) return;
    const value = data.budget;
    const plain = item => !!item && typeof item === 'object' && !Array.isArray(item);
    const count = item => Number.isSafeInteger(item) && item >= 0;
    const positiveTime = item => typeof item === 'number' && Number.isFinite(item) && item > 0;
    const unknownFields = ['maxSubmissions','submissionsUsed','remainingSubmissions',
      'maxExecutionSeconds','deadlineAt','budgetAllowsSubmission'];
    let valid = plain(value) && value.schemaVersion === 1 && value.bindingId === bindingId
      && plain(value.scope) && value.scope.runId === runId
      && typeof value.scope.projectId === 'string' && !!value.scope.projectId
      && typeof value.scope.workflowId === 'string' && !!value.scope.workflowId
      && (!binding || (binding.projectId === value.scope.projectId && binding.workflowId === value.scope.workflowId))
      && positiveTime(value.observedAt) && plain(value.usage)
      && ['tokens','modelCalls','cost'].every(key => value.usage[key] === null);
    if (valid && value.state === 'configured') {
      valid = count(value.maxSubmissions) && value.maxSubmissions >= 1 && value.maxSubmissions <= 10000
        && count(value.submissionsUsed) && Number.isSafeInteger(value.remainingSubmissions)
        && value.remainingSubmissions === value.maxSubmissions - value.submissionsUsed
        && count(value.maxExecutionSeconds) && value.maxExecutionSeconds >= 1 && value.maxExecutionSeconds <= 604800
        && (value.submissionsUsed === 0 ? value.deadlineAt === null : positiveTime(value.deadlineAt))
        && typeof value.budgetAllowsSubmission === 'boolean'
        && value.budgetAllowsSubmission === (value.submissionsUsed < value.maxSubmissions
          && (value.deadlineAt === null || value.observedAt < value.deadlineAt));
    } else if (valid) {
      valid = ['unknown','not_configured'].includes(value.state) && unknownFields.every(key => value[key] === null);
    }
    const section = document.createElement('section');
    section.id = 'run-budget-summary'; section.className = 'run-budget-summary';
    section.setAttribute('aria-labelledby','run-budget-title');
    const title = document.createElement('h4'); title.id = 'run-budget-title'; title.textContent = '运行额度'; section.append(title);
    const message = document.createElement('p'); message.className = 'field-help'; section.append(message);
    if (!valid || value.state === 'unknown') {
      message.textContent = '运行额度暂时无法核对，不能据此判断仍有额度或用量为零。';
    } else if (value.state === 'not_configured') {
      message.textContent = '此运行未配置提交次数与执行阶段时限；节点限制和其他准入规则仍然适用。';
    } else {
      const over = value.submissionsUsed - value.maxSubmissions;
      message.textContent = value.budgetAllowsSubmission
        ? '此快照中的运行额度尚有剩余；是否可执行还取决于权限、审批与资源。'
        : over > 0
          ? `此快照中的提交次数已超出上限 ${over} 次；计量延迟不能据此把活动进程标成已停止。`
          : '此快照中的运行额度已耗尽或时限已到；不能据此判定活动进程已停止。';
      const fields = document.createElement('dl'); fields.className = 'run-budget-fields';
      const rows = [['节点执行已提交', `${value.submissionsUsed} / ${value.maxSubmissions} 次`],
        [over > 0 ? '超出提交次数' : '剩余提交次数', `${over > 0 ? over : value.remainingSubmissions} 次`],
        ['执行阶段时限', `${value.maxExecutionSeconds} 秒`],
        ['执行阶段截止', value.deadlineAt === null ? '首次节点提交后开始计时' : new Date(value.deadlineAt * 1000).toLocaleString()],
        ['本地观察时间', new Date(value.observedAt * 1000).toLocaleString()]];
      for (const [label, text] of rows) {
        const dt = document.createElement('dt'), dd = document.createElement('dd');
        dt.textContent = label; dd.textContent = text; fields.append(dt,dd);
      }
      section.append(fields);
    }
    const hint = document.createElement('p'); hint.className = 'field-help';
    hint.textContent = '仅当前运行的节点提交额度；重试、循环和同一运行中的嵌套调用共用，取消不退款。时限从首次节点提交起算，不含此前等待。完整 token、模型调用次数与费用未知。';
    section.append(hint); byId('run-result').append(section);
  }
  function usageMessage(task) {
    if (!Object.hasOwn(task, "usage")) return "";
    const value=task.usage, unknown="完整任务 token、父角色用量、模型调用次数和费用未知。";
    if (!value || value.coverage!=="partial" || value.tokens!==null || value.parentTokens!==null
        || value.modelCalls!==null || value.cost!==null) return `用量信息暂时无法核对。${unknown}`;
    const codex=value.codex;
    if (codex===null) return `尚无可关联的 Codex 用量记录。${unknown}`;
    const tokens=codex?.tokens, keys=["input_tokens","cached_input_tokens","cache_write_input_tokens","output_tokens","reasoning_output_tokens","total_tokens"];
    if (!codex || codex.schemaVersion!==1 || codex.source!=="codex-rollout" || codex.coverage!=="reported-turn-records"
        || typeof codex.codexThreadId!=="string" || !codex.codexThreadId
        || typeof codex.codexTurnId!=="string" || !codex.codexTurnId
        || codex.codexThreadId!==task.executionIdentity?.codexThreadId
        || codex.codexTurnId!==task.executionIdentity?.codexTurnId
        || !Number.isSafeInteger(codex.samplesObserved) || codex.samplesObserved<1
        || !tokens || Object.keys(tokens).length!==keys.length
        || !keys.every(key=>Number.isSafeInteger(tokens[key])&&tokens[key]>=0)
        || tokens.cached_input_tokens>tokens.input_tokens || tokens.reasoning_output_tokens>tokens.output_tokens
        || tokens.total_tokens!==tokens.input_tokens+tokens.output_tokens) return `Codex 用量来源或计数无法核对。${unknown}`;
    return `Codex 已关联 ${codex.samplesObserved} 条响应记录：输入 ${tokens.input_tokens} token（缓存读取 ${tokens.cached_input_tokens}），缓存写入 ${tokens.cache_write_input_tokens} token，输出 ${tokens.output_tokens} token（推理 ${tokens.reasoning_output_tokens}），已报告合计 ${tokens.total_tokens} token。缓存和推理字段不额外相加。此为已报告快照，不能证明完整调用或实际账单。${unknown}`;
  }
  function runUsageMessage(response, bindingId, runId, binding) {
    if (!Object.hasOwn(response, "usage")) return "";
    const usage=response.usage, unknown="完整任务 token、父角色用量、模型调用次数和费用未知。";
    const cannot="本运行用量无法核对，不能据此判断预算有余或零消费。";
    const exact=(value,keys)=>value&&typeof value==="object"&&!Array.isArray(value)
      &&Object.keys(value).sort().join("\0")===[...keys].sort().join("\0");
    const rootKeys=["bindingId","schemaVersion","scope","state","localTasks","observedTasks","unmeasuredTasks","tokens","parentTokens","modelCalls","cost","codex"];
    if(!exact(usage,rootKeys)||usage.schemaVersion!==1||usage.bindingId!==bindingId
        ||!exact(usage.scope,["projectId","workflowId","runId"])
        ||typeof usage.scope.projectId!=="string"||!usage.scope.projectId
        ||typeof usage.scope.workflowId!=="string"||!usage.scope.workflowId
        ||usage.scope.runId!==runId||!runId
        ||(binding&&typeof binding.projectId==="string"&&usage.scope.projectId!==binding.projectId)
        ||(binding&&typeof binding.workflowId==="string"&&usage.scope.workflowId!==binding.workflowId)) return `${cannot}${unknown}`;
    if(usage.tokens!==null||usage.parentTokens!==null||usage.modelCalls!==null||usage.cost!==null)return `${cannot}${unknown}`;
    const counts=[usage.localTasks,usage.observedTasks,usage.unmeasuredTasks];
    const scopeLabel=`范围依据同源服务返回的项目 ${usage.scope.projectId}、工作流 ${usage.scope.workflowId} 和当前运行标识。`;
    if(usage.state==="unknown") {
      if(counts.every(value=>value===null)&&usage.codex===null)return `本运行用量状态未知，当前没有可核对的任务计数或 Codex 用量记录。${scopeLabel}${cannot}${unknown}`;
      return `${cannot}${unknown}`;
    }
    if(usage.state!=="partial"||!counts.every(value=>Number.isSafeInteger(value)&&value>=0)
        ||usage.observedTasks+usage.unmeasuredTasks>Number.MAX_SAFE_INTEGER
        ||usage.observedTasks+usage.unmeasuredTasks!==usage.localTasks) return `${cannot}${unknown}`;
    const prefix=`${scopeLabel}本地登记 ${usage.localTasks} 项，${usage.observedTasks} 项有已报告记录，${usage.unmeasuredTasks} 项尚无可测数据。`;
    if(usage.codex===null) return usage.observedTasks===0
      ? `${prefix}当前没有可核对的 Codex 响应记录。${unknown}范围仅限当前原运行；不遍历恢复子运行或子流程。`
      : `${cannot}${unknown}`;
    const codex=usage.codex, names=["input_tokens","cached_input_tokens","cache_write_input_tokens","output_tokens","reasoning_output_tokens","total_tokens"];
    const tokens=codex?.tokens;
    if(!exact(codex,["source","coverage","tokens","samplesObserved"])||codex.source!=="codex-rollout"
        ||codex.coverage!=="reported-turn-records"||!Number.isSafeInteger(codex.samplesObserved)||codex.samplesObserved<usage.observedTasks
        ||usage.observedTasks===0||!exact(tokens,names)||!names.every(name=>Number.isSafeInteger(tokens[name])&&tokens[name]>=0)
        ||tokens.cached_input_tokens>tokens.input_tokens||tokens.reasoning_output_tokens>tokens.output_tokens
        ||tokens.input_tokens+tokens.output_tokens>Number.MAX_SAFE_INTEGER
        ||tokens.total_tokens!==tokens.input_tokens+tokens.output_tokens) return `${cannot}${unknown}`;
    return `${prefix}Codex 已报告 ${codex.samplesObserved} 条响应记录：输入 ${tokens.input_tokens} token（缓存读取 ${tokens.cached_input_tokens}），缓存写入 ${tokens.cache_write_input_tokens} token，输出 ${tokens.output_tokens} token（推理 ${tokens.reasoning_output_tokens}），已报告合计 ${tokens.total_tokens} token。缓存与推理字段不额外叠加；样本不代表全部模型调用。范围仅限当前原运行，不遍历恢复子运行或子流程。${unknown}`;
  }
  serviceCapabilities = window.AgentLineServiceCapabilities?.create({request, context: () => ({epoch, authorized: !!token}), safe: safeText});
  function notice(message, tone = "warn") {
    const element = byId("notice");
    element.textContent = message;
    element.className = `notice ${tone}`;
    element.hidden = false;
  }
  function authControls(connected) {
    document.querySelectorAll("[data-auth]").forEach((button) => { button.disabled = !connected; });
    byId("login-form").hidden = connected;
    byId("connected-actions").hidden = !connected;
    byId("connection-badge").textContent = connected ? "操作已授权" : "尚未授权";
    byId("connection-badge").className = connected ? "badge ok" : "badge";
    byId("connection-title").textContent = connected ? "运行操作已授权" : "运行操作授权";
    byId("connection-hint").textContent = connected
      ? "当前身份可管理已授权项目。退出后清除本页凭证与执行结果。"
      : "查看任务与服务列表请返回首页；运行或管理任务时在此授权，凭证刷新即清除。";
    serviceCapabilities?.sync();
    updateStartAvailability();
  }
  function updateStartAvailability() {
    const button = byId("start-form").querySelector("button[type=submit]");
    button.disabled = !token || Boolean(executionBlockReason) || button.getAttribute("aria-busy") === "true";
    if (executionBlockReason) byId("start-feedback").textContent = executionBlockReason;
  }
  function empty(target, title, hint) {
    const element = byId(target);
    element.replaceChildren();
    element.className = "result empty";
    const heading = document.createElement("p");
    heading.textContent = title;
    const description = document.createElement("small");
    description.textContent = hint;
    element.append(heading, description);
  }
  function logout(showMessage = true) {
    epoch += 1;
    healthGeneration += 1;
    clearRunDirectory(false);
    token = "";
    pendingStartAttempts.clear();
    serviceCapabilities?.clear();
    workItems?.clear();
    triggerInspection?.clear();
    workflowBindings.clear();
    executionBlockReason = "";
    selectedTask = null; selectedTaskData = null; cancelTarget = null; taskGeneration += 1;
    clearRunControl(); byId('stop-run-dialog').close(); byId('pause-run-dialog').close();
    listGeneration += 1; listLoading = false;
    taskCursors = [null]; taskPage = 0; taskNext = null;
    taskFiltersDirty = false;
    for (const id of ['task-project-id','task-workflow-id','task-run-id','task-work-item-id','task-node-id']) byId(id).value = '';
    byId("task-list").replaceChildren();
    byId("task-list-message").textContent = "首页可直接查看任务列表；授权后在此读取详细记录。";
    updatePagination();
    byId("token").value = "";
    byId("cancel-task").hidden = true;
    byId("binding-options").replaceChildren();
    byId('run-directory-binding').replaceChildren(new Option('授权后选择工作流',''));
    byId('run-directory-list').replaceChildren();
    byId("binding-hint").textContent = "授权后读取当前凭证可访问的工作流。";
    byId("cancel-dialog").close();
    authControls(false);
    updateRunDirectoryPagination();
    byId("storage-state").textContent = "等待授权";
    byId("capability-state").textContent = "尚未核验";
    byId("admission-state").textContent = "尚未核验";
    clearObserverHealth();
    empty("run-result", "还没有查询结果", "授权后，可按原运行标识查询详情。");
    empty("task-result", "尚未选择节点任务", "授权后，可按原任务标识查询详情。");
    byId("start-feedback").textContent = "当前凭证已清除，触发标识仍保留以便核对。";
    if (showMessage) notice("已退出操作身份并清除凭证。正在执行的任务不会因此停止。");
  }
  window.addEventListener('pagehide', () => {
    pageDiscarded = true;
    window.AgentLineRunObservation?.browserDiscardRequest();
  });
  window.addEventListener('pageshow', event => {
    if (!event.persisted) return;
    pageDiscarded = false;
    if (token) restoreLinkedObservation();
  });
  async function request(path, options = {}) {
    if (pageDiscarded) throw new Error("页面已关闭，未再发送请求。后台任务不会因此取消。");
    if (!token) throw new Error("请先授权运行操作。");
    const currentEpoch = epoch;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(`/extension/v1${path}`, {
        method: options.body ? "POST" : "GET", redirect: "error", credentials: "omit", cache: "no-store",
        headers: { Authorization: `Bearer ${token}`, ...(options.body ? { "Content-Type": "application/json" } : {}) },
        ...(options.body ? { body: JSON.stringify(options.body) } : {}), signal: controller.signal,
      });
      if (currentEpoch !== epoch) throw new Error("操作身份已更改，请重新查询。");
      const content = await response.json().catch(() => null);
      if (currentEpoch !== epoch) throw new Error("操作身份已更改，请重新查询。");
      if (response.status === 401) {
        logout(false);
        throw new Error("运行凭证无效或已失效，请重新授权。");
      }
      if (!response.ok) {
        const fallback = ({403: "当前凭证无权执行此操作。", 404: "未找到记录，请核对标识与项目权限。", 409: "提交标识与已有记录冲突，请先核对原任务。", 422: "输入格式不符合接口要求。", 502: "上游服务暂时不可查询。", 503: "服务尚未就绪，请检查服务状态。"})[response.status] || "服务未完成请求，请稍后查询原记录。";
        const error = new Error(typeof content?.detail === "string" ? safeText(content.detail) : fallback); error.status = response.status; throw error;
      }
      if (!content || typeof content !== "object") throw new Error("服务响应无法识别，请核对原记录。");
      return content;
    } catch (error) {
      if (error instanceof TypeError || error.name === "AbortError") {
        throw new Error(options.body && !options.readOnly
          ? "未收到明确回执，执行可能已经开始。保留原触发标识并查询，勿生成新标识重试。"
          : "连接中断或查询超时，当前状态尚未确认，请重新查询。");
      }
      throw error;
    } finally { window.clearTimeout(timeout); }
  }
  function clearRunControl() { runInputReuse?.clear(); byId('run-health-summary')?.remove(); byId('run-budget-summary')?.remove(); runView?.clear(); selectedRun = null; stopRunTarget=null; pauseRunTarget=null; byId('pause-run-dialog').close(); byId('run-controls').hidden = true; for (const id of ['run-control-state','run-control-counts','run-control-hint','stop-run-identity','pause-run-identity']) byId(id).textContent = ''; }
  function updateRunDirectoryPagination() {
    byId('run-directory-prev').disabled = !token || runDirectoryLoading || runDirectoryPage === 0;
    byId('run-directory-next').disabled = !token || runDirectoryLoading || !runDirectoryNext;
    byId('run-directory-page').textContent = `第 ${runDirectoryPage + 1} 页`;
    byId('refresh-run-directory').disabled = !token || runDirectoryLoading || !byId('run-directory-binding').value;
    byId('run-directory-binding').disabled = !token || runDirectoryLoading || workflowBindings.size === 0;
    byId('run-directory-page-size').disabled = !token || runDirectoryLoading || !byId('run-directory-binding').value;
    byId('run-directory-list').querySelectorAll('button').forEach(button => { button.disabled = !token || runDirectoryLoading; });
  }
  function clearRunDirectory(clearRows = true, clearSelection = true) {
    runDirectoryGeneration += 1; runDirectoryLoading = false; runDirectoryPage = 0; runDirectoryNext = null;
    runDirectoryCursors = [null]; runDirectoryPages = []; runDirectorySeen.clear();
    if (clearRows && byId('run-directory-list')) byId('run-directory-list').replaceChildren();
    if (byId('run-directory-message')) byId('run-directory-message').textContent = token
      ? '请选择当前凭证可见的工作流绑定后刷新本地提交记录。旧服务不可用时仍可手动查询运行。'
      : '授权后读取可见工作流的本地提交记录；旧服务不可用时仍可手动查询运行。';
    if (clearSelection && byId('run-directory-binding')) byId('run-directory-binding').value = '';
    updateRunDirectoryPagination();
  }
  function resetRunDirectoryPages() {
    runDirectoryGeneration += 1; runDirectoryLoading = false;
    runDirectoryPage = 0; runDirectoryNext = null; runDirectoryCursors = [null];
    runDirectoryPages = []; runDirectorySeen.clear();
  }
  function runDirectoryStatus(value) {
    return ({intent:'提交意向已保存',accepted:'已接收',uncertain:'结果待核对',rejected:'已拒绝',unknown:'状态未知'})[value];
  }
  function runDirectoryScope() {
    const bindingId = byId('run-directory-binding').value;
    const scope = workflowBindings.get(bindingId);
    return scope && typeof scope.projectId === 'string' && scope.projectId
      && typeof scope.workflowId === 'string' && scope.workflowId ? {bindingId, ...scope} : null;
  }
  function validateRunDirectory(data, scope, limit, cursor, page) {
    const allowed = new Set(['intent','accepted','uncertain','rejected','unknown']);
    if (!data || data.bindingId !== scope.bindingId || data.projectId !== scope.projectId || data.workflowId !== scope.workflowId
        || data.source !== 'local_submission_journal' || data.executionStateIncluded !== false || !Array.isArray(data.runs)
        || data.runs.length > limit || !(data.nextCursor === null || typeof data.nextCursor === 'string')) throw new Error('运行目录响应身份或结构无法核验。');
    const ids = new Set();
    for (const item of data.runs) {
      if (!item || typeof item.runId !== 'string' || !item.runId || item.workflowId !== scope.workflowId
          || !allowed.has(item.submissionStatus) || !(item.updatedAt === null || (typeof item.updatedAt === 'number' && Number.isFinite(item.updatedAt) && item.updatedAt >= 0 && item.updatedAt <= 8_640_000_000_000))
          || ids.has(item.runId)) throw new Error('运行目录记录字段异常或标识重复。');
      ids.add(item.runId);
    }
    if (data.nextCursor !== null && (!data.runs.length || data.nextCursor !== data.runs[data.runs.length - 1].runId)) throw new Error('运行目录游标与当前页不一致。');
    if (page > 0 && cursor === null) throw new Error('运行目录分页游标缺失。');
    if (data.nextCursor !== null && data.nextCursor === cursor) throw new Error('运行目录游标未向前推进。');
    const previous = runDirectoryPages[page - 1];
    if (page > 0 && previous && ids.size && [...ids].some(id => previous.ids.has(id))) throw new Error('运行目录分页重复，已停止继续翻页。');
    if (page < runDirectoryPages.length && runDirectoryPages[page]
        && JSON.stringify([...ids]) !== JSON.stringify(runDirectoryPages[page].idsList)) throw new Error('运行目录旧页面与本次读取不一致，请刷新列表。');
    if (data.nextCursor !== null && runDirectoryCursors.slice(0, page + 1).includes(data.nextCursor)) throw new Error('运行目录游标形成循环。');
    return {ids, idsList:[...ids]};
  }
  function renderRunDirectory(data, pageInfo) {
    const list = byId('run-directory-list'); list.replaceChildren();
    data.runs.forEach(item => {
      const row = document.createElement('li'), button = document.createElement('button');
      button.type = 'button'; button.className = 'task-row'; button.dataset.runId = item.runId;
      button.dataset.bindingId = data.bindingId; button.dataset.projectId = data.projectId;
      button.dataset.workflowId = data.workflowId; button.dataset.epoch = String(epoch);
      button.dataset.directoryGeneration = String(runDirectoryGeneration);
      const title = document.createElement('strong'); title.textContent = item.runId;
      const context = document.createElement('span'); context.className = 'task-context';
      context.textContent = `本地提交状态：${runDirectoryStatus(item.submissionStatus)} · 尚未查询原生执行`;
      const time = document.createElement('span'); time.className = 'task-context';
      time.textContent = item.updatedAt === null ? '记录时间：未知' : `记录更新时间：${new Date(item.updatedAt * 1000).toLocaleString()}`;
      button.append(title, context, time); row.append(button); list.append(row);
    });
    const scope = runDirectoryScope();
    byId('run-directory-message').textContent = data.runs.length
      ? `本地提交记录 ${data.runs.length} 条；列表不含原生执行状态。点击记录仅填入查询表单，需再按“查询运行状态”核对。`
      : '此绑定当前没有本地提交记录；可继续手动查询已知运行标识。';
    runDirectoryPage = pageInfo.page; runDirectoryNext = data.nextCursor;
    runDirectoryPages[pageInfo.page] = pageInfo;
    for (let index=0; index<=pageInfo.page; index++) runDirectoryPages[index]?.ids.forEach(id => runDirectorySeen.add(id));
    updateRunDirectoryPagination();
  }
  async function loadRunDirectory(page = 0) {
    const scope = runDirectoryScope();
    if (!token || !scope || runDirectoryLoading) return;
    const generation = ++runDirectoryGeneration, capturedEpoch = epoch;
    const limit = Number(byId('run-directory-page-size').value), cursor = runDirectoryCursors[page];
    if (!Number.isInteger(limit) || ![10,25,50].includes(limit) || cursor === undefined) return;
    runDirectoryLoading = true; updateRunDirectoryPagination();
    byId('run-directory-message').textContent = '正在读取本地提交状态；尚未查询原生执行。';
    try {
      const query = new URLSearchParams({limit:String(limit)}); if (cursor) query.set('after',cursor);
      const data = await request(`/workflows/${encodeURIComponent(scope.bindingId)}/runs?${query}`);
      if (generation !== runDirectoryGeneration || capturedEpoch !== epoch || runDirectoryScope()?.bindingId !== scope.bindingId) return;
      const knownPage = Boolean(runDirectoryPages[page]);
      const pageInfo = validateRunDirectory(data,scope,limit,cursor,page);
      if (!knownPage && page > 0 && data.runs.length && runDirectorySeen.size && [...pageInfo.ids].some(id => runDirectorySeen.has(id))) throw new Error('运行目录出现已显示记录，已停止分页以避免循环。');
      if (data.nextCursor !== null) runDirectoryCursors[page + 1] = data.nextCursor;
      renderRunDirectory(data,pageInfo);
    } catch (error) {
      if (generation !== runDirectoryGeneration || capturedEpoch !== epoch) return;
      runDirectoryPage = 0; runDirectoryNext = null; runDirectoryCursors = [null];
      runDirectoryPages = []; runDirectorySeen.clear();
      byId('run-directory-list').replaceChildren();
      byId('run-directory-message').textContent = error.status === 404
        ? cursor ? '分页位置或绑定已变化，请手动刷新目录；不会重新启动运行。'
          : '运行目录或当前绑定不可用；仍可在上方手动查询已授权的原运行。'
        : `运行目录暂不可核验：${error.message} 可保留运行标识并使用上方手动查询。`;
    } finally {
      if (generation === runDirectoryGeneration && capturedEpoch === epoch) { runDirectoryLoading = false; updateRunDirectoryPagination(); }
    }
  }
  byId('run-directory-binding').addEventListener('change', () => {
    clearRunDirectory(true,false);
    if (byId('run-directory-binding').value) loadRunDirectory(0);
  });
  byId('refresh-run-directory').addEventListener('click', () => {
    resetRunDirectoryPages(); byId('run-directory-list').replaceChildren(); loadRunDirectory(0);
  });
  byId('run-directory-page-size').addEventListener('change', () => {
    resetRunDirectoryPages(); byId('run-directory-list').replaceChildren();
    if (byId('run-directory-binding').value) loadRunDirectory(0);
  });
  byId('run-directory-prev').addEventListener('click', () => { if (runDirectoryPage > 0) loadRunDirectory(runDirectoryPage - 1); });
  byId('run-directory-next').addEventListener('click', () => { if (runDirectoryNext) loadRunDirectory(runDirectoryPage + 1); });
  byId('run-directory-list').addEventListener('click', event => {
    const button = event.target.closest('button[data-run-id]');
    const scope = runDirectoryScope();
    if (!button || !scope || !button.isConnected || !token
        || button.dataset.bindingId !== scope.bindingId || button.dataset.projectId !== scope.projectId
        || button.dataset.workflowId !== scope.workflowId || button.dataset.epoch !== String(epoch)
        || button.dataset.directoryGeneration !== String(runDirectoryGeneration)) return;
    runQueryGeneration += 1; clearRunControl(); byId('stop-run-dialog').close(); byId('pause-run-dialog').close();
    byId('query-binding').value = scope.bindingId; byId('run-id').value = button.dataset.runId;
    rememberObservation(scope.bindingId, button.dataset.runId);
    runInputReuse?.clear();
    empty('run-result','尚未查询原生执行','此记录只提供本地提交状态。请按“查询运行状态”核对原生执行；不会自动查询、启动或恢复。');
    notice('已填入原运行标识，当前仍只掌握本地提交状态。请手动查询原生执行。','warn');
  });
  function validControl(control) {
    return control?.supported === true && ['active','paused','stop_requested'].includes(control.state)
      && Number.isSafeInteger(control.version) && control.version >= 0
      && (control.state !== 'paused' || ['continue','interrupt','unrecorded'].includes(control.activeExecution))
      && ['total','queued','active','uncertain','resourcesUnreleased'].every(key => Number.isSafeInteger(control.taskCounts?.[key]) && control.taskCounts[key] >= 0);
  }
  function pauseChoiceHint(choice) {
    if (choice === 'continue') return '活动任务继续执行，后继任务准入已暂停。继续需要你明确操作。';
    if (choice === 'interrupt') return '已申请中断活动任务，尚未确认停止。后继任务准入已暂停。';
    if (choice === 'unrecorded') return '这次暂停没有记录活动任务是继续还是中断，不能据此判断。后继任务准入已暂停。';
    return '暂停选择无法核对。请查询原运行，不要把它当成已停止。';
  }
  function renderRunControl(binding, runId, control) {
    if (!validControl(control)) { clearRunControl(); return; }
    const key = JSON.stringify([binding,runId]), pending = pendingControls.get(key);
    if (pending && control.operationId === pending.operationId && control.version > pending.expectedVersion) pendingControls.delete(key);
    selectedRun = {binding,runId,key,control};
    const unresolved = pendingControls.get(key), counts=control.taskCounts;
    byId('run-controls').hidden = false;
    byId('run-control-state').textContent = ({active:'后继任务准入开放',paused:'后继任务准入已暂停',stop_requested:'已申请停止，仍需核对任务退出'})[control.state] + ' · 版本 ' + control.version;
    const verificationKeys = ['verificationPending','verificationCancelRequested','verificationCanceled'];
    const verificationCounts = verificationKeys.every(name => Number.isSafeInteger(counts[name]) && counts[name] >= 0)
      ? `测试验收待确认 ${counts.verificationPending} · 验收停止待确认 ${counts.verificationCancelRequested} · 已取消验收 ${counts.verificationCanceled}`
      : verificationKeys.every(name => !Object.hasOwn(counts,name)) ? '测试验收状态未提供' : '测试验收状态待核对';
    byId('run-control-counts').textContent = `任务共 ${counts.total} · 排队 ${counts.queued} · 活动 ${counts.active} · 未知 ${counts.uncertain} · 编码资源待确认 ${counts.resourcesUnreleased} · ${verificationCounts}`;
    byId('run-control-hint').textContent = unresolved ? '此控制请求结果未知，操作标识：'+unresolved.operationId+'。只查询原运行核对，勿重复提交。'
      : control.state==='paused' ? pauseChoiceHint(control.activeExecution)
      : control.state==='stop_requested' ? '停止申请已记录。资源未释放不等于已停止；原生取消状态也不能证明 Codex 已退出。'
      : '控制只影响当前流程。原生执行状态和资源释放情况分别核对。';
    byId('pause-run').disabled = controlBusy || !!unresolved || control.state!=='active';
    byId('continue-run').disabled = controlBusy || !!unresolved || control.state!=='paused';
    byId('stop-run').disabled = controlBusy || !!unresolved || control.state==='stop_requested';
  }
  async function controlRun(action, activeExecution) {
    const selected=selectedRun, captured=epoch;
    if(!selected || controlBusy || pendingControls.has(selected.key)) return;
    if(action==='pause' && !['continue','interrupt'].includes(activeExecution)) return;
    if(selected.binding!==byId('query-binding').value.trim() || selected.runId!==byId('run-id').value.trim()) { clearRunControl(); return; }
    const operation={operationId:'control-'+(typeof crypto.randomUUID==="function"?crypto.randomUUID():Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,"0")).join("")),action,expectedVersion:selected.control.version};
    if(action==='pause') operation.activeExecution=activeExecution;
    pendingControls.set(selected.key,operation); controlBusy=true;
    // Disable before issuing a write; never treat local intent as a response.
    for(const id of ['pause-run','continue-run','stop-run']) byId(id).disabled=true;
    byId('run-control-hint').textContent='控制请求提交中，请保留原运行标识。';
    try {
      const data=await request(`/workflows/${encodeURIComponent(selected.binding)}/runs/${encodeURIComponent(selected.runId)}/control`,{body:operation});
      if(captured!==epoch || selectedRun?.key!==selected.key) return;
      if(!validControl(data.control) || data.control.operationId!==operation.operationId || data.control.version<=operation.expectedVersion) throw new Error('控制回执尚未核实，请查询原运行。');
      pendingControls.delete(selected.key); renderRunControl(selected.binding,selected.runId,data.control);
      notice(action==='stop'?'停止申请已记录，请继续查询资源退出；尚不能判定已停止。':action==='pause'&&operation.activeExecution==='interrupt'?'已申请中断活动任务，尚未确认停止。请查询原运行。':'控制状态已更新；请查询原运行核对执行进度。','warn');
    } catch(error) {
      if(error.status===409) pendingControls.delete(selected.key);
      if(captured!==epoch || selectedRun?.key!==selected.key) return;
      if(error.status===409) { clearRunControl(); notice('控制版本已变化，此操作未确认生效。请先查询最新运行状态。','warn'); }
      else { byId('run-control-hint').textContent='控制结果未知，操作标识：'+operation.operationId+'。仅查询原运行核对，不自动重发。'; notice('未核实控制结果，请保留原标识并查询。','warn'); }
    } finally {
      controlBusy=false;
      if(captured===epoch && selectedRun?.key===selected.key) renderRunControl(selected.binding,selected.runId,selectedRun.control);
    }
  }
  byId('pause-run').addEventListener('click',()=>{if(!selectedRun)return;pauseRunTarget={key:selectedRun.key,version:selectedRun.control.version};byId('pause-run-identity').textContent=selectedRun.binding+' / '+selectedRun.runId;byId('pause-run-dialog').showModal();});
  byId('dismiss-pause-run').addEventListener('click',()=>{pauseRunTarget=null;byId('pause-run-dialog').close();});
  function confirmPause(choice){byId('pause-run-dialog').close();if(!selectedRun||pauseRunTarget?.key!==selectedRun.key||pauseRunTarget.version!==selectedRun.control.version){pauseRunTarget=null;notice('确认期间运行状态已变化，请重新查询后再操作。','warn');return;}pauseRunTarget=null;controlRun('pause',choice);}
  byId('pause-run-continue').addEventListener('click',()=>confirmPause('continue'));
  byId('pause-run-interrupt').addEventListener('click',()=>confirmPause('interrupt'));
  byId('continue-run').addEventListener('click',()=>controlRun('continue'));
  byId('stop-run').addEventListener('click',()=>{if(selectedRun){stopRunTarget={key:selectedRun.key,version:selectedRun.control.version};byId('stop-run-identity').textContent=selectedRun.binding+' / '+selectedRun.runId;byId('stop-run-dialog').showModal();}});
  byId('dismiss-stop-run').addEventListener('click',()=>byId('stop-run-dialog').close());
  byId('confirm-stop-run').addEventListener('click',()=>{byId('stop-run-dialog').close(); byId('pause-run-dialog').close();if(!selectedRun||stopRunTarget?.key!==selectedRun.key||stopRunTarget.version!==selectedRun.control.version){notice('确认期间运行状态已变化，请重新查询后再操作。','warn');return;}stopRunTarget=null;controlRun('stop');});
  for(const id of ['query-binding','run-id']) byId(id).addEventListener('input',()=>{runQueryGeneration++;clearRunControl();byId('stop-run-dialog').close(); byId('pause-run-dialog').close();empty('run-result','运行标识已更改','请查询当前运行，旧结果已清除。');});
  async function busy(button, action) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try { await action(); }
    catch (error) { notice(error.message || "操作未完成，请检查服务状态。", "error"); }
    finally {
      button.removeAttribute("aria-busy");
      button.disabled = button.hasAttribute("data-auth") && !token;
      if (button.form === byId("start-form")) updateStartAvailability();
    }
  }
  function details(target, rows, raw, contextRows = [], settingsRows = [], outputRows = []) {
    const element = byId(target);
    element.replaceChildren();
    element.className = "result";
    const list = document.createElement("dl");
    rows.forEach(([name, value]) => {
      const key = document.createElement("dt");
      const item = document.createElement("dd");
      key.textContent = name;
      item.textContent = safeText(value);
      list.append(key, item);
    });
    const disclosure = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "原始结果与证据（高级）";
    const pre = document.createElement("pre");
    pre.textContent = safeText(raw);
    disclosure.append(summary, pre);
    element.append(list);
    for (const [title, entries] of [["流程与执行标识", contextRows], ["用量与执行设置", settingsRows], ["运行输出", outputRows]]) {
      if (!entries.length) continue;
      const trace = document.createElement("details");
      trace.className = "task-trace";
      const heading = document.createElement("summary");
      heading.textContent = title;
      const context = document.createElement("dl");
      for (const [name, value] of entries) {
        const key = document.createElement("dt"), item = document.createElement("dd");
        key.textContent = name;
        item.textContent = value === undefined || value === null || value === "" ? "尚无记录" : safeText(value);
        context.append(key, item);
      }
      trace.append(heading, context);
      element.append(trace);
    }
    element.append(disclosure);
  }
  function businessRows(value, projectId) {
    return window.AgentLineActivity.businessLines(value, projectId);
  }
  function inputObject() {
    let input;
    try { input = JSON.parse(byId("workflow-input").value); }
    catch { throw new Error("输入参数不是有效 JSON，请检查引号与逗号。"); }
    if (!input || typeof input !== "object" || Array.isArray(input)) throw new Error("输入参数必须是 JSON 对象，例如 {}。");
    function check(value, depth = 0) {
      if (depth > 64) throw new Error('输入嵌套超过64层，请简化后再启动。');
      if (typeof value === 'number' && (!Number.isFinite(value) || Math.abs(value) > Number.MAX_SAFE_INTEGER)) {
        throw new Error('输入数值超出JavaScript安全范围；大整数编号请加引号改为文本，未提交。');
      }
      if (value && typeof value === 'object') for (const item of Object.values(value)) check(item, depth + 1);
    }
    check(input);
    return input;
  }
  function newKey() {
    runInputReuse?.clear();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    byId("trigger-key").value = `web-${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
    byId("start-feedback").textContent = executionBlockReason || "已准备新的触发标识，尚未提交。";
  }
  function clearObserverHealth() {
    byId("observer-health").hidden = true;
    byId("observer-health").className = "observer-health";
    byId("observer-state").textContent = "";
    byId("observer-hint").textContent = "";
  }
  function observerMessage(label, hint, tone = "warn") {
    byId("observer-health").hidden = false;
    byId("observer-health").className = `observer-health ${tone}`;
    byId("observer-state").textContent = label;
    byId("observer-hint").textContent = hint;
  }
  function renderObserverHealth(data) {
    if (!Object.hasOwn(data, "executionObserver")) { clearObserverHealth(); return; }
    const states = {
      healthy: ["巡检正常", "本地巡检最近完成。任务是否推进，请查询原任务。", "ok"],
      degraded: ["巡检遇到错误", "最近一次巡检遇到错误，请刷新状态并核对原任务。", "warn"],
      delayed: ["巡检完成延迟", "观察窗口内尚无新扫描完成。请保留原任务标识，刷新状态后核对。", "warn"],
      stopped: ["巡检线程已停止", "请核对服务状态；任务是否停止，以原任务回执为准。", "warn"],
      starting: ["等待首轮巡检", "巡检已启动，尚未完成首次扫描。", ""],
      not_started: ["巡检尚未启动", "执行巡检尚未开始，请刷新服务状态。", "warn"],
      not_configured: ["未配置执行巡检", "当前服务没有配置执行巡检，任务状态以原任务查询结果为准。", ""],
    };
    const value = data.executionObserver;
    const finiteOrNull = (number) => number === null || (typeof number === "number" && Number.isFinite(number) && number >= 0);
    const valid = value && typeof value === "object" && !Array.isArray(value) && Object.hasOwn(states, value.status)
      && (value.status === "not_configured" || (finiteOrNull(value.lastScanCompletedAt)
        && finiteOrNull(value.secondsSinceCompletedScanOrStart) && typeof value.observationWindowSeconds === "number"
        && Number.isFinite(value.observationWindowSeconds) && value.observationWindowSeconds > 0
        && (!["healthy", "degraded"].includes(value.status) || (typeof value.lastScanCompletedAt === "number"
          && typeof value.secondsSinceCompletedScanOrStart === "number"
          && value.secondsSinceCompletedScanOrStart <= value.observationWindowSeconds))));
    if (!valid) {
      observerMessage("巡检状态待核对", "暂时无法确认巡检状态，请刷新状态并查询原任务。");
      return;
    }
    const [title, hint, tone] = states[value.status];
    let cadenceHint = '';
    if (Object.hasOwn(value, 'queryCadence')) {
      const cadence = value.queryCadence;
      const validCadence = cadence && typeof cadence === 'object' && !Array.isArray(cadence)
        && cadence.scope === 'authorized_projects_last_scan_page'
        && Number.isSafeInteger(cadence.observedTasks) && cadence.observedTasks >= 0
        && Number.isSafeInteger(cadence.deferredTasks) && cadence.deferredTasks >= 0
        && cadence.observedTasks + cadence.deferredTasks <= 500
        && typeof cadence.maxDeferredSeconds === 'number' && Number.isFinite(cadence.maxDeferredSeconds)
        && cadence.maxDeferredSeconds >= 0 && cadence.maxDeferredSeconds <= 30
        && (cadence.deferredTasks > 0 || cadence.maxDeferredSeconds === 0);
      cadenceHint = validCadence
        ? ' 最近一页授权项目记录：尝试查询 ' + cadence.observedTasks + ' 项，退避等待 ' + cadence.deferredTasks + ' 项。'
          + (cadence.deferredTasks ? ' 最长约 ' + Math.ceil(cadence.maxDeferredSeconds) + ' 秒后再查，取消与到期处理不等待。' : '')
        : ' 查询节奏摘要待核对。';
    }
    observerMessage(title, hint + cadenceHint, tone);
  }
  async function loadHealth() {
    const generation = ++healthGeneration, healthEpoch = epoch;
    if (!byId("observer-health").hidden) observerMessage("正在查询巡检状态", "正在读取服务状态，请稍候。", "");
    let data;
    try { data = await request("/health"); }
    catch (error) {
      if (generation !== healthGeneration || healthEpoch !== epoch) return null;
      observerMessage("巡检状态待核对", "状态查询未完成，请刷新状态并核对原任务。");
      throw error;
    }
    if (generation !== healthGeneration || healthEpoch !== epoch) return null;
    renderObserverHealth(data);
    executionBlockReason = Object.prototype.hasOwnProperty.call(data, "executionBlockReason")
      ? typeof data.executionBlockReason === "string" && data.executionBlockReason.trim() && data.executionBlockReason.length <= 400
        ? safeText(data.executionBlockReason) : "执行能力信息异常，已停止新启动，请核对服务状态。"
      : "";
    byId("storage-state").textContent = data.storage === "available" ? "可用" : "待核对";
    byId("capability-state").textContent = statusLabel(data.controlledExecution);
    byId("admission-state").textContent = executionBlockReason ? "已停止新执行" : data.reconciliationRequired ? "需恢复核对" : data.readyForExecution ? "允许新节点任务" : "节点执行尚未开放";
    updateStartAvailability();
    return data;
  }
  async function loadBindings() {
    const generation=epoch;
    try {
      const data = await request("/workflows");
      if(generation!==epoch||!token)return;
      if (!Array.isArray(data.workflows)) throw new Error("绑定列表响应无法识别。");
      const list = byId("binding-options");
      list.replaceChildren();
      workflowBindings.clear();
      const directorySelect = byId('run-directory-binding');
      directorySelect.replaceChildren(new Option('选择当前凭证可见绑定', ''));
      clearRunDirectory(true,false);
      data.workflows.forEach((binding) => {
        if (!binding || typeof binding.bindingId !== 'string' || !binding.bindingId) return;
        workflowBindings.set(binding.bindingId,{projectId:binding.projectId,workflowId:binding.workflowId});
        const option = document.createElement("option");
        option.value = binding.bindingId;
        option.label = typeof binding.projectId === 'string' ? `项目：${binding.projectId}` : '已配置绑定';
        list.append(option);
        if (typeof binding.projectId === 'string' && binding.projectId && typeof binding.workflowId === 'string' && binding.workflowId)
          directorySelect.add(new Option(`${binding.workflowId} · ${binding.bindingId}（项目 ${binding.projectId}）`,binding.bindingId));
      });
      byId("binding-hint").textContent = data.workflows.length
        ? `当前可访问 ${data.workflows.length} 个工作流绑定。可输入标识筛选。`
        : "当前凭证下尚无已配置的工作流绑定。";
    } catch {
      if(generation!==epoch)return;
      workflowBindings.clear();
      byId('run-directory-binding').replaceChildren(new Option('绑定目录不可用，可手动查询', ''));
      clearRunDirectory(true,false);
      byId("binding-hint").textContent = "暂时无法读取绑定列表；可填写已知标识，权限由服务核验。";
    }
    updateRunDirectoryPagination();
  }
  byId("login-form").addEventListener("submit", (event) => {
    event.preventDefault();
    busy(event.submitter || event.currentTarget.querySelector("button[type=submit]"), async () => {
      const enteredToken = byId("token").value.trim();
      logout(false);
      token = enteredToken;
      epoch += 1;
      const loginEpoch = epoch;
      clearRunDirectory();
      try {
        const health = await loadHealth();
        if (!health) return;
        byId("token").value = "";
        authControls(true);
        notice(executionBlockReason || (health.readyForExecution ? "操作已授权，可以查询记录与启动已配置流程。" : "操作已授权。受控节点执行尚未开放；已配置的工作流可独立查询与启动。"), !executionBlockReason && health.readyForExecution ? "ok" : "warn");
        await loadBindings();
        await loadTasks();
        if (loginEpoch === epoch && token) await workItems?.connect();
        if (loginEpoch === epoch && token) await triggerInspection?.connect();
        if (loginEpoch === epoch && token) await restoreLinkedObservation();
      } catch (error) { logout(false); throw error; }
    });
  });
  byId("logout").addEventListener("click", () => logout());
  byId("refresh-health").addEventListener("click", (event) => busy(event.currentTarget, async () => {
    if (!await loadHealth()) return;
    notice(executionBlockReason || "已更新服务状态。", executionBlockReason ? "warn" : "ok");
  }));
  byId("new-key").addEventListener("click", newKey);
  for (const eventName of ['input','change']) byId('start-form').addEventListener(eventName, () => runInputReuse?.clear('启动表单已改变，请重新读取原输入后核对。'));
  byId("start-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (executionBlockReason) { notice(executionBlockReason, "warn"); return; }
    busy(event.submitter || event.currentTarget.querySelector("button[type=submit]"), async () => {
      const binding = byId("binding").value.trim();
      const inputs = inputObject();
      const workItemId = workItems?.selection(workflowBindings.get(binding)?.projectId);
      byId("query-binding").value = binding;
      byId("run-id").value = "";
      const queryGeneration=++runQueryGeneration, queryEpoch=epoch;
      clearRunControl();
      empty("run-result", "正在提交", "等待服务回执，尚未确认执行结果。");
      byId("start-feedback").textContent = "正在提交，请保留当前触发标识。";
      const idempotencyKey = byId("trigger-key").value.trim();
      const startAttemptKey = JSON.stringify([binding,idempotencyKey]);
      pendingStartAttempts.add(startAttemptKey);
      let data;
      try {
        data = await request(`/workflows/${encodeURIComponent(binding)}/runs`, {
          body: { idempotencyKey, input: inputs,
            ...(workItemId ? {workItemId} : {}) },
        });
      } catch (error) {
        if (queryEpoch !== epoch) return;
        if(queryGeneration===runQueryGeneration&&queryEpoch===epoch) empty("run-result", "未取得提交回执", "请保留当前触发标识，先核对原任务。");
        byId("start-feedback").textContent = "未确认提交结果。不要使用新标识重复发起。";
        throw error;
      }
      if (queryEpoch !== epoch) return;
      if (['accepted','rejected'].includes(data.status) && typeof data.runId === 'string'
          && /^[A-Za-z0-9_-]{1,160}$/.test(data.runId)) pendingStartAttempts.delete(startAttemptKey);
      byId("start-feedback").textContent = `${statusLabel(data.status)}。回执不代表执行成功，请查询运行状态。`;
      if(queryGeneration!==runQueryGeneration||queryEpoch!==epoch||byId('query-binding').value.trim()!==binding) {
        byId('start-feedback').textContent += ` 原运行标识：${data.runId || '未返回'}；当前查询选择已变化，未覆盖。`;
        return;
      }
      if (data.runId) { byId("run-id").value = data.runId; rememberObservation(binding, data.runId); }
      runInputReuse?.clear();
      details("run-result", [["提交状态", statusLabel(data.status)], ["运行标识", data.runId || "未返回"], ["执行结论", "尚未查询"], ["验收结论", "未验证"],
        ...businessRows(data.workItem, workflowBindings.get(binding)?.projectId)], data);
      notice("已收到提交记录，请查询原运行标识核对结果。", data.status === "accepted" ? "ok" : "warn");
    });
  });
  byId("run-form").addEventListener("submit", (event) => {
    event.preventDefault();
    busy(event.submitter || event.currentTarget.querySelector("button[type=submit]"), async () => {
      const selectedBinding=byId("query-binding").value.trim(), selectedRunId=byId("run-id").value.trim();
      rememberObservation(selectedBinding, selectedRunId);
      const queryGeneration=++runQueryGeneration, queryEpoch=epoch;
      clearRunControl();
      const binding = encodeURIComponent(selectedBinding);
      const run = encodeURIComponent(byId("run-id").value.trim());
      empty("run-result", "正在查询", "等待服务返回最新状态。");
      let data;
      try { data = await request(`/workflows/${binding}/runs/${run}`); }
      catch (error) { if(queryGeneration!==runQueryGeneration||queryEpoch!==epoch) return; empty("run-result", "未能读取运行状态", "保留原标识，稍后继续查询。"); throw error; }
      if(queryGeneration!==runQueryGeneration||queryEpoch!==epoch||selectedBinding!==byId('query-binding').value.trim() || selectedRunId!==byId('run-id').value.trim()) return;
      renderRunControl(selectedBinding,selectedRunId,data.control);
      const native = data.execution?.response?.data || data.execution?.response || {};
      const submission = data.resumeSubmission;
      const resumed = data.resumption;
      const hasResume = Boolean(submission || resumed);
      const rows = [["提交状态", statusLabel(data.submission?.status)], [hasResume ? "原运行状态" : "执行状态", statusLabel(data.execution?.status)], ["运行标识", data.execution?.runId || byId("run-id").value]];
      const outputRows = [];
      const unresolved = submission && ["intent", "uncertain", "unknown"].includes(submission.status);
      if (hasResume) {
        rows.push(["最近恢复提交", statusLabel(submission?.status || resumed?.submissionStatus)]);
        if (submission?.contextId) rows.push(["恢复等待标识", submission.contextId]);
        if (unresolved) rows.push(["恢复执行状态", "最新恢复提交结果未知，请保留原标识继续查询"]);
        else rows.push(["恢复执行状态", statusLabel(resumed?.execution?.status)]);
        rows.push(["恢复运行标识", unresolved ? (submission.runId || "尚未确认；保留原运行标识") : (resumed?.runId || submission?.runId || "尚未确认")]);
        if (resumed) {
          if (unresolved) rows.push(["此前已接收的恢复状态", statusLabel(resumed.execution?.status)], ["此前恢复运行标识", resumed.runId || "尚未确认"]);
          const child = resumed.execution?.response?.data || resumed.execution?.response || {};
          outputRows.push([unresolved ? "此前恢复输出" : "恢复输出", child.output === undefined || child.output === null ? "尚无输出" : child.output]);
        }
      }
      rows.push(["验收结论", "本接口未提供独立验收结论"]);
      rows.push(...businessRows(data.workItem, workflowBindings.get(selectedBinding)?.projectId));
      const runUsage=runUsageMessage(data,selectedBinding,selectedRunId,workflowBindings.get(selectedBinding));
      outputRows.push([hasResume ? "原运行输出" : "输出", native.output === undefined || native.output === null ? "尚无输出" : native.output]);
      details("run-result", rows, data, [], runUsage ? [["本运行已报告用量",runUsage]] : [], outputRows);
      renderRunBudget(data,selectedBinding,selectedRunId,workflowBindings.get(selectedBinding));
      window.AgentLineRunHealth?.render(byId('run-result'),data.health,{bindingId:selectedBinding,runId:selectedRunId,
        ...workflowBindings.get(selectedBinding)});
      const taskScope = workflowBindings.get(selectedBinding);
      if (taskScope && typeof taskScope.projectId === "string" && taskScope.projectId
          && typeof taskScope.workflowId === "string" && taskScope.workflowId) {
        const viewTasks = document.createElement("button");
        viewTasks.type = "button"; viewTasks.className = "secondary run-task-directory-link";
        viewTasks.dataset.auth = "";
        viewTasks.textContent = "查看本次运行的节点任务";
        viewTasks.addEventListener("click", () => {
          const currentScope = workflowBindings.get(selectedBinding);
          if (queryGeneration !== runQueryGeneration || queryEpoch !== epoch
              || byId("query-binding").value.trim() !== selectedBinding
              || byId("run-id").value.trim() !== selectedRunId
              || !currentScope || currentScope.projectId !== taskScope.projectId || currentScope.workflowId !== taskScope.workflowId) return;
          viewRunTasks(taskScope.projectId, taskScope.workflowId, selectedRunId);
        });
        byId("run-result").append(viewTasks);
      }
      if (unresolved) notice("最新恢复提交结果未知。此前恢复记录不代表本次结果；请保留原运行标识继续查询，勿重复提交。", "warn");
      else if (resumed?.execution?.status === "failed") notice("恢复执行失败。原运行可能仍显示等待恢复，请按恢复运行标识核对失败记录。", "error");
      else if (hasResume && (!resumed || !resumed.execution?.status || resumed.execution.status === "unknown")) notice("恢复执行结果尚未确认，请保留原运行标识和恢复运行标识继续查询。", "warn");
      else notice(data.execution?.status === "unknown" ? "当前执行结果未知，请保留原标识继续核对。" : "已读取最新运行状态。", data.execution?.status === "unknown" ? "warn" : "ok");
    });
  });
  byId('read-run-health').addEventListener('click', event => busy(event.currentTarget, async () => {
    if (!byId('run-form').reportValidity()) return;
    const bindingId = byId('query-binding').value.trim(), runId = byId('run-id').value.trim();
    if (!/^[A-Za-z0-9_-]{1,160}$/.test(bindingId) || !/^[A-Za-z0-9_-]{1,160}$/.test(runId)) {
      notice('请填写有效的工作流绑定和原运行标识。','warn'); return;
    }
    rememberObservation(bindingId, runId);
    const queryGeneration = ++runQueryGeneration, queryEpoch = epoch;
    clearRunControl(); byId('stop-run-dialog').close(); byId('pause-run-dialog').close();
    empty('run-result','正在读取本地记录','不会访问原生执行服务或调用模型。');
    let data;
    try { data = await request(`/workflows/${encodeURIComponent(bindingId)}/runs/${encodeURIComponent(runId)}/health`); }
    catch (error) {
      if (queryGeneration !== runQueryGeneration || queryEpoch !== epoch) return;
      empty('run-result','本地记录暂时无法核对','保留原标识，不自动重新提交任务。'); throw error;
    }
    if (queryGeneration !== runQueryGeneration || queryEpoch !== epoch
        || bindingId !== byId('query-binding').value.trim() || runId !== byId('run-id').value.trim()) return;
    if (data.source !== 'local_run_health' || data.readOnly !== true || data.bindingId !== bindingId) {
      empty('run-result','本地记录响应无法核对','没有获得可信的当前运行摘要。');
      notice('本地检查身份无法核对，请保留原运行标识。','warn'); return;
    }
    details('run-result',[['查询范围','仅本地任务、事件与工作区租约'],['运行标识',runId],
      ['原生执行状态','本次未查询'],['执行、验收与记忆结论','各项记录分别显示，不作整体成功判断']],data);
    window.AgentLineRunHealth?.render(byId('run-result'),data,{bindingId,runId,...workflowBindings.get(bindingId)});
    const panel = byId('run-health-summary'); if (panel) panel.open = true;
    notice('已读取本地检查摘要；此查询没有核对原生执行是否仍活动。','warn');
  }));
  function updatePagination() {
    byId("task-prev").disabled = !token || listLoading || taskFiltersDirty || taskPage === 0;
    byId("task-next").disabled = !token || listLoading || taskFiltersDirty || !taskNext;
    byId("task-page").textContent = `第 ${taskPage + 1} 页`;
  }
  function readTaskFilters() {
    const raw = Object.fromEntries([['projectId','task-project-id'],['workflowId','task-workflow-id'],['runId','task-run-id'],['workItemId','task-work-item-id'],['nodeId','task-node-id']]
      .map(([key,id]) => [key,byId(id).value]));
    if (Object.values(raw).some(value => value.length > 256)) return {error:"筛选标识最多 256 个字符。"};
    if (Object.values(raw).some(value => value.length > 0 && !value.trim())) return {error:"筛选标识去除首尾空格后不能为空。"};
    const filters = Object.fromEntries(Object.entries(raw).map(([key,value]) => [key,value.trim() || null]));
    if ((filters.workflowId || filters.runId || filters.nodeId) && (!filters.projectId || !filters.workflowId || !filters.runId)) {
      return {error:"按运行或节点筛选时，项目、流程和运行标识必须同时填写；未提交不完整筛选。"};
    }
    if (filters.workItemId !== null && (!filters.projectId || !/^[A-Za-z0-9_-]{1,128}$/.test(filters.workItemId))) {
      return {error:'按业务任务查找时必须填写项目，业务任务标识限128位字母、数字、下划线或连字符。'};
    }
    return {filters};
  }
  function exactTaskFilters(value, filters) {
    const keys = ['projectId','runId','workflowId'];
    if (filters.workItemId !== null) keys.push('workItemId');
    if (filters.nodeId !== null) keys.push('nodeId');
    return value && typeof value === "object" && !Array.isArray(value)
      && Object.keys(value).sort().join(',') === keys.sort().join(',')
      && keys.every(key => value[key] === filters[key]);
  }
  function invalidateTaskDirectoryForEdit() {
    listGeneration += 1; listLoading = false; taskNext = null; taskCursors = [null]; taskPage = 0;
    taskFiltersDirty = true; byId("task-list").replaceChildren();
    byId("task-list-message").textContent = "范围条件已修改，尚未应用；请应用范围或手动刷新后读取。";
    updatePagination();
  }
  async function loadTasks(page = 0) {
    if (page > 0 && taskFiltersDirty) return;
    const selection = readTaskFilters();
    if (selection.error) {
      listGeneration += 1; listLoading = false; taskNext = null; taskCursors = [null]; taskPage = 0;
      taskFiltersDirty = true; byId("task-list").replaceChildren();
      byId("task-list-message").textContent = selection.error; updatePagination(); return;
    }
    const filters = selection.filters;
    taskFiltersDirty = false;
    const limit = Number(byId("task-page-size").value);
    if (!Number.isInteger(limit) || ![10,25,50].includes(limit) || taskCursors[page] === undefined) return;
    const generation = ++listGeneration;
    listLoading = true;
    taskNext = null;
    updatePagination();
    byId("task-list").replaceChildren();
    byId("task-list-message").textContent = "正在读取授权任务…";
    const query = new URLSearchParams({ limit: String(limit) });
    if (byId("task-status").value) query.set("status", byId("task-status").value);
    if (taskCursors[page]) query.set("after", taskCursors[page]);
    for (const [key,value] of Object.entries(filters)) if (value !== null) query.set(key,value);
    try {
      const data = await request(`/node-tasks?${query}`);
      if (generation !== listGeneration) return;
      if (!Array.isArray(data.tasks)) throw new Error("任务列表响应无法识别。");
      if (data.tasks.length > limit) throw new Error("任务目录返回记录数超过请求页长。");
      const hasScope = Object.values(filters).some(value => value !== null);
      if (hasScope && !Object.hasOwn(data,"filters")) throw new Error("当前后台不支持按范围筛选任务目录，请升级后台后再查询。");
      if (Object.hasOwn(data,"filters") && !exactTaskFilters(data.filters,filters)) throw new Error("任务目录筛选回显与本次选择不一致，拒绝展示未筛选结果。");
      if (!Object.hasOwn(data,"nextCursor")) throw new Error("任务目录缺少分页回执，请刷新核对。");
      const nextCursor = data.nextCursor;
      if (!(nextCursor === null || (typeof nextCursor === "string" && nextCursor.length > 0 && nextCursor.length <= 128))) throw new Error("任务目录分页游标无法核验。");
      const pageIds = new Set();
      for (const task of data.tasks) {
        if (!task || typeof task.taskId !== "string" || !task.taskId || pageIds.has(task.taskId)) throw new Error("任务目录记录标识异常或重复。");
        pageIds.add(task.taskId);
        if (filters.projectId !== null && task.projectId !== filters.projectId) throw new Error("任务目录记录不属于所选项目，拒绝展示。");
        if (filters.workflowId !== null && (task.workflow?.id !== filters.workflowId || task.workflow?.runId !== filters.runId)) throw new Error("任务目录记录不属于所选流程和运行，拒绝展示。");
        if (filters.nodeId !== null && task.workflow?.nodeId !== filters.nodeId) throw new Error('任务目录记录不属于所选节点，拒绝展示。');
        if (filters.workItemId !== null && (task.workItem?.workItemId !== filters.workItemId
            || task.workItem?.projectId !== filters.projectId)) throw new Error('任务目录记录不属于所选业务任务，拒绝展示。');
      }
      if (nextCursor) {
        if (!data.tasks.length || nextCursor !== data.tasks[data.tasks.length-1].taskId) throw new Error("任务目录游标与当前页末项不一致。");
        if (nextCursor === taskCursors[page]) throw new Error("任务目录分页游标未前进，请刷新核对。");
      }
      const rows = [];
      for (const task of data.tasks) {
        const item = document.createElement("li");
        const button = document.createElement("button");
        button.className = "task-row";
        const workflow = task.workflow && typeof task.workflow === "object" ? task.workflow : {};
        const identity = value => typeof value === "string" && value ? value : "尚无记录";
        const title = document.createElement("strong");
        title.textContent = typeof task.roleBindingId === "string" && task.roleBindingId ? safeText(task.roleBindingId) : "节点任务";
        const context = document.createElement("span");
        context.className = "task-context";
        context.textContent = safeText(`项目：${identity(task.projectId)} · 节点：${identity(workflow.nodeId)}`);
        const nodeIdentity = document.createElement("span"); nodeIdentity.className = "task-context";
        nodeIdentity.textContent = safeText(`任务：${task.taskId} · 流程：${identity(workflow.id)} · 运行：${identity(workflow.runId)} · 节点激活：${identity(workflow.nodeExecutionId)} · 尝试：${identity(workflow.attemptId)}`);
        const states = document.createElement("span");
        states.className = "task-states";
        const statusRows = [["执行", statusLabel(task.executionStatus)], ["验证", verificationLabel(task.verificationStatus)], ["记忆", memoryLabel(task.memoryStatus)]];
        if (Object.hasOwn(task, "executionObservation")) statusRows.push(["最近核对", observationLabel(task.executionObservation)]);
        for (const [name, value] of statusRows) {
          const state = document.createElement("span");
          state.textContent = `${name}：${value}`;
          states.append(state);
        }
        button.append(title, context, states);
        const revision = window.AgentLineActivity.workflowRevision(workflow);
        if (revision !== null) {
          const line = document.createElement("span");
          line.className = "task-context";
          line.textContent = safeText(`固定流程版本：${revision || "尚无记录"}`);
          button.append(line);
        }
        const agent = window.AgentLineActivity.responsibleAgent(task);
        if (agent !== null) {
          const line = document.createElement("span");
          line.className = "task-context";
          line.textContent = safeText(`负责人 Agent：${agent || "原记录未提供"}`);
          button.append(line);
        }
        for (const [name, value] of window.AgentLineActivity.rows(task.activity)) {
          const line = document.createElement("span");
          line.className = "task-context";
          line.textContent = `${name}：${value}`;
          button.append(line);
        }
        const waiting = window.AgentLineActivity.waitingReason(task);
        if (waiting) {
          const line = document.createElement("span");
          line.className = "task-context";
          line.textContent = `等待原因：${waiting}`;
          button.append(line);
        }
        const blockCategory = window.AgentLineActivity.namedBlock(task);
        if (blockCategory) {
          const line = document.createElement("span");
          line.className = "task-context";
          line.textContent = `阻塞类别：${blockCategory}`;
          button.append(line);
        }
        const attention = window.AgentLineActivity.attention(task);
        if (attention && attention !== waiting) {
          const line = document.createElement("span");
          line.className = "task-context";
          line.textContent = `需要关注：${attention}`;
          button.append(line);
        }
        if (Object.hasOwn(task, "workItem")) {
          for (const [name, value] of businessRows(task.workItem, task.projectId)) {
            const line = document.createElement("span");
            line.className = "task-context";
            line.textContent = safeText(`${name}：${value}`);
            button.append(line);
          }
        }
        button.addEventListener("click", () => {
          byId("task-id").value = task.taskId;
          byId("task-form").requestSubmit();
          byId("task-form").scrollIntoView({ block: "start" });
        });
        const identifiers = document.createElement("details"), identifiersTitle = document.createElement("summary");
        identifiers.className = "task-row-identifiers"; identifiersTitle.textContent = "查看任务标识";
        identifiers.append(identifiersTitle, nodeIdentity);
        item.append(button, identifiers); rows.push(item);
      }
      taskPage = page; taskNext = nextCursor || null;
      if (taskNext) taskCursors[page+1] = taskNext;
      byId("task-list").replaceChildren(...rows);
      byId("task-list-message").textContent = data.tasks.length
        ? `本页 ${data.tasks.length} 条任务；执行成功与验证通过分别记录。`
        : "当前筛选下没有可访问的任务。";
    } catch (error) {
      if (generation !== listGeneration) return;
      byId("task-list").replaceChildren();
      byId("task-list-message").textContent = error.message || "未能读取任务列表，请刷新重试。";
      notice(error.message, "error");
    } finally {
      if (generation === listGeneration) { listLoading = false; updatePagination(); }
    }
  }
  function resetTasks() { taskFiltersDirty = false; taskCursors = [null]; taskPage = 0; taskNext = null; return loadTasks(); }
  function viewRunTasks(projectId, workflowId, runId, nodeId = null) {
    if (!token || ![projectId,workflowId,runId].every(value => typeof value === 'string'
        && value.trim() === value && value.length > 0 && value.length <= 256)) return;
    if (nodeId !== null && (typeof nodeId !== 'string' || !nodeId || nodeId.trim() !== nodeId || nodeId.length > 256)) return;
    byId('task-node-id').value = nodeId || '';
    byId('task-project-id').value = projectId;
    byId('task-workflow-id').value = workflowId;
    byId('task-run-id').value = runId;
    byId('task-work-item-id').value = '';
    byId('task-status').value = '';
    byId('task-scope-options').open = true;
    byId('task-filter-apply').click();
    byId('tasks').scrollIntoView({block:'start'});
  }
  function viewBusinessTasks({projectId, workItemId}) {
    if (!token || typeof projectId !== 'string' || !projectId || projectId.trim() !== projectId
        || projectId.length > 256 || typeof workItemId !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(workItemId)) return;
    byId('task-project-id').value = projectId;
    byId('task-work-item-id').value = workItemId;
    byId('task-node-id').value = '';
    byId('task-workflow-id').value = ''; byId('task-run-id').value = '';
    byId('task-status').value = ''; byId('task-scope-options').open = true;
    byId('task-filter-apply').click();
    byId('tasks').scrollIntoView({block:'start'});
  }
  byId("refresh-tasks").addEventListener("click", resetTasks);
  byId("task-filter-apply").addEventListener("click", resetTasks);
  byId("task-filter-clear").addEventListener("click", () => {
    for (const id of ['task-project-id','task-workflow-id','task-run-id','task-work-item-id','task-node-id']) byId(id).value = '';
    resetTasks();
  });
  for (const id of ['task-project-id','task-workflow-id','task-run-id','task-work-item-id','task-node-id']) byId(id).addEventListener("input", invalidateTaskDirectoryForEdit);
  byId("task-status").addEventListener("change", resetTasks);
  byId("task-page-size").addEventListener("change", resetTasks);
  byId("task-next").addEventListener("click", () => {
    if (listLoading || taskFiltersDirty || !taskNext) return;
    taskCursors[taskPage + 1] = taskNext; loadTasks(taskPage + 1);
  });
  byId("task-prev").addEventListener("click", () => {
    if (!listLoading && !taskFiltersDirty && taskPage > 0) loadTasks(taskPage - 1);
  });
  function outputValidationMessage(value) {
    const note = window.AgentLineActivity && window.AgentLineActivity.outputShapeNote;
    return typeof note === "function" ? note(value) : "输出结构核对结果未知，请查询原任务。";
  }
  function showArtifactEvidence(data) {
    const section = document.createElement("details"), heading = document.createElement("summary");
    section.className = "task-trace";
    heading.textContent = "产物与独立验收记录";
    section.append(heading);
    const note = (text) => { const p = document.createElement("p"); p.textContent = text; section.append(p); };
    const sameIdentity = (value) => value && typeof value === "object" && !Array.isArray(value)
      && data.executionIdentity && typeof data.executionIdentity === "object" && !Array.isArray(data.executionIdentity)
      && Object.keys(data.executionIdentity).length > 0
      && Object.keys(value).length === Object.keys(data.executionIdentity).length
      && Object.keys(data.executionIdentity).every((key) => typeof data.executionIdentity[key] === "string"
        && data.executionIdentity[key].length > 0
        && value[key] === data.executionIdentity[key]);
    const records = Array.isArray(data.evidence) ? data.evidence.filter((item) => item?.kind === "verification") : [];
    const receipt = records.length === 1 ? records[0] : null;
    if (!receipt || receipt.taskId !== data.taskId || !sameIdentity(receipt.executionIdentity)
        || !["passed", "failed"].includes(receipt.status) || receipt.status !== data.verificationStatus
        || !Array.isArray(receipt.checks)
        || !receipt.checks.length) {
      note(records.length ? "验收记录的数量、任务或执行身份未能准确对应，请核对原始证据。"
        : "尚无正式验收结果记录。模型输出中的成功说明不能代替验收证据。");
      byId("task-result").append(section); return;
    }
    note("以下为本次执行保存的历史验收记录。点击下载时才重新读取文件并核对摘要；文件已变化则拒绝下载，不运行测试。单个文件核对成功不代表整项任务验收通过。");
    const reasons = {
      exact_artifact_policy_matched: "符合声明的字节与大小策略", sha256_mismatch: "内容摘要不符",
      size_mismatch: "文件大小不符", artifact_not_regular_file: "目标不是普通文件",
      artifact_exceeds_limit: "文件超过读取上限", artifact_unavailable_or_symlink: "文件不可读或路径含符号链接",
      artifact_path_replaced_during_read: "读取期间路径被替换", artifact_changed_during_read: "读取期间文件变化",
      command_exit_zero: "命令正常退出", command_exit_nonzero: "命令以非零状态退出",
      output_limit_exceeded: "输出超过上限", oom_killed: "执行因内存不足终止",
    };
    const digest = (value) => typeof value === "string" && /^[a-f0-9]{64}$/.test(value) ? value : "尚无有效记录";
    const size = (value) => Number.isSafeInteger(value) && value >= 0 ? `${value} 字节` : "尚无有效记录";
    let unsupported = 0;
    for (const [index, check] of receipt.checks.slice(0, 20).entries()) {
      const rows = [["验收条目", index + 1], ["记录结果", verificationLabel(check?.status)],
        ["结果说明", reasons[check?.reason] || "未识别的结果原因，请核对原始证据"]];
      if (check?.kind === "artifact_sha256" && typeof check.path === "string") {
        rows.push(["产物路径", check.path], ["期望摘要", digest(check.expectedSha256)],
          ["实际摘要", digest(check.actualSha256)], ["实际大小", size(check.actualSize)],
          ["期望大小", check.expectedSize === null ? "未限制精确大小" : size(check.expectedSize)],
          ["读取上限", size(check.maxBytes)]);
      } else if (check?.kind === "test_command" && typeof check.testId === "string") {
        const exit = check.resourceExitReceipt;
        const confirmed = exit?.source === "docker-inspect-before-remove" && exit.taskId === data.taskId
          && sameIdentity(exit.executionIdentity) && /^[a-f0-9]{64}$/.test(check.containerId || "")
          && exit.containerId === check.containerId && /^[a-f0-9]{64}$/.test(receipt.policySha256 || "")
          && exit.policySha256 === receipt.policySha256 && typeof check.environmentId === "string"
          && check.environmentId.length > 0 && exit.environmentId === check.environmentId
          && exit.testId === check.testId && exit.exitCode === check.exitCode
          && Number.isSafeInteger(check.exitCode) && check.exitCode >= 0 && check.exitCode <= 255
          && exit.running === false && exit.pid === 0 && exit.status === "exited";
        rows.push(["独立测试", check.testId], ["退出码", Number.isSafeInteger(check.exitCode)
          && check.exitCode >= 0 && check.exitCode <= 255 ? check.exitCode : "尚无有效记录"],
          ["输出截断", check.outputTruncated === true ? "是" : check.outputTruncated === false ? "否" : "未知"],
          ["执行资源", confirmed ? "记录已确认退出" : "退出记录未能准确对应"]);
      } else { unsupported += 1; continue; }
      const list = document.createElement("dl");
      for (const [name, value] of rows) {
        const key = document.createElement("dt"), item = document.createElement("dd");
        key.textContent = name; item.textContent = String(value); list.append(key, item);
      }
      section.append(list);
      if (data.artifactAccess?.configured === true && data.artifactAccess.maxBytes === 1048576
          && data.executionStatus === "succeeded" && data.resourcesReleased === true
          && ["passed", "failed"].includes(data.verificationStatus)
          && check.kind === "artifact_sha256" && check.status === "passed"
          && /^[a-f0-9]{64}$/.test(check.actualSha256 || "")
          && check.actualSha256 === check.expectedSha256
          && Number.isSafeInteger(check.actualSize) && check.actualSize >= 0 && check.actualSize <= 1048576) {
        const download = document.createElement("button"), state = document.createElement("p");
        download.type = "button"; download.className = "secondary"; download.textContent = "核对并下载产物";
        state.className = "field-help"; state.setAttribute("role", "status");
        section.append(download, state);
        const capturedEpoch = epoch, generation = taskGeneration;
        const current = () => section.isConnected && capturedEpoch === epoch
          && generation === taskGeneration && selectedTaskData === data && Boolean(token);
        let loading = false;
        download.addEventListener("click", async () => {
          if (!current() || loading) return;
          loading = true; download.disabled = true; download.setAttribute("aria-busy", "true");
          state.textContent = "正在核对当前文件与原验收记录…";
          try {
            const result = await request(`/node-tasks/${encodeURIComponent(data.taskId)}/artifacts/${index}`);
            if (!current()) return;
            if (result.source !== "verified_current_artifact" || result.readOnly !== true
                || result.taskId !== data.taskId || !sameIdentity(result.executionIdentity)
                || result.checkIndex !== index || result.path !== check.path
                || result.sha256 !== check.actualSha256 || result.size !== check.actualSize
                || result.encoding !== "base64" || typeof result.contentBase64 !== "string"
                || result.contentBase64.length !== 4 * Math.ceil(check.actualSize / 3)
                || !/^[A-Za-z0-9+/]*={0,2}$/.test(result.contentBase64)) {
              throw new Error("下载内容与原验收记录无法对应，请刷新任务详情后重试。");
            }
            const binary = atob(result.contentBase64);
            if (binary.length !== check.actualSize || btoa(binary) !== result.contentBase64) {
              throw new Error("下载内容大小或编码不符，请刷新任务详情后重试。");
            }
            if (!current()) return;
            const bytes = Uint8Array.from(binary, character => character.charCodeAt(0));
            const url = URL.createObjectURL(new Blob([bytes], {type: "application/octet-stream"}));
            const link = document.createElement("a");
            try {
              link.href = url;
              link.download = check.path.split("/").pop().replace(/[\u0000-\u001f\u007f<>:"/\\|?*]/g, "_").slice(0, 180) || "artifact.bin";
              document.body.append(link); link.click();
            } finally {
              link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
            }
            state.textContent = "已核对并发起浏览器下载；是否保存请以浏览器为准。";
          } catch (error) {
            if (current()) state.textContent = error.message || "产物无法读取，请刷新任务详情后重试。";
          } finally {
            loading = false;
            if (current()) { download.disabled = false; download.removeAttribute("aria-busy"); }
          }
        });
      }
    }
    if (unsupported) note(`前20项中有 ${unsupported} 项无法按已知格式展示，请核对原始证据。`);
    if (receipt.checks.length > 20) note(`本区仅展示前20项，共 ${receipt.checks.length} 项；完整内容见原始证据。`);
    byId("task-result").append(section);
  }
  function showTaskEvents(data) {
    const panel = document.createElement("details"), heading = document.createElement("summary");
    panel.className = "task-trace"; heading.textContent = "任务事件时间线";
    const hint = document.createElement("p"), state = document.createElement("p");
    hint.textContent = "仅显示本地持久事件，不代表原生实时状态。手动读取，每次50条；本页最多保留200条，服务端历史不受影响。";
    state.setAttribute("role", "status"); state.textContent = "尚未读取。任务状态以当前详情和独立验收记录为准。";
    const list = document.createElement("ol"), read = document.createElement("button");
    list.className = "task-event-list"; list.setAttribute("aria-label", "本任务已读取事件");
    read.type = "button"; read.className = "secondary"; read.textContent = "读取任务事件";
    panel.append(heading, hint, state, list, read); byId("task-result").append(panel);
    const taskId = data.taskId, capturedEpoch = epoch, generation = taskGeneration;
    let cursor = 0, loading = false, trimmed = false;
    const current = () => panel.isConnected && capturedEpoch === epoch && generation === taskGeneration
      && selectedTask === taskId && Boolean(token);
    read.addEventListener("click", async () => {
      if (!current() || loading) return;
      loading = true; read.disabled = true; read.setAttribute("aria-busy", "true");
      state.textContent = "正在读取本任务事件摘要…";
      try {
        const page = await request(`/node-tasks/${encodeURIComponent(taskId)}/events?after=${cursor}&limit=50`);
        if (!current()) return;
        if (!Array.isArray(page.events) || page.events.length > 50 || !Number.isSafeInteger(page.nextCursor)) {
          throw new Error("事件摘要格式无法核对，游标未改变。");
        }
        const shown = new Set(Array.from(list.children, item => Number(item.dataset.seq)));
        let next = cursor;
        for (const event of page.events) {
          if (!event || event.task_id !== taskId || !Number.isSafeInteger(event.seq)
              || typeof event.kind !== "string" || !event.kind || event.kind.length > 160
              || typeof event.created_at !== "number" || !Number.isFinite(event.created_at)
              || event.created_at < 0 || event.created_at > 8_640_000_000_000) {
            throw new Error("事件任务、序号或时间无法准确对应，游标未改变。");
          }
          if (shown.has(event.seq)) {
            if (event.seq > next) next = event.seq;
            continue;
          }
          if (event.seq <= next) throw new Error("事件任务、序号或时间无法准确对应，游标未改变。");
          next = event.seq;
        }
        if (page.nextCursor !== next) throw new Error("事件游标与本页记录不一致，游标未改变。");
        for (const event of page.events) {
          if (shown.has(event.seq)) continue;
          const item = document.createElement("li"), title = document.createElement("strong"), time = document.createElement("span");
          item.dataset.seq = String(event.seq);
          title.textContent = window.AgentLineActivity.eventLabel(event.kind);
          const category = window.AgentLineActivity.categoryLabel(event.category);
          const source = typeof event.source_identity === "string" && event.source_identity ? ` · 来源 ${event.source_identity}` : "";
          const run = typeof event.run_id === "string" && event.run_id ? ` · 运行 ${event.run_id}` : "";
          time.textContent = `事件 ${event.seq} · ${new Date(event.created_at * 1000).toLocaleString()}${category ? ` · 类别 ${category}` : ""}${run}${source}`;
          item.append(title, time); list.append(item); shown.add(event.seq);
        }
        while (list.children.length > 200) { list.firstElementChild.remove(); trimmed = true; }
        cursor = next; read.textContent = "读取后续事件";
        state.textContent = (page.events.length ? `本次读取 ${page.events.length} 条，当前保留 ${list.children.length} 条。`
          : "当前游标后尚无新事件，可稍后手动读取。") + (trimmed ? "较早事件已从本页收起，重新查询本任务可从头读取。" : "");
      } catch (error) {
        if (current()) state.textContent = `${error.message || "事件读取未完成。"} 已读取记录保留，可手动重查。`;
      } finally {
        loading = false;
        if (current()) { read.disabled = false; read.removeAttribute("aria-busy"); }
      }
    });
  }
  function taskAgentId(task) {
    const value = task.roleAgentId;
    return typeof value === 'string' && value.trim() === value && value.length > 0
      && value.length <= 256 && !value.includes('\0') ? value : '原记录未提供';
  }
  function showBatchItem(data) {
    if (!Object.hasOwn(data, 'batchItem')) return;
    const item = data.batchItem;
    const valid = item && typeof item === 'object' && !Array.isArray(item)
      && Object.keys(item).sort().join(',') === 'index,input,inputSha256,itemId,recipeId'
      && typeof item.recipeId === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(item.recipeId)
      && typeof item.itemId === 'string' && /^item_[0-9a-f]{24}$/.test(item.itemId)
      && Number.isInteger(item.index) && item.index >= 1 && item.index <= 20
      && typeof item.inputSha256 === 'string' && /^[0-9a-f]{64}$/.test(item.inputSha256);
    const panel = document.createElement('details');
    panel.className = 'task-trace';
    const heading = document.createElement('summary');
    heading.textContent = valid ? '批量项 ' + item.index + ' · 查看本项数据' : '批量项信息待核对';
    panel.append(heading);
    if (valid) {
      const list = document.createElement('dl');
      for (const [name, value] of [['批量配方', item.recipeId], ['项标识', item.itemId],
          ['输入指纹（SHA-256）', item.inputSha256], ['本项数据', item.input]]) {
        const key = document.createElement('dt'), content = document.createElement('dd');
        key.textContent = name; content.textContent = safeText(value); list.append(key, content);
      }
      panel.append(list);
    }
    byId('task-result').append(panel);
  }
  function nativeIdentityNote(data, key) {
    const availability = data.executionIdentityAvailability;
    if (!availability || typeof availability !== "object" || Array.isArray(availability)
        || availability.readOnly !== true || availability.mutatesStoredIdentity !== false
        || !availability.fields || typeof availability.fields !== "object" || Array.isArray(availability.fields)) return null;
    const field = availability.fields[key];
    if (!field || typeof field !== "object" || Array.isArray(field)) return null;
    if (!["not_written", "unavailable", "recorded"].includes(field.state)) return "尚无记录";
    const reason = field.reason;
    if (typeof reason !== "string" || reason.trim() !== reason || reason.length === 0 || reason.length > 400
        || reason.includes("agent-line-") || reason.includes("\0")) return "尚无记录";
    return reason;
  }
  function showTask(data) {
    selectedTask = data.taskId; selectedTaskData = data;
    if (data.verificationCancellationRequested === true || data.verificationStatus === "canceled" || data.executionStatus === "cancel_requested" || data.executionStatus === "canceled") pendingTaskCancels.delete(data.taskId);
    const executionEnded = ["succeeded", "failed", "canceled", "cancelled", "completed"].includes(data.executionStatus);
    const resourceStatus = data.submissionBlock && data.executionStatus === 'registered'
      ? '尚未启动，无执行资源' : data.resourcesReleased === true
      ? data.verificationStatus === "canceled" ? "编码执行资源已退出，验收已取消"
        : data.verificationCancellationRequested === true ? "编码执行资源已退出，验收停止待确认"
        : data.canCancelVerification === true ? "编码执行资源已退出，验收仍在进行" : "执行资源已退出"
      : data.resourcesReleased === false ? "执行资源退出待确认" : "执行资源退出未确认";
    const workflow = data.workflow || {}, identity = data.executionIdentity || {};
    const contextRows = [["任务标识", data.taskId], ["角色绑定", data.roleBindingId], ["负责人 Agent", taskAgentId(data)], ["流程标识", workflow.id], ["发布版本", workflow.revision], ["运行标识", workflow.runId],
      ["节点标识", workflow.nodeId], ["节点激活", workflow.nodeExecutionId], ["本次尝试", workflow.attemptId],
      ["原生执行任务", identity.upstreamTaskId], ["Codex 会话", identity.codexThreadId], ["Codex 回合", identity.codexTurnId]];
    const sessionNote = nativeIdentityNote(data, "roleSessionId");
    const executorNote = nativeIdentityNote(data, "executorAgentId");
    if (sessionNote && executorNote) contextRows.push(["角色会话说明", sessionNote], ["执行者说明", executorNote]);
    if (data.roleRevision !== undefined) contextRows.push(["角色版本", data.roleRevision]);
    if (data.workItem?.projectId === data.projectId && typeof data.workItem?.workItemId === 'string') {
      contextRows.push(['业务任务标识', data.workItem.workItemId], ['业务目标摘要', data.workItem.definitionSha256]);
    }
    const waiting = window.AgentLineActivity.waitingReason(data);
    const attention = window.AgentLineActivity.attention(data);
    const limitsMessage = executionLimitsMessage(data);
    const budgetMessage = runBudgetMessage(data);
    const providerUsage = usageMessage(data);
    const capacity = data.projectConcurrency;
    let capacityMessage = '';
    if (Object.hasOwn(data, 'projectConcurrency')) {
      capacityMessage = '当前项目并发容量待核对';
      if (capacity?.state === 'known' && capacity.policy === 'minimum_unfinished_task_limit'
          && Number.isInteger(capacity.maxConcurrentTasks) && capacity.maxConcurrentTasks >= 1 && capacity.maxConcurrentTasks <= 100
          && Number.isSafeInteger(capacity.occupied) && capacity.occupied >= 0
          && capacity.available === Math.max(0, capacity.maxConcurrentTasks - capacity.occupied)
          && capacity.admissionBlocked === (capacity.occupied >= capacity.maxConcurrentTasks)) {
        capacityMessage = '上限 ' + capacity.maxConcurrentTasks + ' · 占用 ' + capacity.occupied + ' · 剩余 ' + capacity.available
          + '（当前项目记录快照，原角色与工作区限制继续生效）';
      }
    }
    const settingsRows = [...(capacityMessage ? [['项目并发', capacityMessage]] : []), ...(providerUsage ? [["已报告用量", providerUsage]] : []),
      ...(limitsMessage ? [["执行限制", limitsMessage]] : []), ...(budgetMessage ? [["运行额度", budgetMessage]] : [])];
    details("task-result", [["所属项目", data.projectId], ["负责人 Agent", taskAgentId(data)], ["执行状态", statusLabel(data.executionStatus)], ["执行进度", executionEnded ? "执行已结束" : "执行尚未确认结束"], ["资源退出", resourceStatus], ["验证状态", verificationLabel(data.verificationStatus)], ["记忆回写", memoryLabel(data.memoryStatus)],
      ...window.AgentLineActivity.rows(data.activity),
      ...(waiting ? [["等待原因", waiting]] : []),
      ...(window.AgentLineActivity.namedBlock(data) ? [["阻塞类别", window.AgentLineActivity.namedBlock(data)]] : []),
      ...businessRows(data.workItem, data.projectId),
      ...(attention && attention !== waiting ? [["需要关注", attention]] : []),
      ...(Object.hasOwn(data, "outputValidation") ? [["结果结构", outputValidationMessage(data.outputValidation)]] : []),
      ...(window.AgentLineActivity?.showsBusinessFields?.(data.outputValidation)
        && data.structuredOutput && typeof data.structuredOutput === "object" && !Array.isArray(data.structuredOutput)
        ? [["业务结果（Agent 提供）", data.structuredOutput]] : []),
      ...(Object.hasOwn(data, "executionObservation") ? [["最近核对", observationLabel(data.executionObservation)]] : [])], data, contextRows, settingsRows);
    showBatchItem(data);
    showArtifactEvidence(data);
    showTaskEvents(data);
    const memoryEpoch = epoch, memoryGeneration = taskGeneration;
    window.AgentLineMemoryTrace?.render({container:byId('task-result'),task:data,request,safeText,
      isCurrent:() => Boolean(token) && memoryEpoch === epoch && memoryGeneration === taskGeneration && selectedTaskData === data});
    window.AgentLineTaskKanban?.render({container:byId('task-result'),task:data,request,safeText,
      isCurrent:() => Boolean(token) && memoryEpoch === epoch && memoryGeneration === taskGeneration && selectedTaskData === data});
    if ([data.projectId, workflow.id, workflow.runId].every(value => typeof value === 'string'
        && value.trim() === value && value.length > 0 && value.length <= 256)) {
      const view = document.createElement('button');
      view.type = 'button'; view.className = 'secondary run-task-directory-link';
      view.dataset.auth = ''; view.textContent = '查看同次运行的其他节点';
      const capturedEpoch = epoch, capturedGeneration = taskGeneration;
      view.addEventListener('click', () => {
        if (capturedEpoch !== epoch || capturedGeneration !== taskGeneration || selectedTaskData !== data) return;
        viewRunTasks(data.projectId, workflow.id, workflow.runId);
      });
      byId('task-result').append(view);
      if (typeof workflow.nodeId === 'string' && workflow.nodeId.trim() === workflow.nodeId
          && workflow.nodeId.length > 0 && workflow.nodeId.length <= 256) {
        const attempts = document.createElement('button');
        attempts.type = 'button'; attempts.className = 'secondary run-task-directory-link';
        attempts.dataset.auth = ''; attempts.textContent = '查看此节点的全部激活与尝试';
        attempts.addEventListener('click', () => {
          if (capturedEpoch !== epoch || capturedGeneration !== taskGeneration || selectedTaskData !== data) return;
          viewRunTasks(data.projectId, workflow.id, workflow.runId, workflow.nodeId);
        });
        byId('task-result').append(attempts);
      }
    }
    if (Object.hasOwn(data, "executionObservation") && observationLabel(data.executionObservation) !== "已取得原执行记录") {
      const note = document.createElement("p");
      note.className = "task-observation-note";
      note.textContent = "最近核对未取得明确结果，已记录的执行事实保持不变。请继续查询原任务，不要重新提交。";
      byId("task-result").append(note);
    }
    const explanation = document.createElement("p");
    explanation.textContent = pendingTaskCancels.has(data.taskId)
      ? "停止请求结果未知，请查询原任务核对；不会自动重新提交。"
      : data.verificationCancellationRequested === true && data.verificationStatus !== "canceled"
      ? "已申请停止验收，等待测试退出确认；已完成的编码与产物保留。"
      : data.verificationStatus === "canceled"
      ? "验收已取消，编码结果和产物保留；未完成的测试不会视为通过。"
      : data.canCancelVerification === true
      ? "编码已完成，测试验收仍在进行。停止验收不会删除产物或撤销已完成编码。"
      : data.submissionBlock
      ? "后台只会核对本任务已固定的角色版本，不会自动改用新的角色配置。"
      : "取消请求不等于执行资源已退出。执行结束与资源退出分别确认；资源退出未确认时，请继续查询原任务。";
    byId("task-result").append(explanation);
    const approvalLink = document.createElement("a");
    approvalLink.href = "./approvals.html#execution-task=" + encodeURIComponent(data.taskId);
    approvalLink.textContent = "查看执行权限审批";
    byId("task-result").append(approvalLink);
    if (["succeeded", "failed", "canceled"].includes(data.executionStatus) && data.resourcesReleased === true
        && /^[A-Za-z0-9_-]{1,160}$/.test(data.projectId || "")
        && /^[A-Za-z0-9_-]{1,160}$/.test(data.roleBindingId || "")
        && /^task-[0-9a-f]{32}$/.test(data.taskId || "")) {
      const memoryLink = document.createElement("a");
      const params = new URLSearchParams({"source-project": data.projectId, "source-binding": data.roleBindingId, "source-task": data.taskId});
      memoryLink.href = `./memories.html#${params}`;
      memoryLink.textContent = data.memoryStatus === "committed" ? "查看来源记忆" : "整理经验候选";
      const memoryAction = document.createElement("p");
      memoryAction.append(memoryLink);
      byId("task-result").append(memoryAction);
    }
    const reconcileEpoch = epoch, reconcileGeneration = taskGeneration;
    window.AgentLineTaskReconcile?.render({container:byId('task-result'), task:data, request,
      isCurrent:() => Boolean(token) && epoch === reconcileEpoch && taskGeneration === reconcileGeneration && selectedTaskData === data,
      onTask:showTask, notify:notice});
    window.AgentLineTaskWorkflowDraft?.render({container:byId('task-result'), task:data, request, safeText,
      isCurrent:() => Boolean(token) && epoch === reconcileEpoch && taskGeneration === reconcileGeneration && selectedTaskData === data});
    byId("cancel-task").textContent = data.canCancelVerification === true ? "停止验收" : "申请取消此任务";
    byId("cancel-task").hidden = data.canCancelVerification !== true && ["succeeded", "failed", "canceled", "cancelled", "cancel_requested"].includes(data.executionStatus);
    byId("cancel-task").disabled = pendingTaskCancels.has(data.taskId);
  }
  byId("task-form").addEventListener("submit", (event) => {
    event.preventDefault();
    busy(event.submitter || event.currentTarget.querySelector("button[type=submit]"), async () => {
      const generation = ++taskGeneration, taskId = byId("task-id").value.trim(), currentEpoch = epoch;
      selectedTask = null; selectedTaskData = null; cancelTarget = null; byId("cancel-dialog").close();
      byId("cancel-task").hidden = true;
      empty("task-result", "正在查询", "等待服务返回真实任务状态。");
      try {
        const data = await request(`/node-tasks/${encodeURIComponent(taskId)}`);
        if (generation !== taskGeneration || currentEpoch !== epoch) return;
        if (data.taskId !== taskId) throw new Error("任务响应标识不一致，请查询原任务。");
        showTask(data);
        notice("已读取节点任务状态。", "ok");
      } catch (error) {
        if (generation !== taskGeneration || currentEpoch !== epoch) return;
        empty("task-result", "未能读取任务", "请核对任务标识、权限和连接状态。"); throw error;
      }
    });
  });
  byId("task-id").addEventListener("input", () => {
    taskGeneration += 1; selectedTask = null; selectedTaskData = null; cancelTarget = null;
    byId("cancel-task").hidden = true; byId("cancel-dialog").close();
    empty("task-result", "任务标识已更改", "请查询当前任务，旧结果已清除。");
  });
  byId("cancel-task").addEventListener("click", () => {
    if (!selectedTask || pendingTaskCancels.has(selectedTask)) return;
    const verification = selectedTaskData?.canCancelVerification === true;
    cancelTarget = { taskId: selectedTask, generation: taskGeneration, epoch, verification };
    byId("cancel-title").textContent = verification ? "停止当前任务验收？" : "取消当前节点任务？";
    byId("cancel-description").textContent = verification
      ? "停止当前测试并撤销后续测试。不会删除产物，也不会撤销已完成的编码；请求接收后仍需查询确认测试退出。"
      : "将向服务提交取消请求。请求被接收后，仍需查询确认执行是否已经停止。";
    byId("confirm-cancel").textContent = verification ? "确认停止验收" : "确认申请取消";
    byId("cancel-dialog").showModal();
  });
  byId("keep-task").addEventListener("click", () => { cancelTarget = null; byId("cancel-dialog").close(); });
  byId("confirm-cancel").addEventListener("click", (event) => busy(event.currentTarget, async () => {
    const target = cancelTarget;
    if (!target || target.taskId !== selectedTask || target.generation !== taskGeneration || target.epoch !== epoch) return;
    if (pendingTaskCancels.has(target.taskId)) return;
    pendingTaskCancels.add(target.taskId); byId("cancel-task").disabled = true; byId("cancel-dialog").close(); cancelTarget = null;
    try {
      const data = await request(`/node-tasks/${encodeURIComponent(target.taskId)}/cancel`, { body: {} });
      if (target.epoch !== epoch || target.generation !== taskGeneration || selectedTask !== target.taskId) return;
      if (data.taskId !== target.taskId) throw new Error("停止请求返回了不同任务，请查询原任务核对。");
      showTask(data);
      if (target.verification) {
        notice(data.verificationStatus === "canceled" ? "验收已取消，编码结果和产物保留。" : "已提交停止验收请求，等待测试退出确认，请继续查询原任务。", data.verificationStatus === "canceled" ? "ok" : "warn");
      } else {
        notice(data.resourcesReleased === true ? "服务已确认执行资源退出，请查看任务执行与验证结果。" : "已提交取消请求，执行资源退出尚未确认，请继续查询原任务。", data.resourcesReleased === true ? "ok" : "warn");
      }
    } catch (error) {
      if (target.epoch !== epoch || target.generation !== taskGeneration || selectedTask !== target.taskId) return;
      notice("停止请求未取得明确回执，请查询原任务核对；不会自动重新提交。", "warn");
    }
  }));
  byId("token").value = "";
  authControls(false);
    updateRunDirectoryPagination();
  newKey();
  // A nonsecret editor deep link only prefills fields; it never authenticates or starts a run.
  const linkedObservation = linkedRunObservation();
  const linkedBinding = linkedObservation?.bindingId || new URLSearchParams(window.location.search).get("binding");
  if (linkedBinding && /^[A-Za-z0-9_-]{1,160}$/.test(linkedBinding)) {
    byId("binding").value = linkedBinding;
    byId("query-binding").value = linkedBinding;
    if (linkedObservation) {
      byId("run-id").value = linkedObservation.runId;
      notice("已填入原运行标识。关闭页面不会取消后台任务。授权后只按该运行标识重新查询，不会启动新运行。", "warn");
    } else notice("已填入发布版本绑定。请输入独立运行凭证；仅活动版本允许新运行，历史版本可查询。", "warn");
  }
  function prefillTaskForQuery(taskId) {
    if (typeof taskId !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(taskId)) return;
    taskGeneration += 1;
    selectedTask = null; selectedTaskData = null; cancelTarget = null;
    byId('cancel-task').hidden = true; byId('cancel-dialog').close();
    byId('task-id').value = taskId;
    empty('task-result', '已填入所选任务', '授权后点击“查询任务”，读取该任务的详细记录。');
    notice(token ? '已填入所选任务。点击“查询任务”读取详情。'
      : '已填入所选任务。详细记录需要运行凭证，授权后点击“查询任务”。', 'warn');
  }
  function prefillLinkedTask() {
    const ids = new URLSearchParams(window.location.hash.slice(1)).getAll('task');
    if (ids.length === 1) prefillTaskForQuery(ids[0]);
  }
  window.addEventListener('hashchange', prefillLinkedTask);
  prefillLinkedTask();
})();
