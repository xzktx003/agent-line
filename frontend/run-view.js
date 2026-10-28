'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const svgNS = 'http://www.w3.org/2000/svg';
  const safeId = (value, max) => typeof value === 'string' && value.length > 0
    && value.length <= max && /^[A-Za-z0-9_-]+$/.test(value);
  const plain = value => !!value && typeof value === 'object' && !Array.isArray(value);
  const unixSeconds = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 8_640_000_000_000;
  const nodeTypes = new Set(['start','end','agent','condition','human','loop','parallel','join','subflow_input','subflow_output']);
  const typeLabels = {start:'开始',end:'结束',agent:'Agent',condition:'条件',human:'人工',loop:'循环',parallel:'并行',join:'汇合',subflow_input:'子流程输入',subflow_output:'子流程返回'};
  const executionLabels = {registered:'已登记',submitting:'提交中',uncertain:'待核对',accepted:'已接收',succeeded:'执行成功',failed:'执行失败',cancel_requested:'申请取消',canceled:'已取消'};
  const verificationLabels = {pending:'等待验证',passed:'验证通过',failed:'验证失败',canceled:'已取消验收'};
  const memoryLabels = {not_requested:'未请求回写',pending:'等待核对',committed:'已写入',failed:'写回失败',needs_review:'需要审核',deleting:'删除处理中',deleted:'已删除'};
  const nativeStatusLabels = {queued:'排队中',pending:'等待中',running:'运行中（原生记录）',paused:'已暂停',redacting:'结果脱敏处理中',completed:'原生执行结束',failed:'原生执行失败',cancelled:'原生已取消'};
  function workflowPausedAnswer(status, paused) {
    if (!Object.hasOwn(nativeStatusLabels, status)) return null;
    if (paused != null) return status === 'paused' ? '是' : null;
    if (status === 'paused') return '是，本次记录未提供具体暂停点';
    return '否';
  }
  function codexActivityAnswer(value) {
    if (value !== 'unknown') return null;
    return '未知。本次记录没有 Codex 进程活动证据，不根据流程是否暂停推断';
  }
  function fixedRunContext(identity) {
    if (!plain(identity) || !safeId(identity.projectId,160)
        || !Number.isSafeInteger(identity.revision) || identity.revision < 1) return [];
    return [['项目', identity.projectId], ['固定流程版本', String(identity.revision)],
      ['操作者', '本次提交回执没有操作者字段']];
  }
  function connectionAnswer(state) {
    if (!plain(state) || state.phase === 'idle') return '尚未读取运行图。这不是 Codex 连接状态';
    if (state.phase === 'ok') return '最近一次运行图读取成功。这不表示连线已命中，也不表示 Codex 仍连接';
    if (state.phase === 'failed') return state.keptGraph === true
      ? '最近一次运行图读取失败。固定结构仍是上次成功读取，节点计数已隐藏。这不表示 Codex 已断开'
      : '最近一次运行图读取失败，没有可保留的运行图。这不表示 Codex 已断开';
    return null;
  }
  function nodeDetailAction(taskId, authorized) {
    const closed = {queries: false, canvasToolBodies: false};
    if (authorized !== true) return {action: 'needs_authorization', ...closed};
    if (typeof taskId !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(taskId)) return {action: 'ignore', ...closed};
    return {action: 'query_existing_detail', taskId, queries: true, canvasToolBodies: false};
  }
  function nodeDetailHint() {
    return '查看任务详情会读取原任务记录中的角色会话、Codex 执行标识、产物、测试证据和记忆追踪入口；画布不加载历史工具正文。';
  }
  function attemptRetention(tasks) {
    if (!Array.isArray(tasks) || tasks.length < 2) return {state: 'single', note: null};
    const ids = [];
    for (const task of tasks) {
      if (!plain(task) || typeof task.attemptId !== 'string' || !task.attemptId || task.attemptId.length > 256) {
        return {state: 'unverified', note: '尝试标识无法核对，不能把这些记录说成新尝试。'};
      }
      ids.push(task.attemptId);
    }
    if (new Set(ids).size < 2) return {state: 'same_attempt', note: '这些登记记录分别保留，不能据此判断已经产生了新的尝试。'};
    const failed = tasks.some(task => task.executionStatus === 'failed' || task.verificationStatus === 'failed');
    return failed
      ? {state: 'distinct_with_failure', note: '不同尝试分别保留。失败证据仍在对应任务详情中，不会被其他尝试覆盖。'}
      : {state: 'distinct', note: '不同尝试分别保留。新的尝试不会替换先前尝试的记录。'};
  }
  function rerunBoundaryNote() {
    return '重新运行会创建新的运行记录。请使用“以原输入准备新运行”，不要把本次运行当成可原地重跑。';
  }
  function runTaskBlockReason(task) {
    const reason = window.AgentLineActivity && window.AgentLineActivity.waitingReason;
    if (!plain(task) || typeof reason !== 'function') return null;
    const value = reason(task);
    return typeof value === 'string' && value.length > 0 && value.length <= 240 ? value : null;
  }
  function parameterAcceptance(payload, expected, redact) {
    const hide = typeof redact === 'function' ? redact : value => String(value ?? '');
    if (!plain(payload) || !plain(expected) || payload.source !== 'local_run_input' || payload.readOnly !== true
        || payload.executionSupported !== false || payload.bindingId !== expected.bindingId
        || payload.projectId !== expected.projectId || payload.workflowId !== expected.workflowId
        || payload.runId !== expected.runId || !/^[a-f0-9]{64}$/.test(payload.inputSha256 || '')
        || !plain(payload.input)) return {state: 'invalid'};
    let raw;
    try { raw = JSON.stringify(payload.input); }
    catch { return {state: 'invalid'}; }
    if (new TextEncoder().encode(raw).length > 64000) return {state: 'too_large'};
    const secret = value => {
      if (typeof value === 'string') return hide(value) !== value;
      if (Array.isArray(value)) return value.some(secret);
      return plain(value) && Object.keys(value).some(key => hide(key) !== key || secret(value[key]));
    };
    if (secret(payload.input) || hide(raw) !== raw) return {state: 'hidden'};
    return {state: 'visible', text: hide(JSON.stringify(payload.input, null, 2))};
  }

  function identityKey(value) {
    if (!plain(value) || !safeId(value.bindingId,160) || !safeId(value.runId,256)
      || (value.projectId !== undefined && value.projectId !== null && !safeId(value.projectId,160))) return null;
    return JSON.stringify([value.bindingId,value.runId,value.projectId ?? null]);
  }
  function keysDiffer(left, right) {
    const keys = ['bindingId','projectId','workflowId','runId','revision','graphSha256'];
    return !plain(left) || !plain(right) || keys.some(key => left[key] !== right[key]);
  }
  function validate(data, expected, source, cachedIdentity = null, cachedGraph = null) {
    const value = data?.identity, graph = source === 'fixed_publication_run_view' ? data?.graph : cachedGraph, coverage = data?.coverage;
    const identityKeys = ['bindingId','projectId','workflowId','runId','revision','graphSha256'];
    const identityMatches = cachedIdentity === null || (plain(value)
      && Object.keys(value).sort().join(',') === identityKeys.slice().sort().join(',')
      && identityKeys.every(key => value[key] === cachedIdentity[key]));
    if (!plain(data) || data.source !== source || data.readOnly !== true
      || !plain(value) || value.bindingId !== expected.bindingId || value.runId !== expected.runId
      || !safeId(value.bindingId,160) || !safeId(value.projectId,160) || !safeId(value.workflowId,160)
      || !Number.isSafeInteger(value.revision) || value.revision < 1
      || typeof value.graphSha256 !== 'string' || !/^[a-f0-9]{64}$/.test(value.graphSha256)
      || (expected.projectId !== undefined && expected.projectId !== null && value.projectId !== expected.projectId)
      || !identityMatches
      || (source === 'fixed_publication_run_view' && (!plain(graph) || !Array.isArray(graph.nodes) || !graph.nodes.length || graph.nodes.length > 200
        || !Array.isArray(graph.edges) || graph.edges.length > 1000))
      || (source === 'fixed_publication_run_state' && Object.hasOwn(data,'graph'))
      || !plain(graph)
      || !Array.isArray(data.tasks) || data.tasks.length > 500
      || !plain(coverage) || typeof coverage.complete !== 'boolean' || coverage.limit !== 500
      || data.tasks.length > coverage.limit || (!coverage.complete && data.tasks.length !== coverage.limit)
      || !unixSeconds(data.observedAt)
      || data.edgeExecution !== 'unknown' || data.nativeNodeExecution !== 'unknown') {
      throw new Error('运行图响应身份或结构无法核验。');
    }
    const nodeIds = new Set(), executionIds = new Set();
    for (const node of graph.nodes) {
      if (!plain(node) || !safeId(node.id,128) || nodeIds.has(node.id)
        || !safeId(node.executionNodeId,200) || executionIds.has(node.executionNodeId)
        || !nodeTypes.has(node.type) || typeof node.name !== 'string' || node.name.length > 255
        || !plain(node.position) || !Number.isFinite(node.position.x) || !Number.isFinite(node.position.y)
        || Math.abs(node.position.x) > 10000 || Math.abs(node.position.y) > 10000) {
        throw new Error('运行图包含无效节点或位置。');
      }
      nodeIds.add(node.id); executionIds.add(node.executionNodeId);
    }
    const edgeIds = new Set();
    for (const edge of graph.edges) {
      if (!plain(edge) || !safeId(edge.id,200) || edgeIds.has(edge.id)
        || !nodeIds.has(edge.source) || !nodeIds.has(edge.target)
        || (Object.hasOwn(edge,'branch') && (typeof edge.branch !== 'string' || edge.branch.length > 128))) {
        throw new Error('运行图包含无效连线引用。');
      }
      edgeIds.add(edge.id);
    }
    const taskIds = new Set();
    for (const task of data.tasks) {
      if (!plain(task) || !safeId(task.taskId,128) || taskIds.has(task.taskId)
        || !executionIds.has(task.nodeId) || typeof task.nodeExecutionId !== 'string' || !task.nodeExecutionId || task.nodeExecutionId.length > 256
        || typeof task.attemptId !== 'string' || !task.attemptId || task.attemptId.length > 256
        || !(task.roleAgentId === null || (typeof task.roleAgentId === 'string' && task.roleAgentId.length <= 256))
        || typeof task.executionStatus !== 'string' || !task.executionStatus || task.executionStatus.length > 128
        || typeof task.verificationStatus !== 'string' || !task.verificationStatus || task.verificationStatus.length > 128
        || typeof task.memoryStatus !== 'string' || !task.memoryStatus || task.memoryStatus.length > 128
        || typeof task.resourcesReleased !== 'boolean') {
        throw new Error('运行图包含无法归属到节点的任务记录。');
      }
      taskIds.add(task.taskId);
    }
    return data;
  }
  function validateNative(data, expected, cachedIdentity) {
    const keys = ['bindingId','projectId','workflowId','runId','revision','graphSha256'];
    const identity = data?.identity, native = data?.native, local = data?.local, paused = native?.paused;
    const optionalId = value => value === null || (typeof value === 'string' && value.length > 0
      && value.length <= 256 && value.trim() === value && !value.includes('\0'));
    const waitingForValues = new Set(['agent_result','business_input','scheduled_time','unknown']);
    const validPause = paused === null || (plain(paused)
      && (paused.kind === null || paused.kind === 'time' || paused.kind === 'human')
      && waitingForValues.has(paused.waitingFor)
      && (paused.kind === 'time' ? paused.waitingFor === 'scheduled_time'
        : paused.kind === 'human' ? paused.waitingFor !== 'scheduled_time' : paused.waitingFor === 'unknown')
      && optionalId(paused.contextId) && optionalId(paused.blockedOnBlockId)
      && unixSeconds(paused.pausedAt) && (paused.resumeAt === null || unixSeconds(paused.resumeAt))
      && Number.isSafeInteger(paused.pausePointCount) && paused.pausePointCount >= 0
      && Number.isSafeInteger(paused.resumedCount) && paused.resumedCount >= 0 && paused.resumedCount <= paused.pausePointCount
      && typeof paused.automaticResumeNeedsAttention === 'boolean');
    if (!plain(data) || data.source !== 'fixed_publication_run_native_summary' || data.readOnly !== true
      || !plain(identity) || Object.keys(identity).sort().join(',') !== keys.slice().sort().join(',')
      || !keys.every(key => identity[key] === cachedIdentity[key])
      || !unixSeconds(data.observedAt) || !plain(native)
      || !Object.hasOwn(nativeStatusLabels,native.status)
      || !(native.startedAt === null || unixSeconds(native.startedAt))
      || !(native.endedAt === null || unixSeconds(native.endedAt))
      || !(native.durationMs === null || (typeof native.durationMs === 'number' && Number.isFinite(native.durationMs) && native.durationMs >= 0))
      || !validPause || (paused !== null && native.status !== 'paused') || !plain(local) || !unixSeconds(local.observedAt)
      || !Number.isSafeInteger(local.taskCount) || local.taskCount < 0 || local.taskCount > 500
      || !Number.isSafeInteger(local.resourcesUnreleased) || local.resourcesUnreleased < 0 || local.resourcesUnreleased > local.taskCount
      || !plain(local.coverage) || typeof local.coverage.complete !== 'boolean' || local.coverage.limit !== 500
      || local.taskCount > local.coverage.limit || (!local.coverage.complete && local.taskCount !== 500)
      || data.codexActivity !== 'unknown' || data.nodeExecution !== 'unknown' || data.edgeExecution !== 'unknown') {
      throw new Error('原生流程状态身份或结构无法核验。');
    }
    return data;
  }
  function label(map, value) { return Object.hasOwn(map,value) ? map[value] : '状态待核对'; }
  function appendText(parent, tag, className, value) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = value;
    parent.append(element);
    return element;
  }
  function renderGraph(data, selectedExecutionNodeId, openTask, taskSection, taskList, taskMessage, onSelect) {
    const root = document.createElement('div');
    const graph = data.graph, tasks = data.tasks;
    const countsByNode = new Map();
    for (const task of tasks) {
      if (!countsByNode.has(task.nodeId)) countsByNode.set(task.nodeId, []);
      countsByNode.get(task.nodeId).push(task);
    }
    const xValues = graph.nodes.map(node => node.position.x), yValues = graph.nodes.map(node => node.position.y);
    const minX = Math.min(0,...xValues), maxX = Math.max(1,...xValues), minY = Math.min(0,...yValues), maxY = Math.max(1,...yValues);
    const width = Math.max(720, maxX-minX+240), height = Math.max(320, maxY-minY+180);
    const svg = document.createElementNS(svgNS,'svg');
    svg.setAttribute('viewBox',`0 0 ${width} ${height}`); svg.setAttribute('role','group');
    svg.setAttribute('aria-label','固定版本流程结构图；可用 Tab 选择节点');
    const definitions = document.createElementNS(svgNS,'defs');
    const arrow = document.createElementNS(svgNS,'marker');
    for (const [key,value] of Object.entries({id:'run-view-arrow',viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:6,markerHeight:6,orient:'auto'})) arrow.setAttribute(key,String(value));
    const arrowPath = document.createElementNS(svgNS,'path');
    arrowPath.setAttribute('d','M 0 0 L 10 5 L 0 10 z'); arrowPath.setAttribute('fill','var(--muted)');
    arrow.append(arrowPath); definitions.append(arrow); svg.append(definitions);
    let scale = 1;
    function setScale(value) {
      scale = Math.max(.1, Math.min(3, value));
      svg.setAttribute('width',String(width * scale)); svg.setAttribute('height',String(height * scale));
    }
    setScale(1);
    const zoom = document.createElement('div'); zoom.className = 'run-view-actions';
    for (const [name,action] of [['缩小',()=>setScale(scale/1.25)],['放大',()=>setScale(scale*1.25)],
      ['适合宽度',()=>setScale(Math.max(320,graphContainerWidth())/width)]]) {
      const control = appendText(zoom,'button','secondary',name); control.type='button'; control.addEventListener('click',action);
    }
    function graphContainerWidth() { return Math.max(320,(root.parentElement?.clientWidth || 664)-24); }
    const positions = new Map();
    for (const node of graph.nodes) positions.set(node.id,{x:node.position.x-minX+120,y:node.position.y-minY+80});
    for (const edge of graph.edges) {
      const from = positions.get(edge.source), to = positions.get(edge.target);
      const line = document.createElementNS(svgNS,'line');
      const horizontal = Math.abs(to.x-from.x) >= Math.abs(to.y-from.y);
      const direction = Math.sign(horizontal ? to.x-from.x : to.y-from.y) || 1;
      line.setAttribute('x1',String(from.x+(horizontal?90*direction:0))); line.setAttribute('y1',String(from.y+(horizontal?0:38*direction)));
      line.setAttribute('x2',String(to.x-(horizontal?90*direction:0))); line.setAttribute('y2',String(to.y-(horizontal?0:38*direction)));
      line.setAttribute('marker-end','url(#run-view-arrow)');
      line.setAttribute('class','run-view-edge'); svg.append(line);
      if (edge.branch) {
        const marker = document.createElementNS(svgNS,'text');
        marker.setAttribute('x',String((from.x+to.x)/2)); marker.setAttribute('y',String((from.y+to.y)/2-8));
        marker.setAttribute('class','run-view-branch'); marker.textContent = ({body:'循环体',done:'完成',else:'否则'})[edge.branch] || '条件出口'; svg.append(marker);
      }
    }
    const nodeButtons = new Map();
    function choose(node) {
      selectedExecutionNodeId = node.executionNodeId;
      for (const [id, group] of nodeButtons) {
        const active = id === selectedExecutionNodeId;
        group.setAttribute('aria-pressed',String(active));
        group.classList.toggle('selected',active);
      }
      renderNodeTasks(node, countsByNode.get(node.executionNodeId) || [], taskList, taskSection, taskMessage, openTask);
      onSelect(selectedExecutionNodeId);
    }
    for (const node of graph.nodes) {
      const point = positions.get(node.id), group = document.createElementNS(svgNS,'g');
      group.setAttribute('class','run-view-node'); group.setAttribute('tabindex','0'); group.setAttribute('role','button');
      group.dataset.executionNodeId = node.executionNodeId;
      group.setAttribute('aria-label',`${node.name || typeLabels[node.type]}；${(countsByNode.get(node.executionNodeId)||[]).length} 条已登记任务`);
      group.setAttribute('aria-pressed',String(node.executionNodeId === selectedExecutionNodeId));
      group.setAttribute('transform',`translate(${point.x} ${point.y})`);
      const rect = document.createElementNS(svgNS,'rect'); rect.setAttribute('x','-90'); rect.setAttribute('y','-38'); rect.setAttribute('width','180'); rect.setAttribute('height','76'); rect.setAttribute('rx','12');
      const title = document.createElementNS(svgNS,'text'); title.setAttribute('y','-8'); title.setAttribute('class','run-view-node-title');
      const name = node.name || typeLabels[node.type], letters = Array.from(name);
      title.textContent = letters.length > 12 ? letters.slice(0,12).join('')+'…' : name;
      const fullName = document.createElementNS(svgNS,'title'); fullName.textContent = name; group.append(fullName);
      const nodeTasks = countsByNode.get(node.executionNodeId) || [];
      const state = document.createElementNS(svgNS,'text'); state.setAttribute('y','17'); state.setAttribute('class','run-view-node-state');
      state.textContent = `已登记 ${nodeTasks.length} 条`;
      group.append(rect,title,state); group.addEventListener('click',()=>choose(node));
      group.addEventListener('keydown',event=>{ if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); choose(node); } });
      svg.append(group); nodeButtons.set(node.executionNodeId,group);
    }
    const caption = document.createElement('p'); caption.className = 'field-help run-view-caption';
    caption.textContent = data.coverage.complete ? `共 ${graph.nodes.length} 个定义节点，登记任务 ${tasks.length} 条。连线仅表示定义关系，不表示分支已命中。` : `任务记录最多展示 ${data.coverage.limit} 条，当前页已截断；登记数可能不完整。连线不表示分支已命中。`;
    root.append(zoom,caption,svg);
    if (selectedExecutionNodeId) {
      const node = graph.nodes.find(item=>item.executionNodeId===selectedExecutionNodeId);
      if (node) choose(node);
      else onSelect(null);
    } else {
      onSelect(null);
    }
    return root;
  }
  function renderNodeTasks(node, tasks, list, section, message, openTask) {
    section.hidden = false; list.replaceChildren();
    const tally = field => {
      const counts = new Map();
      for (const task of tasks) counts.set(task[field],(counts.get(task[field])||0)+1);
      const labels = field === 'executionStatus' ? executionLabels : field === 'verificationStatus' ? verificationLabels : memoryLabels;
      return [...counts].map(([value,count])=>`${count}条${labels[value] || '待核对'}`).join('、') || '0条';
    };
    const retention = attemptRetention(tasks);
    message.textContent = `${node.name || typeLabels[node.type]}：执行：${tally('executionStatus')}；验收：${tally('verificationStatus')}；记忆：${tally('memoryStatus')}。`
      + (tasks.length ? ' 全部已知尝试分别列出，不推断哪次是最终尝试。' : ' 此节点没有可显示的登记任务；这不能说明原生节点未运行。')
      + (retention.note ? ' ' + retention.note : '')
      + (tasks.length && typeof openTask === 'function' ? ' ' + nodeDetailHint() : '');
    for (const task of tasks) {
      const li = document.createElement('li'), row = document.createElement('div'); row.className='task-row run-view-task-row';
      appendText(row,'strong','',`执行 ${label(executionLabels,task.executionStatus)} · 验收 ${label(verificationLabels,task.verificationStatus)} · 记忆 ${label(memoryLabels,task.memoryStatus)}`);
      appendText(row,'span','task-context',`执行资源：${task.resourcesReleased ? '已确认退出' : '退出未确认'}${task.roleAgentId ? ` · Agent ${task.roleAgentId}` : ''}`);
      if (typeof task.attemptId === 'string' && task.attemptId) appendText(row,'span','task-context',`尝试 ${task.attemptId}`);
      const blockReason = runTaskBlockReason(task);
      if (blockReason) appendText(row,'span','task-context',`阻塞原因：${blockReason}`);
      const blockCategory = window.AgentLineActivity && window.AgentLineActivity.namedBlock && window.AgentLineActivity.namedBlock(task);
      if (blockCategory) appendText(row,'span','task-context',`阻塞类别：${blockCategory}`);
      const ids = document.createElement('details'); ids.className='task-row-identifiers';
      const summary = document.createElement('summary'); summary.textContent='查看任务标识';
      appendText(ids,'p','task-context',`任务 ${task.taskId} · 节点激活 ${task.nodeExecutionId} · 尝试 ${task.attemptId}`);
      ids.prepend(summary); li.append(row,ids);
      if (typeof openTask === 'function') {
        const action = document.createElement('button'); action.type='button'; action.className='quiet run-view-open-task';
        const failedAttempt = retention.state === 'distinct_with_failure'
          && (task.executionStatus === 'failed' || task.verificationStatus === 'failed');
        action.textContent = failedAttempt ? '查看失败证据' : '查看任务详情';
        action.addEventListener('click',()=>openTask(task.taskId)); li.append(action);
      }
      list.append(li);
    }
  }
  function create(options) {
    if (!options || typeof options.request !== 'function' || typeof options.identity !== 'function')
      throw new TypeError('运行图需要授权请求与当前运行身份读取接口。');
    let generation = 0, loading = false, currentKey = null, selectedExecutionNodeId = null;
    let cachedIdentity = null, cachedGraph = null, lastSuccessAt = null, timer = null, stateRefreshFailed = false;
    let latestTasks = [];
    let autoGeneration = 0;
    let nativeGeneration = 0, nativeLoading = false;
    const button = $('run-view-read'), message = $('run-view-message'), graphContainer = $('run-view-graph');
    const observed = $('run-view-observed'), connection = $('run-view-connection'), tasks = $('run-view-tasks'), taskList = $('run-view-task-list');
    const details = $('run-view'), auto = $('run-view-auto');
    const nativeButton = $('run-view-native-read'), nativePanel = $('run-view-native');
    const nativeMessage = $('run-view-native-message'), nativeSummary = $('run-view-native-summary');
    const nativeIds = $('run-view-native-ids');
    const contextList = $('run-view-context');
    const parameterSection = $('run-view-parameters');
    const parameterButton = $('run-view-parameters-read');
    const parameterMessage = $('run-view-parameters-message');
    const parameterPreview = $('run-view-parameters-preview');
    let parameterGeneration = 0, parameterLoading = false;
    const decisions = window.AgentLineRunDecisions?.create({
      container:$('run-view-decisions'), request:options.request,
      identity:()=>cachedIdentity && identityKey(options.identity())===currentKey ? cachedIdentity : null,
      graph:()=>cachedGraph
    });
    const clearTimer = () => { if (timer !== null) window.clearTimeout(timer); timer = null; };
    const hasInteractiveFocus = () => graphContainer.contains(document.activeElement) || tasks.contains(document.activeElement);
    const sameCachedIdentity = () => {
      const value = options.identity();
      return cachedIdentity && identityKey(value) === currentKey;
    };
    function hideStaleTasks() {
      for (const state of graphContainer.querySelectorAll('.run-view-node-state')) state.textContent = '状态未更新';
      for (const group of graphContainer.querySelectorAll('.run-view-node')) {
        const node = cachedGraph?.nodes.find(item=>item.executionNodeId===group.dataset.executionNodeId);
        group.setAttribute('aria-label',`${node?.name || '节点'}；状态未更新`);
      }
      const caption = graphContainer.querySelector('.run-view-caption');
      if (caption) caption.textContent='固定流程结构保留；节点任务计数已隐藏，状态未更新。连线仅表示定义关系，不表示分支已命中。';
      tasks.hidden = true; taskList.replaceChildren();
    }
    function pauseAutoTimer() {
      clearTimer();
      if (auto.checked && cachedGraph) message.textContent='自动更新已暂停；展开运行图并返回此页面后继续。';
    }
    function scheduleAuto(delay = 10000) {
      clearTimer();
      if (!auto.checked || !cachedGraph || !cachedIdentity || !sameCachedIdentity()) {
        if (cachedIdentity && !sameCachedIdentity()) { clear(); message.textContent='运行身份已变化，请重新读取运行图。'; }
        return;
      }
      if (!details.open || document.visibilityState !== 'visible' || loading) return;
      if (hasInteractiveFocus()) { message.textContent='自动更新已暂停，离开图或节点任务后继续。'; return; }
      timer = window.setTimeout(() => {
        timer = null;
        if (hasInteractiveFocus()) { message.textContent='自动更新已暂停，离开图或节点任务后继续。'; return; }
        refreshState(true);
      }, delay);
    }
    function updateSnapshot(data) {
      latestTasks = data.tasks;
      const counts = new Map();
      for (const task of data.tasks) {
        if (!counts.has(task.nodeId)) counts.set(task.nodeId,[]);
        counts.get(task.nodeId).push(task);
      }
      for (const group of graphContainer.querySelectorAll('.run-view-node')) {
        const nodeTasks = counts.get(group.dataset.executionNodeId) || [];
        const state = group.querySelector('.run-view-node-state');
        if (state) state.textContent = `已登记 ${nodeTasks.length} 条`;
        const node = cachedGraph.nodes.find(item=>item.executionNodeId===group.dataset.executionNodeId);
        if (node) group.setAttribute('aria-label',`${node.name || typeLabels[node.type]}；${nodeTasks.length} 条已登记任务`);
      }
      const caption = graphContainer.querySelector('.run-view-caption');
      if (caption) caption.textContent = data.coverage.complete
        ? `共 ${cachedGraph.nodes.length} 个定义节点，登记任务 ${data.tasks.length} 条。连线仅表示定义关系，不表示分支已命中。`
        : `任务记录最多展示 ${data.coverage.limit} 条，当前页已截断；登记数可能不完整。连线不表示分支已命中。`;
      if (selectedExecutionNodeId) {
        const node = cachedGraph.nodes.find(item=>item.executionNodeId===selectedExecutionNodeId);
        if (node) renderNodeTasks(node,counts.get(node.executionNodeId)||[],taskList,tasks,$('run-view-tasks-message'),options.openTask);
        else { selectedExecutionNodeId=null; tasks.hidden=true; taskList.replaceChildren(); }
      }
    }
    function showCurrentSelection() {
      if (stateRefreshFailed) { hideStaleTasks(); return; }
      const node = cachedGraph?.nodes.find(item=>item.executionNodeId===selectedExecutionNodeId);
      if (node) renderNodeTasks(node,latestTasks.filter(task=>task.nodeId===node.executionNodeId),
        taskList,tasks,$('run-view-tasks-message'),options.openTask);
    }
    function showConnection(state) {
      if (!connection) return;
      const text = connectionAnswer(state);
      if (text) connection.textContent = text;
    }
    function showFixedContext(identity) {
      if (!contextList) return;
      contextList.replaceChildren();
      const rows = fixedRunContext(identity);
      if (!rows.length) { contextList.hidden = true; return; }
      for (const [labelText, valueText] of rows) addNativeField(contextList, labelText, valueText);
      contextList.hidden = false;
    }
    function resetParameters(note) {
      parameterGeneration += 1; parameterLoading = false;
      if (parameterPreview) parameterPreview.replaceChildren();
      if (parameterMessage) parameterMessage.textContent = note || '读取运行图后，可单独查看本次根提交的任务参数。这是本地保存的输入，不是 Codex 提示词。';
      if (parameterButton) parameterButton.disabled = parameterLoading || !cachedIdentity;
      if (parameterSection) parameterSection.hidden = !cachedIdentity;
    }
    function clear() {
      generation += 1; clearTimer(); loading = false; currentKey = null; selectedExecutionNodeId = null;
      decisions?.clear();
      autoGeneration += 1;
      nativeGeneration += 1; nativeLoading = false;
      cachedIdentity = null; cachedGraph = null; lastSuccessAt = null; stateRefreshFailed = false;
      latestTasks = [];
      auto.checked = false; auto.disabled = true;
      nativeButton.disabled = true; nativeButton.textContent='读取原生流程状态'; nativePanel.hidden = true; nativeMessage.textContent='尚未读取原生流程状态。';
      nativeSummary.replaceChildren(); nativeIds.replaceChildren();
      button.disabled = false; button.textContent='读取运行图'; observed.textContent='尚未读取'; showConnection({phase:'idle'});
      message.textContent='查询运行后，可手动读取固定版本运行图。'; graphContainer.replaceChildren(); graphContainer.hidden=true;
      tasks.hidden=true; $('run-view-tasks-message').textContent=''; taskList.replaceChildren();
      showFixedContext(null); resetParameters();
    }
    function identityChanged(key) {
      if (identityKey(options.identity()) === key) return false;
      clear(); message.textContent='运行身份已变化，请查询当前运行后重新读取。';
      return true;
    }
    async function loadGraph() {
      if (loading) return;
      const identity = options.identity(), key = identityKey(identity);
      if (!identity || !key) { clear(); message.textContent='请先查询有效运行，再读取运行图。'; return; }
      if (cachedIdentity && currentKey !== key) clear();
      nativeGeneration += 1; nativeLoading = false; nativeButton.disabled = true; nativeButton.textContent='读取原生流程状态';
      nativePanel.hidden = true; nativeMessage.textContent='运行图刷新后，需重新读取原生流程状态。';
      nativeSummary.replaceChildren(); nativeIds.replaceChildren();
      const generationAtStart = ++generation;
      decisions?.clear();
      currentKey = key; loading=true; button.disabled=true; button.textContent='正在读取…';
      message.textContent='正在读取固定版本结构和已登记任务…'; observed.textContent='正在读取…'; tasks.hidden=true; taskList.replaceChildren();
      if (!cachedGraph) { graphContainer.replaceChildren(); graphContainer.hidden=true; }
      try {
        const data = await options.request(`/workflows/${encodeURIComponent(identity.bindingId)}/runs/${encodeURIComponent(identity.runId)}/view`);
        const latest = options.identity();
        if (generationAtStart !== generation || identityKey(latest) !== key || currentKey !== key) return;
        validate(data,identity,'fixed_publication_run_view');
        const previousSelection = selectedExecutionNodeId;
        cachedIdentity = {...data.identity}; cachedGraph = data.graph; currentKey = key; latestTasks=data.tasks;
        decisions?.setContext(cachedIdentity,cachedGraph);
        stateRefreshFailed=false;
        const rendered = renderGraph(data,previousSelection,options.openTask,tasks,taskList,$('run-view-tasks-message'),value=>{selectedExecutionNodeId=value;showCurrentSelection();});
        if (generationAtStart !== generation || identityKey(options.identity()) !== key) return;
        graphContainer.replaceChildren(rendered); graphContainer.hidden=false;
        lastSuccessAt=data.observedAt; stateRefreshFailed=false; auto.disabled=false;
        nativeButton.disabled=false;
        observed.textContent=`最近成功读取：${new Date(lastSuccessAt*1000).toLocaleString()} · 版本 ${data.identity.revision}`;
        showConnection({phase:'ok'});
        showFixedContext(data.identity); resetParameters();
        message.textContent=(data.coverage.complete ? '运行图读取完成。' : `运行图读取完成；节点任务记录受 ${data.coverage.limit} 条上限截断。`) + ' ' + rerunBoundaryNote();
      } catch (error) {
        if (generationAtStart !== generation || identityKey(options.identity()) !== key) return;
        decisions?.clear();
        if (cachedGraph && cachedIdentity && currentKey === key && lastSuccessAt !== null) {
          stateRefreshFailed=true; auto.checked=false; clearTimer(); hideStaleTasks();
          nativeButton.disabled=false;
          observed.textContent=`最近成功读取：${new Date(lastSuccessAt*1000).toLocaleString()} · 状态未更新`;
          showConnection({phase:'failed', keptGraph:true});
          message.textContent=`运行图状态刷新失败，已停止自动更新；保留固定结构并隐藏旧任务状态。可手动重读运行图。${error.message || ''}`;
        } else {
          cachedGraph=null; cachedIdentity=null; currentKey=null; selectedExecutionNodeId=null; auto.disabled=true;
          nativeButton.disabled=true;
          graphContainer.replaceChildren(); graphContainer.hidden=true; tasks.hidden=true; observed.textContent='未读取';
          showConnection({phase:'failed', keptGraph:false});
          showFixedContext(null); resetParameters();
          message.textContent=`无法核验运行图：${error.message || '请刷新重试。'}`;
        }
      } finally {
        if (generationAtStart === generation) {
          loading=false; button.disabled=false; button.textContent='刷新运行图';
          if (!identityChanged(key)) scheduleAuto();
          if (auto.checked && (!details.open || document.visibilityState !== 'visible'))
            message.textContent='自动更新已暂停；展开运行图并返回此页面后继续。';
        }
      }
    }
    async function refreshState(automatic = false) {
      if (loading || !cachedGraph || !cachedIdentity) return;
      const identity = options.identity(), key = identityKey(identity);
      if (!identity || key !== currentKey) { identityChanged(currentKey); return; }
      const generationAtStart = ++generation, expectedIdentity = {...cachedIdentity};
      const autoAtStart = autoGeneration;
      loading=true; button.disabled=true; button.textContent='正在更新…'; decisions?.refreshControls(true);
      message.textContent=automatic ? '正在自动更新本地节点状态…' : '正在手动更新本地节点状态…';
      try {
        const query = new URLSearchParams({graphSha256:expectedIdentity.graphSha256});
        const data = await options.request(`/workflows/${encodeURIComponent(identity.bindingId)}/runs/${encodeURIComponent(identity.runId)}/view/state?${query}`);
        if (generationAtStart !== generation || identityKey(options.identity()) !== key || currentKey !== key
          || !details.open || document.visibilityState !== 'visible') return;
        if (automatic && (!auto.checked || autoAtStart !== autoGeneration)) return;
        if (data?.identity?.graphSha256 !== expectedIdentity.graphSha256) decisions?.clear();
        validate(data,identity,'fixed_publication_run_state',expectedIdentity,cachedGraph);
        if (hasInteractiveFocus()) { message.textContent='已暂停更新以保留当前焦点，离开图或任务后继续。'; return; }
        const scrollX = window.scrollX, scrollY = window.scrollY, graphScrollLeft = graphContainer.scrollLeft, graphScrollTop = graphContainer.scrollTop;
        const renderedTaskNode = selectedExecutionNodeId ? cachedGraph.nodes.find(node=>node.executionNodeId===selectedExecutionNodeId) : null;
        stateRefreshFailed=false;
        updateSnapshot(data);
        if (generationAtStart !== generation || identityKey(options.identity()) !== key) return;
        lastSuccessAt=data.observedAt; stateRefreshFailed=false;
        observed.textContent=`最近成功更新：${new Date(lastSuccessAt*1000).toLocaleString()} · 版本 ${data.identity.revision}`;
        showConnection({phase:'ok'});
        message.textContent=(data.coverage.complete ? '本地节点状态已更新；原生节点执行和分支命中仍未知。' : `本地状态已更新，但任务记录受 ${data.coverage.limit} 条上限截断；原生节点执行和分支命中仍未知。`) + ' ' + rerunBoundaryNote();
        tasks.hidden = !renderedTaskNode;
        graphContainer.scrollLeft=graphScrollLeft; graphContainer.scrollTop=graphScrollTop;
        window.scrollTo(scrollX,scrollY);
      } catch (error) {
        if (generationAtStart !== generation || identityKey(options.identity()) !== key) return;
        if (automatic && (!auto.checked || autoAtStart !== autoGeneration)) return;
        stateRefreshFailed=true; auto.checked=false; clearTimer(); hideStaleTasks();
        observed.textContent=`最近成功读取：${new Date(lastSuccessAt*1000).toLocaleString()} · 状态未更新`;
        showConnection({phase:'failed', keptGraph:true});
        message.textContent=`${automatic ? '自动' : '手动'}状态更新失败，已停止自动更新；保留固定结构并隐藏旧任务状态。可手动重读运行图。${error.message || ''}`;
      } finally {
        if (generationAtStart === generation) {
          loading=false; button.disabled=false; button.textContent='刷新运行图';
          decisions?.refreshControls(false);
          if (identityKey(options.identity()) !== key) identityChanged(key);
          else if (auto.checked) scheduleAuto();
          if (auto.checked && (!details.open || document.visibilityState !== 'visible'))
            message.textContent='自动更新已暂停；展开运行图并返回此页面后继续。';
        }
      }
    }
    function addNativeField(parent, labelText, valueText) {
      appendText(parent,'dt','',labelText);
      appendText(parent,'dd','',valueText);
    }
    function renderNative(data) {
      const native = data.native, local = data.local, paused = native.paused;
      nativeSummary.replaceChildren(); nativeIds.replaceChildren();
      addNativeField(nativeSummary,'原生整体状态',nativeStatusLabels[native.status]);
      addNativeField(nativeSummary,'整体流程是否暂停',workflowPausedAnswer(native.status, paused) || '无法从本次记录判断');
      addNativeField(nativeSummary,'原生开始/排队记录',native.startedAt === null ? '未提供' : new Date(native.startedAt*1000).toLocaleString());
      addNativeField(nativeSummary,'原生结束记录',native.endedAt === null ? '未提供' : new Date(native.endedAt*1000).toLocaleString());
      addNativeField(nativeSummary,'原生运行时长',native.durationMs === null ? '未提供' : `${native.durationMs} 毫秒`);
      if (paused) {
        const waitLabels = {agent_result:'等待 Agent 结果',business_input:'等待业务输入',scheduled_time:'定时等待',unknown:'等待用途未知'};
        addNativeField(nativeSummary,'暂停信息',`${waitLabels[paused.waitingFor]}；恢复点总计 ${paused.pausePointCount} 个，已恢复 ${paused.resumedCount} 个${paused.automaticResumeNeedsAttention ? '；自动恢复需要关注' : ''}`);
      } else addNativeField(nativeSummary,'暂停信息',native.status === 'paused' ? '原生记录已暂停，未提供具体暂停点' : '本次原生记录未暂停，因此没有暂停点');
      addNativeField(nativeSummary,'本地登记任务资源退出确认',`${local.resourcesUnreleased} 条已登记任务尚未确认资源退出（不含未提交任务）`);
      addNativeField(nativeSummary,'Codex 活动',codexActivityAnswer(data.codexActivity) || '未知');
      addNativeField(nativeSummary,'原生状态读取时间',new Date(data.observedAt*1000).toLocaleString());
      addNativeField(nativeSummary,'本地任务快照时间',new Date(local.observedAt*1000).toLocaleString());
      addNativeField(nativeSummary,'本地任务记录',local.coverage.complete
        ? `${local.taskCount} 条`
        : `${local.taskCount} 条（已达 ${local.coverage.limit} 条上限，记录不完整）`);
      if (paused) {
        if (paused.contextId !== null) addNativeField(nativeIds,'上下文标识',paused.contextId);
        if (paused.blockedOnBlockId !== null) addNativeField(nativeIds,'代表性等待点标识',paused.blockedOnBlockId);
      }
      nativePanel.hidden=false;
      nativeMessage.textContent='只读原生汇总；不表示节点执行、分支命中、验收或记忆完成。';
    }
    async function readNative() {
      if (nativeLoading || !cachedGraph || !cachedIdentity) return;
      const identity = options.identity(), key = identityKey(identity);
      if (!identity || key !== currentKey || !sameCachedIdentity()) { identityChanged(currentKey); return; }
      const requestGeneration = ++nativeGeneration, expectedIdentity = {...cachedIdentity};
      nativeLoading=true; nativeButton.disabled=true; nativeButton.textContent='正在读取…';
      nativePanel.hidden=false; nativeSummary.replaceChildren(); nativeIds.replaceChildren();
      nativeMessage.textContent='正在读取原生流程状态…';
      try {
        const query = new URLSearchParams({graphSha256:expectedIdentity.graphSha256});
        const data = await options.request(`/workflows/${encodeURIComponent(identity.bindingId)}/runs/${encodeURIComponent(identity.runId)}/view/native?${query}`);
        if (requestGeneration !== nativeGeneration || identityKey(options.identity()) !== key || currentKey !== key
          || !cachedIdentity || keysDiffer(cachedIdentity,expectedIdentity) || !cachedGraph) return;
        validateNative(data,expectedIdentity,expectedIdentity);
        renderNative(data);
      } catch (error) {
        if (requestGeneration !== nativeGeneration || identityKey(options.identity()) !== key || currentKey !== key) return;
        nativeSummary.replaceChildren(); nativeIds.replaceChildren(); nativePanel.hidden=false;
        nativeMessage.textContent=`原生状态无法核验，当前摘要已清除；本地流程图保留。${error.message || ''}`;
      } finally {
        if (requestGeneration === nativeGeneration) {
          nativeLoading=false; nativeButton.disabled=!cachedGraph || !cachedIdentity;
          nativeButton.textContent='读取原生流程状态';
          if (identityKey(options.identity()) !== key || currentKey !== key) identityChanged(key);
        }
      }
    }
    function onRead() {
      if (cachedGraph && cachedIdentity && currentKey === identityKey(options.identity())) {
        if (stateRefreshFailed) loadGraph();
        else refreshState(false);
      } else loadGraph();
    }
    parameterButton?.addEventListener('click', async () => {
      if (parameterLoading || !cachedIdentity || !sameCachedIdentity()) return;
      const identity = {...cachedIdentity}, key = currentKey, attempt = ++parameterGeneration;
      parameterLoading = true; parameterButton.disabled = true;
      if (parameterPreview) parameterPreview.replaceChildren();
      if (parameterMessage) parameterMessage.textContent = '正在读取本地根提交的任务参数，不访问原生执行服务…';
      try {
        const payload = await options.request(`/workflows/${encodeURIComponent(identity.bindingId)}/runs/${encodeURIComponent(identity.runId)}/input`);
        if (attempt !== parameterGeneration || currentKey !== key || !cachedIdentity || keysDiffer(cachedIdentity, identity)) return;
        const accepted = parameterAcceptance(payload, identity, options.redact);
        if (accepted.state === 'visible') {
          const pre = document.createElement('pre'); pre.className = 'technical'; pre.textContent = accepted.text;
          parameterPreview.replaceChildren(pre);
          parameterMessage.textContent = '这是本次运行根提交保存的任务参数。未显示 Codex 提示词、工具正文或操作者。';
        } else if (accepted.state === 'hidden') parameterMessage.textContent = '任务参数包含当前凭证，已禁止展示。';
        else if (accepted.state === 'too_large') parameterMessage.textContent = '任务参数超过 64,000 字节展示上限，未显示正文。';
        else parameterMessage.textContent = '任务参数回执无法核对，已清除预览。';
      } catch (error) {
        if (attempt !== parameterGeneration || currentKey !== key) return;
        if (parameterPreview) parameterPreview.replaceChildren();
        if (parameterMessage) parameterMessage.textContent = error?.message || '任务参数暂时无法读取。';
      } finally {
        if (attempt === parameterGeneration) { parameterLoading = false; parameterButton.disabled = !cachedIdentity; }
      }
    });
    button.addEventListener('click',onRead);
    nativeButton.addEventListener('click',readNative);
    auto.addEventListener('change',()=>{
      autoGeneration += 1;
      if (!auto.checked) { clearTimer(); return; }
      if (!cachedGraph || !sameCachedIdentity()) {
        auto.checked=false; message.textContent='请先读取当前运行图，再启用自动更新。'; return;
      }
      scheduleAuto();
    });
    details.addEventListener('toggle',()=>{ if (details.open) scheduleAuto(); else pauseAutoTimer(); });
    document.addEventListener('visibilitychange',()=>{
      if (document.visibilityState === 'visible') scheduleAuto(); else pauseAutoTimer();
    });
    graphContainer.addEventListener('focusout',()=>queueMicrotask(()=>scheduleAuto()));
    tasks.addEventListener('focusout',()=>queueMicrotask(()=>scheduleAuto()));
    return {clear};
  }
  window.AgentLineRunView = {create, workflowPausedAnswer, codexActivityAnswer, fixedRunContext, parameterAcceptance, runTaskBlockReason, connectionAnswer, nodeDetailAction, nodeDetailHint, attemptRetention, rerunBoundaryNote};
})();
