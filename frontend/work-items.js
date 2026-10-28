'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const safeId = value => typeof value === 'string' && /^[A-Za-z0-9_-]{1,160}$/.test(value);
  const safeWorkItemId = value => typeof value === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(value);
  const plain = value => !!value && typeof value === 'object' && !Array.isArray(value)
    && Object.getPrototypeOf(value) === Object.prototype;
  const exact = (value, keys) => plain(value) && Object.keys(value).length === keys.length
    && keys.every(key => Object.hasOwn(value, key));
  const text = (value, max) => typeof value === 'string' && !!value.trim() && value.length <= max && !value.includes('\0');
  const unixSeconds = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 8_640_000_000_000;
  let request = null, openRun = null, openTasks = null, connected = false, generation = 0, runGeneration = 0, requestCounter = 0;
  let projectId = '', projects = [], itemPage = 0, itemCursors = [null], itemPages = [], itemNext = null;
  let itemBusy = false, selected = null, runsWorkItem = null, runPage = 0, runCursors = [null], runPages = [], runNext = null, runBusy = false;
  let intent = null, reloadAfterCreate = false;

  function updateControls() {
    if (!$('work-items-project')) return;
    $('work-items-project').disabled = !connected || itemBusy || projects.length === 0;
    if (intent) $('work-items-project').disabled = true;
    $('work-items-refresh').disabled = !connected || itemBusy || !projectId;
    $('work-items-page-size').disabled = !connected || itemBusy || !projectId;
    $('work-items-prev').disabled = !connected || itemBusy || itemPage === 0;
    $('work-items-next').disabled = !connected || itemBusy || itemNext === null;
    $('work-item-create-submit').disabled = !connected || itemBusy || !projectId || !!intent;
    $('work-item-create-check').disabled = !connected || itemBusy || !intent;
    $('work-items-list').querySelectorAll('button').forEach(button => { button.disabled = !connected || itemBusy || runBusy; });
    $('work-item-runs-prev').disabled = !connected || runBusy || runPage === 0;
    $('work-item-runs-next').disabled = !connected || runBusy || runNext === null;
    $('work-item-runs-list').querySelectorAll('button').forEach(button => { button.disabled = !connected || runBusy; });
    $('work-items-page').textContent = `第 ${itemPage + 1} 页`;
    $('work-item-runs-page').textContent = `第 ${runPage + 1} 页`;
  }
  function clearSelection(message = '尚未选用业务任务；运行仍可按原方式启动。') {
    selected = null;
    runsWorkItem = null;
    $('work-item-start-selection').textContent = message;
    $('work-item-start-clear').hidden = true;
    $('work-item-runs').hidden = true;
    $('work-item-runs-title').textContent = '关联运行';
    $('work-item-runs-list').replaceChildren();
    $('work-item-runs-message').textContent = '';
    runGeneration += 1; runBusy = false;
    runPage = 0; runCursors = [null]; runPages = []; runNext = null;
    updateControls();
  }
  function clearItemPages() {
    itemPage = 0; itemCursors = [null]; itemPages = []; itemNext = null;
    $('work-items-list').replaceChildren();
  }
  function clear() {
    generation += 1; connected = false; itemBusy = false; runBusy = false;
    projectId = ''; projects = []; clearItemPages(); clearSelection();
    $('work-items-project').replaceChildren(new Option('连接后读取项目', ''));
    $('work-items-page-size').value = '10';
    $('work-items-message').textContent = '连接后读取项目业务任务。';
    $('work-item-title').value = ''; $('work-item-goal').value = ''; $('work-item-criteria').value = '';
    $('work-item-title').disabled = false; $('work-item-goal').disabled = false; $('work-item-criteria').disabled = false;
    $('work-item-create-id').textContent = ''; $('work-item-create-message').textContent = '请填写固定目标与可验证的验收条件。';
    $('work-item-create-submit').hidden = false; $('work-item-create-check').hidden = true;
    $('work-item-create-details').open = false; intent = null; reloadAfterCreate = false;
    $('work-item-runs').hidden = true; $('work-item-runs-list').replaceChildren();
    runGeneration += 1; runBusy = false;
    runPage = 0; runCursors = [null]; runPages = []; runNext = null;
    updateControls();
  }
  function validateProjectList(data) {
    if (!exact(data, ['source','projects']) || data.source !== 'configured_projects' || !Array.isArray(data.projects))
      throw new Error('项目列表响应无法核验。');
    const seen = new Set();
    for (const project of data.projects) {
      if (!exact(project, ['projectId','name']) || !safeId(project.projectId) || !text(project.name, 256) || seen.has(project.projectId))
        throw new Error('项目列表包含无效或重复记录。');
      seen.add(project.projectId);
    }
  }
  function validateRecord(record, expectedProject = projectId) {
    if (!exact(record, ['source','workItemId','projectId','title','goal','acceptanceCriteria','definitionSha256','createdAt'])
      || record.source !== 'local_work_item' || !safeWorkItemId(record.workItemId) || record.projectId !== expectedProject
      || !safeId(record.projectId) || !text(record.title, 160) || !text(record.goal, 4000)
      || !Array.isArray(record.acceptanceCriteria) || record.acceptanceCriteria.length < 1 || record.acceptanceCriteria.length > 8
      || record.acceptanceCriteria.some(item => !text(item, 500)) || new Set(record.acceptanceCriteria).size !== record.acceptanceCriteria.length
      || typeof record.definitionSha256 !== 'string' || !/^[a-f0-9]{64}$/.test(record.definitionSha256)
      || !unixSeconds(record.createdAt)) throw new Error('业务任务记录字段无法核验。');
    return record;
  }
  function validatePage(data, capturedProject, limit, cursor, targetPage) {
    if (!exact(data, ['source','projectId','items','nextCursor']) || data.source !== 'local_work_item_directory'
      || data.projectId !== capturedProject || !Array.isArray(data.items) || data.items.length > limit
      || !(data.nextCursor === null || (typeof data.nextCursor === 'string' && data.nextCursor.length > 0 && data.nextCursor.length <= 128)))
      throw new Error('业务任务列表范围或结构无法核验。');
    const ids = new Set();
    data.items.forEach(record => {
      validateRecord(record, capturedProject);
      if (ids.has(record.workItemId)) throw new Error('业务任务页包含重复标识。');
      ids.add(record.workItemId);
    });
    if (data.nextCursor !== null && (!data.items.length || data.nextCursor !== data.items[data.items.length - 1].workItemId
      || data.nextCursor === cursor)) throw new Error('业务任务分页游标与当前页不一致。');
    if (targetPage > 0 && cursor === null) throw new Error('业务任务分页游标缺失。');
    const prior = new Set();
    for (let page = 0; page < itemPages.length; page += 1) {
      if (page === targetPage) continue;
      (itemPages[page] || []).forEach(record => prior.add(record.workItemId));
    }
    if ([...ids].some(id => prior.has(id))) throw new Error('业务任务分页出现重复记录。');
    return ids;
  }
  function itemPath(project, id = '') {
    const base = `/work-items/projects/${encodeURIComponent(project)}/items`;
    return id ? `${base}/${encodeURIComponent(id)}` : base;
  }
  function listLimit() {
    const limit = Number($('work-items-page-size').value);
    if (!Number.isInteger(limit) || ![10,25,50].includes(limit)) throw new Error('每页数量无法核验。');
    return limit;
  }
  function renderItems(records) {
    const list = $('work-items-list'); list.replaceChildren();
    for (const record of records) {
      const li = document.createElement('li'), row = document.createElement('div');
      row.className = 'task-row work-item-row';
      const heading = document.createElement('strong'); heading.textContent = record.title;
      const context = document.createElement('span'); context.className = 'task-context';
      context.textContent = `验收条件 ${record.acceptanceCriteria.length} 项 · 建立于 ${new Date(record.createdAt * 1000).toLocaleString()}`;
      const goal = document.createElement('span'); goal.className = 'work-item-goal'; goal.textContent = record.goal;
      const actions = document.createElement('div'); actions.className = 'work-item-actions';
      const use = document.createElement('button'); use.type = 'button'; use.className = 'secondary'; use.textContent = selected?.workItemId === record.workItemId ? '已选用' : '选用';
      use.dataset.action = 'select'; use.dataset.workItemId = record.workItemId;
      const runs = document.createElement('button'); runs.type = 'button'; runs.className = 'quiet'; runs.textContent = '查看关联运行';
      runs.dataset.action = 'runs'; runs.dataset.workItemId = record.workItemId;
      if (typeof openTasks === 'function') {
        const tasks = document.createElement('button'); tasks.type = 'button'; tasks.className = 'quiet';
        tasks.textContent = '查看节点任务'; tasks.dataset.action = 'tasks'; tasks.dataset.workItemId = record.workItemId;
        actions.append(tasks);
      }
      const criteria = document.createElement('details'); criteria.className = 'work-item-criteria';
      const summary = document.createElement('summary'); summary.textContent = `查看验收条件（${record.acceptanceCriteria.length}）`;
      const criteriaList = document.createElement('ol');
      record.acceptanceCriteria.forEach(item => { const entry = document.createElement('li'); entry.textContent = item; criteriaList.append(entry); });
      criteria.append(summary, criteriaList);
      const ids = document.createElement('details'); ids.className = 'task-row-identifiers';
      const idsSummary = document.createElement('summary'); idsSummary.textContent = '查看任务标识';
      const identity = document.createElement('span'); identity.className = 'task-context'; identity.textContent = `项目：${record.projectId} · 任务：${record.workItemId} · 定义摘要：${record.definitionSha256}`;
      ids.append(idsSummary, identity);
      actions.append(use, runs); row.append(heading, context, goal, criteria, ids, actions); li.append(row); list.append(li);
    }
  }
  async function loadItems(targetPage = 0) {
    if (!connected || !projectId || itemBusy) return;
    const capturedGeneration = generation, capturedProject = projectId, cursor = itemCursors[targetPage];
    if (cursor === undefined) return;
    let limit;
    try { limit = listLimit(); } catch (error) { $('work-items-message').textContent = error.message; return; }
    itemBusy = true; updateControls(); $('work-items-message').textContent = '正在读取本地登记的业务任务。';
    try {
      const query = new URLSearchParams({limit:String(limit)}); if (cursor) query.set('after', cursor);
      const data = await request(`${itemPath(capturedProject)}?${query}`);
      if (capturedGeneration !== generation || !connected || capturedProject !== projectId) return;
      const ids = validatePage(data, capturedProject, limit, cursor, targetPage);
      itemPages[targetPage] = data.items;
      itemPage = targetPage; itemNext = data.nextCursor;
      if (itemNext !== null) itemCursors[targetPage + 1] = itemNext;
      renderItems(data.items);
      $('work-items-message').textContent = data.items.length ? `显示 ${data.items.length} 个业务任务。选择任务后可关联多个新运行。` : '此项目尚无业务任务。';
    } catch (error) {
      if (capturedGeneration !== generation || !connected || capturedProject !== projectId) return;
      $('work-items-list').replaceChildren();
      $('work-items-message').textContent = `业务任务列表无法核验：${error.message}。可刷新重试，已确认的分页游标保留。`;
    } finally {
      if (capturedGeneration === generation) { itemBusy = false; updateControls(); }
    }
  }
  function setSelection(record) {
    selected = record;
    showRuns(record);
    $('work-item-start-selection').textContent = `已选用「${record.title}」· 项目 ${record.projectId} · 运行输入仍由启动表单提供。`;
    $('work-item-start-clear').hidden = false;
    renderItems(itemPages[itemPage] || []);
  }
  function showRuns(record) {
    runGeneration += 1; runBusy = false;
    runsWorkItem = record;
    $('work-item-runs').hidden = false;
    $('work-item-runs-title').textContent = `「${record.title}」的关联运行`;
    runPage = 0; runCursors = [null]; runPages = []; runNext = null;
    $('work-item-runs-list').replaceChildren();
    loadRuns(0);
  }
  function validateRuns(data, capturedProject, capturedId, limit, cursor, targetPage) {
    if (!exact(data, ['source','projectId','workItemId','executionStateIncluded','runs','nextCursor'])
      || data.source !== 'local_work_item_runs' || data.projectId !== capturedProject || data.workItemId !== capturedId
      || data.executionStateIncluded !== false || !Array.isArray(data.runs) || data.runs.length > limit
      || !(data.nextCursor === null || (typeof data.nextCursor === 'string' && data.nextCursor.length > 0 && data.nextCursor.length <= 160)))
      throw new Error('关联运行列表范围或结构无法核验。');
    const ids = new Set();
    for (const run of data.runs) {
      if (!exact(run, ['bindingId','workflowId','runId','submissionStatus','recordedAt'])
        || !safeId(run.bindingId) || !safeId(run.workflowId) || !safeId(run.runId)
        || !text(run.submissionStatus, 40) || !unixSeconds(run.recordedAt) || ids.has(run.runId))
        throw new Error('关联运行记录字段无效或重复。');
      ids.add(run.runId);
    }
    if (data.nextCursor !== null && (!data.runs.length || data.nextCursor !== data.runs[data.runs.length - 1].runId || data.nextCursor === cursor))
      throw new Error('关联运行分页游标与当前页不一致。');
    if (targetPage > 0 && cursor === null) throw new Error('关联运行分页游标缺失。');
    const prior = new Set();
    for (let page = 0; page < runPages.length; page += 1) {
      if (page === targetPage) continue;
      (runPages[page] || []).forEach(run => prior.add(run.runId));
    }
    if ([...ids].some(id => prior.has(id)))
      throw new Error('关联运行分页出现重复记录。');
    return ids;
  }
  function renderRuns(runs) {
    const submissionLabels = {intent:'已登记提交意向', accepted:'已接收提交回执', rejected:'提交被拒绝', uncertain:'提交结果待核对'};
    const list = $('work-item-runs-list'); list.replaceChildren();
    for (const run of runs) {
      const li = document.createElement('li'), row = document.createElement('div'); row.className = 'task-row work-item-run-row';
      const heading = document.createElement('strong'); heading.textContent = `本地提交状态 · ${submissionLabels[run.submissionStatus] || '待核对'}`;
      const context = document.createElement('span'); context.className = 'task-context'; context.textContent = `工作流：${run.workflowId} · 提交记录：${new Date(run.recordedAt * 1000).toLocaleString()}`;
      const view = document.createElement('button'); view.type = 'button'; view.className = 'secondary'; view.textContent = '查看运行';
      view.dataset.bindingId = run.bindingId; view.dataset.runId = run.runId; view.dataset.action = 'open-run';
      const ids = document.createElement('details'); ids.className = 'task-row-identifiers';
      const summary = document.createElement('summary'); summary.textContent = '查看运行标识';
      const identity = document.createElement('span'); identity.className = 'task-context'; identity.textContent = `绑定：${run.bindingId} · 运行：${run.runId}`;
      ids.append(summary, identity); row.append(heading, context, ids, view); li.append(row); list.append(li);
    }
  }
  async function loadRuns(targetPage = 0) {
    if (!connected || !runsWorkItem || runBusy) return;
    const capturedGeneration = generation, capturedRunGeneration = runGeneration;
    const capturedProject = runsWorkItem.projectId, capturedId = runsWorkItem.workItemId;
    const cursor = runCursors[targetPage], limit = 10;
    if (cursor === undefined) return;
    runBusy = true; updateControls(); $('work-item-runs-message').textContent = '正在读取本地关联提交记录；不会查询执行状态。';
    try {
      const query = new URLSearchParams({limit:String(limit)}); if (cursor) query.set('after', cursor);
      const data = await request(`${itemPath(capturedProject, capturedId)}/runs?${query}`);
      if (capturedGeneration !== generation || capturedRunGeneration !== runGeneration || !connected || runsWorkItem?.workItemId !== capturedId) return;
      const ids = validateRuns(data, capturedProject, capturedId, limit, cursor, targetPage);
      runPages[targetPage] = data.runs;
      runPage = targetPage; runNext = data.nextCursor;
      if (runNext !== null) runCursors[targetPage + 1] = runNext;
      renderRuns(data.runs);
      $('work-item-runs-message').textContent = data.runs.length ? '此列表只记录本地提交情况，不表示工作流或业务目标已完成。' : '此业务任务尚无关联运行。';
    } catch (error) {
      if (capturedGeneration !== generation || capturedRunGeneration !== runGeneration || !connected || runsWorkItem?.workItemId !== capturedId) return;
      $('work-item-runs-list').replaceChildren();
      $('work-item-runs-message').textContent = `关联运行列表无法核验：${error.message}。可刷新业务任务后重试。`;
    } finally {
      if (capturedGeneration === generation && capturedRunGeneration === runGeneration) { runBusy = false; updateControls(); }
    }
  }
  function makeWorkItemId() {
    requestCounter += 1;
    const random = globalThis.crypto?.randomUUID ? crypto.randomUUID().replaceAll('-', '') : `${Date.now().toString(36)}${requestCounter.toString(36)}`;
    return `workitem_${random}`.slice(0,128);
  }
  function definitionBytes(body) {
    // Match the server's ASCII-escaped immutable definition, including scope.
    const value = {schemaVersion:1,projectId,...body};
    return JSON.stringify(value).replace(/[\u0080-\uffff]/g,
      char => `\\u${char.charCodeAt(0).toString(16).padStart(4,'0')}`).length;
  }
  function buildIntent() {
    const title = $('work-item-title').value.trim(), goal = $('work-item-goal').value.trim();
    const criteria = $('work-item-criteria').value.split(/\r?\n/).map(item => item.trim()).filter(Boolean);
    const id = makeWorkItemId();
    if (!safeWorkItemId(id) || !text(title,160) || !text(goal,4000) || !criteria.length || criteria.length > 8
      || criteria.some(item => !text(item,500)) || new Set(criteria).size !== criteria.length)
      throw new Error('请填写名称、目标和 1–8 条不重复的验收条件；每条验收条件不超过 500 字。');
    const body = {workItemId:id,title,goal,acceptanceCriteria:criteria};
    if (definitionBytes(body) > 8000) throw new Error('登记内容编码后超过 8000 字节，请缩短文字。');
    return {projectId, workItemId:id, body};
  }
  function lockIntent() {
    const locked = !!intent;
    for (const id of ['work-item-title','work-item-goal','work-item-criteria']) $(id).disabled = locked;
    $('work-item-create-id').textContent = intent ? `固定登记标识：${intent.workItemId}` : '';
    $('work-item-create-submit').hidden = locked;
    $('work-item-create-check').hidden = !locked;
    updateControls();
  }
  function confirmCreated(record, reused = false) {
    validateRecord(record, intent?.projectId || projectId);
    if (!intent || record.workItemId !== intent.workItemId || record.title !== intent.body.title || record.goal !== intent.body.goal
      || JSON.stringify(record.acceptanceCriteria) !== JSON.stringify(intent.body.acceptanceCriteria))
      throw new Error('服务端记录与原登记内容不一致，保留原标识并人工核对。');
    const createdProject = intent.projectId;
    $('work-item-create-message').textContent = reused ? '原标识的业务任务已存在并与内容一致，现可选用。' : '业务任务已登记；此操作没有启动工作流。';
    intent = null; reloadAfterCreate = true; lockIntent();
    $('work-item-title').value = ''; $('work-item-goal').value = ''; $('work-item-criteria').value = '';
    clearItemPages(); projectId = createdProject; $('work-items-project').value = projectId;
    setSelection(record); loadItems(0);
  }
  async function submitCreate() {
    if (!connected || itemBusy || !projectId || intent) return;
    let snapshot;
    try { snapshot = buildIntent(); } catch (error) { $('work-item-create-message').textContent = error.message; return; }
    intent = snapshot; lockIntent(); itemBusy = true;
    $('work-item-create-message').textContent = '正在登记固定目标；不会启动工作流。';
    const capturedGeneration = generation;
    try {
      const result = await request(itemPath(snapshot.projectId), {body:snapshot.body});
      if (capturedGeneration !== generation || !connected || intent !== snapshot) return;
      if (!exact(result, ['workItem','reused']) || typeof result.reused !== 'boolean') throw new Error('登记回执无法核验。');
      confirmCreated(result.workItem, result.reused);
    } catch (error) {
      if (capturedGeneration !== generation || !connected || intent !== snapshot) return;
      if (error.status === 422 || error.status === 409) {
        // This endpoint rejects invalid/conflicting definitions without writing them.
        // Keep the user's text; another POST still requires an explicit click.
        intent = null;
        $('work-item-create-message').textContent = error.status === 409
          ? `原标识 ${snapshot.workItemId} 存在冲突，本次没有登记新内容。请先从任务目录核对；若需要独立任务，修改后再次点击登记。不会自动重新提交。`
          : `登记内容未获准：${error.message}。请修改后再次点击登记；尚未启动工作流。`;
        return;
      }
      $('work-item-create-message').textContent = `登记结果尚未确认${error.status ? `（HTTP ${error.status}）` : '，连接或回执异常'}。保留原标识和内容，只能核对原登记。`;
    } finally {
      if (capturedGeneration === generation) {
        itemBusy = false; lockIntent(); updateControls();
        if (reloadAfterCreate) { reloadAfterCreate = false; loadItems(0); }
      }
    }
  }
  async function checkIntent() {
    if (!connected || itemBusy || !intent) return;
    const snapshot = intent, capturedGeneration = generation; itemBusy = true; updateControls();
    $('work-item-create-message').textContent = '正在读取原标识；不会重新提交登记。';
    try {
      const record = await request(itemPath(snapshot.projectId, snapshot.workItemId));
      if (capturedGeneration !== generation || !connected || intent !== snapshot) return;
      confirmCreated(record, true);
    } catch (error) {
      if (capturedGeneration !== generation || !connected || intent !== snapshot) return;
      $('work-item-create-message').textContent = error.status === 404
        ? '原标识当前未找到，登记结果仍待确认；保留原标识和内容，不会自动重提或换标识。'
        : `原登记仍无法核验：${error.message}。保留原标识和内容，可再次只读核对。`;
    } finally {
      if (capturedGeneration === generation) {
        itemBusy = false; lockIntent(); updateControls();
        if (reloadAfterCreate) { reloadAfterCreate = false; loadItems(0); }
      }
    }
  }
  async function connect() {
    generation += 1; connected = true; itemBusy = false; projectId = ''; projects = []; clearItemPages(); clearSelection();
    $('work-items-project').replaceChildren(new Option('正在读取项目…', ''));
    $('work-items-message').textContent = '正在读取授权项目列表。'; updateControls();
    const capturedGeneration = generation;
    try {
      const data = await request('/work-items/projects');
      if (capturedGeneration !== generation || !connected) return;
      validateProjectList(data); projects = data.projects;
      const select = $('work-items-project'); select.replaceChildren(new Option('选择项目', ''));
      projects.forEach(item => select.add(new Option(item.name, item.projectId)));
      $('work-items-message').textContent = projects.length ? '请选择项目以读取业务任务。' : '当前凭证没有可登记业务任务的项目。';
      if (projects.length) { select.value = projects[0].projectId; projectId = select.value; loadItems(0); }
    } catch (error) {
      if (capturedGeneration !== generation || !connected) return;
      $('work-items-project').replaceChildren(new Option('项目列表不可用', ''));
      $('work-items-message').textContent = `项目列表无法核验：${error.message}`;
    } finally { if (capturedGeneration === generation) updateControls(); }
  }
  $('work-items-project').addEventListener('change', () => {
    generation += 1; itemBusy = false; runBusy = false; projectId = $('work-items-project').value; clearItemPages(); clearSelection();
    $('work-items-message').textContent = projectId ? '正在读取所选项目的业务任务。' : '请选择项目。';
    updateControls(); if (projectId) loadItems(0);
  });
  $('work-items-refresh').addEventListener('click', () => {
    generation += 1; itemBusy = false; runGeneration += 1; runBusy = false; clearItemPages();
    if (runsWorkItem) showRuns(runsWorkItem);
    if (projectId) loadItems(0);
  });
  $('work-items-page-size').addEventListener('change', () => {
    generation += 1; itemBusy = false; runGeneration += 1; runBusy = false; clearItemPages();
    if (runsWorkItem) showRuns(runsWorkItem);
    if (projectId) loadItems(0);
  });
  $('work-items-prev').addEventListener('click', () => { if (itemPage > 0) loadItems(itemPage - 1); });
  $('work-items-next').addEventListener('click', () => { if (itemNext !== null) loadItems(itemPage + 1); });
  $('work-items-list').addEventListener('click', event => {
    const button = event.target.closest('button[data-work-item-id]');
    if (!button || !button.isConnected || !connected || itemBusy) return;
    const record = (itemPages[itemPage] || []).find(item => item.workItemId === button.dataset.workItemId);
    if (!record) return;
    if (button.dataset.action === 'select') setSelection(record);
    else if (button.dataset.action === 'runs') showRuns(record);
    else if (button.dataset.action === 'tasks' && typeof openTasks === 'function')
      openTasks({projectId:record.projectId,workItemId:record.workItemId});
  });
  $('work-item-start-clear').addEventListener('click', () => clearSelection());
  $('work-item-create-submit').addEventListener('click', submitCreate);
  $('work-item-create-check').addEventListener('click', checkIntent);
  $('work-item-runs-prev').addEventListener('click', () => { if (runPage > 0) loadRuns(runPage - 1); });
  $('work-item-runs-next').addEventListener('click', () => { if (runNext !== null) loadRuns(runPage + 1); });
  $('work-item-runs-list').addEventListener('click', event => {
    const button = event.target.closest('button[data-action="open-run"]');
    if (!button || !button.isConnected || !connected || runBusy || typeof openRun !== 'function') return;
    openRun({bindingId:button.dataset.bindingId,runId:button.dataset.runId});
    $('work-item-runs-message').textContent = '已填入原运行标识；需要点击“查询进度”后才会读取详情。';
  });

  window.AgentLineWorkItems = {
    create(options) {
      if (!options || typeof options.request !== 'function' || typeof options.openRun !== 'function')
        throw new TypeError('业务任务目录需要授权请求与运行表单回填接口。');
      request = options.request; openRun = options.openRun;
      openTasks = typeof options.openTasks === 'function' ? options.openTasks : null;
      return {
        connect,
        clear,
        clearStartSelection: clearSelection,
        selection(bindingProjectId) {
          if (!selected) return null;
          if (!safeId(selected.projectId) || !safeId(bindingProjectId) || selected.projectId !== bindingProjectId)
            throw new Error('所选业务任务与当前工作流所属项目不一致，请清除关联或选择同项目任务。');
          return selected.workItemId;
        }
      };
    }
  };
})();
