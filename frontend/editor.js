'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const labels = {start:'开始', condition:'条件分支', human:'人工确认', loop:'有限循环', parallel:'并行分发', join:'结果汇合', agent:'Agent任务', end:'结束', subflow_input:'子流程入口', subflow_output:'子流程返回'};
  const outcomes = {subworkflow_not_admitted:'子流程来源、角色策略或父运行预算未符合准入要求。未保存或发布，请核对配置后再操作', execution_required:'任务执行与结果回传尚未就绪，已阻止发布。草稿已保留，请核对执行服务配置后再发布', memory_commit_required:'此节点需要提交记忆，但尚未配置同一角色的记忆审核与回写，已阻止发布。草稿已保留，请核对配置', verification_required:'自动验收策略尚未配置或不受支持，发布被阻止。请核对服务端验收配置', archived:'流程已归档，归档前审计已保留', created:'已创建 Sim 草稿', saved:'草稿已保存并读回核验', published:'已发布并核验版本', conflict:'草稿已被其他操作修改，请重新读取后核对', readback_mismatch:'保存后读回不一致，请重新读取核对', approval_binding_required:'人工确认节点尚未绑定审批，发布被阻止', publication_mismatch:'发布版本核验不一致，请核对 Sim 状态', not_submitted:'请求未准入，未提交修改', role_binding_required:'角色绑定不可用，发布被阻止。请刷新角色绑定并核对节点配置', native_binding_required:'原版等待节点尚未全部绑定，发布被阻止。原图未覆盖', native_task_input_required:'该等待节点没有唯一 taskInput，不能交给既有角色。请改为人工确认'};
  let token = '', epoch = 0, sequence = 0, project = '', workflow = '', documentValue = null, digest = '', selected = '';
  let portableTemplates = null, batchRoleSignature = '', versionHistory = null;
  let subworkflows = null;
  let dirty = false, formDirty = false, busy = false, editable = false, workflowServerEditable = false, canPublish = false, page = 0, cursors = [''], nextCursor = null;
  // Only operation identities survive a disconnect, preventing accidental new mutation intents.
  let nativeBinding = null, nativeWaitView = null;
  let roles = [], rolesReady = false, outputSchemaIds = [], nodeLimitsSupported = false, humanApprovalTtlSupported = false, humanApprovalQuestionSupported = false, maxStaticParallelGroups = 1, subworkflowAuthoring = null;
  const staticParallelGroupLimit = value => Number.isInteger(value) && value >= 1 && value <= 10 ? value : 1;
  const subworkflowCapability = () => subworkflowAuthoring?.editorSupported===true && subworkflowAuthoring.maxDepth===1 && subworkflowAuthoring.requiresRunBudget===true;
  const loadedDocumentEditable = doc => workflowServerEditable && supportedDocument(doc) && (!subworkflowGroups(doc).length || subworkflowCapability());
  const subworkflowScopeCurrent = scope => Boolean(token)&&!!scope&&scope.projectId===project&&scope.workflowId===workflow&&scope.epoch===epoch;
  const canInsertSubworkflow = () => Boolean(token&&workflow&&editable&&rolesReady&&subworkflowCapability()&&!dirty&&!formDirty&&!busy&&!pending()&&!nodeDrag&&!edgeDrag);
  const canRemoveSubworkflow = () => Boolean(token&&workflow&&editable&&rolesReady&&subworkflowCapability()&&!formDirty&&!busy&&!pending()&&!nodeDrag&&!edgeDrag);
  const fieldsOutputSchema = 'fields-result-v1', nestedOutputSchema = 'fields-result-v2';
  let nestedFieldsTouched = false;
  let humanApprovalQuestionIssue = false, humanApprovalQuestionEdited = false;
  let outputFieldsDraft = [], outputFieldsIssue = '', outputFieldsOriginalPresent = false, outputFieldsDiscard = false, outputFieldsSeeded = false, outputFieldsSchemaSelection = '';
  const outputSchemas = {
    'task-result': {label:'现有执行结果', hint:'沿用现有执行结果格式。'},
    'coding-result-v1': {label:'编码结果（JSON）', hint:'正文需完整输出 JSON，包含 summary、status（completed、blocked 或 failed）和相对路径数组 changedFiles。'},
    'review-result-v1': {label:'审查意见（JSON）', hint:'正文需完整输出 JSON，包含 summary、布尔值 approved 和字符串数组 issues。审查意见不是人工审批，也不代表独立测试通过。'},
    'workflow-draft-v1': {label:'自然语言流程草稿（可移植 JSON）', hint:'输出供人工核对后导入的可移植流程草稿。结构核对不代替人工确认；不会自动保存或发布。'},
    'fields-result-v1': {label:'自定义字段（JSON）', hint:'定义字段名、类型和必填项；正文需按此结构输出 JSON。'},
    'fields-result-v2': {label:'嵌套结构（高级 JSON）', hint:'支持对象与数组；逐层校验必填字段，不自动补字段或转换类型。'}
  };
  const outputSchemaAvailable = id => typeof id === 'string'
    && Object.hasOwn(outputSchemas, id) && outputSchemaIds.includes(id)
    && (id !== nestedOutputSchema || !!window.AgentLineStructuredFields);
  function exactKeys(value, keys) {
    return !!value && typeof value === 'object' && !Array.isArray(value)
      && Object.getPrototypeOf(value) === Object.prototype && Object.keys(value).length === keys.length
      && keys.every(key => Object.hasOwn(value,key));
  }
  function validRoleDetails(details) {
    if (!exactKeys(details,['source','displayName','taskRole','access','revision','memory','execution','verification'])
      || details.source !== 'configured_role_summary' || typeof details.displayName !== 'string'
      || !details.displayName.trim() || details.displayName.length > 255 || details.displayName.includes('\0')
      || ![null,'planner','developer','reviewer'].includes(details.taskRole)
      || ![null,'readonly','delegate'].includes(details.access)
      || !(details.revision === null || (typeof details.revision === 'string' && /^qwenpaw-role-v1:[a-f0-9]{64}$/.test(details.revision)))
      || details.memory !== 'qwenpaw_native_role') return false;
    const execution = details.execution, verification = details.verification;
    if (!exactKeys(execution,['source','backend','executorAgentId','sandbox','approvalPolicy','modelId','workspace'])
      || !['native_binding_descriptor_at_startup','unavailable'].includes(execution.source)
      || ![null,'codex'].includes(execution.backend)
      || !(execution.executorAgentId === null || (typeof execution.executorAgentId === 'string' && /^[A-Za-z0-9_-]{1,160}$/.test(execution.executorAgentId)))
      || ![null,'read-only'].includes(execution.sandbox) || ![null,'never'].includes(execution.approvalPolicy)
      || execution.modelId !== null || execution.workspace !== 'project_registered') return false;
    if (execution.source === 'unavailable' && [execution.backend,execution.executorAgentId,execution.sandbox,execution.approvalPolicy].some(value=>value!==null)) return false;
    if (execution.source === 'native_binding_descriptor_at_startup' && execution.backend !== 'codex') return false;
    if ((execution.sandbox === null) !== (execution.approvalPolicy === null)) return false;
    if (!exactKeys(verification,['state','policyId','requireTests','artifactCount','testCount'])
      || !['configured','unavailable','invalid'].includes(verification.state)) return false;
    if (verification.state !== 'configured') return [verification.policyId,verification.requireTests,verification.artifactCount,verification.testCount].every(value=>value===null);
    return typeof verification.policyId === 'string' && /^[A-Za-z0-9_-]{1,160}$/.test(verification.policyId)
      && typeof verification.requireTests === 'boolean'
      && Number.isSafeInteger(verification.artifactCount) && verification.artifactCount >= 0 && verification.artifactCount <= 32
      && Number.isSafeInteger(verification.testCount) && verification.testCount >= 0 && verification.testCount <= 8
      && (verification.requireTests ? verification.testCount >= 1 : verification.testCount === 0 && verification.artifactCount >= 1);
  }
  function validWorkflowDraftRole(role) {
    return validRoleDetails(role?.details) && role.details.taskRole === 'planner'
      && role.details.access === 'readonly'
      && role.details.execution.source === 'native_binding_descriptor_at_startup'
      && role.details.execution.sandbox === 'read-only'
      && role.details.execution.approvalPolicy === 'never';
  }
  const outputFieldTypes = ['string','boolean','number','integer','string_array'];
  function validOutputFields(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value) || Object.getPrototypeOf(value) !== Object.prototype
      || Object.keys(value).length !== 1 || !Object.hasOwn(value, 'fields') || !Array.isArray(value.fields)
      || value.fields.length < 1 || value.fields.length > 32) return false;
    const names = new Set(); let required = false;
    for (const field of value.fields) {
      if (!field || typeof field !== 'object' || Array.isArray(field) || Object.getPrototypeOf(field) !== Object.prototype
        || Object.keys(field).length !== 3 || !['name','type','required'].every(key => Object.hasOwn(field,key))
        || typeof field.name !== 'string' || !/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(field.name)
        || ['constructor','prototype','__proto__'].includes(field.name) || names.has(field.name)
        || !outputFieldTypes.includes(field.type) || typeof field.required !== 'boolean') return false;
      names.add(field.name); required ||= field.required;
    }
    return required;
  }
  function cloneOutputFields(value) { return value.fields.map(field => ({name:field.name,type:field.type,required:field.required})); }
  const validNestedFields = value => window.AgentLineStructuredFields?.validDefinition(value) === true;
  const customOutputSchema = id => [fieldsOutputSchema,nestedOutputSchema].includes(id);
  const validDeclaredFields = (id,value) => id === nestedOutputSchema ? validNestedFields(value) : id === fieldsOutputSchema && validOutputFields(value);
  function validConditionNumber(value) {
    return typeof value === 'number' && Number.isFinite(value)
      && Math.abs(value) <= Number.MAX_SAFE_INTEGER;
  }
  function customConditionFieldError(source, fieldPath, operator, value) {
    if (source?.type !== 'agent' || !customOutputSchema(source.config.outputSchemaId) || typeof fieldPath !== 'string' || !fieldPath.startsWith('result.output.')) return '';
    const match = /^result\.output\.result\.([A-Za-z][A-Za-z0-9_]{0,63})$/.exec(fieldPath);
    const definition = source.config.outputSchemaId === nestedOutputSchema
      ? (validNestedFields(source.config.outputFields) ? window.AgentLineStructuredFields.scalarFields(source.config.outputFields)
        .find(field => fieldPath === 'result.output.result.' + field.path.join('.')) : null)
      : match && validOutputFields(source.config.outputFields)
        ? source.config.outputFields.fields.find(field => field.name === match[1]) : null;
    if (!definition || (source.config.outputSchemaId !== nestedOutputSchema && !definition.required) || definition.type === 'string_array') return '自定义格式条件只能引用已声明的必填标量字段。';
    const expected = definition.type === 'integer' || definition.type === 'number' ? 'number' : definition.type;
    if (typeof value !== expected || (expected === 'number' && !Number.isFinite(value))) return '条件值类型须与自定义字段“' + definition.name + '”一致。';
    if (expected !== 'number' && !['eq','ne'].includes(operator)) return '文本和布尔字段只支持等于或不等于。';
    return '';
  }
  const outstanding = new Map();
  const savedCopies = new Map();
  const copyDrafts = new Map();
  const templates = globalThis.AgentLineTemplates;
  const templateCatalog = templates ? templates.catalog() : [];
  let workflowDescription = '', appliedSearch = '';
  let archiveAudit = null, archivesNext = null;
  let currentOperation = null, publishedBindings = [];
  let canvasView = null, nodeDrag = null, edgeDrag = null, paletteDrag = null, ignoreClickUntil = 0;
  const operationId = () => 'operation-' + (globalThis.crypto?.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + '-' + (++sequence));
  // Sim document identifiers permit letters, digits and underscores only (max 64).
  const uid = prefix => prefix + '_' + (globalThis.crypto?.randomUUID ? crypto.randomUUID().replaceAll('-', '') : Date.now().toString(36) + '_' + (++sequence));
  const enc = encodeURIComponent;
  const base = () => '/extension/v1/authoring/projects/' + enc(project);
  const tell = (message, error = false) => { $('notice').textContent = message; $('notice').classList.toggle('error', error); };
  const el = (tag, text, cls) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (cls) node.className = cls; return node; };
  const discard = () => !(dirty || formDirty || nodeDrag?.moved) || confirm('还有未保存的编辑。确定放弃这些编辑吗？');
  function pending() { return outstanding.has(project); }
  function controls() {
    const connected = !!token;
    const blocked = busy || pending() || !!nodeDrag || !!edgeDrag;
    versionHistory?.refreshControls(blocked || !token || !workflow);
    $('logout').disabled = !connected;
    $('login').disabled = connected || busy;
    $('project').disabled = !connected || busy;
    $('refresh').disabled = !project || busy;
    for (const id of ['workflow-search','workflow-search-submit','workflow-search-clear']) $(id).disabled = !project || busy;
    $('refresh-roles').disabled = !project || busy;
    $('previous').disabled = !project || busy || page === 0;
    $('next').disabled = !project || busy || nextCursor === null;
    for (const id of ['workflow-name','workflow-description','create']) $(id).disabled = !project || blocked;
    const template = templateCatalog.find(t => t.id === $('template-choice').value);
    const workflowDraftTemplate = template?.id === 'workflow-draft-v1';
    for (const id of ['template-choice','template-name']) $(id).disabled = !project || blocked || !rolesReady;
    $('template-roles').querySelectorAll('select').forEach(select => { select.disabled = !project || blocked || !rolesReady; });
    $('template-submit').disabled = !project || blocked || !rolesReady || !template
      || new Set(roles.map(r=>r.roleAgentId)).size < template.slots.length
      || (workflowDraftTemplate && (!outputSchemaAvailable('workflow-draft-v1')
        || !validWorkflowDraftRole(roles.find(r=>r.roleBindingId===$('template-role-planner')?.value))));
    $('archive-workflow').disabled = !workflow || !editable || dirty || formDirty || blocked;
    $('refresh-archives').disabled = !project || busy;
    $('archives-next').disabled = !project || busy || archivesNext === null;
    $('archive-download').disabled = !archiveAudit || !project || busy;
    $('archive-reconcile').disabled = !archiveAudit || busy;
    $('reload').disabled = !workflow || busy || !!nodeDrag;
    $('canvas-focus').disabled = !selected || blocked;
    for (const id of ['copy-name', 'copy-submit']) $(id).disabled = !workflow || !editable || dirty || formDirty || blocked;
    const agentsBlocked = documentValue?.nodes.some(n => n.type === 'agent' && (!rolesReady
      || !roles.some(r => r.roleBindingId === n.config.roleBindingId)
      || !outputSchemaAvailable(n.config.outputSchemaId)
      || (customOutputSchema(n.config.outputSchemaId) ? !validDeclaredFields(n.config.outputSchemaId,n.config.outputFields) : Object.hasOwn(n.config,'outputFields'))
      || (Object.hasOwn(n.config, 'limits') && (!nodeLimitsSupported || !validNodeLimits(n.config.limits)))));
    const parallelCapabilityBlocked = !!documentValue && (documentValue.nodes.filter(n=>n.type==='parallel').length > maxStaticParallelGroups
      || documentValue.nodes.filter(n=>n.type==='join').length > maxStaticParallelGroups);
    if (parallelCapabilityBlocked) for (const id of ['copy-name','copy-submit']) $(id).disabled = true;
    const selectedAgent = documentValue?.nodes.find(n => n.id === selected && n.type === 'agent');
    const selectedHuman = documentValue?.nodes.find(n => n.id === selected && n.type === 'human');
    const selectedNode = documentValue?.nodes.find(n => n.id === selected);
    $('node-form').querySelectorAll('input,select,textarea,button').forEach(control=>{ control.disabled=!selectedNode||!editable||blocked; });
    $('agent-custom-limits').disabled = !selectedAgent || !nodeLimitsSupported || !editable || blocked;
    for (const id of ['agent-max-execution-seconds','agent-max-business-attempts']) $(id).disabled = !selectedAgent || !nodeLimitsSupported || !editable || blocked || !$('agent-custom-limits').checked;
    $('human-approval-ttl-enabled').disabled = !selectedHuman || !humanApprovalTtlSupported || !editable || blocked;
    $('human-approval-ttl-seconds').disabled = !selectedHuman || !humanApprovalTtlSupported || !editable || blocked || !$('human-approval-ttl-enabled').checked;
    $('human-approval-question').disabled = !selectedHuman || !humanApprovalQuestionSupported || !editable || blocked;
    $('save').disabled = !workflow || !editable || !dirty || formDirty || blocked || agentsBlocked || parallelCapabilityBlocked;
    $('publish').disabled = !workflow || !editable || dirty || formDirty || !canPublish || blocked || agentsBlocked || parallelCapabilityBlocked;
    $('graph-controls').disabled = !editable || !documentValue || blocked || parallelCapabilityBlocked;
    $('observe').disabled = !currentOperation || currentOperation.status === 'rejected' || !connected || busy;
    nativeBinding?.sync(!token || !workflow || blocked);
    $('draft-status').textContent = !workflow ? '尚未加载草稿' : Array.isArray(nativeWaitView) && !editable ? '下方只绑定，不覆盖原图' : !editable || parallelCapabilityBlocked ? '此图包含未支持的内容，只读保护，不能覆盖' : formDirty ? '节点表单尚未应用，请先应用节点配置' : dirty ? '未保存的本地草稿' : '当前草稿已读取 · 发布需单独操作';
    $('copy-hint').textContent = !workflow ? '先打开要复制的流程。' : !editable || parallelCapabilityBlocked ? '当前图包含未支持内容，不能复制。' : dirty || formDirty ? '先应用节点配置并保存当前草稿，再复制。' : '复制当前已加载的草稿；创建后核对内容并保存。副本不会自动发布、运行或创建角色。';
    if (workflow && agentsBlocked) $('draft-status').textContent += documentValue?.nodes.some(n => n.type === 'agent' && Object.hasOwn(n.config, 'limits') && !nodeLimitsSupported)
      ? ' · 当前后台不支持此图已有的节点限额，已保留配置；请升级后台后再应用、保存或发布'
      : ' · Agent 角色绑定、结果格式或节点限额未核验/不可用，保存与发布已暂停';
    syncBatch(blocked);
    portableTemplates?.sync();
    subworkflows?.sync(blocked);
    if (documentValue && protectedSubworkflowIds().has(selected)) {
      $('node-form').querySelectorAll('input,select,textarea,button').forEach(control=>{ control.disabled=true; });
      $('node-form').dataset.subworkflowReadonly='true';
    } else {
      delete $('node-form').dataset.subworkflowReadonly;
    }
  }
  async function api(path, options = {}) {
    const captured = epoch, credential = token;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 25000);
    try {
      const response = await fetch(path, {...options, cache:'no-store', signal:controller.signal, headers:{'Authorization':'Bearer ' + credential, ...(options.body ? {'Content-Type':'application/json'} : {})}});
      const payload = await response.json().catch(() => null);
      if (captured !== epoch) throw new Error('STALE');
      if (!response.ok) {
        const error = new Error(typeof payload?.detail === 'string' && /[\u3400-\u9fff]/.test(payload.detail) ? payload.detail.slice(0,300) : '请求未完成，请核对权限和配置（' + response.status + '）');
        error.status = response.status;
        // These responses are produced before a mutation intent is admitted.
        // Storage/transport 503 remains ambiguous unless the server proves admission closed.
        error.notAdmitted = [401,403,404,422].includes(response.status) || response.headers.get('X-Agent-Line-Admission') === 'closed';
        throw error;
      }
      if (!payload || typeof payload !== 'object') throw new Error('服务响应无法核验');
      return payload;
    } finally { clearTimeout(timer); }
  }
  function clearDraft() {
    nativeWaitView = null;
    nativeBinding?.clear();
    versionHistory?.clear();
    portableTemplates?.clear();
    subworkflows?.clear();
    finishNodeDrag(false); finishEdgeDrag(false); paletteDrag = null; canvasView = null; $('canvas-scale').textContent = '100%';
    archiveAudit = null; archivesNext = null; $('archive-list').replaceChildren(); $('archive-audit').hidden = true; $('archive-version-list').replaceChildren(); $('archive-run-list').replaceChildren(); $('archive-audit-summary').textContent = ''; $('archive-audit-heading').textContent='归档前审计'; $('archive-reconcile').hidden=true;
    workflow = ''; documentValue = null; digest = ''; selected = ''; editable = false; workflowServerEditable = false; dirty = false; formDirty = false; humanApprovalQuestionIssue = false; humanApprovalQuestionEdited = false;
    workflowDescription = ''; $('copy-name').value = ''; $('copy-details').open = false;
    publishedBindings = []; renderPublishedBindings();
    $('flow-heading').textContent = '选择一个流程开始搭建';
    for (const id of ['parallel-branches','parallel-join','join-parallel','join-required']) $(id).replaceChildren(); $('parallel-state').textContent=''; $('join-state').textContent=''; $('end-result-source').replaceChildren(); $('loop-body').replaceChildren(); $('loop-body-state').textContent=''; $('node-form').reset(); outputFieldsDraft=[]; nestedFieldsTouched=false; $('agent-nested-fields').value=''; $('agent-nested-fields-panel').hidden=true; outputFieldsIssue=''; outputFieldsOriginalPresent=false; outputFieldsDiscard=false; outputFieldsSeeded=false; outputFieldsSchemaSelection=''; clearRoleSummary(); $('agent-output-fields-list').replaceChildren(); $('agent-output-fields-panel').hidden=true; $('agent-output-fields-hint').textContent='定义字段名、类型和必填项；至少一个字段必填。'; $('agent-output-fields-discard').checked=false; $('agent-role').replaceChildren(); $('agent-source').replaceChildren(); $('agent-output-schema').replaceChildren(); $('agent-role-state').textContent = ''; $('agent-output-schema-hint').textContent = '新格式需在正文中输出完整 JSON。条件可读取 result.output.result.status 或 result.output.result.approved。Agent 审查意见不是人工审批，也不代表独立测试通过。'; $('agent-limits-hint').textContent = '限额配置能力尚未读取。'; $('agent-limits-config').hidden = true; $('human-approval-ttl-config').hidden = true; $('human-approval-ttl-hint').textContent = '当前后台未声明人工确认有效期能力。'; $('human-approval-question').value=''; $('human-approval-question-hint').textContent='当前后台未声明确认事项能力。'; $('node-form').hidden = true; $('selection-empty').hidden = false;
    $('rule-list').replaceChildren(); $('canvas').replaceChildren(); $('node-list').replaceChildren(); $('edge-list').replaceChildren();
    for (const id of ['edge-source','edge-target','edge-branch']) $(id).replaceChildren();
    controls();
  }
  function showOperation(operation) {
    currentOperation = operation;
    $('operation-panel').hidden = !operation;
    $('operation-status').textContent = operation ? outcomes[operation.receipt?.outcome] || '结果尚未确认。保留原操作编号，只查询，不重新提交。' : '';
    $('operation-id').textContent = operation ? '操作编号：' + operation.operationId + ' · 类型：' + ({create:'创建',save:'保存',publish:'发布',archive:'归档'}[operation.kind] || '') : '';
    controls();
  }
  function logout() {
    if (!discard()) return;
    epoch++; savedCopies.clear(); copyDrafts.clear(); roles = []; rolesReady = false; outputSchemaIds = []; nodeLimitsSupported = false; humanApprovalTtlSupported = false; humanApprovalQuestionSupported = false; maxStaticParallelGroups = 1; $('roles-status').textContent = '尚未读取角色绑定。'; $('parallel-capability-hint').textContent = '当前项目最多支持 1 组静态并行（旧后台默认上限）。'; token = ''; project = ''; busy = false; canPublish = false; nextCursor = null; cursors = ['']; page = 0;
    appliedSearch = ''; $('workflow-search').value = ''; $('workflow-search-status').textContent = '请选择项目后搜索。'; $('page-label').textContent = '第 1 页';
    $('token').value = ''; $('workflow-name').value = ''; $('workflow-description').value = ''; resetTemplates(); resetBatch();
    $('project').replaceChildren(new Option('请先连接', '')); $('workflow-list').replaceChildren(el('p','尚未加载流程。','muted'));
    $('connection').textContent = '尚未连接'; clearDraft(); showOperation(null); tell('已退出并清空页面内容。未确认操作的编号仅在本页内存保留。');
  }
  $('logout').addEventListener('click', logout);
  $('login-form').addEventListener('submit', async event => {
    event.preventDefault(); if (busy || token) return;
    token = $('token').value.trim(); $('token').value = ''; if (!token) return;
    epoch++; const captured = epoch; busy = true; controls(); tell('正在读取授权项目…');
    try {
      const data = await api('/extension/v1/authoring/projects');
      if (!Array.isArray(data.projects)) throw new Error('项目列表无法核验');
      canPublish = data.canPublish === true;
      $('project').replaceChildren(new Option('请选择项目', ''));
      data.projects.forEach(item => $('project').add(new Option(item.name || item.id, item.id)));
      $('connection').textContent = '编辑服务已连接'; tell(data.projects.length ? '请选择项目，打开已有流程或创建草稿。' : '此凭证暂无可编辑项目。');
    } catch (error) { if (captured === epoch) { token = ''; tell(error.status === 404 ? '本实例尚未启用流程编辑，请核对部署配置。' : error.message, true); } }
    finally { if (captured === epoch) { busy = false; controls(); } }
  });
  $('project').addEventListener('change', async () => {
    if (!discard()) { $('project').value = project; return; }
    epoch++; savedCopies.clear(); copyDrafts.clear(); roles = []; rolesReady = false; outputSchemaIds = []; nodeLimitsSupported = false; humanApprovalTtlSupported = false; humanApprovalQuestionSupported = false; maxStaticParallelGroups = 1; $('roles-status').textContent = '尚未读取角色绑定。'; $('parallel-capability-hint').textContent = '当前项目最多支持 1 组静态并行（角色能力尚未读取）。'; busy = false; project = $('project').value; page = 0; cursors = ['']; nextCursor = null;
    appliedSearch = ''; $('workflow-search').value = ''; $('workflow-search-status').textContent = '请选择项目后搜索。'; $('page-label').textContent = '第 1 页';
    clearDraft(); $('workflow-name').value = ''; $('workflow-description').value = ''; resetTemplates(); resetBatch();
    $('workflow-list').replaceChildren(); showOperation(outstanding.get(project) || null); tell('正在读取项目流程…');
    if (project) { const selectedEpoch = epoch; await loadRoles(); if (project && selectedEpoch === epoch) await listWorkflows(); } else { tell('请选择项目。'); controls(); }
  });
  async function loadRoles() {
    if (!project || busy) return;
    const captured = epoch; busy = true; rolesReady = false; clearRoleSummary(); roles = []; outputSchemaIds = []; nodeLimitsSupported = false; humanApprovalTtlSupported = false; humanApprovalQuestionSupported = false; maxStaticParallelGroups = 1; subworkflowAuthoring=null; editable = loadedDocumentEditable(documentValue); $('parallel-capability-hint').textContent = '当前项目最多支持 1 组静态并行（角色能力尚未读取）。'; controls();
    $('roles-status').textContent = '正在读取服务端配置的既有角色绑定…';
    try {
      const data = await api(base() + '/roles');
      const declared = Object.hasOwn(data, 'outputSchemaIds') ? data.outputSchemaIds : ['task-result'];
      if (!Array.isArray(data.roles) || data.source !== 'configured_existing_roles'
          || data.roles.some(r => !r || typeof r.roleBindingId !== 'string' || typeof r.roleAgentId !== 'string' || r.memoryPolicyId !== 'native-role')
          || new Set(data.roles.map(r => r.roleBindingId)).size !== data.roles.length
          || !Array.isArray(declared) || declared.some(id => typeof id !== 'string' || !id.trim())) throw new Error('角色绑定或结果格式列表无法核验');
      roles = data.roles; outputSchemaIds = [...new Set(declared)]; nodeLimitsSupported = data.nodeLimitsSupported === true; humanApprovalTtlSupported = data.humanApprovalTtlSupported === true; humanApprovalQuestionSupported = data.humanApprovalQuestionSupported === true; maxStaticParallelGroups = staticParallelGroupLimit(data.maxStaticParallelGroups); subworkflowAuthoring=plain(data.subworkflowAuthoring)?data.subworkflowAuthoring:null; editable = loadedDocumentEditable(documentValue); $('parallel-capability-hint').textContent = '当前项目最多支持 ' + maxStaticParallelGroups + ' 组静态并行；每组仍需独立配对分发与汇合。'; rolesReady = true;
      $('roles-status').textContent = (roles.length ? '已读取 ' + roles.length + ' 个配置中的既有角色绑定及服务端声明的结果格式；这不是 QwenPaw 实时健康检查。' : '此项目没有可用的已配置角色绑定，含 Agent 节点的草稿不能保存。')
        + (nodeLimitsSupported ? ' 支持自定义节点限额。' : ' 当前后台未声明自定义节点限额能力。')
        + (humanApprovalTtlSupported ? ' 支持人工确认有效期。' : ' 当前后台未声明人工确认有效期能力。')
        + (humanApprovalQuestionSupported ? ' 支持确认事项。' : ' 当前后台未声明确认事项能力。');
    } catch (error) { if (captured === epoch) { maxStaticParallelGroups = 1; subworkflowAuthoring=null; editable = loadedDocumentEditable(documentValue); $('parallel-capability-hint').textContent = '角色能力读取失败，按旧后台上限 1 组静态并行保护草稿。'; $('roles-status').textContent = '角色绑定读取失败，含 Agent 节点的草稿不能保存。请刷新重试。'; } }
    finally { if (captured === epoch) { busy = false; nativeBinding?.rolesUpdated(); const node = documentValue?.nodes.find(n => n.id === selected); if (node?.type === 'agent') { fillAgentRoles($('agent-role').value || node.config.roleBindingId); fillAgentOutputSchemas($('agent-output-schema').value || node.config.outputSchemaId); updateAgentLimits(node); } else if (node?.type === 'human') { updateHumanApprovalTtl(node); updateHumanApprovalQuestion(node); } renderTemplateRoles(); controls(); } }
  }
  $('refresh-roles').addEventListener('click', loadRoles);
  function resetTemplates() {
    $('template-name').value = ''; $('template-details').open = false;
    $('template-roles').replaceChildren(); $('template-description').textContent = '';
    $('template-hint').textContent = '先选择项目并读取既有角色绑定。';
  }
  function renderTemplateRoles() {
    const template = templateCatalog.find(item => item.id === $('template-choice').value);
    const workflowDraftTemplate = template?.id === 'workflow-draft-v1';
    $('template-roles').replaceChildren();
    $('template-description').textContent = template?.description || '模板目录不可用，请重新加载页面。';
    if (!template) return;
    for (const [label, field] of [['需要提供', 'inputContract'], ['预期产物', 'outputContract'], ['完成条件', 'completionCriteria']]) {
      if (typeof template[field] === 'string' && template[field].trim()) {
        $('template-description').append(el('p', label + '：' + template[field]));
      }
    }
    for (const slot of template.slots) {
      const label = el('label',slot.name), select = el('select');
      select.id = 'template-role-' + slot.id; label.htmlFor = select.id; select.required = true;
      select.add(new Option('请选择当前项目的既有角色', ''));
      (workflowDraftTemplate ? roles.filter(validWorkflowDraftRole) : roles)
        .forEach(role => select.add(new Option(roleDisplayName(role), role.roleBindingId)));
      if (roles.some(role=>role.roleBindingId === slot.id)) select.value = slot.id;
      label.append(select); $('template-roles').append(label);
    }
    $('template-hint').textContent = workflowDraftTemplate && !outputSchemaAvailable('workflow-draft-v1')
      ? '当前服务端未声明 workflow-draft-v1 结果格式，不能载入此模板。请升级服务端后重新读取角色配置。'
      : !rolesReady ? '角色列表尚不可核验，请先刷新角色绑定。'
      : workflowDraftTemplate && !roles.some(validWorkflowDraftRole)
        ? '此模板仅接受元数据完整的规划角色：只读访问、只读沙箱且命令审批策略为 never。角色信息缺失或控制不符时不能载入。'
      : new Set(roles.map(r=>r.roleAgentId)).size < template.slots.length
        ? '此模板需要 ' + template.slots.length + ' 个不同的正式角色。请先完成项目角色配置，或选择适合当前角色数量的模板。'
        : workflowDraftTemplate
          ? '选择合规规划角色后生成本地流程草稿。输出是可移植候选，须人工核对并另行导入；不会自动保存、发布或发起新流程。'
          : '选择角色后创建独立草稿；内容需要另行保存，不会自动发布、执行或创建 Agent。';
    controls();
  }
  $('template-choice').replaceChildren(...templateCatalog.map(item => new Option(item.name,item.id)));
  $('template-choice').addEventListener('change',renderTemplateRoles);
  function roleDisplayName(role) { return validRoleDetails(role?.details) ? role.details.displayName : role.roleAgentId; }
  function clearRoleSummary() {
    $('agent-role-summary').hidden = true;
    $('agent-role-summary-title').textContent = '';
    $('agent-role-summary-note').textContent = '';
    $('agent-role-summary-values').replaceChildren();
    $('agent-role-summary-details').open = false;
    $('agent-role-summary-details-list').replaceChildren();
  }
  function roleSummaryLine(list, term, value) {
    const row=el('div'), name=el('dt',term), description=el('dd',value);
    row.append(name,description); list.append(row);
  }
  function renderRoleSummary() {
    const panel=$('agent-role-summary'), role=rolesReady ? roles.find(item=>item.roleBindingId===$('agent-role').value) : null;
    if (!role) { clearRoleSummary(); return; }
    panel.hidden=false;
    const details=role.details;
    if (!Object.hasOwn(role,'details')) {
      $('agent-role-summary-title').textContent='配置摘要未提供';
      $('agent-role-summary-note').textContent='当前后台未返回角色详情；原有绑定选择仍可使用。配置与启动时快照不代表当前健康或执行就绪。';
      $('agent-role-summary-values').replaceChildren(); $('agent-role-summary-details').open=false; $('agent-role-summary-details').hidden=true; $('agent-role-summary-details-list').replaceChildren();
      return;
    }
    if (!validRoleDetails(details)) {
      $('agent-role-summary-title').textContent='配置摘要待核对';
      $('agent-role-summary-note').textContent='角色详情无法核对；原有绑定选择仍保留，请刷新确认。配置与启动时快照不代表当前健康或执行就绪。';
      $('agent-role-summary-values').replaceChildren(); $('agent-role-summary-details').open=false; $('agent-role-summary-details').hidden=true; $('agent-role-summary-details-list').replaceChildren();
      return;
    }
    $('agent-role-summary-title').textContent=details.displayName;
    $('agent-role-summary-note').textContent='摘要来自已配置内容和原生启动时快照，不表示当前健康或执行就绪。';
    const values=$('agent-role-summary-values'); values.replaceChildren();
    roleSummaryLine(values,'配置访问',details.access==='readonly'?'只读':details.access==='delegate'?'允许委派':'待核对');
    roleSummaryLine(values,'记忆','QwenPaw 原生角色记忆');
    roleSummaryLine(values,'独立验收',details.verification.state==='configured' ? (details.verification.requireTests?'命令与产物校验':'产物校验') : details.verification.state==='invalid'?'配置无效':'未提供');
    const technical=$('agent-role-summary-details-list'); technical.replaceChildren();
    const known=value=>value===null?'未提供（待核对）':String(value);
    roleSummaryLine(technical,'角色标识',role.roleAgentId);
    roleSummaryLine(technical,'绑定标识',role.roleBindingId);
    roleSummaryLine(technical,'任务角色',({planner:'规划',developer:'开发',reviewer:'审查'})[details.taskRole] || '未提供（待核对）');
    roleSummaryLine(technical,'配置修订',known(details.revision));
    const execution=details.execution;
    roleSummaryLine(technical,'执行描述符',execution.source==='native_binding_descriptor_at_startup'?'原生绑定启动时快照':'启动时未提供');
    roleSummaryLine(technical,'执行后端',execution.backend==='codex'?'Codex':'未提供（待核对）');
    roleSummaryLine(technical,'执行器标识',known(execution.executorAgentId));
    roleSummaryLine(technical,'沙箱覆盖',execution.sandbox==='read-only'?'只读沙箱':'当前接口未提供');
    roleSummaryLine(technical,'命令审批覆盖',execution.approvalPolicy==='never'?'不请求交互审批':'当前接口未提供');
    roleSummaryLine(technical,'模型标识',known(details.execution.modelId));
    roleSummaryLine(technical,'工作区','项目已登记工作区');
    const verification=details.verification;
    roleSummaryLine(technical,'验收策略',known(verification.policyId));
    roleSummaryLine(technical,'独立命令验收',verification.requireTests===null?'未提供（待核对）':verification.requireTests?'需要':'未配置');
    roleSummaryLine(technical,'产物数量',known(verification.artifactCount));
    roleSummaryLine(technical,'测试数量',known(verification.testCount));
    $('agent-role-summary-details').hidden=false;
  }
  function fillAgentRoles(binding) {
    $('agent-role').replaceChildren(new Option('请选择既有角色绑定', ''));
    roles.forEach(r => $('agent-role').add(new Option(roleDisplayName(r), r.roleBindingId)));
    if (binding && !roles.some(r => r.roleBindingId === binding)) $('agent-role').add(new Option('失效或未核验的原绑定 · ' + binding, binding));
    $('agent-role').value = binding || ''; roleHint();
  }
  function captureOutputFieldsDraft() {
    const rows = $('agent-output-fields-list')?.querySelectorAll('.output-field-row');
    if (!rows) return;
    outputFieldsDraft = [...rows].map(row => ({
      name:row.querySelector('[data-output-field="name"]').value,
      type:row.querySelector('[data-output-field="type"]').value,
      required:row.querySelector('[data-output-field="required"]').checked
    }));
  }
  function outputFieldsConfig() { return {fields:outputFieldsDraft.map(field => ({...field}))}; }
  function renderOutputFields() {
    const id = $('agent-output-schema').value;
    const isFields = id === fieldsOutputSchema;
    $('agent-nested-fields-panel').hidden = id !== nestedOutputSchema;
    const hasExisting = outputFieldsOriginalPresent || outputFieldsDraft.length > 0 || nestedFieldsTouched;
    $('agent-output-fields-panel').hidden = id === nestedOutputSchema || (!isFields && !hasExisting);
    $('agent-output-fields-list').replaceChildren();
    if (!outputFieldsIssue) outputFieldsDraft.forEach((field, index) => {
      const row = el('div',undefined,'output-field-row');
      const nameLabel = el('label','字段名');
      const name = el('input'); name.dataset.outputField = 'name'; name.maxLength = 64; name.value = field.name;
      name.disabled = !isFields;
      nameLabel.append(name);
      const typeLabel = el('label','类型');
      const type = el('select'); type.dataset.outputField = 'type';
      for (const [value,text] of [['string','文本'],['boolean','布尔值'],['number','数字'],['integer','整数'],['string_array','文本数组']]) type.add(new Option(text,value));
      type.value = field.type; type.disabled = !isFields; typeLabel.append(type);
      const requiredLabel = el('label','必填'); requiredLabel.className = 'output-field-required';
      const required = el('input'); required.type = 'checkbox'; required.dataset.outputField = 'required'; required.checked = field.required; required.disabled = !isFields;
      requiredLabel.append(required);
      const remove = el('button','删除','secondary'); remove.type = 'button'; remove.setAttribute('aria-label','删除字段 '+(field.name || (index+1)));
      remove.disabled = !isFields;
      remove.addEventListener('click',() => { captureOutputFieldsDraft(); outputFieldsDraft.splice(index,1); renderOutputFields(); formDirty=true; controls(); });
      row.append(nameLabel,typeLabel,requiredLabel,remove); $('agent-output-fields-list').append(row);
    });
    $('agent-output-fields-add').hidden = !isFields || !!outputFieldsIssue;
    $('agent-output-fields-add').disabled = !isFields || !!outputFieldsIssue || outputFieldsDraft.length >= 32;
    $('agent-output-fields-rebuild').hidden = !isFields || !outputFieldsIssue;
    $('agent-output-fields-discard-wrap').hidden = isFields || !hasExisting;
    $('agent-output-fields-discard').checked = outputFieldsDiscard;
    const warning = outputFieldsIssue
      ? outputFieldsIssue + ' 原始配置仍保留；可明确重建字段，或切换格式并勾选应用时丢弃。'
      : !isFields && hasExisting
        ? '当前格式不使用自定义字段。若要切换格式，请勾选“应用时丢弃”；原节点会在应用前保持不变。'
        : '字段名使用 ASCII 字母开头，可含数字和下划线；名称需唯一且至少一个字段必填。数组字段不能用于标量条件比较。';
    $('agent-output-fields-hint').textContent = warning + (isFields ? ' 用于条件判断的字段需设为必填。文本最多 4000 字符且不能含 NUL；数值须有限且绝对值不超过 9007199254740991；整数为严格整数；文本数组最多 100 项、每项最多 2000 字符且不能含 NUL。业务字段（包括 approved）不等于人工审批。' : '');
    $('agent-output-fields-hint').classList.toggle('error', !!outputFieldsIssue);
  }
  function loadOutputFields(node) {
    nestedFieldsTouched=false;
    $('agent-nested-fields').value=JSON.stringify(node.config.outputFields || {fields:[{name:'analysis',type:'object',required:true,fields:[{name:'status',type:'string',required:true}]}]},null,2);
    outputFieldsOriginalPresent = Object.hasOwn(node.config,'outputFields');
    outputFieldsSchemaSelection = node.config.outputSchemaId;
    outputFieldsDraft = []; outputFieldsDiscard = false; outputFieldsSeeded = false; outputFieldsIssue = '';
    if (outputFieldsOriginalPresent) {
      if (node.config.outputSchemaId === nestedOutputSchema) {
        outputFieldsIssue='嵌套定义不能直接改成平面字段；如需转换，请明确重建字段。';
      } else if (validOutputFields(node.config.outputFields)) {
        outputFieldsDraft = cloneOutputFields(node.config.outputFields); outputFieldsSeeded = true;
      } else outputFieldsIssue = '已存自定义字段结构无效或包含未知内容，不能静默重建。';
    } else if (node.config.outputSchemaId === fieldsOutputSchema) {
      outputFieldsIssue = '此节点已使用自定义字段格式，但未保存有效字段定义。';
    }
    renderOutputFields();
  }
  $('agent-nested-fields').addEventListener('input',() => { nestedFieldsTouched=true; });
  $('agent-output-fields-add').addEventListener('click',() => {
    captureOutputFieldsDraft();
    if (outputFieldsDraft.length >= 32) return;
    outputFieldsDraft.push({name:'',type:'string',required:false});
    renderOutputFields(); formDirty=true; controls();
  });
  $('agent-output-fields-rebuild').addEventListener('click',() => {
    outputFieldsDraft = [{name:'summary',type:'string',required:true}]; outputFieldsIssue=''; outputFieldsSeeded=true;
    renderOutputFields(); formDirty=true; controls();
  });
  $('agent-output-fields-discard').addEventListener('change',() => { outputFieldsDiscard=$('agent-output-fields-discard').checked; formDirty=true; controls(); });
  function roleHint() {
    const role = roles.find(r => r.roleBindingId === $('agent-role').value);
    $('agent-role-state').textContent = !rolesReady ? '角色列表尚不可核验，不能保存 Agent 配置。' : role ? '固定角色：' + roleDisplayName(role) + '。这里只核对服务端配置，执行前仍需验证原生可用性。' : '此绑定不在当前项目配置中。保留原标识，请明确选择有效绑定。';
    renderRoleSummary();
  }
  $('agent-role').addEventListener('change', roleHint);
  function fillAgentOutputSchemas(preferred) {
    const select = $('agent-output-schema');
    select.replaceChildren(new Option('请选择服务端已声明的结果格式', ''));
    for (const [id, definition] of Object.entries(outputSchemas)) {
      if (outputSchemaIds.includes(id)) select.add(new Option(definition.label + ' · ' + id, id));
    }
    if (preferred && !outputSchemaAvailable(preferred)) {
      const reason = Object.hasOwn(outputSchemas, preferred) ? '当前后端未声明' : '本地不支持';
      select.add(new Option('保留原格式（' + reason + '）· ' + preferred, preferred));
    } else if (!preferred) {
      select.add(new Option('原节点未声明结果格式', '__undeclared__'));
    }
    select.value = preferred || '__undeclared__';
    updateOutputSchemaHint();
  }
  function updateOutputSchemaHint() {
    const id = $('agent-output-schema').value;
    if(outputFieldsSchemaSelection===nestedOutputSchema && id!==nestedOutputSchema && nestedFieldsTouched) { $('agent-output-schema').value=nestedOutputSchema; tell('嵌套定义尚未应用。请先应用节点配置，或重新选择节点并确认放弃修改。',true); return; }
    captureOutputFieldsDraft();
    if(id===nestedOutputSchema && outputFieldsSchemaSelection===fieldsOutputSchema && outputFieldsDraft.length) {
      const flat=outputFieldsConfig();
      if(!outputFieldsIssue && validOutputFields(flat)) $('agent-nested-fields').value=JSON.stringify(flat,null,2);
      else { $('agent-output-schema').value=fieldsOutputSchema; tell('平面字段尚未有效配置，请先修正字段，或重新选择节点并确认放弃修改。',true); return; }
    }
    if (id === fieldsOutputSchema && outputFieldsSchemaSelection !== fieldsOutputSchema) outputFieldsDiscard = false;
    outputFieldsSchemaSelection = id;
    if (id === fieldsOutputSchema && !outputFieldsIssue && !outputFieldsSeeded && !outputFieldsOriginalPresent && !outputFieldsDraft.length) {
      outputFieldsDraft = [{name:'summary',type:'string',required:true}]; outputFieldsSeeded=true;
    }
    renderOutputFields();
    if (!id || id === '__undeclared__') {
      $('agent-output-schema-hint').textContent = '请选择当前后端声明且本地支持的结果格式。新格式需在正文中输出完整 JSON；条件可读取 result.output.result.status 或 result.output.result.approved。Agent 审查意见不是人工审批，也不代表独立测试通过。';
    } else if (!outputSchemaAvailable(id)) {
      $('agent-output-schema-hint').textContent = '已保留原格式标识“' + id + '”，但本地不支持或当前后端未声明。请核对原配置；选择有效格式前不能应用、保存或发布。';
    } else {
      const fields = id === 'task-result' ? 'result.verificationStatus 或 result.output.text'
        : id === 'coding-result-v1' ? 'result.output.result.status'
        : id === 'review-result-v1' ? 'result.output.result.approved' : 'result.output.result.<字段名>';
      $('agent-output-schema-hint').textContent = outputSchemas[id].hint + ' 条件可读取 ' + fields + '。Agent 审查意见不是人工审批，也不代表独立测试通过。';
    }
    if (id === fieldsOutputSchema && outputSchemaAvailable(id)) $('agent-output-schema-hint').textContent += ' 用于条件比较的字段需设为必填标量；业务字段（包括 approved）不等于人工审批。';
    controls();
  }
  $('agent-output-schema').addEventListener('change', updateOutputSchemaHint);
  function flowEdges() {
    // Loop completion follows its body semantically; the visible done edge is
    // not a bypass around the body for result-source dominance checks.
    return documentValue.edges.map(edge => {
      const loop = documentValue.nodes.find(n => n.id === edge.source && n.type === 'loop');
      return loop && edge.branch === 'done' ? {...edge, source:loop.config.body} : edge;
    });
  }
  function loopBodyIds() { return new Set(documentValue.nodes.filter(n=>n.type==='loop').map(n=>n.config.body)); }
  function fillLoopBodies(node, preferred) {
    $('loop-body').replaceChildren(new Option('请选择既有 Agent 节点',''));
    documentValue.nodes.filter(n=>n.type==='agent').forEach(n=>$('loop-body').add(new Option(n.name,n.id)));
    if(preferred && ![...$('loop-body').options].some(o=>o.value===preferred)) $('loop-body').add(new Option('失效的原循环体 · '+preferred,preferred));
    $('loop-body').value=preferred||''; loopHint();
  }
  function loopHint() {
    const body=documentValue?.nodes.find(n=>n.id===$('loop-body').value&&n.type==='agent');
    $('loop-body-state').textContent=!body?'原循环体不存在或未选择，请明确选择 Agent。':body.config.allowFailureBranch!==true?'此 Agent 尚未开启允许失败分支，请在 Agent 配置中自行启用；不会自动修改策略。':'循环体：'+body.name+'。请连接循环体出口与完成出口。';
  }
  $('loop-body').addEventListener('change',loopHint);
  const selectedValues = id => [...$(id).selectedOptions].map(o=>o.value);
  function fillNodeChoices(id, kind, values, multiple=false) {
    const select=$(id); select.replaceChildren(); if(!multiple) select.add(new Option('请选择配对节点',''));
    documentValue.nodes.filter(n=>n.type===kind).forEach(n=>select.add(new Option(n.name,n.id)));
    values.filter(v=>v && ![...select.options].some(o=>o.value===v)).forEach(v=>select.add(new Option('失效的原节点 · '+v,v)));
    [...select.options].forEach(o=>{o.selected=values.includes(o.value);});
  }
  function fillParallel(node, preserve=false) {
    fillNodeChoices('parallel-branches','agent',preserve?selectedValues('parallel-branches'):node.config.branches,true);
    fillNodeChoices('parallel-join','join',[preserve?$('parallel-join').value:node.config.join]);
    $('parallel-state').textContent='分支策略与连线不会自动改写；请核对每个分支的允许失败设置。';
  }
  function fillJoin(node, preserve=false) {
    fillNodeChoices('join-parallel','parallel',[preserve?$('join-parallel').value:node.config.parallelId]);
    fillRequired(preserve?selectedValues('join-required'):node.config.requiredBranches); joinPolicy();
  }
  function fillRequired(values) {
    const fork=documentValue?.nodes.find(n=>n.id===$('join-parallel').value&&n.type==='parallel');
    const select=$('join-required'); select.replaceChildren();
    (fork?.config.branches||[]).forEach(id=>{const branch=documentValue.nodes.find(n=>n.id===id&&n.type==='agent'); select.add(new Option(branch?.name||'失效的原分支 · '+id,id));});
    values.filter(v=>![...select.options].some(o=>o.value===v)).forEach(v=>select.add(new Option('不在配对分支中 · '+v,v)));
    [...select.options].forEach(o=>{o.selected=values.includes(o.value);});
    $('join-state').textContent=fork?'仅接收配对并行分发的全部分支。':'配对的并行分发不存在或尚未选择。';
  }
  function joinPolicy() { $('join-required-config').hidden=$('join-policy').value!=='required_success'; }
  $('join-policy').addEventListener('change',joinPolicy);
  $('join-parallel').addEventListener('change',()=>fillRequired(selectedValues('join-required')));
  function canSupply(source, target, includeHuman=false) {
    if (source.type==='subflow_input') return subworkflowGroupFor(source.id)?.members.has(target) === true;
    if (source.type === 'start') return true;
    if ((source.type !== 'agent' && source.type !== 'join' && source.type!=='subflow_output' && !(includeHuman && source.type === 'human')) || source.id === target) return false;
    const seen = new Set(), queue = [source.id];
    while(queue.length) { const id = queue.pop(); if(seen.has(id)) continue; seen.add(id); flowEdges().filter(e => e.source === id).forEach(e => queue.push(e.target)); }
    return seen.has(target);
  }
  function fillAgentSources(node, preferred) {
    $('agent-source').replaceChildren(new Option('请选择输入来源', ''));
    documentValue.nodes.filter(n => canSupply(n, node.id) && (n.type==='start' || n.type==='subflow_input' || resultSourceValid(n.id,node.id))).forEach(n => $('agent-source').add(new Option(shownNodeName(n) + (n.type === 'start' ? ' · 开始输入' : n.type==='subflow_input' ? ' · 子流程输入' : n.type==='subflow_output' ? ' · 子流程返回' : n.type === 'join' ? ' · 汇合输出' : ' · Agent 输出'), n.id)));
    if (preferred && ![...$('agent-source').options].some(o => o.value === preferred)) $('agent-source').add(new Option('失效或尚未连接的来源 · ' + preferred, preferred));
    $('agent-source').value = preferred || '';
  }
  function updateAgentLimits(node, loadValues = false) {
    if (loadValues) {
      const configured = Object.hasOwn(node.config, 'limits');
      $('agent-custom-limits').checked = configured;
      $('agent-max-execution-seconds').value = String(configured ? node.config.limits?.maxExecutionSeconds ?? '' : 3600);
      $('agent-max-business-attempts').value = String(configured ? node.config.limits?.maxBusinessAttempts ?? '' : 3);
    }
    const configured = Object.hasOwn(node.config, 'limits');
    $('agent-limits-config').hidden = !$('agent-custom-limits').checked;
    $('agent-limits-hint').textContent = nodeLimitsSupported
      ? '时限从每次实际提交意图起算。提交次数与同一运行、同一节点的循环/重试共用实际提交配额；次数不足时会阻止后续提交，需人工处理，不是自动重试次数。未知受理不退还额度。'
      : configured
        ? '当前后台未声明此能力。已配置限额会保留；升级后台后才能应用、保存或发布。'
        : '当前后台未声明此能力，不能新增限额。未配置时不会写入 limits。';
  }
  function validNodeLimits(limits) {
    return !!limits && typeof limits === 'object' && !Array.isArray(limits)
      && Object.keys(limits).sort().join(',') === 'maxBusinessAttempts,maxExecutionSeconds'
      && Number.isInteger(limits.maxExecutionSeconds) && limits.maxExecutionSeconds >= 1 && limits.maxExecutionSeconds <= 86400
      && Number.isInteger(limits.maxBusinessAttempts) && limits.maxBusinessAttempts >= 1 && limits.maxBusinessAttempts <= 100;
  }
  function validApprovalTtl(value) {
    return typeof value === 'number' && Number.isInteger(value) && value >= 1 && value <= 604800;
  }
  function updateHumanApprovalTtl(node, loadValues = false) {
    if (loadValues) {
      const configured = Object.hasOwn(node.config, 'approvalTtlSeconds');
      $('human-approval-ttl-enabled').checked = configured;
      $('human-approval-ttl-seconds').value = configured ? String(node.config.approvalTtlSeconds) : '3600';
    }
    const configured = Object.hasOwn(node.config, 'approvalTtlSeconds');
    $('human-approval-ttl-config').hidden = !$('human-approval-ttl-enabled').checked;
    $('human-approval-ttl-hint').textContent = !humanApprovalTtlSupported
      ? configured ? '当前后台未声明此能力。已有有效期配置已保留；升级后台后才能应用、保存或发布。' : '未配置时沿用原审批期限；当前后台未声明自定义有效期能力。'
      : '有效期从首次登记审批起算。过期只会拒绝新的审批决定，不会自动批准、拒绝、恢复或取消流程；可手动停止原流程。';
  }
  function validApprovalQuestion(value) {
    return typeof value === 'string' && value.trim().length > 0
      && Array.from(value).length <= 2000 && !value.includes('\0');
  }
  function updateHumanApprovalQuestion(node, loadValues = false) {
    if (loadValues) {
      const configured = Object.hasOwn(node.config, 'question');
      humanApprovalQuestionIssue = configured && !validApprovalQuestion(node.config.question);
      humanApprovalQuestionEdited = false;
      $('human-approval-question').value = configured && typeof node.config.question === 'string'
        ? node.config.question : configured ? JSON.stringify(node.config.question) : '';
    }
    const configured = Object.hasOwn(node.config, 'question');
    $('human-approval-question-hint').textContent = !humanApprovalQuestionSupported
      ? configured ? '当前后台未声明此能力。已有确认事项已保留；升级后台后才能应用、保存或发布。' : '未配置时保持原审批表单；当前后台未声明确认事项能力。'
      : '此文字会随发布版本和运行冻结，只说明需要核对的事项，不会授予额外权限。留空表示不配置。';
  }
  $('agent-custom-limits').addEventListener('change', () => {
    $('agent-limits-config').hidden = !$('agent-custom-limits').checked;
    controls();
  });
  $('human-approval-ttl-enabled').addEventListener('change', () => {
    $('human-approval-ttl-config').hidden = !$('human-approval-ttl-enabled').checked;
    controls();
  });
  $('human-approval-question').addEventListener('input', () => { humanApprovalQuestionEdited = true; });
  function localSubmissionLabel(value) {
    const invalid = '本地提交摘要待核对';
    if (!value || typeof value !== 'object' || Array.isArray(value)
        || value.source !== 'local_root_submissions_all_versions'
        || !Number.isSafeInteger(value.count) || value.count < 0) return invalid;
    if (value.count === 0) return value.latest === null ? '本地提交：尚无记录（全部版本）' : invalid;
    const latest = value.latest;
    const statuses = {intent:'提交意图已登记',accepted:'提交已受理',uncertain:'提交结果待核对',rejected:'提交已拒绝',unknown:'提交状态未知'};
    if (!latest || typeof latest !== 'object' || Array.isArray(latest)
        || typeof latest.runId !== 'string' || !/^[A-Za-z0-9_-]{1,160}$/.test(latest.runId)
        || typeof latest.submissionStatus !== 'string'
        || !Object.hasOwn(statuses, latest.submissionStatus)
        || !(latest.updatedAt === null || (typeof latest.updatedAt === 'number'
          && Number.isFinite(latest.updatedAt) && latest.updatedAt >= 0
          && latest.updatedAt <= 8640000000000))) return invalid;
    const timestamp = latest.updatedAt === null ? null : new Date(latest.updatedAt * 1000);
    const time = timestamp && !Number.isNaN(timestamp.getTime())
      ? timestamp.toLocaleString('zh-CN', {hour12:false}) : '时间待核对';
    return '本地提交：' + value.count + ' 次（全部版本） · 最近登记：'
      + statuses[latest.submissionStatus] + ' · 记录更新：' + time + '；不代表执行结果';
  }
  async function listWorkflows() {
    if (!project || busy) return;
    const captured = epoch; busy = true; controls();
    try {
      const data = await api(base() + '/workflows?limit=20&after=' + enc(cursors[page] || '') + (appliedSearch ? '&search=' + enc(appliedSearch) : ''));
      if (!Array.isArray(data.workflows)) throw new Error('流程列表无法核验');
      if (appliedSearch && data.search !== appliedSearch) throw new Error('当前服务未确认此搜索条件，请部署支持搜索的服务后再查询。');
      nextCursor = data.nextCursor ?? null;
      $('workflow-list').replaceChildren();
      for (const row of data.workflows) {
        const button = el('button', undefined, 'workflow-choice'); button.type = 'button'; button.setAttribute('aria-pressed', String(workflow === row.id));
        const text = el('span', row.name || row.id); if (row.description) text.append(el('small', ' · ' + row.description));
        const count = Number.isSafeInteger(row.runCount) && row.runCount >= 0 ? row.runCount : null;
        const last = typeof row.lastRunAt === 'string' ? new Date(row.lastRunAt) : null;
        const lastLabel = last && !Number.isNaN(last.getTime()) ? last.toLocaleString('zh-CN', {hour12:false}) : count === 0 ? '尚无运行' : '尚无记录';
        const details = el('span', undefined, 'workflow-summary');
        const version = Number.isSafeInteger(row.currentVersion) && row.currentVersion > 0 ? row.currentVersion : null;
        const publication = row.deploymentStatus === 'draft' ? '草稿 · 暂无当前发布版本' :
          row.deploymentStatus === 'deployed' && version !== null ? '当前发布：v' + version + (row.needsRedeployment === true ? ' · 草稿有未发布修改' : '') : '发布状态待核对';
        details.append(el('small', publication),
          el('small', '原生运行次数：' + (count === null ? '尚无记录' : count) + ' · 最近运行：' + lastLabel));
        if (Object.hasOwn(row, 'localSubmissions')) details.append(el('small', localSubmissionLabel(row.localSubmissions)));
        if (row.archivePending) { details.append(el('small','归档结果待核对，请查询原操作')); button.disabled=true; }
        button.append(text, details);
        button.addEventListener('click', () => { if (!busy && discard()) loadWorkflow(row.id); });
        $('workflow-list').append(button);
      }
      if (!data.workflows.length) $('workflow-list').append(el('p', appliedSearch ? (nextCursor !== null ? '本页没有匹配项，可继续下一页查找。' : '本页没有匹配项，已到列表末尾。') : '本页没有流程。可创建草稿，或按服务端游标继续翻页。','muted'));
      $('workflow-search-status').textContent = appliedSearch ? '当前搜索：“' + appliedSearch + '”。按项目目录分页查找，下一页继续相同搜索。' : '当前显示本项目流程。搜索会按名称和描述逐页匹配。';
      $('page-label').textContent = '第 ' + (page + 1) + ' 页';
    } catch (error) { if (captured === epoch) { nextCursor = null; $('workflow-search-status').textContent = '列表读取失败，请核对提示后重新查询。'; tell(error.message, true); } }
    finally { if (captured === epoch) { busy = false; controls(); } }
  }
  async function searchWorkflows(clear = false) {
    if (!project || busy) return;
    appliedSearch = clear ? '' : $('workflow-search').value.trim();
    $('workflow-search').value = appliedSearch;
    page = 0; cursors = ['']; nextCursor = null;
    $('workflow-list').replaceChildren(el('p', '正在读取匹配流程…', 'muted'));
    $('workflow-search-status').textContent = '正在查询…';
    await listWorkflows();
  }
  $('workflow-search-form').addEventListener('submit', event => { event.preventDefault(); searchWorkflows(); });
  $('workflow-search-clear').addEventListener('click', () => searchWorkflows(true));
  $('refresh').addEventListener('click', listWorkflows);
  $('next').addEventListener('click', () => { if (!busy && nextCursor !== null) { cursors[++page] = nextCursor; listWorkflows(); } });
  $('previous').addEventListener('click', () => { if (!busy && page) { page--; listWorkflows(); } });
  function supportedDocument(doc, retainOverCapability = false) {
    if (!doc) return true;
    if (doc.schemaVersion!==1 || !Array.isArray(doc.nodes) || !Array.isArray(doc.edges)
      || doc.nodes.some(n => !n || !Object.hasOwn(labels,n.type) || !n.config)
      || doc.edges.some(edge=>!plain(edge)||typeof edge.id!=='string'||typeof edge.source!=='string'||typeof edge.target!=='string')
      || new Set(doc.nodes.map(node=>node.id)).size!==doc.nodes.length || new Set(doc.edges.map(edge=>edge.id)).size!==doc.edges.length) return false;
    const forkCount=doc.nodes.filter(n=>n.type==='parallel').length, joinCount=doc.nodes.filter(n=>n.type==='join').length;
    if (retainOverCapability ? forkCount>10 || joinCount>10
      : forkCount>maxStaticParallelGroups || joinCount>maxStaticParallelGroups) return false;
    if (doc.nodes.some(n=>n.type==='loop') && doc.nodes.some(n=>['parallel','join'].includes(n.type))) return false;
    const wrappers=doc.nodes.filter(n=>n.type==='subflow_input'||n.type==='subflow_output'), groups=subworkflowGroups(doc), known=new Set(groups.flatMap(group=>[...group.members]));
    if (wrappers.some(node=>!known.has(node.id))) return false;
    return doc.nodes.every(n => n.type==='parallel' ? Object.keys(n.config).sort().join(',')==='branches,join,maxConcurrency' && Array.isArray(n.config.branches) && n.config.branches.every(v=>typeof v==='string') && typeof n.config.join==='string' && Number.isInteger(n.config.maxConcurrency) : n.type==='join' ? Object.keys(n.config).sort().join(',')==='parallelId,policy,requiredBranches' && typeof n.config.parallelId==='string' && ['all_success','allow_partial','required_success'].includes(n.config.policy) && Array.isArray(n.config.requiredBranches) && n.config.requiredBranches.every(v=>typeof v==='string') : true);
  }
  const plain = value => !!value && typeof value==='object' && !Array.isArray(value) && Object.getPrototypeOf(value)===Object.prototype;
  const projectedName = value => Array.from(value).slice(0,240).join('');
  const stableJson = value => JSON.stringify(value,(_key,item)=>plain(item)?Object.fromEntries(Object.keys(item).sort().map(key=>[key,item[key]])):item);
  function projectedConfig(node,mapping,startId,outputMapping) {
    const config=JSON.parse(JSON.stringify(node.config));
    if(node.type==='end') {
      const result={entryNodeId:mapping[startId]};
      if(Object.hasOwn(config,'resultFrom')) result.resultFrom=mapping[config.resultFrom]; else result.output=config.output;
      if(outputMapping!==undefined) result.outputMapping=outputMapping;
      return result;
    }
    if(node.type==='agent') config.inputFrom.nodeId=mapping[config.inputFrom.nodeId];
    else if(node.type==='condition') for(const rule of config.rules) rule.source=Object.hasOwn(rule,'source')?mapping[rule.source]:mapping[startId];
    else if(node.type==='loop') config.body=mapping[config.body];
    else if(node.type==='parallel') { config.branches=config.branches.map(id=>mapping[id]); config.join=mapping[config.join]; }
    else if(node.type==='join') { config.parallelId=mapping[config.parallelId]; config.requiredBranches=config.requiredBranches.map(id=>mapping[id]); }
    return config;
  }
  function subworkflowGroups(doc=documentValue) {
    const groups=[];
    if (!doc || !Array.isArray(doc.nodes) || !Array.isArray(doc.edges)) return groups;
    const nodes=new Map(doc.nodes.map(node=>[node.id,node]));
    for (const entry of doc.nodes.filter(node=>node.type==='subflow_input')) {
      const c=entry.config, expected=['reference','sourceDocument','callId','inputFrom'];
      if (!plain(c) || !expected.every(key=>Object.hasOwn(c,key)) || Object.keys(c).some(key=>!expected.includes(key)&&!['inputMapping','outputMapping'].includes(key))
        || !plain(c.reference) || Object.keys(c.reference).sort().join(',')!=='deploymentVersionId,graphSha256,projectId,revision,workflowId'
        || c.reference.projectId!==project || !Number.isSafeInteger(c.reference.revision) || c.reference.revision<1
        || typeof c.reference.workflowId!=='string' || typeof c.reference.deploymentVersionId!=='string' || !/^[a-f0-9]{64}$/.test(c.reference.graphSha256)
        || !plain(c.sourceDocument) || c.sourceDocument.schemaVersion!==1 || !Array.isArray(c.sourceDocument.nodes) || !Array.isArray(c.sourceDocument.edges)
        || c.sourceDocument.nodes.some(node=>!plain(node)||typeof node.id!=='string'||!['start','end','agent','condition','human','loop','parallel','join'].includes(node.type)||typeof node.name!=='string'||!plain(node.config))
        || c.sourceDocument.edges.some(edge=>!plain(edge)||typeof edge.id!=='string'||typeof edge.source!=='string'||typeof edge.target!=='string')
        || c.sourceDocument.nodes.filter(node=>node.type==='start').length!==1 || c.sourceDocument.nodes.filter(node=>node.type==='end').length!==1
        || new Set(c.sourceDocument.nodes.map(node=>node.id)).size!==c.sourceDocument.nodes.length
        || !plain(c.inputFrom) || Object.keys(c.inputFrom).sort().join(',')!=='field,nodeId'
        || !['input','output'].includes(c.inputFrom.field) || typeof c.inputFrom.nodeId!=='string'
        || (Object.hasOwn(c,'inputMapping')&&!plain(c.inputMapping)&&!Array.isArray(c.inputMapping))
        || (Object.hasOwn(c,'outputMapping')&&!plain(c.outputMapping)&&!Array.isArray(c.outputMapping)) || typeof c.callId!=='string') continue;
      const exit=nodes.get(c.callId), ec=exit?.config;
      if (!exit || exit.type!=='subflow_output' || !plain(ec) || ec.entryNodeId!==entry.id
        || !Object.hasOwn(ec,'entryNodeId') || !(Object.hasOwn(ec,'resultFrom') !== Object.hasOwn(ec,'output'))
        || Object.keys(ec).some(key=>!['entryNodeId','resultFrom','output','outputMapping'].includes(key))) continue;
      if (Object.hasOwn(c,'outputMapping')!==Object.hasOwn(ec,'outputMapping')
        || (Object.hasOwn(c,'outputMapping')&&JSON.stringify(c.outputMapping)!==JSON.stringify(ec.outputMapping))) continue;
      const members=new Set([entry.id,exit.id]), queue=[entry.id]; let valid=true;
      while(queue.length) {
        const id=queue.pop();
        if (id===exit.id) continue;
        const current=nodes.get(id), config=current?.config||{};
        const references=[...(Array.isArray(config.branches)?config.branches:[]),...(Array.isArray(config.requiredBranches)?config.requiredBranches:[]),
          ...[config.body,config.join,config.parallelId,config.resultFrom,...(current?.type==='subflow_input'?[]:[config.inputFrom?.nodeId])],...(Array.isArray(config.rules)?config.rules.map(rule=>plain(rule)?rule.source:null):[])].filter(value=>typeof value==='string');
        for (const referenced of references) if(nodes.has(referenced)&&!members.has(referenced)){members.add(referenced);queue.push(referenced);}
        for (const edge of doc.edges.filter(item=>item.source===id)) {
          if (!nodes.has(edge.target) || edge.target===entry.id) { valid=false; break; }
          if (!members.has(edge.target)) { members.add(edge.target); queue.push(edge.target); }
        }
        if (!valid) break;
      }
      const incoming=doc.edges.filter(edge=>members.has(edge.target)&&!members.has(edge.source));
      const outgoing=doc.edges.filter(edge=>members.has(edge.source)&&!members.has(edge.target));
      const internal=doc.edges.filter(edge=>members.has(edge.source)&&members.has(edge.target));
      if (!valid || members.size!==c.sourceDocument.nodes.length || internal.length!==c.sourceDocument.edges.length || incoming.length!==1 || incoming[0].target!==entry.id
        || outgoing.length!==1 || outgoing[0].source!==exit.id || internal.some(edge=>edge.source===exit.id)
        || !doc.nodes.some(node=>node.id===exit.id)) continue;
      const parentSource=nodes.get(c.inputFrom.nodeId);
      if (!parentSource || members.has(parentSource.id)
        || !((parentSource.type==='start'&&c.inputFrom.field==='input')
          || (['agent','join','subflow_output'].includes(parentSource.type)&&c.inputFrom.field==='output'))) continue;
      if (Object.hasOwn(ec,'resultFrom') && !members.has(ec.resultFrom)) continue;
      const sourceStart=c.sourceDocument.nodes.find(node=>node.type==='start')?.id, mapping=Object.create(null); mapping[sourceStart]=entry.id;
      const sourceEnd=c.sourceDocument.nodes.find(node=>node.type==='end')?.id; mapping[sourceEnd]=exit.id;
      let projected=true;
      for(const child of c.sourceDocument.nodes.filter(node=>node.type!=='start'&&node.type!=='end')) {
        const childType=child.type, expectedName=projectedName(child.name);
        const candidates=[...members].map(id=>nodes.get(id)).filter(node=>node&&node.type===childType&&node.name.startsWith('SF')&&/^SF[a-f0-9]{12}_/.test(node.name)&&node.name.slice(15)===expectedName);
        if(candidates.length!==1){projected=false;break;}
        mapping[child.id]=candidates[0].id;
      }
      if(!projected) continue;
      try { for(const child of c.sourceDocument.nodes) {
        const actual=nodes.get(mapping[child.id]);
        if(child.type==='start') {
          if(!actual || actual.name.slice(15)!==projectedName(child.name) || !/^SF[a-f0-9]{12}_/.test(actual.name)) { projected=false; break; }
          continue;
        }
        if(!actual || actual.name!=='SF'+actual.name.slice(2,14)+'_'+projectedName(child.name)
          || stableJson(actual.config)!==stableJson(projectedConfig(child,mapping,sourceStart,c.outputMapping))) { projected=false; break; }
      } } catch { projected=false; }
      if(!projected) continue;
      if(c.sourceDocument.edges.some(edge=>!plain(edge)||!Object.hasOwn(mapping,edge.source)||!Object.hasOwn(mapping,edge.target))) continue;
      const projectedEdges=c.sourceDocument.edges.map(edge=>({source:mapping[edge.source],target:mapping[edge.target],...(Object.hasOwn(edge,'branch')?{branch:edge.branch}:{})}));
      const actualEdges=internal.map(edge=>({source:edge.source,target:edge.target,...(Object.hasOwn(edge,'branch')?{branch:edge.branch}:{})}));
      if(stableJson(projectedEdges.map(stableJson).sort())!==stableJson(actualEdges.map(stableJson).sort())) continue;
      if (groups.some(group=>[...members].some(id=>group.members.has(id)))) continue;
      groups.push({entryId:entry.id,exitId:exit.id,callId:c.callId,members,entry,exit,incoming:incoming[0],outgoing:outgoing[0]});
    }
    return groups;
  }
  function protectedSubworkflowIds(doc=documentValue) { return new Set(subworkflowGroups(doc).flatMap(group=>[...group.members])); }
  function subworkflowGroupFor(id) { return subworkflowGroups().find(group=>group.members.has(id)); }
  function shownNodeName(node) {
    const group=subworkflowGroupFor(node.id);
    if (!group) return node.name;
    return node.name.replace(/^SF[a-f0-9]{12}_/, '');
  }
  function edgeEditAllowed(source,target) {
    const sourceGroup=subworkflowGroupFor(source), targetGroup=subworkflowGroupFor(target);
    return !sourceGroup && !targetGroup;
  }
  const starter = () => ({schemaVersion:1, nodes:[{id:uid('start'),type:'start',name:'开始',position:{x:50,y:110},config:{}},{id:uid('end'),type:'end',name:'结束',position:{x:410,y:110},config:{output:{}}}], edges:[], variables:{}});
  async function loadWorkflow(id) {
    epoch++; const captured = epoch; busy = true; controls();
    try {
      const data = await api(base() + '/workflows/' + enc(id));
      if (data.workflowId !== id) throw new Error('草稿身份与请求不一致，请重新核对。');
      clearDraft(); workflow = id; publishedBindings = Array.isArray(data.publishedBindings) ? data.publishedBindings : []; renderPublishedBindings(); digest = data.stateSha256; workflowServerEditable = data.editable === true;
      canvasView = null;
      editable = loadedDocumentEditable(data.document);
      documentValue = editable ? (data.document || (data.draftEmpty ? starter() : null))
        : supportedDocument(data.document, true) ? data.document : null;
      if (!documentValue) editable = false;
      if (data.draftEmpty && documentValue) { documentValue.edges.push({id:uid('edge'),source:documentValue.nodes[0].id,target:documentValue.nodes[1].id}); dirty = true; }
      $('flow-heading').textContent = data.name || '流程草稿';
      workflowDescription = data.description || '';
      $('copy-name').value = Array.from(data.name || '流程').slice(0,120).join('') + '（副本）';
      nativeWaitView = Array.isArray(data.nativeWaits) ? data.nativeWaits : null;
      nativeBinding?.show(data.nativeWaits ?? null);
      render(); tell(!editable ? (nativeWaitView ? '下方只绑定，不覆盖原图。' : '当前图包含未支持内容，已禁止编辑和覆盖。请使用 Sim 原生编辑器。') : data.draftEmpty ? '已生成开始 → 结束初稿，尚未保存到 Sim。' : '草稿已加载。修改后先保存，再发布。');
      return data;
    } catch (error) { if (captured === epoch) tell(error.message, true); }
    finally { if (captured === epoch) { busy = false; controls(); } }
  }
  $('reload').addEventListener('click', () => { if (discard()) loadWorkflow(workflow); });
  function renderPublishedBindings() {
    const container = $('published-bindings'); container.replaceChildren();
    const valid = publishedBindings.filter(b => b && typeof b.bindingId === 'string' && /^[A-Za-z0-9_-]{1,160}$/.test(b.bindingId) && Number.isInteger(Number(b.revision)) && Number(b.revision) > 0);
    container.hidden = valid.length === 0;
    if (!valid.length) return;
    container.append(el('h3','已发布版本'));
    container.append(el('p','仅活动版本允许新运行；历史版本可查询。请在运行页另行输入运行凭证，编辑凭证不会随链接传递。','muted'));
    for (const binding of valid) { const link=el('a','版本 '+binding.revision+' · 前往运行 / 查询','run-binding-link'); link.href='./runs.html?binding='+enc(binding.bindingId); container.append(link); }
  }
  function resultSourceValid(sourceId, consumer, includeHuman=false) {
    const source=documentValue?.nodes.find(n=>n.id===sourceId);
    if (source?.type==='subflow_input') return false;
    const sourceGroup=source&&subworkflowGroupFor(source.id), consumerGroup=subworkflowGroupFor(consumer);
    if (sourceGroup && sourceGroup.callId!==consumerGroup?.callId && source.type!=='subflow_output') return false;
    if(!source || (source.type!=='agent' && source.type!=='join' && source.type!=='subflow_output' && !(includeHuman && source.type==='human')) || !canSupply(source,consumer,includeHuman)) return false;
    const start=documentValue.nodes.find(n=>n.type==='start'); if(!start) return false;
    const seen=new Set(), queue=[start.id];
    while(queue.length) { const id=queue.pop(); if(id===sourceId||seen.has(id)) continue; seen.add(id); flowEdges().filter(e=>e.source===id).forEach(e=>queue.push(e.target)); }
    return !seen.has(consumer);
  }
  function subworkflowInputSourceValid(sourceId,consumer) {
    const source=documentValue?.nodes.find(node=>node.id===sourceId), sourceGroup=source&&subworkflowGroupFor(source.id), consumerGroup=subworkflowGroupFor(consumer);
    if(source?.type!=='subflow_input'||!sourceGroup||sourceGroup.callId!==consumerGroup?.callId||!canSupply(source,consumer)) return false;
    const start=documentValue.nodes.find(node=>node.type==='start'); if(!start) return false;
    const seen=new Set(),queue=[start.id];
    while(queue.length){const id=queue.pop();if(id===sourceId||seen.has(id))continue;seen.add(id);flowEdges().filter(edge=>edge.source===id).forEach(edge=>queue.push(edge.target));}
    return !seen.has(consumer);
  }
  function fillResultSources(select, consumer, preferred, includeInput=false) {
    select.replaceChildren(new Option(includeInput?'开始输入':'请选择前置 Agent', ''));
    documentValue.nodes.filter(n=>(n.type==='subflow_input'&&includeInput&&subworkflowGroupFor(n.id)?.members.has(consumer))||resultSourceValid(n.id,consumer,includeInput)).forEach(n=>select.add(new Option(shownNodeName(n)+(n.type==='subflow_input'?' · 子流程输入':n.type==='subflow_output'?' · 子流程返回':n.type==='human'?' · 人工确认结果':n.type==='join'?' · 汇合结果':' · Agent 结果'),n.id)));
    if(preferred && ![...select.options].some(o=>o.value===preferred)) select.add(new Option('原来源失效或不是必经节点 · '+preferred,preferred));
    select.value=preferred||'';
  }
  function fillConditionFieldSuggestions(select, sourceId) {
    const previous = select.value;
    select.replaceChildren(new Option('选择建议字段', ''));
    const source = documentValue?.nodes.find(n => n.id === sourceId);
    let fields = [];
    if (source?.type === 'agent') {
      fields = [['result.verificationStatus', '验收状态 · result.verificationStatus']];
      if (!customOutputSchema(source.config.outputSchemaId)) fields.push(['result.output.text', '执行文本 · result.output.text']);
      if (source.config.outputSchemaId === 'coding-result-v1') fields.push(
        ['result.output.result.status', '编码状态 · result.output.result.status'],
        ['result.output.result.summary', '编码摘要 · result.output.result.summary']
      );
      if (source.config.outputSchemaId === 'review-result-v1') fields.push(
        ['result.output.result.approved', '审查结论 · result.output.result.approved'],
        ['result.output.result.summary', '审查摘要 · result.output.result.summary']
      );
      if(source.config.outputSchemaId===nestedOutputSchema && validNestedFields(source.config.outputFields)) {
        for(const field of window.AgentLineStructuredFields.scalarFields(source.config.outputFields)) { const path='result.output.result.'+field.path.join('.'); fields.push([path,field.path.join('.')+' · '+field.type]); }
      }
      if (source.config.outputSchemaId === fieldsOutputSchema && validOutputFields(source.config.outputFields)) {
        for (const field of source.config.outputFields.fields) {
          if (!field.required || field.type === 'string_array') continue;
          fields.push(['result.output.result.' + field.name, field.name + ' · ' + field.type]);
        }
      }
    } else if (source?.type === 'join' || source?.type === 'subflow_output') {
      fields = [['result.verificationStatus', '验收状态 · result.verificationStatus']];
    } else if (source?.type === 'human') {
      fields = [['result.approval.approved', '人工确认 · result.approval.approved']];
    } else if (source?.type === 'subflow_input') {
      fields = [['input.value', '子流程输入 · input.value']];
    }
    fields.forEach(([value, label]) => select.add(new Option(label, value)));
    select.value = fields.some(([value]) => value === previous) ? previous : '';
  }
  function endMode() { const result=$('end-mode').value==='result'; $('end-result-config').hidden=!result; $('end-literal-config').hidden=result; }
  $('end-mode').addEventListener('change',endMode);
  async function processOperation(operation) {
    if (!operation || operation.projectId !== project || !['create','save','publish','archive'].includes(operation.kind)) throw new Error('操作回执身份无法核验');
    const known = operation.status === 'accepted' && Object.hasOwn(outcomes, operation.receipt?.outcome);
    if (known) outstanding.delete(project); else outstanding.set(project, {projectId:project, operationId:operation.operationId, kind:operation.kind});
    showOperation(operation);
    if (!known) return;
    const receipt = operation.receipt;
    if (operation.kind === 'archive' && receipt.outcome === 'archived') {
      clearDraft(); busy=false; await listWorkflows(); await readArchives();
    }
    if (receipt.outcome === 'published' && receipt.workflowId === workflow && typeof receipt.bindingId === 'string') {
      publishedBindings = [...publishedBindings.filter(b => b.bindingId !== receipt.bindingId), {bindingId:receipt.bindingId, revision:receipt.revision}]; renderPublishedBindings();
    }
    if (operation.kind === 'save' && receipt.outcome === 'saved' && receipt.workflowId === workflow && receipt.stateSha256) {
      if (savedCopies.get(operation.operationId) === JSON.stringify(documentValue) && !formDirty) {
        digest = receipt.stateSha256; dirty = false;
      } else {
        workflowServerEditable = false; editable = false;
        tell('原保存操作已成功，但本页草稿已变化。编辑内容仍保留，请核对后重新读取。', true);
      }
    }
    if (['conflict','readback_mismatch','publication_mismatch'].includes(receipt.outcome)) {
      // Refreshing role capabilities must not lift a conflict/readback fence.
      workflowServerEditable = false; editable = false;
      tell(outcomes[receipt.outcome] + '。本地编辑仍保留，请核对后再选择重新读取。', true);
    } else if (!(receipt.outcome === 'saved' && !editable)) tell(outcomes[receipt.outcome] + (receipt.outcome === 'published' ? ' · 版本 ' + receipt.revision : ''));
    controls();
  }
  async function mutate(kind, path, payload, copy = null) {
    if (busy || pending()) return;
    const captured = epoch, newOperationId = operationId();
    const identity = {projectId:project,operationId:newOperationId,kind};
    if (kind === 'save') savedCopies.set(newOperationId, JSON.stringify(payload.document));
    if (copy) copyDrafts.set(newOperationId, copy);
    outstanding.set(project, identity); showOperation(identity); busy = true; controls();
    try {
      const data = await api(path, {method:'POST',body:JSON.stringify({operationId:newOperationId,...payload})});
      if (data.operationId !== newOperationId || data.kind !== kind) throw new Error('操作回执与请求不一致');
      await processOperation(data);
      return data;
    } catch (error) { if (captured === epoch) {
      if (error.notAdmitted) {
        outstanding.delete(project); savedCopies.delete(newOperationId); copyDrafts.delete(newOperationId);
        showOperation({...identity,status:'rejected',receipt:{outcome:'not_submitted'}});
        tell('修改未提交：' + error.message + '。请修正配置或等待准入恢复后，再主动提交。', true);
      } else tell('提交结果尚未确认：' + error.message + '。请只查询原操作，不重新提交。', true);
    } }
    finally { if (captured === epoch) { busy = false; controls(); } }
  }
  $('create-form').addEventListener('submit', async event => {
    event.preventDefault(); if (!project || !discard()) return;
    if (!$('workflow-name').value.trim()) { tell('请填写非空流程名称。', true); return; }
    await mutate('create',base() + '/workflows',{name:$('workflow-name').value.trim(),description:$('workflow-description').value});
    if (currentOperation?.receipt?.outcome === 'created' && currentOperation.projectId === project) {
      const id = currentOperation.receipt.workflowId; await listWorkflows(); if (id) await loadWorkflow(id);
    }
  });
  async function openCopiedDraft(operation) {
    const copy = copyDrafts.get(operation?.operationId);
    if (!copy || operation.projectId !== project || operation.kind !== 'create' || operation.status !== 'accepted' || operation.receipt?.outcome !== 'created') return;
    const id = operation.receipt.workflowId;
    if (typeof id !== 'string' || !id || id === copy.source) {
      outstanding.set(project, {projectId:project,operationId:operation.operationId,kind:'create'});
      showOperation({...operation,status:'uncertain',receipt:null});
      tell('副本身份无法核验，请只查询原创建操作。', true); return;
    }
    // Reopening is GET only, including after an uncertain create. Saving the
    // copied content is a separate, explicit use of the existing save action.
    const data = await loadWorkflow(id);
    if (!data || !token || operation.projectId !== project || workflow !== id) return;
    if (!data.draftEmpty || !editable) { copyDrafts.delete(operation.operationId); tell('新流程已有内容，已读取实际草稿，未覆盖。请核对原创建操作。', true); return; }
    documentValue = copy.document; dirty = true; selected = ''; render(); controls();
    copyDrafts.delete(operation.operationId);
    tell(copy.templateName ? '模板“' + copy.templateName + '”已载入，尚未保存到 Sim。请核对节点和角色后点击“保存草稿”。'
      : '副本内容已载入，尚未保存到 Sim。请核对后点击“保存草稿”；原流程与发布版本保持不变。');
    $('save').focus();
  }
  $('archive-workflow').addEventListener('click', async () => {
    if (!workflow || !editable || dirty || formDirty || busy || pending()) return;
    if (!confirm('归档会停用此流程的原生触发器，保留归档前审计。确定归档当前流程吗？')) return;
    await mutate('archive',base()+'/workflows/'+enc(workflow)+'/archive',{expectedStateSha256:digest});
  });
  async function readArchives(after = '') {
    if (!project || !token) return;
    const captured=epoch; busy=true; controls();
    try {
      const data=await api(base()+'/archives'+(after?'?after='+enc(after):''));
      if (captured !== epoch) return;
      if (!Array.isArray(data.archives)) throw new Error('归档列表响应无效');
      archivesNext=data.nextCursor || null; $('archive-list').replaceChildren();
      for (const row of data.archives) {
        const button=el('button',(row.name || row.workflowId)+' · '+(row.status==='accepted'?'已归档':'归档结果待核对'),'secondary archive-choice');
        button.type='button'; button.addEventListener('click',()=>readArchiveAudit(row.operationId)); $('archive-list').append(button);
      }
      if (!data.archives.length) $('archive-list').append(el('p','本页没有归档记录。','muted'));
    } catch(error) { if(captured===epoch) { archivesNext=null; tell('归档列表未能读取：'+error.message,true); } }
    finally { if(captured===epoch) { busy=false; controls(); } }
  }
  async function readArchiveAudit(operationId) {
    if (!project || busy) return;
    const captured=epoch; busy=true; controls();
    try {
      const data=await api(base()+'/archives/'+enc(operationId)); if(captured!==epoch) return;
      if (data.projectId!==project || data.operationId!==operationId || data.readOnly!==true || !data.snapshot
          || !Array.isArray(data.snapshot.versions) || !Array.isArray(data.snapshot.runs)) throw new Error('归档审计身份无法核验');
      archiveAudit=data; $('archive-audit').hidden=false; $('archive-reconcile').hidden=data.status==='accepted';
      $('archive-audit-heading').textContent='归档前审计 · '+(data.snapshot.detail?.name || data.snapshot.workflowId);
      $('archive-audit-summary').textContent=(data.status==='accepted'?'归档已确认。':'归档结果待核对。')+'以下为归档前接口可读取的记录，共'+data.snapshot.versions.length+'个版本、'+data.snapshot.runs.length+'次运行；只读，不恢复或执行流程。';
      $('archive-version-list').replaceChildren(...data.snapshot.versions.map(v=>el('p','版本 '+v.version+' · '+v.id)));
      $('archive-run-list').replaceChildren(...data.snapshot.runs.map(r=>el('p','运行 '+r.runId+' · '+({completed:'已完成',failed:'失败',cancelled:'已取消'}[r.status] || '状态待核对'))));
    } catch(error) { if(captured===epoch) { archiveAudit=null; $('archive-audit').hidden=true; tell('归档审计未能读取：'+error.message,true); } }
    finally { if(captured===epoch) { busy=false; controls(); } }
  }
  $('refresh-archives').addEventListener('click',()=>readArchives());
  $('archives-next').addEventListener('click',()=>{if(archivesNext&&!busy) readArchives(archivesNext);});
  $('archive-reconcile').addEventListener('click',async()=>{
    if(!archiveAudit || busy || archiveAudit.status==='accepted') return;
    const captured=epoch, operationId=archiveAudit.operationId; busy=true; controls();
    try {
      const data=await api(base()+'/archives/'+enc(operationId)+'/reconcile',{method:'POST'});
      if(captured!==epoch) return;
      await processOperation(data);
      await readArchiveAudit(operationId);
    } catch(error) { if(captured===epoch) tell('归档核对未完成，保持原记录：'+error.message,true); }
    finally { if(captured===epoch) {busy=false; controls();} }
  });
  $('archive-download').addEventListener('click',()=>{
    if(!archiveAudit || busy) return;
    const url=URL.createObjectURL(new Blob([JSON.stringify(archiveAudit,null,2)],{type:'application/json'}));
    const anchor=el('a'); anchor.href=url; anchor.download='workflow-archive-audit.json'; anchor.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  $('copy-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!project || !workflow || !editable || !documentValue || dirty || formDirty || busy || pending()) return;
    const name = $('copy-name').value.trim();
    if (!name || name.length > 255) { tell('请填写 1–255 个字符的副本名称。', true); return; }
    const copy = {source:workflow, document:JSON.parse(JSON.stringify(documentValue))};
    const operation = await mutate('create',base() + '/workflows',{name,description:workflowDescription},copy);
    await openCopiedDraft(operation);
  });
  $('template-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!project || !rolesReady || busy || pending() || !discard()) return;
    const template = templateCatalog.find(item=>item.id === $('template-choice').value);
    const name = $('template-name').value.trim();
    if (!name || name.length > 255) { tell('请填写 1–255 个字符的流程名称。',true); return; }
    try {
      if (!template) throw new Error('请选择可用模板。');
      const mapping = Object.fromEntries(template.slots.map(slot=>[slot.id,$('template-role-' + slot.id).value]));
      const copy = {source:workflow,templateName:template.name,
        document:templates.instantiate(template.id,mapping,roles,uid('tpl'))};
      // discard() above explicitly settled unsaved source edits. Clear that
      // local source now so an unknown create can later reopen only its target.
      clearDraft();
      const operation = await mutate('create',base() + '/workflows',{name,description:template.description},copy);
      await openCopiedDraft(operation);
    } catch (error) { tell(error.message,true); }
  });
  function resetBatch() {
    $('batch-form').reset(); $('batch-details').open = false;
    $('batch-input-hint').textContent = '每行一项，空行忽略；保留非空行的原始文字。';
  }
  function syncBatch(blocked) {
    const signature = JSON.stringify(roles.map(role => [role.roleBindingId,role.details?.displayName]));
    if (signature !== batchRoleSignature) {
      batchRoleSignature = signature; const previous = $('batch-role').value;
      $('batch-role').replaceChildren(new Option('请选择既有角色',''),
        ...roles.map(role => new Option(role.details?.displayName || role.roleBindingId,role.roleBindingId)));
      $('batch-role').value = roles.some(role => role.roleBindingId === previous) ? previous : '';
    }
    const disabled = !project || blocked || !rolesReady || !outputSchemaAvailable('task-result');
    for (const id of ['batch-name','batch-role','batch-instruction','batch-policy','batch-mode','batch-items','batch-submit']) $(id).disabled = disabled;
  }
  $('batch-mode').addEventListener('change',() => {
    $('batch-input-hint').textContent = $('batch-mode').value === 'lines'
      ? '每行一项，空行忽略；保留非空行的原始文字。'
      : '填写 2–20 项的 JSON 数组；每项可为对象、数组或标量。较长标识请用字符串，数值须有限且在安全范围内。';
  });
  function finiteBatchJson(key,value) {
    if (typeof value === 'number' && (!Number.isFinite(value) || Math.abs(value) > Number.MAX_SAFE_INTEGER)) {
      throw new Error('数据中的数字须有限且不超出安全范围；长编号请写成字符串。');
    }
    return value;
  }
  function batchBytes(value) {
    // Match the deployment's ASCII JSON size envelope; field ordering changes
    // neither this estimate nor the persisted item meaning. Server rechecks.
    return JSON.stringify(value,finiteBatchJson).replace(/[^\x00-\x7e]/g,
      character => '\\u' + character.charCodeAt(0).toString(16).padStart(4,'0')).length;
  }
  function validBatchPreview(data, body, currentProject) {
    if (!data || data.source !== 'validated_static_batch' || data.projectId !== currentProject
        || data.recipeId !== body.recipeId || !/^[a-f0-9]{64}$/.test(data.recipeSha256 || '')
        || !data.document || !Array.isArray(data.document.nodes) || !Array.isArray(data.document.edges)
        || !supportedDocument(data.document) || !Array.isArray(data.items) || data.items.length !== body.items.length
        || data.document.nodes.length !== body.items.length + 4) return false;
    const ids = new Set(), itemIds = new Set();
    for (const [index,item] of data.items.entries()) {
      if (!item || item.index !== index + 1 || typeof item.itemId !== 'string' || !/^item_[a-f0-9]{24}$/.test(item.itemId)
          || typeof item.nodeId !== 'string' || !/^item_[a-f0-9]{32}$/.test(item.nodeId)
          || ids.has(item.nodeId) || itemIds.has(item.itemId) || !/^[a-f0-9]{64}$/.test(item.inputSha256 || '')) return false;
      ids.add(item.nodeId); itemIds.add(item.itemId);
      const node = data.document.nodes.find(candidate => candidate.id === item.nodeId);
      if (!node || node.type !== 'agent' || node.config.roleBindingId !== body.roleBindingId
          || node.config.memoryPolicyId !== 'native-role' || node.config.outputSchemaId !== 'task-result'
          || node.config.allowFailureBranch !== true || node.config.requireMemoryCommit !== false) return false;
    }
    const fork = data.document.nodes.filter(node => node.type === 'parallel'), join = data.document.nodes.filter(node => node.type === 'join');
    return fork.length === 1 && join.length === 1 && fork[0].config.maxConcurrency === body.items.length
      && Array.isArray(fork[0].config.branches) && fork[0].config.branches.length === ids.size
      && new Set(fork[0].config.branches).size === ids.size && fork[0].config.branches.every(id => ids.has(id))
      && fork[0].config.join === join[0].id && join[0].config.parallelId === fork[0].id
      && join[0].config.policy === body.policy && Array.isArray(join[0].config.requiredBranches) && join[0].config.requiredBranches.length === 0;
  }
  $('batch-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!project || !rolesReady || busy || pending() || nodeDrag || edgeDrag || !outputSchemaAvailable('task-result')) return;
    const name = $('batch-name').value.trim(), instruction = $('batch-instruction').value;
    const roleBindingId = $('batch-role').value, policy = $('batch-policy').value;
    const captured = epoch, currentProject = project;
    let body;
    try {
      if (!name || name.length > 255 || !instruction.trim() || instruction.length > 2000) throw new Error('请填写流程名称和不超过2000字符的共享任务说明。');
      if (!roles.some(role => role.roleBindingId === roleBindingId)) throw new Error('请明确选择本项目既有角色。');
      const input = $('batch-items').value;
      const items = $('batch-mode').value === 'lines' ? input.split(/\r?\n/).filter(line => line.trim()) : JSON.parse(input);
      if (!Array.isArray(items) || items.length < 2 || items.length > 20) throw new Error('请提供2–20项数据。');
      if (items.some(item => batchBytes(item) > 1200)) throw new Error('单项数据过长，每项规范JSON上限1200字节，请缩短。');
      body = {recipeId:uid('batch'),roleBindingId,instruction,items,policy};
      if (batchBytes(body) > 64000) throw new Error('批量配方过长，请缩短数据和任务说明。');
      if (!token || JSON.stringify(body).includes(token)) throw new Error('数据含当前编辑凭证，请移除后再创建。');
      if (!discard()) return;
    } catch (error) { tell(error instanceof SyntaxError ? 'JSON格式无效，请提供一个完整数组。' : error.message,true); return; }
    let preview;
    busy = true; controls(); tell('正在校验批量数据和角色，尚未创建或运行流程…');
    try {
      preview = await api(base() + '/batches/preview',{method:'POST',body:JSON.stringify(body,finiteBatchJson)});
      if (captured !== epoch || currentProject !== project) return;
      if (!validBatchPreview(preview,body,currentProject)) throw new Error('批量配方响应的项目、逐项身份或图结构无法核对。');
    } catch (error) { if(captured === epoch) tell('批量配方未创建：'+error.message,true); return; }
    finally { if(captured === epoch) { busy=false; controls(); } }
    if (captured !== epoch || currentProject !== project || pending()) return;
    const copy = {source:workflow,templateName:'静态批量处理',document:preview.document};
    clearDraft();
    const operation = await mutate('create',base() + '/workflows',
      {name,description:`静态批量 ${body.items.length} 项；${policy === 'all_success' ? '全部独立验收通过' : '允许部分失败且至少一项独立验收通过'}；配方 ${body.recipeId}`},copy);
    await openCopiedDraft(operation);
  });
  $('save').addEventListener('click', async () => {
    if (formDirty) { tell('请先应用节点配置。', true); return; }
    const error = validateGraph(); if (error) { tell(error, true); return; }
    await mutate('save',base() + '/workflows/' + enc(workflow) + '/save',{expectedStateSha256:digest,document:documentValue});
  });
  $('publish').addEventListener('click', () => {
    if (formDirty) { tell('请先应用节点配置。', true); return; }
    const error = validateGraph(); if (error) { tell(error, true); return; }
    mutate('publish',base() + '/workflows/' + enc(workflow) + '/publish',{expectedStateSha256:digest});
  });
  $('observe').addEventListener('click', async () => {
    if (!currentOperation || busy) return;
    const captured = epoch, identity = currentOperation; busy = true; controls();
    try {
      const data = await api(base() + '/operations/' + enc(identity.operationId) + '?kind=' + enc(identity.kind));
      if (data.operationId !== identity.operationId || data.kind !== identity.kind) throw new Error('操作身份不一致');
      await processOperation(data);
      if (copyDrafts.has(data.operationId) && !dirty && !formDirty) await openCopiedDraft(data);
    } catch (error) { if (captured === epoch) tell('尚未核对原操作：' + error.message + '。保持原编号，不重新提交。', true); }
    finally { if (captured === epoch) { busy = false; controls(); } }
  });
  function validateGraph() {
    if (!documentValue) return '请先读取草稿。';
    const {nodes,edges} = documentValue, starts = nodes.filter(n => n.type === 'start');
    if (nodes.length < 2 || nodes.length > 200) return '流程需要 2–200 个节点。';
    if (!supportedDocument(documentValue)) return '此图包含无法安全识别的固定子流程结构，已保留只读保护。';
    if (subworkflowGroups().length && !subworkflowCapability()) return '当前后台未声明单层子流程编辑能力；已保留原图，不能保存或发布。';
    if (!nodes.some(n => n.type === 'end')) return '流程需要至少一个结束节点。';
    if (new Set(nodes.map(n => n.name.toLowerCase())).size !== nodes.length) return '节点名称不能重复，请修改名称。';
    if (starts.length !== 1) return '流程需要且只能有一个开始节点。';
    const loops=nodes.filter(n=>n.type==='loop'), bodies=loopBodyIds();
    if(loops.length && nodes.some(n=>['parallel','join'].includes(n.type))) return '静态并行与有限循环暂不能混用，请保留原图并分别搭建。';
    const parallelError=validateParallel(nodes,edges); if(parallelError) return parallelError;
    if(loops.length>1) return '当前最多支持一个有限循环，不支持嵌套循环。';
    for(const loop of loops) {
      const body=nodes.find(n=>n.id===loop.config.body&&n.type==='agent');
      if(!body) return '有限循环的原循环体已失效，请重新选择 Agent。';
      if(!Number.isInteger(loop.config.maxIterations)||loop.config.maxIterations<1||loop.config.maxIterations>3) return '有限循环次数必须为 1–3 的整数。';
      if(body.config.allowFailureBranch!==true) return '循环体 Agent 必须明确开启允许失败结果进入后续分支。';
      const incoming=edges.filter(e=>e.target===body.id);
      if(incoming.length!==1||incoming[0].source!==loop.id||incoming[0].branch!=='body') return '循环体只允许从所属有限循环的循环体出口进入。';
    }
    for (const node of nodes) {
      const outgoing = edges.filter(e => e.source === node.id);
      if (node.type === 'condition') {
        const branches = [...node.config.rules.map(r => r.id),'else'];
        if (!node.config.rules.length || outgoing.length !== branches.length || branches.some(b => outgoing.filter(e => e.branch === b).length !== 1)) return '条件节点“' + node.name + '”的每条规则与否则分支都需要一条连线。';
      } else if(node.type==='parallel') {
        if(outgoing.length!==node.config.branches.length) return '并行分发必须准确连接每一个已选择的分支。';
      } else if(node.type==='loop') {
        if(outgoing.length!==2||outgoing.filter(e=>e.branch==='body'&&e.target===node.config.body).length!==1||outgoing.filter(e=>e.branch==='done'&&e.target!==node.config.body).length!==1) return '有限循环需要一条指向所选 Agent 的循环体连线，以及一条完成连线。';
      } else if(bodies.has(node.id)) { if(outgoing.length) return '循环体 Agent 不能有普通出线，请从有限循环的完成出口连接后续节点。';
      } else if (outgoing.length !== (node.type === 'end' ? 0 : 1)) return '节点“' + node.name + '”' + (node.type === 'end' ? '不能有出线。' : '需要且只能有一条出线。');
    }
    const visited = new Set(), active = new Set(); let cycle = false;
    function visit(id) { if (active.has(id)) { cycle = true; return; } if (visited.has(id)) return; active.add(id); visited.add(id); edges.filter(e => e.source === id).forEach(e => visit(e.target)); active.delete(id); }
    visit(starts[0].id);
    if(cycle) return '当前不支持循环连线。';
    if(visited.size !== nodes.length) return '存在无法从开始节点到达的节点，请补全连线。';
    for (const node of nodes.filter(n => n.type === 'human')) {
      if (!node.config || typeof node.config !== 'object' || Array.isArray(node.config)
          || node.config.requiredBoolean !== 'approved') return '人工确认节点配置无效，请保留原图并核对确认字段。';
      const configKeys = Object.keys(node.config);
      if (configKeys.some(key => !['requiredBoolean','approvalTtlSeconds','question'].includes(key))) return '人工确认节点包含未知配置，已保留原字段；请核对后再保存。';
      if (Object.hasOwn(node.config, 'approvalTtlSeconds')) {
        if (!humanApprovalTtlSupported) return '当前后台未声明人工确认有效期能力；已有配置已保留，请升级后台后再保存或发布。';
        if (!validApprovalTtl(node.config.approvalTtlSeconds)) return '人工确认有效期必须是 1–604800 秒的严格整数。';
      }
      if (Object.hasOwn(node.config, 'question')) {
        if (!humanApprovalQuestionSupported) return '当前后台未声明确认事项能力；已有内容已保留，请升级后台后再保存或发布。';
        if (!validApprovalQuestion(node.config.question)) return '确认事项须为去除首尾空白后非空、最多 2000 字且不含 NUL 的文本。';
      }
    }
    for(const node of nodes.filter(n => n.type === 'agent')) {
      if(!rolesReady || !roles.some(r => r.roleBindingId === node.config.roleBindingId)) return 'Agent 节点的角色绑定未核验，请刷新并核对。';
      if(!node.config.instruction.trim()) return '请填写 Agent 任务模板。';
      if(!outputSchemaAvailable(node.config.outputSchemaId)) return 'Agent 结果格式未由当前后端声明或本地不支持，请明确选择有效格式。';
      if(customOutputSchema(node.config.outputSchemaId) && !validDeclaredFields(node.config.outputSchemaId,node.config.outputFields)) return 'Agent 自定义结果字段无效，须有 1–32 个字段、唯一名称、合法类型且至少一项必填。';
      if(!customOutputSchema(node.config.outputSchemaId) && Object.hasOwn(node.config,'outputFields')) return '非自定义字段格式不能包含 outputFields，请在节点表单中明确勾选丢弃并应用。';
      if(Object.hasOwn(node.config,'limits') && !nodeLimitsSupported) return '当前后台未声明节点限额能力；已有限额已保留，请升级后台后再保存或发布。';
      if(Object.hasOwn(node.config,'limits') && !validNodeLimits(node.config.limits)) return 'Agent 节点限额无效，请核对时限和实际提交次数上限。';
      const source = nodes.find(n => n.id === node.config.inputFrom.nodeId);
      const sourceGroup=source&&subworkflowGroupFor(source.id), consumerGroup=subworkflowGroupFor(node.id);
      if(!source || !canSupply(source,node.id) || (sourceGroup&&sourceGroup.callId!==consumerGroup?.callId&&source.type!=='subflow_output')
        || node.config.inputFrom.field !== (source.type==='start'||source.type==='subflow_input'?'input':'output')) return 'Agent 输入来源失效，请重新选择。';
      const reachable=new Set(), queue=[starts[0].id];
      while(queue.length) { const id=queue.pop(); if(id===source.id||reachable.has(id)) continue; reachable.add(id); flowEdges().filter(e=>e.source===id).forEach(e=>queue.push(e.target)); }
      if(reachable.has(node.id)) return 'Agent“'+node.name+'”的输入来源不是所有路径的必经节点，请调整连线。';
    }
    for (const node of nodes) {
      if (node.type === 'condition') for (const rule of node.config.rules) {
        if (typeof rule.value === 'number' && !validConditionNumber(rule.value)) return '条件节点“' + node.name + '”：比较值超出安全数字范围，请将大整数编号改用文本比较。';
        const source = nodes.find(item => item.id === rule.source);
        const fieldError = customConditionFieldError(source,rule.field,rule.operator,rule.value);
        if (fieldError) return '条件节点“' + node.name + '”：' + fieldError;
      }
      const references = node.type === 'condition' ? node.config.rules.filter(r => Object.hasOwn(r,'source')).map(r => r.source) : node.type === 'end' && Object.hasOwn(node.config,'resultFrom') ? [node.config.resultFrom] : [];
      if(references.some(source => !(node.type==='condition'&&subworkflowInputSourceValid(source,node.id))&&!resultSourceValid(source,node.id,node.type==='condition'))) return '节点“'+node.name+'”引用的结果来源必须是所有路径必经的前序节点。';
    }
    return '';
  }
  function validateParallel(nodes,edges) {
    const forks=nodes.filter(n=>n.type==='parallel'), joins=nodes.filter(n=>n.type==='join'), used=new Set();
    if(forks.length>maxStaticParallelGroups||joins.length>maxStaticParallelGroups) return '当前后台最多支持 '+maxStaticParallelGroups+' 组静态并行与结果汇合，请保留超限原图并核对角色能力。';
    for(const fork of forks) {
      const {branches,join,maxConcurrency}=fork.config;
      if(!Array.isArray(branches)||branches.length<2||branches.length>20||new Set(branches).size!==branches.length) return '并行分发需要 2–20 个不同的 Agent 分支。';
      if(!Number.isInteger(maxConcurrency)||maxConcurrency<branches.length||maxConcurrency>20) return '最大并发数须为分支数至 20 的整数。';
      const partner=joins.find(n=>n.id===join&&n.config.parallelId===fork.id);
      if(!partner||joins.filter(n=>n.config.parallelId===fork.id).length!==1) return '并行分发与结果汇合必须一一配对，请核对原节点标识。';
      const outgoing=edges.filter(e=>e.source===fork.id), incoming=edges.filter(e=>e.target===partner.id);
      if(outgoing.length!==branches.length||incoming.length!==branches.length) return '每个并行分支须准确连接「并行分发 → Agent → 结果汇合」，不能旁路。';
      for(const id of branches) {
        const branch=nodes.find(n=>n.id===id&&n.type==='agent');
        if(!branch) return '并行分发的原分支已失效，请明确选择有效 Agent。';
        if(used.has(id)) return '同一个 Agent 不能归属多个并行分发。'; used.add(id);
        if(branch.config.allowFailureBranch!==true) return '并行分支 Agent 须自行开启允许已确认失败结果进入后续分支。';
        const ins=edges.filter(e=>e.target===id), outs=edges.filter(e=>e.source===id);
        if(ins.length!==1||ins[0].source!==fork.id||ins[0].branch||outs.length!==1||outs[0].target!==partner.id||outs[0].branch) return '每个并行 Agent 只允许一个入口和出口，须连接配对的分发与汇合。';
      }
      const {policy,requiredBranches}=partner.config;
      if(!['all_success','allow_partial','required_success'].includes(policy)||!Array.isArray(requiredBranches)||new Set(requiredBranches).size!==requiredBranches.length||requiredBranches.some(id=>!branches.includes(id))||(policy==='required_success'?requiredBranches.length===0:requiredBranches.length!==0)) return '汇合策略的必需分支须为配对分支的非空子集，其他策略须为空。';
    }
    if(joins.some(join=>!forks.some(fork=>fork.id===join.config.parallelId&&fork.config.join===join.id))) return '结果汇合的原并行分发已失效，请明确配对。';
    return '';
  }
  function branchOptions() {
    $('edge-branch').replaceChildren();
    const node = documentValue?.nodes.find(n => n.id === $('edge-source').value);
    if (node?.type === 'condition') {
      node.config.rules.forEach((r,i) => $('edge-branch').add(new Option('规则 ' + (i+1) + '：' + r.field, r.id)));
      $('edge-branch').add(new Option('否则（else）', 'else'));
    } else if(node?.type==='loop') { $('edge-branch').add(new Option('循环体','body')); $('edge-branch').add(new Option('完成','done')); } else $('edge-branch').add(new Option('默认', ''));
  }
  $('edge-source').addEventListener('change', branchOptions);
  function canvasPoint(event) {
    const matrix = $('canvas').getScreenCTM();
    if (!matrix) return null;
    const point = $('canvas').createSVGPoint(); point.x = event.clientX; point.y = event.clientY;
    return point.matrixTransform(matrix.inverse());
  }
  function changeCanvasScale(factor) {
    if (!documentValue || nodeDrag) return;
    const box = $('canvas').viewBox.baseVal;
    const previous = canvasView?.scale || 1, scale = Math.max(0.25, Math.min(4, previous * factor));
    const ratio = previous / scale, width = box.width * ratio, height = box.height * ratio;
    canvasView = {x:box.x + (box.width-width)/2, y:box.y + (box.height-height)/2, width, height, scale};
    $('canvas-scale').textContent = Math.round(scale*100) + '%'; render();
  }
  $('canvas-in').addEventListener('click', () => changeCanvasScale(1.25));
  $('canvas-out').addEventListener('click', () => changeCanvasScale(0.8));
  $('canvas-fit').addEventListener('click', () => {
    if (nodeDrag) return;
    canvasView = null; $('canvas-scale').textContent = '100%'; render();
  });
  $('canvas-focus').addEventListener('click', () => {
    const node = documentValue?.nodes.find(n => n.id === selected); if (!node || nodeDrag) return;
    const box = $('canvas').viewBox.baseVal;
    canvasView = {x:node.position.x+85-box.width/2,y:node.position.y+32-box.height/2,
      width:box.width,height:box.height,scale:canvasView?.scale || 1}; render();
  });
  $('canvas').addEventListener('pointerdown', event => {
    if (event.target.closest('.flow-output')) { startEdgeDrag(event); return; }
    const group = event.target.closest('[data-node-id]');
    if (edgeDrag || !group || event.button !== 0 || event.isPrimary === false || nodeDrag || busy || pending() || !editable) return;
    if (formDirty) { tell('请先应用或放弃节点配置，再拖动节点。', true); return; }
    const node = documentValue?.nodes.find(n => n.id === group.dataset.nodeId);
    const point = canvasPoint(event); if (!node || !point) return;
    event.preventDefault();
    const box = $('canvas').viewBox.baseVal;
    canvasView = {x:box.x,y:box.y,width:box.width,height:box.height,scale:canvasView?.scale || 1};
    selectNode(node.id);
    nodeDrag = {id:node.id,pointer:event.pointerId,epoch,start:point,position:{...node.position},dirty,moved:false};
    $('canvas').setPointerCapture(event.pointerId); $('canvas').classList.add('dragging');
    controls();
  });
  $('canvas').addEventListener('pointermove', event => {
    if (edgeDrag) { moveEdgeDrag(event); return; }
    const drag = nodeDrag;
    if (!drag || event.pointerId !== drag.pointer || drag.epoch !== epoch) return;
    const node = documentValue?.nodes.find(n => n.id === drag.id), point = canvasPoint(event);
    if (!node || !point) return;
    const x = Math.round(Math.max(0, Math.min(3000, drag.position.x+point.x-drag.start.x)));
    const y = Math.round(Math.max(0, Math.min(3000, drag.position.y+point.y-drag.start.y)));
    if (x === node.position.x && y === node.position.y) return;
    node.position = {x,y}; drag.moved = true;
    $('node-x').value = x; $('node-y').value = y; render();
  });
  function finishNodeDrag(commit) {
    const drag = nodeDrag; if (!drag) return;
    nodeDrag = null;
    const node = drag.epoch === epoch && documentValue?.nodes.find(n => n.id === drag.id);
    if (node) {
      if (!commit) node.position = drag.position;
      dirty = drag.dirty || (commit && (node.position.x !== drag.position.x || node.position.y !== drag.position.y));
      $('node-x').value = node.position.x; $('node-y').value = node.position.y;
      if (drag.moved) ignoreClickUntil = Date.now()+500;
    }
    $('canvas').classList.remove('dragging');
    if ($('canvas').hasPointerCapture(drag.pointer)) $('canvas').releasePointerCapture(drag.pointer);
    if (node) { render(); controls(); }
  }
  $('canvas').addEventListener('pointerup', event => { if (edgeDrag?.pointer === event.pointerId) finishEdgeDrag(true, event); else if (nodeDrag?.pointer === event.pointerId) finishNodeDrag(true); });
  function cancelCanvasPointer(event) {
    if (nodeDrag?.pointer === event.pointerId) finishNodeDrag(false);
    if (edgeDrag?.pointer === event.pointerId) finishEdgeDrag(false);
  }
  $('canvas').addEventListener('pointercancel', cancelCanvasPointer);
  $('canvas').addEventListener('lostpointercapture', cancelCanvasPointer);
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && (nodeDrag || edgeDrag)) { event.preventDefault(); finishNodeDrag(false); finishEdgeDrag(false); }
  });
  function startEdgeDrag(event) {
    if (event.button !== 0 || event.isPrimary === false || nodeDrag || edgeDrag || busy || pending() || !editable) return;
    if (formDirty) { tell('请先应用或放弃节点配置，再拖拽连线。',true); return; }
    const source = event.target.closest('[data-node-id]')?.dataset.nodeId;
    const node = documentValue?.nodes.find(n=>n.id===source); if (!node) return;
    event.preventDefault(); event.stopPropagation();
    if ($('edge-source').value !== source) { $('edge-source').value = source; branchOptions(); }
    edgeDrag = {source,branch:$('edge-branch').value,pointer:event.pointerId,epoch};
    $('canvas').setPointerCapture(event.pointerId);
    tell('正在从“'+node.name+'”的“'+($('edge-branch').selectedOptions[0]?.textContent || '默认')+'”出口连线，拖到终点节点后释放；Escape取消。'); controls();
  }
  function moveEdgeDrag(event) {
    if (edgeDrag?.pointer !== event.pointerId || edgeDrag.epoch !== epoch) return;
    const point = canvasPoint(event), node = documentValue?.nodes.find(n=>n.id===edgeDrag.source); if (!point || !node) return;
    $('canvas').querySelector('.edge-preview')?.remove();
    $('canvas').append(svg('path',{class:'edge-preview',d:`M ${node.position.x+170} ${node.position.y+32} L ${point.x} ${point.y}`}));
  }
  function finishEdgeDrag(commit, event = null) {
    const drag = edgeDrag; if (!drag) return;
    edgeDrag = null; $('canvas').querySelector('.edge-preview')?.remove();
    if ($('canvas').hasPointerCapture(drag.pointer)) $('canvas').releasePointerCapture(drag.pointer);
    ignoreClickUntil = Date.now()+500; controls();
    if (!commit || drag.epoch !== epoch) return;
    const target = document.elementFromPoint(event.clientX,event.clientY)?.closest('[data-node-id]')?.dataset.nodeId;
    if (!target) { tell('未连接到终点节点，原连线保持。'); return; } connectNodes(drag.source,target,drag.branch);
  }
  $('drag-node').addEventListener('dragstart', event => {
    if (!documentValue || !editable || busy || pending() || nodeDrag || edgeDrag || formDirty) { event.preventDefault(); return; }
    paletteDrag = {epoch,workflow,project,type:$('node-type').value,nonce:operationId()};
    event.dataTransfer.setData('application/x-agent-line-node',paletteDrag.nonce); event.dataTransfer.effectAllowed='copy';
  });
  $('drag-node').addEventListener('dragend', () => { paletteDrag = null; });
  $('canvas').addEventListener('dragover', event => {
    if (paletteDrag?.epoch === epoch && !busy && !pending()) { event.preventDefault(); event.dataTransfer.dropEffect='copy'; }
  });
  $('canvas').addEventListener('drop', event => {
    const drag = paletteDrag; paletteDrag = null;
    if (!drag || drag.epoch !== epoch || drag.workflow !== workflow || drag.project !== project
      || event.dataTransfer.getData('application/x-agent-line-node') !== drag.nonce) return;
    event.preventDefault(); const point=canvasPoint(event); if (!point) return;
    addNode(drag.type,{x:Math.round(Math.max(0,Math.min(3000,point.x-85))),y:Math.round(Math.max(0,Math.min(3000,point.y-32)))});
  });
  $('canvas-layout').addEventListener('click', () => {
    if (!documentValue || !editable || busy || pending() || nodeDrag || edgeDrag) return;
    if (formDirty) { tell('请先应用或放弃节点配置，再自动布局。',true); return; }
    try {
      const laidOut = globalThis.AgentLineLayout.layout(documentValue);
      if (JSON.stringify(laidOut.nodes.map(n=>n.position)) === JSON.stringify(documentValue.nodes.map(n=>n.position))) return;
      documentValue=laidOut; canvasView=null; $('canvas-scale').textContent='100%';
      if (selected) { const node=documentValue.nodes.find(n=>n.id===selected); $('node-x').value=node.position.x; $('node-y').value=node.position.y; }
      changed(); tell('已自动整理节点位置，尚未保存；节点配置和连线保持。');
    } catch (error) { tell(error.message || '此图暂不能自动布局，请先核对节点和连线。',true); }
  });
  function svg(tag, attrs, text) { const n = document.createElementNS('http://www.w3.org/2000/svg', tag); for (const [k,v] of Object.entries(attrs)) n.setAttribute(k,String(v)); if (text !== undefined) n.textContent = text; return n; }
  function render() {
    $('canvas').replaceChildren(); $('node-list').replaceChildren(); $('edge-list').replaceChildren();
    if (!documentValue) return;
    const nodes = documentValue.nodes, canvas = $('canvas');
    const width = Math.max(900,...nodes.map(n => n.position.x + 220)), height = Math.max(440,...nodes.map(n => n.position.y + 120));
    if (canvasView) canvas.setAttribute('viewBox', `${canvasView.x} ${canvasView.y} ${canvasView.width} ${canvasView.height}`);
    else canvas.setAttribute('viewBox','0 0 ' + width + ' ' + height);
    const defs = svg('defs',{}), marker = svg('marker',{id:'arrow',viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:7,markerHeight:7,orient:'auto-start-reverse'});
    marker.append(svg('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'var(--muted)'})); defs.append(marker); canvas.append(defs);
    for (const edge of documentValue.edges) {
      const a = nodes.find(n => n.id === edge.source), b = nodes.find(n => n.id === edge.target); if (!a || !b) continue;
      const x = a.position.x+170, y = a.position.y+32, tx = b.position.x, ty = b.position.y+32;
      const siblings = documentValue.edges.filter(e=>e.source===edge.source && e.target===edge.target);
      const offset = (siblings.findIndex(e=>e.id===edge.id)-(siblings.length-1)/2)*36;
      canvas.append(svg('path',{d:`M ${x} ${y} C ${x+65} ${y+offset}, ${tx-65} ${ty+offset}, ${tx} ${ty}`,class:'flow-edge','marker-end':'url(#arrow)'}));
      const branch = edge.branch === 'body' ? '循环体' : edge.branch === 'done' ? '完成' : edge.branch === 'else' ? '否则' : edge.branch ? '规则 ' + ((a.config.rules||[]).findIndex(r => r.id === edge.branch)+1) : '';
      if (branch) canvas.append(svg('text',{x:(x+tx)/2,y:(y+ty)/2+offset*0.75-10,class:'edge-label'},branch));
      const row = el('div',undefined,'edge-row'); row.append(el('span',shownNodeName(a) + (branch ? ' · ' + branch : '') + ' → ' + shownNodeName(b)));
      const remove = el('button','删除连线','secondary'); remove.type='button'; remove.disabled=!editable||busy||pending()||!edgeEditAllowed(edge.source,edge.target); remove.addEventListener('click',() => { if(remove.disabled) return; documentValue.edges = documentValue.edges.filter(e => e.id !== edge.id); changed(); }); row.append(remove); $('edge-list').append(row);
    }
    for (const node of nodes) {
      const group = svg('g',{'data-node-id':node.id,class:'flow-node' + (selected === node.id ? ' selected' : ''),transform:`translate(${node.position.x} ${node.position.y})`,tabindex:0,role:'button','aria-label':labels[node.type]+'：'+node.name,'aria-pressed':String(selected===node.id)});
      const visibleName=shownNodeName(node);
      group.append(svg('rect',{width:170,height:64,rx:8}),svg('text',{x:14,y:24},visibleName.length>12?visibleName.slice(0,12)+'…':visibleName),svg('text',{x:14,y:46,class:'node-kind'},node.type==='loop' ? '有限循环 · 最多 '+node.config.maxIterations+' 次' : labels[node.type]));
      const nodeGroup=subworkflowGroupFor(node.id);
      if (node.type !== 'end' && !loopBodyIds().has(node.id) && (!nodeGroup||nodeGroup.exitId===node.id)) {
        const output = svg('circle', {cx:170,cy:32,r:9,class:'flow-output',tabindex:0,role:'button',
          'aria-label':'从“'+node.name+'”拖拽连线；按回车使用连线表单'});
        output.append(svg('title', {}, '拖到终点节点；条件与循环出口可在下方分支选择'));
        output.addEventListener('click', event => event.stopPropagation());
        output.addEventListener('keydown', event => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault(); event.stopPropagation(); $('edge-source').value = node.id; branchOptions(); $('edge-target').focus();
            tell('已选择连线起点，请选择分支和终点，再点击“添加连线”。');
          }
        }); group.append(output);
      }
      group.addEventListener('click',() => { if (Date.now() >= ignoreClickUntil) selectNode(node.id); }); group.addEventListener('keydown',e => { if (e.key==='Enter'||e.key===' ') { e.preventDefault(); selectNode(node.id); } }); canvas.append(group);
      const button=el('button',visibleName,'secondary'); button.type='button'; button.setAttribute('aria-pressed',String(selected===node.id)); button.addEventListener('click',()=>selectNode(node.id)); $('node-list').append(button);
    }
    const oldSource=$('edge-source').value, oldTarget=$('edge-target').value;
    for (const [id,filter] of [['edge-source',n=>n.type!=='end'&&!loopBodyIds().has(n.id)&&(!protectedSubworkflowIds().has(n.id)||subworkflowGroupFor(n.id)?.exitId===n.id)],['edge-target',n=>n.type!=='start'&&(!protectedSubworkflowIds().has(n.id)||subworkflowGroupFor(n.id)?.entryId===n.id)]]) { $(id).replaceChildren(); nodes.filter(filter).forEach(n=>$(id).add(new Option(shownNodeName(n),n.id))); }
    if (nodes.some(n=>n.id===oldSource&&n.type!=='end'&&!loopBodyIds().has(n.id)&&(!protectedSubworkflowIds().has(n.id)||subworkflowGroupFor(n.id)?.exitId===n.id))) $('edge-source').value=oldSource;
    if (nodes.some(n=>n.id===oldTarget&&n.type!=='start'&&(!protectedSubworkflowIds().has(n.id)||subworkflowGroupFor(n.id)?.entryId===n.id))) $('edge-target').value=oldTarget;
    branchOptions(); const currentNode=nodes.find(n=>n.id===selected);
    if(currentNode?.type==='parallel') fillParallel(currentNode,true);
    if(currentNode?.type==='join') fillJoin(currentNode,true);
    if(currentNode?.type==='loop') fillLoopBodies(currentNode,$('loop-body').value||currentNode.config.body);
    if(currentNode?.type==='condition') $('rule-list').querySelectorAll('[data-field=source]').forEach(select=>{ fillResultSources(select,selected,select.value,true); const suggestions=select.closest('.rule')?.querySelector('[data-field="field-suggestion"]'); if(suggestions) fillConditionFieldSuggestions(suggestions,select.value); });
    if(currentNode?.type==='end') fillResultSources($('end-result-source'),selected,$('end-result-source').value||currentNode.config.resultFrom||'');
    const agentNode = nodes.find(n => n.id === selected && n.type === 'agent'); if(agentNode) fillAgentSources(agentNode, $('agent-source').value || agentNode.config.inputFrom.nodeId); controls();
  }
  function changed() { dirty=true; render(); }
  function addNode(type, position = null) {
    if (!documentValue || !editable || busy || pending() || nodeDrag || edgeDrag || !Object.hasOwn(labels,type)) return;
    if(type==='subflow_input'||type==='subflow_output') { tell('子流程入口和返回节点只能由固定版本子流程操作加入。',true); return; }
    if (documentValue.nodes.length >= 200) { tell('最多支持200个节点，请先整理现有节点。',true); return; }
    if(['parallel','join'].includes(type)&&documentValue.nodes.filter(n=>n.type===type).length>=maxStaticParallelGroups) { tell('当前后台最多支持 '+maxStaticParallelGroups+' 组静态并行与结果汇合，已达到上限。',true); return; }
    if((type==='loop'&&documentValue.nodes.some(n=>['parallel','join'].includes(n.type))) || (['parallel','join'].includes(type)&&documentValue.nodes.some(n=>n.type==='loop'))) { tell('静态并行与有限循环暂不能混用，请保留原图并分别搭建。',true); return; }
    if(type==='loop'&&documentValue.nodes.some(n=>n.type==='loop')) { tell('当前只支持一个有限循环，不支持嵌套。',true); return; }
    if (type==='start'&&documentValue.nodes.some(n=>n.type==='start')) { tell('只能有一个开始节点。',true); return; }
    if (formDirty&&!confirm('放弃尚未应用的节点配置？')) return;
    const node={id:uid(type),type,name:labels[type],position:position || {x:100+(documentValue.nodes.length%3)*230,y:Math.min(3000,80+Math.floor(documentValue.nodes.length/3)*120)},config:type==='condition'?{rules:[{id:uid('rule'),field:'input.approved',operator:'eq',value:true}]}:type==='end'?{output:{}}:type==='human'?{requiredBoolean:'approved'}:{}};
    let suffix=2; while(documentValue.nodes.some(n=>n.name.toLowerCase()===node.name.toLowerCase())) node.name=labels[type]+suffix++;
    if(type==='parallel') node.config={branches:[],join:'',maxConcurrency:2};
    if(type==='join') node.config={parallelId:'',policy:'all_success',requiredBranches:[]};
    if(type==='loop') node.config={body:'',maxIterations:3};
    if(type==='agent') node.config={roleBindingId:'',instruction:'',inputFrom:{nodeId:documentValue.nodes.find(n=>n.type==='start')?.id || '',field:'input'},memoryPolicyId:'native-role',outputSchemaId:'task-result',requireMemoryCommit:false,allowFailureBranch:false};
    documentValue.nodes.push(node); formDirty=false; changed(); selectNode(node.id);
  }
  function connectNodes(source,target,branch) {
    if (!documentValue || !editable || busy || pending() || nodeDrag || edgeDrag) return;
    if (!documentValue.nodes.some(n=>n.id===source && n.type!=='end') || !documentValue.nodes.some(n=>n.id===target && n.type!=='start')) { tell('连线起点或终点不可用。',true); return; }
    if (!source||!target||source===target) { tell('请选择不同的起点与终点。',true); return; }
    if (!edgeEditAllowed(source,target)) { tell('固定子流程及入口、返回连线不能单独修改。请通过子流程面板整体移除并重新预览插入。',true); return; }
    if(loopBodyIds().has(source)) { tell('循环体不能有普通出线，请使用有限循环的完成出口。',true); return; }
    if (documentValue.edges.some(e=>e.source===source&&(e.branch||'')===branch && (documentValue.nodes.find(n=>n.id===source)?.type!=='parallel' || e.target===target))) { tell('此出口已有连线，请先删除原连线。',true); return; }
    documentValue.edges.push({id:uid('edge'),source,target,...(branch?{branch}:{})}); changed();
  }
  $('connect').addEventListener('click',()=>connectNodes($('edge-source').value,$('edge-target').value,$('edge-branch').value));
  function ruleRow(rule) {
    const row=el('div',undefined,'rule'); row.dataset.ruleId=rule.id; row.append(el('p','条件规则'));
    function field(title, control, name) { const label=el('label',title); control.dataset.field=name; label.append(control); row.append(label); return control; }
    const source=field('条件数据来源',el('select'),'source'); fillResultSources(source,selected,rule.source||'',true);
    const fieldInput=field('字段路径（可手填）',el('input'),'field'); fieldInput.value=rule.field; fieldInput.required=true;
    const fieldSuggestion=field('可用字段建议',el('select'),'field-suggestion');
    fillConditionFieldSuggestions(fieldSuggestion,source.value);
    fieldSuggestion.addEventListener('change',()=>{
      if(fieldSuggestion.value) {
        fieldInput.value=fieldSuggestion.value;
        const selectedSource=documentValue.nodes.find(n=>n.id===source.value);
        const custom=selectedSource?.type==='agent' && selectedSource.config.outputSchemaId===nestedOutputSchema && validNestedFields(selectedSource.config.outputFields)
          ? window.AgentLineStructuredFields.scalarFields(selectedSource.config.outputFields).find(item=>fieldSuggestion.value==='result.output.result.'+item.path.join('.'))
          : selectedSource?.type==='agent'&&selectedSource.config.outputSchemaId===fieldsOutputSchema
            ? selectedSource.config.outputFields?.fields?.find(item=>fieldSuggestion.value==='result.output.result.'+item.name) : null;
        if(custom) {
          const valueType=custom.type==='integer'||custom.type==='number'?'number':custom.type;
          type.value=valueType;
          if(valueType==='boolean'&&!['true','false'].includes(value.value)) value.value='true';
          if(valueType==='number'&&(!value.value.trim()||!Number.isFinite(Number(value.value)))) value.value='0';
        }
        formDirty=true; controls();
      }
    });
    const hint=el('p','按来源选择建议字段会填入上方路径；也可手动填写高级路径。开始输入仍需填写 input.xxx。','muted'); row.append(hint);

    const operator=field('比较方式',el('select'),'operator'); [['eq','等于'],['ne','不等于'],['gt','大于'],['gte','大于等于'],['lt','小于'],['lte','小于等于']].forEach(([v,t])=>operator.add(new Option(t,v))); operator.value=rule.operator;
    const type=field('值类型',el('select'),'type'); [['string','文本'],['number','数字'],['boolean','布尔值'],['null','空值']].forEach(([v,t])=>type.add(new Option(t,v))); type.value=rule.value===null?'null':typeof rule.value;
    const value=field('比较值（布尔值填写 true 或 false）',el('input'),'value'); value.value=rule.value===null?'':String(rule.value);
    function syncSource(changed=false) {
      const human=documentValue.nodes.some(n=>n.id===source.value&&n.type==='human');
      fieldInput.readOnly=human; type.disabled=human;
      [...operator.options].forEach(option=>{option.disabled=human&&!['eq','ne'].includes(option.value);});
      fillConditionFieldSuggestions(fieldSuggestion,source.value);
      hint.textContent=human?'人工确认是否批准：true 表示批准，false 表示拒绝。字段固定为 result.approval.approved，仅支持等于或不等于。':'选择字段建议会填入路径，也可手动填写高级路径；开始输入仍需填写 input.xxx。';
      if(human && changed) { fieldInput.value='result.approval.approved'; type.value='boolean'; if(!['true','false'].includes(value.value)) value.value='true'; if(!['eq','ne'].includes(operator.value)) operator.value='eq'; }
      fieldInput.placeholder=source.value?'result.verificationStatus':'input.xxx';
    }
    source.addEventListener('change',()=>syncSource(true)); syncSource();
    const remove=el('button','删除规则','secondary'); remove.type='button'; remove.addEventListener('click',()=>{row.remove(); formDirty=true; controls();}); row.append(remove); $('rule-list').append(row);
  }
  function selectNode(id) {
    if (busy||pending()||!editable) return;
    if (formDirty&&!confirm('放弃尚未应用的节点配置？')) return;
    selected=id; formDirty=false; const node=documentValue.nodes.find(n=>n.id===id); if (!node) return;
    $('node-form').hidden=false; $('selection-empty').hidden=true; $('node-name').value=shownNodeName(node); $('node-id').textContent=node.id;
    $('node-x').value=node.position.x; $('node-y').value=node.position.y;
    $('parallel-config').hidden=node.type!=='parallel'; $('join-config').hidden=node.type!=='join';
    if(node.type==='parallel') { fillParallel(node); $('parallel-cap').value=node.config.maxConcurrency; }
    if(node.type==='join') { $('join-policy').value=node.config.policy; fillJoin(node); }
    $('loop-config').hidden=node.type!=='loop';
    if(node.type==='loop') { fillLoopBodies(node,node.config.body); $('loop-max').value=node.config.maxIterations; } else { $('loop-body').replaceChildren(); $('loop-body-state').textContent=''; }
    $('agent-config').hidden=node.type!=='agent';
    $('agent-batch-item').hidden = node.type !== 'agent' || !Object.hasOwn(node.config, 'batchItem');
    $('agent-batch-item').open = false;
    const batchText = node.type === 'agent' && Object.hasOwn(node.config, 'batchItem')
      ? JSON.stringify(node.config.batchItem, null, 2) : '';
    $('agent-batch-data').textContent = token ? batchText.split(token).join('[凭证已隐藏]') : batchText;
    if(node.type==='agent') { loadOutputFields(node); fillAgentRoles(node.config.roleBindingId); fillAgentOutputSchemas(node.config.outputSchemaId); fillAgentSources(node,node.config.inputFrom.nodeId); $('agent-instruction').value=node.config.instruction; $('agent-require-memory').checked=node.config.requireMemoryCommit; $('agent-allow-failure').checked=node.config.allowFailureBranch; updateAgentLimits(node,true); } else { clearRoleSummary(); nestedFieldsTouched=false; $('agent-nested-fields').value=''; $('agent-nested-fields-panel').hidden=true; $('agent-instruction').value=''; $('agent-role').replaceChildren(); $('agent-output-schema').replaceChildren(); $('agent-source').replaceChildren(); $('agent-custom-limits').checked=false; $('agent-max-execution-seconds').value='3600'; $('agent-max-business-attempts').value='3'; $('agent-limits-config').hidden=true; $('agent-limits-hint').textContent='限额仅适用于 Agent 节点。'; }
    $('condition-config').hidden=node.type!=='condition'; $('end-config').hidden=node.type!=='end'; $('human-config').hidden=node.type!=='human';
    if (node.type === 'human') { updateHumanApprovalTtl(node,true); updateHumanApprovalQuestion(node,true); }
    else { $('human-approval-ttl-enabled').checked=false; $('human-approval-ttl-seconds').value='3600'; $('human-approval-ttl-config').hidden=true; $('human-approval-ttl-hint').textContent='此设置仅适用于人工确认节点。'; $('human-approval-question').value=''; $('human-approval-question-hint').textContent='此设置仅适用于人工确认节点。'; humanApprovalQuestionIssue=false; humanApprovalQuestionEdited=false; }
    $('rule-list').replaceChildren(); if(node.type==='condition') node.config.rules.forEach(ruleRow);
    $('end-mode').value=node.type==='end'&&Object.hasOwn(node.config,'resultFrom')?'result':'literal';
    if(node.type==='end') fillResultSources($('end-result-source'),node.id,node.config.resultFrom||'');
    $('end-output').value=node.type==='end'&&Object.hasOwn(node.config,'output')?JSON.stringify(node.config.output,null,2):'{}'; endMode();
    let readonly=$('node-form').querySelector('[data-subworkflow-readonly-note]');
    if (!readonly) { readonly=el('p','固定版本子流程成员不可单独编辑、删除或改内部连线。可拖动节点调整画布位置，或在子流程面板整体移除。','muted'); readonly.dataset.subworkflowReadonlyNote='true'; $('node-form').prepend(readonly); }
    readonly.hidden=!protectedSubworkflowIds().has(node.id); render();
  }
  $('node-form').addEventListener('input',()=>{captureOutputFieldsDraft();formDirty=true;controls();});
  $('node-form').addEventListener('change',()=>{captureOutputFieldsDraft();formDirty=true;controls();});
  $('add-rule').addEventListener('click',()=>{ruleRow({id:uid('rule'),field:'input.value',operator:'eq',value:''});formDirty=true;controls();});
  $('node-form').addEventListener('submit',event=>{
    event.preventDefault(); const node=documentValue.nodes.find(n=>n.id===selected); if(!node) return;
    if(protectedSubworkflowIds().has(node.id)) { tell('固定版本子流程成员为只读；可调整位置或整体移除。',true); return; }
    try {
      const name=$('node-name').value.trim(),x=Number($('node-x').value),y=Number($('node-y').value);
      if(!name||!Number.isFinite(x)||!Number.isFinite(y)||x<0||y<0||x>3000||y>3000) throw new Error('请填写名称及 0–3000 范围内的有效位置。');
      if (!/^[\p{L}\p{N}_]+$/u.test(name)) throw new Error('节点名称只能包含文字、数字或下划线。');
      if (documentValue.nodes.some(n=>n.id!==node.id&&n.name.toLowerCase()===name.toLowerCase())) throw new Error('节点名称不能重复。');
      let config=node.config;
      if(node.type==='parallel') {
        const branches=selectedValues('parallel-branches'), join=$('parallel-join').value, maxConcurrency=Number($('parallel-cap').value);
        if(branches.length<2||branches.length>20||branches.some(id=>!documentValue.nodes.some(n=>n.id===id&&n.type==='agent'))) throw new Error('请选择 2–20 个有效的不同 Agent 分支。');
        if(!Number.isInteger(maxConcurrency)||maxConcurrency<branches.length||maxConcurrency>20) throw new Error('最大并发数须为分支数至 20 的整数。');
        if(!documentValue.nodes.some(n=>n.id===join&&n.type==='join')) throw new Error('请选择有效的配对汇合节点。');
        config={branches,join,maxConcurrency};
      }
      if(node.type==='join') {
        const parallelId=$('join-parallel').value, policy=$('join-policy').value, fork=documentValue.nodes.find(n=>n.id===parallelId&&n.type==='parallel');
        const requiredBranches=policy==='required_success'?selectedValues('join-required'):[];
        if(!fork) throw new Error('请选择有效的配对并行分发节点。');
        if(policy==='required_success'&&(!requiredBranches.length||requiredBranches.some(id=>!fork.config.branches.includes(id)))) throw new Error('至少选择一个配对分支作为必须成功的分支。');
        config={parallelId,policy,requiredBranches};
      }
      if(node.type==='loop') {
        const body=documentValue.nodes.find(n=>n.id===$('loop-body').value&&n.type==='agent');
        const maxIterations=Number($('loop-max').value);
        if(!body) throw new Error('请选择有效的既有 Agent 作为循环体。');
        if(!Number.isInteger(maxIterations)||maxIterations<1||maxIterations>3) throw new Error('最多执行次数必须是 1–3 的整数。');
        if(body.config.allowFailureBranch!==true) throw new Error('请先在循环体 Agent 配置中明确开启允许失败分支。');
        config={body:body.id,maxIterations};
      }
      if(node.type==='human') {
        if (Object.hasOwn(node.config, 'approvalTtlSeconds') && !humanApprovalTtlSupported) throw new Error('当前后台未声明人工确认有效期能力；已有配置已保留，升级后台后才能应用。');
        if (Object.hasOwn(node.config, 'question') && !humanApprovalQuestionSupported) throw new Error('当前后台未声明确认事项能力；已有内容已保留，升级后台后才能应用。');
        if (humanApprovalQuestionIssue && !humanApprovalQuestionEdited) throw new Error('原确认事项无效，已保留原内容；请明确修改或清空后再应用。');
        config={...node.config,requiredBoolean:'approved'};
        if ($('human-approval-ttl-enabled').checked) {
          if (!humanApprovalTtlSupported) throw new Error('当前后台未声明人工确认有效期能力，不能新增此设置。');
          const seconds=$('human-approval-ttl-seconds').value;
          if (!/^\d+$/.test(seconds)) throw new Error('人工确认有效期必须填写严格整数秒。');
          const ttl=Number(seconds);
          if (!validApprovalTtl(ttl)) throw new Error('人工确认有效期须为 1–604800 秒的整数。');
          config.approvalTtlSeconds=ttl;
        } else delete config.approvalTtlSeconds;
        const question=$('human-approval-question').value.trim();
        if (question) {
          if (!humanApprovalQuestionSupported) throw new Error('当前后台未声明确认事项能力，不能新增此内容。');
          if (!validApprovalQuestion(question)) throw new Error('确认事项须为去除首尾空白后非空、最多 2000 字且不含 NUL 的文本。');
          config.question=question;
        } else delete config.question;
        humanApprovalQuestionIssue=false; humanApprovalQuestionEdited=false;
      }
      if(node.type==='agent') {
        const role=roles.find(r=>r.roleBindingId===$('agent-role').value), instruction=$('agent-instruction').value;
        if(!rolesReady||!role) throw new Error('请选择当前项目已配置的有效角色绑定。');
        if(!instruction.trim()||instruction.length>4000) throw new Error('任务模板需要 1–4000 个字符。');
        const source=documentValue.nodes.find(n=>n.id===$('agent-source').value);
        if(!source||!canSupply(source,node.id)||(source.type!=='start'&&source.type!=='subflow_input'&&!resultSourceValid(source.id,node.id))) throw new Error('请选择开始输入、子流程输入或已连接到此节点的前置结果。');
        const outputSchemaId=$('agent-output-schema').value;
        if(!outputSchemaAvailable(outputSchemaId)) throw new Error('请选择当前后端声明且本地支持的结果格式。');
        captureOutputFieldsDraft();
        let outputFields;
        if(outputSchemaId===nestedOutputSchema) {
          if(!window.AgentLineStructuredFields) throw new Error('嵌套结构组件尚未加载，请保留原稿后重新打开页面。');
          outputFields=window.AgentLineStructuredFields.parseDefinition($('agent-nested-fields').value);
        } else if(outputSchemaId===fieldsOutputSchema) {
          if(outputFieldsIssue) throw new Error(outputFieldsIssue+'请明确重建字段定义后再应用。');
          outputFields=outputFieldsConfig();
          if(!validOutputFields(outputFields)) throw new Error('自定义字段需有 1–32 项、唯一合法名称、有效类型、必填布尔值且至少一项必填。');
        } else if((outputFieldsOriginalPresent||outputFieldsDraft.length||nestedFieldsTouched)&&!outputFieldsDiscard) {
          throw new Error('切换为其他结果格式前，请明确勾选“应用时丢弃自定义字段”。');
        }
        const hasLimits=$('agent-custom-limits').checked;
        if(hasLimits && !nodeLimitsSupported) throw new Error('当前后台未声明节点限额能力，不能新增或应用限额；请升级后台。');
        if(Object.hasOwn(node.config,'limits') && !nodeLimitsSupported) throw new Error('当前后台未声明节点限额能力；已有配置已保留，升级后台后才能应用。');
        if(hasLimits && Object.hasOwn(node.config,'limits') && !validNodeLimits(node.config.limits)) throw new Error('原节点限额包含无效或未知字段，已保留原配置；请核对后再编辑。');
        let limits;
        if(hasLimits) {
          const seconds=$('agent-max-execution-seconds').value, attempts=$('agent-max-business-attempts').value;
          if(!/^\d+$/.test(seconds)||!/^\d+$/.test(attempts)) throw new Error('时限和实际提交次数必须填写严格整数。');
          limits={maxExecutionSeconds:Number(seconds),maxBusinessAttempts:Number(attempts)};
          if(!validNodeLimits(limits)) throw new Error('时限须为 1–86400 秒，实际提交次数上限须为 1–100 的整数。');
        }
        config={roleBindingId:role.roleBindingId,instruction,inputFrom:{nodeId:source.id,field:source.type==='start'||source.type==='subflow_input'?'input':'output'},memoryPolicyId:'native-role',outputSchemaId,requireMemoryCommit:$('agent-require-memory').checked,allowFailureBranch:$('agent-allow-failure').checked,...(outputFields?{outputFields}:{}),...(hasLimits?{limits}:{}),...(Object.hasOwn(node.config,'batchItem')?{batchItem:JSON.parse(JSON.stringify(node.config.batchItem))}:{})};
      }
      if(node.type==='condition') {
        const rules=[...$('rule-list').children].map(row=>{
          const read=name=>row.querySelector('[data-field="'+name+'"]').value;
          const type=read('type'),raw=read('value'); let value=raw;
          if(type==='number') { if(!raw.trim()||!validConditionNumber(Number(raw))) throw new Error('比较值必须是有限且绝对值不超过9007199254740991的数字；大整数编号请改用文本比较。'); value=Number(raw); }
          if(type==='boolean') { if(!['true','false'].includes(raw)) throw new Error('布尔值请填写 true 或 false。'); value=raw==='true'; }
          if(type==='null') value=null;
          const source=read('source');
          const sourceNode=documentValue.nodes.find(n=>n.id===source);
          if(source && !subworkflowInputSourceValid(source,node.id) && !resultSourceValid(source,node.id,true)) throw new Error('条件来源必须是所有路径必经的前序结果，或此子流程组内的输入。');
          const human=documentValue.nodes.some(n=>n.id===source&&n.type==='human');
          const inputSource=sourceNode?.type==='subflow_input';
          if(human && (read('field')!=='result.approval.approved'||type!=='boolean'||!['eq','ne'].includes(read('operator')))) throw new Error('人工确认条件仅支持 result.approval.approved、布尔值和等于或不等于。');
          const fieldPattern=human ? /^result\.approval\.approved$/ : inputSource || !source ? /^input(?:\.[A-Za-z_][A-Za-z0-9_]*)+$/ : /^result\.(?:verificationStatus|output(?:\.[A-Za-z_][A-Za-z0-9_]*)+)$/;
          if(!fieldPattern.test(read('field'))) throw new Error(source?(inputSource?'子流程输入字段应为 input.xxx。':'Agent 字段应为 result.verificationStatus 或 result.output.xxx。'):'开始字段应为 input.xxx。');
          if(read('field').split('.').some(part=>['__proto__','constructor','prototype'].includes(part))) throw new Error('此字段路径不受支持。');
          if(['gt','gte','lt','lte'].includes(read('operator'))&&type!=='number') throw new Error('大小比较需要数字类型。');
          const customError=customConditionFieldError(documentValue.nodes.find(n=>n.id===source),read('field'),read('operator'),value);
          if(customError) throw new Error(customError);
          return {id:row.dataset.ruleId,operator:read('operator'),field:read('field'),value,...(source?{source}:{})};
        });
        if(!rules.length||rules.length>50) throw new Error('条件节点需要 1–50 条规则。'); config={rules};
      }
      if(node.type==='end' && $('end-mode').value==='result') { const source=$('end-result-source').value; if(!resultSourceValid(source,node.id)) throw new Error('结束结果来源必须是所有路径必经的前序 Agent 或结果汇合。'); config={resultFrom:source}; }
      if(node.type==='end' && $('end-mode').value==='literal') { const output=JSON.parse($('end-output').value); if(!output||typeof output!=='object'||Array.isArray(output)) throw new Error('结束输出必须是 JSON 对象。'); JSON.stringify(output, (key,value)=>{if(typeof value==='number'&&!Number.isFinite(value)) throw new Error('输出中的数字必须有限。');return value;});
        const encoded=JSON.stringify(output); if(encoded.includes('<')||encoded.includes('{{')) throw new Error('结束输出暂不支持模板标记或小于号。'); config={output}; }
      Object.assign(node,{name,position:{x,y},config});
      if(node.type==='agent') loadOutputFields(node);
      if(node.type==='condition') documentValue.edges=documentValue.edges.filter(e=>e.source!==node.id||e.branch==='else'||node.config.rules.some(r=>r.id===e.branch));
      formDirty=false; changed(); tell('节点配置已应用到本地草稿，请保存到 Sim。');
    } catch(error) { tell('节点配置未应用：'+error.message,true); }
  });
  $('delete-node').addEventListener('click',()=>{
    if(protectedSubworkflowIds().has(selected)) { tell('固定版本子流程成员不能单独删除，请使用子流程面板整体移除。',true); return; }
    if(subworkflowGroups().some(group=>group.incoming.source===selected||group.outgoing.target===selected||group.entry.config.inputFrom.nodeId===selected)) {
      tell('此节点仍是固定子流程的入口来源、返回连接或输入来源。请先整体移除相关子流程，再删除节点并重新插入。',true); return;
    }
    documentValue.nodes=documentValue.nodes.filter(n=>n.id!==selected); documentValue.edges=documentValue.edges.filter(e=>e.source!==selected&&e.target!==selected);
    selected='';formDirty=false;$('node-form').hidden=true;$('selection-empty').hidden=false;changed();
  });
  window.addEventListener('beforeunload',event=>{if(dirty||formDirty||outstanding.size){event.preventDefault();event.returnValue='';}});
  subworkflows = window.AgentLineSubworkflows?.create({
    container: $('subworkflow-editor'),
    request: (path, options={}) => {
      if (!token || !project || typeof path!=='string' || !path.startsWith('/')) throw new Error('请先授权并选择项目。');
      const body=options.body;
      const encoded=typeof body==='string'?body:body===undefined?'':JSON.stringify(body);
      if (encoded.includes(token)) throw new Error('子流程请求内容含当前编辑凭证，请移除后再操作。');
      return api(base()+path,{...options,...(body!==undefined&&typeof body!=='string'?{body:encoded}:{})});
    },
    scope: () => token&&project&&workflow ? {projectId:project,workflowId:workflow,epoch} : null,
    isCurrent: subworkflowScopeCurrent,
    getDraft: () => ({document:documentValue,stateSha256:digest,protectedIds:[...protectedSubworkflowIds()],entryIds:subworkflowGroups().map(group=>group.entryId),outputIds:subworkflowGroups().map(group=>group.exitId)}),
    canInsert: canInsertSubworkflow,
    canRemove: canRemoveSubworkflow,
    supported: () => subworkflowCapability(),
    safe: value => token?String(value??'').split(token).join('[凭证已隐藏]'):String(value??''),
    applyDocument: (doc,{scope,parentStateSha256,originalDocument}={}) => {
      if (!subworkflowCapability() || !subworkflowScopeCurrent(scope) || parentStateSha256!==digest
        || JSON.stringify(documentValue)!==JSON.stringify(originalDocument) || !canInsertSubworkflow() || !supportedDocument(doc)) return false;
      documentValue=doc; selected=''; formDirty=false; canvasView=null; $('canvas-scale').textContent='100%';
      $('node-form').hidden=true; $('selection-empty').hidden=false;
      dirty=true; render(); tell('子流程已加入本地父草稿。核对后点击保存草稿；尚未发布或运行。');
      return true;
    },
    removeGroup: callId => {
      if (!subworkflowCapability() || !canRemoveSubworkflow()) return false;
      const group=subworkflowGroups().find(item=>item.callId===callId);
      if (!group || group.incoming.source===group.entryId || group.outgoing.target===group.exitId) return false;
      if (subworkflowGroups().some(other=>other.callId!==callId&&group.members.has(other.entry.config.inputFrom.nodeId))) {
        throw new Error('另一个固定子流程仍使用此组的返回值。请先整体移除依赖它的子流程，再调整来源并重新加入。');
      }
      const retained={...group.incoming,target:group.outgoing.target};
      documentValue.nodes=documentValue.nodes.filter(node=>!group.members.has(node.id));
      documentValue.edges=documentValue.edges.filter(edge=>!group.members.has(edge.source)&&!group.members.has(edge.target));
      documentValue.edges.push(retained);
      if (group.members.has(selected)) { selected=''; $('node-form').hidden=true; $('selection-empty').hidden=false; }
      formDirty=false; changed(); tell('已从本地草稿整体移除固定版本子流程。其他失效的数据引用已保留，请手动修复后保存。');
      return true;
    }
  });
  portableTemplates = window.AgentLinePortableTemplates?.create({
    context: () => ({epoch, projectId:project, workflowId:workflow, document:documentValue,
      editable, formDirty, rolesReady, roles,
      blocked:busy || pending() || !!nodeDrag || !!edgeDrag}),
    request: (path, body) => {
      const text = JSON.stringify(body);
      if (!token || !project) throw new Error('请先授权并选择项目。');
      if (text.includes(token)) throw new Error('节点内容含当前编辑凭证，请移除后再操作模板。');
      return api(base() + path, {method:'POST',body:text});
    },
    applyDocument: document => {
      if (!token || !workflow || !editable || busy || pending() || nodeDrag || edgeDrag
          || formDirty || !supportedDocument(document) || !discard()) return false;
      documentValue = document; selected = ''; formDirty = false;
      canvasView = null; $('canvas-scale').textContent = '100%';
      $('node-form').hidden = true; $('selection-empty').hidden = false;
      changed(); tell('JSON 模板已载入本地画布。核对后点击保存草稿；尚未发布或运行。');
      return true;
    }
  });
  versionHistory = window.AgentLineVersionHistory?.create({
    container: $('version-history'), request: api,
    scope: () => token && project && workflow ? {projectId:project, workflowId:workflow, epoch} : null,
    isCurrent: target => Boolean(token) && target.epoch === epoch && target.projectId === project && target.workflowId === workflow,
    getDraft: () => documentValue,
    safe: value => token ? String(value ?? '').split(token).join('[凭证已隐藏]') : String(value ?? ''),
  });
  nativeBinding = window.AgentLineNativeBinding?.create({
    container: $('native-binding'),
    roles: () => roles,
    token: () => token,
    scope: () => token && project && workflow && digest ? {projectId:project, workflowId:workflow, epoch, digest} : null,
    publish: body => mutate('publish', base() + '/workflows/' + enc(workflow) + '/publish', body),
  });
  controls();
})();
