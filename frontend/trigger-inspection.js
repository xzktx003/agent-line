'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const plain = value => !!value && typeof value === 'object' && !Array.isArray(value);
  const safeId = (value, max) => typeof value === 'string' && value.length > 0
    && value.length <= max && /^[A-Za-z0-9_-]+$/.test(value);
  const unixSeconds = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
  const triggerTypes = new Set(['webhook','schedule']);
  const lookupStatusLabels = {definition_missing:'入口定义未找到',root_missing:'本地入口根记录未找到',recorded:'已找到本地事件记录'};
  const submissionLabels = {intent:'提交意向已记录',accepted:'已接收',rejected:'已拒绝',uncertain:'提交结果待核对'};

  function validateDirectory(data, after, previousLastId) {
    if (!plain(data) || data.source !== 'configured_trigger_directory' || data.configured !== true
      || !Array.isArray(data.triggers) || data.triggers.length > 25
      || !(data.nextCursor === null || safeId(data.nextCursor,128))
      || (data.triggers.length === 0 && data.nextCursor !== null)) {
      throw new Error('自动入口目录响应无法核验。');
    }
    const ids = new Set();
    let previous = previousLastId;
    for (const trigger of data.triggers) {
      if (!plain(trigger) || !safeId(trigger.triggerId,128) || !safeId(trigger.projectId,160)
        || !safeId(trigger.bindingId,160) || !triggerTypes.has(trigger.type) || trigger.state !== 'configured'
        || ids.has(trigger.triggerId) || (previous !== null && trigger.triggerId <= previous)) {
        throw new Error('自动入口包含无效、重复或未按标识排序的记录。');
      }
      ids.add(trigger.triggerId); previous = trigger.triggerId;
    }
    const last = data.triggers.at(-1)?.triggerId ?? null;
    if ((data.nextCursor !== null && data.nextCursor !== last) || (after !== null && data.nextCursor === after)) {
      throw new Error('自动入口分页游标与当前页不匹配。');
    }
    return data;
  }
  function validEventId(type, value) {
    if (typeof value !== 'string' || value.length === 0) return false;
    if (type === 'webhook') return [...value].length <= 1024 && value.trim().length > 0 && !/[\u0000-\u001f\u007f]/.test(value);
    if (type !== 'schedule' || value.startsWith('0000-') || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00Z$/.test(value)) return false;
    const date = new Date(value);
    return Number.isFinite(date.getTime()) && date.toISOString().replace('.000Z','Z') === value;
  }
  function validateLookup(data, trigger) {
    const statuses = ['definition_missing','root_missing','recorded'];
    const absentStatus = data?.lookupStatus === 'definition_missing' || data?.lookupStatus === 'root_missing';
    const validRunId = value => typeof value === 'string'
      && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
    if (!plain(data) || data.source !== 'local_trigger_event_lookup' || data.readOnly !== true
      || data.safeToResubmit !== false || data.triggerId !== trigger.triggerId
      || data.projectId !== trigger.projectId || data.bindingId !== trigger.bindingId || data.type !== trigger.type
      || !statuses.includes(data.lookupStatus) || !unixSeconds(data.observedAt)
      || (absentStatus ? data.submissionStatus !== null
        || (data.lookupStatus === 'definition_missing' ? data.runId !== null : !validRunId(data.runId))
        : !validRunId(data.runId) || !Object.hasOwn(submissionLabels,data.submissionStatus))) {
      throw new Error('原事件核对响应身份或结构无法核验。');
    }
    return data;
  }
  function appendText(parent, tag, className, value) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    item.textContent = value;
    parent.append(item);
    return item;
  }
  function create({request, openRun} = {}) {
    if (typeof request !== 'function') throw new TypeError('自动入口核对需要授权请求接口。');
    let directoryGeneration = 0, lookupGeneration = 0, directoryLoading = false, lookupLoading = false;
    let selected = null, nextCursor = null, pageIndex = 0, cursors = [null], pages = [];
    const message = $('trigger-directory-message'), list = $('trigger-directory-list');
    const pageLabel = $('trigger-directory-page'), previous = $('trigger-directory-prev'), next = $('trigger-directory-next');
    const form = $('trigger-lookup-form'), title = $('trigger-lookup-title'), hint = $('trigger-lookup-hint');
    const eventInput = $('trigger-event-id'), submit = $('trigger-lookup-submit'), lookupMessage = $('trigger-lookup-message');
    const result = $('trigger-lookup-result'), resultMessage = $('trigger-lookup-result-message');
    const resultFields = $('trigger-lookup-result-fields'), resultIds = $('trigger-lookup-result-ids'), openRunButton = $('trigger-open-run');

    function updateDirectoryControls() {
      previous.disabled = directoryLoading || pageIndex === 0;
      next.disabled = directoryLoading || !nextCursor;
      pageLabel.textContent = `第 ${pageIndex + 1} 页`;
    }
    function clearLookup() {
      lookupGeneration += 1; lookupLoading = false; selected = null;
      form.hidden = true; eventInput.value = ''; eventInput.disabled = false; submit.disabled = false;
      result.hidden = true; resultFields.replaceChildren(); resultIds.replaceChildren();
      resultMessage.textContent = ''; lookupMessage.textContent = '输入已有事件标识后进行只读核对。';
      openRunButton.hidden = true; openRunButton.onclick = null;
    }
    function clear() {
      directoryGeneration += 1; directoryLoading = false;
      clearLookup(); nextCursor = null; pageIndex = 0; cursors = [null]; pages = [];
      list.replaceChildren(); message.textContent = '连接后读取已配置入口。';
      updateDirectoryControls();
    }
    function renderLookup(data) {
      const labels = {
        definition_missing:'未找到入口定义。缺少本地定义不表示事件未执行；不要据此重新投递。',
        root_missing:'未找到本地入口根记录。缺少本地记录不表示事件未执行；不要据此重新投递。',
        recorded:`已找到本地提交记录：${submissionLabels[data.submissionStatus]}。此状态不代表流程执行成功。`,
      };
      resultFields.replaceChildren(); resultIds.replaceChildren();
      appendText(resultFields,'dt','', '核对结果'); appendText(resultFields,'dd','',lookupStatusLabels[data.lookupStatus]);
      if (data.submissionStatus !== null) {
        appendText(resultFields,'dt','', '提交情况'); appendText(resultFields,'dd','',submissionLabels[data.submissionStatus]);
      }
      appendText(resultFields,'dt','', '本地核对时间'); appendText(resultFields,'dd','',new Date(data.observedAt*1000).toLocaleString());
      for (const [label,value] of [['入口标识',data.triggerId],['项目标识',data.projectId],['工作流绑定',data.bindingId],['运行标识',data.runId]]) {
        if (value !== null) { appendText(resultIds,'dt','',label); appendText(resultIds,'dd','',value); }
      }
      resultMessage.textContent = labels[data.lookupStatus];
      result.hidden = false;
      openRunButton.hidden = data.runId === null || typeof openRun !== 'function';
      openRunButton.onclick = openRunButton.hidden ? null : () => openRun({bindingId:data.bindingId,runId:data.runId});
    }
    function renderDirectory(records) {
      list.replaceChildren();
      for (const trigger of records) {
        const item = document.createElement('li'), row = document.createElement('div');
        row.className = 'task-row trigger-row';
        appendText(row,'strong','',`${trigger.type === 'webhook' ? 'Webhook' : '计划入口'} · 已配置`);
        appendText(row,'span','task-context',`项目 ${trigger.projectId} · 工作流 ${trigger.bindingId}`);
        const ids = document.createElement('details'); ids.className = 'task-row-identifiers';
        const summary = appendText(ids,'summary','','查看入口标识');
        appendText(ids,'p','task-context',trigger.triggerId); ids.prepend(summary);
        const choose = appendText(row,'button','secondary',selected?.triggerId === trigger.triggerId ? '当前入口' : '选择入口');
        choose.type = 'button'; choose.disabled = lookupLoading;
        choose.addEventListener('click',() => {
          clearLookup(); selected = trigger; form.hidden = false;
          title.textContent = `核对原事件 · ${trigger.type === 'webhook' ? 'Webhook' : '计划入口'}`;
          hint.textContent = trigger.type === 'webhook'
            ? '输入原始 Webhook 事件标识（1–1024 字符，不含控制字符）。'
            : '输入计划原事件标识，格式为 UTC 分钟：YYYY-MM-DDTHH:mm:00Z；不会自动填入当前时间。';
          eventInput.maxLength = trigger.type === 'webhook' ? 1024 : 21;
          eventInput.placeholder = trigger.type === 'schedule' ? '例如 2026-01-31T09:30:00Z' : '输入已有事件标识';
          lookupMessage.textContent = '核对只读取本地原事件记录，不会投递事件。';
          renderDirectory(records);
          eventInput.focus();
        });
        item.append(row,ids); list.append(item);
      }
      updateDirectoryControls();
    }
    async function loadPage(after, index) {
      const generation = ++directoryGeneration;
      clearLookup(); directoryLoading = true; list.replaceChildren(); nextCursor = null;
      message.textContent = '正在读取已配置入口…'; updateDirectoryControls();
      try {
        const query = new URLSearchParams({limit:'25'});
        if (after !== null) query.set('after',after);
        const data = await request(`/triggers?${query}`);
        if (generation !== directoryGeneration) return;
        const previousPage = index > 0 ? pages[index - 1] : null;
        validateDirectory(data,after,previousPage?.lastId ?? null);
        pages[index] = {after, triggers:data.triggers, nextCursor:data.nextCursor, lastId:data.triggers.at(-1)?.triggerId ?? null};
        pages.length = index + 1;
        pageIndex = index; cursors[index] = after; cursors.length = index + 1;
        nextCursor = data.nextCursor;
        renderDirectory(data.triggers);
        message.textContent = data.triggers.length ? `当前页 ${data.triggers.length} 个已配置入口。` : '当前凭证下没有已配置入口。';
      } catch (error) {
        if (generation !== directoryGeneration) return;
        list.replaceChildren(); nextCursor = null; pages = []; cursors = [null]; pageIndex = 0;
        if (error.status === 404 && after === null) {
          clearLookup(); message.textContent = '当前凭证下未配置自动入口。';
        } else message.textContent = `无法核验自动入口目录；可刷新重试。${error.message || ''}`;
      } finally {
        if (generation === directoryGeneration) { directoryLoading = false; updateDirectoryControls(); }
      }
    }
    async function connect() {
      clear();
      return loadPage(null,0);
    }
    async function lookup(event) {
      event.preventDefault();
      if (!selected || lookupLoading) return;
      const trigger = selected, eventId = eventInput.value;
      if (!validEventId(trigger.type,eventId)) {
        lookupMessage.textContent = trigger.type === 'webhook'
          ? '事件标识须为 1–1024 字符且不能包含控制字符。'
          : '请输入有效 UTC 分钟时间，格式为 YYYY-MM-DDTHH:mm:00Z。';
        eventInput.focus(); return;
      }
      const generation = ++lookupGeneration;
      lookupLoading = true; submit.disabled = true; eventInput.disabled = true;
      result.hidden = true; resultFields.replaceChildren(); resultIds.replaceChildren(); openRunButton.hidden = true;
      lookupMessage.textContent = '正在核对原事件记录…';
      try {
        const data = await request(`/triggers/${encodeURIComponent(trigger.triggerId)}/lookup`,{body:{eventId},readOnly:true});
        if (generation !== lookupGeneration || selected?.triggerId !== trigger.triggerId) return;
        renderLookup(validateLookup(data,trigger));
        lookupMessage.textContent = '核对完成；该操作没有投递或重提事件。';
      } catch (error) {
        if (generation !== lookupGeneration || selected?.triggerId !== trigger.triggerId) return;
        result.hidden = true; resultFields.replaceChildren(); resultIds.replaceChildren();
        lookupMessage.textContent = `核对未确认；保留原事件标识，可再次只读核对，不要生成新标识。${error.message || ''}`;
      } finally {
        if (generation === lookupGeneration && selected?.triggerId === trigger.triggerId) {
          lookupLoading = false; submit.disabled = false; eventInput.disabled = false;
        }
      }
    }
    $('trigger-directory-refresh').addEventListener('click',() => loadPage(cursors[pageIndex] ?? null,pageIndex));
    previous.addEventListener('click',() => {
      if (pageIndex > 0 && pages[pageIndex - 1]) {
        const target = pageIndex - 1, cached = pages[target];
        clearLookup(); pageIndex = target; nextCursor = cached.nextCursor;
        renderDirectory(cached.triggers); message.textContent = `当前页 ${cached.triggers.length} 个已配置入口。`;
      }
    });
    next.addEventListener('click',() => {
      if (!directoryLoading && nextCursor) loadPage(nextCursor,pageIndex + 1);
    });
    form.addEventListener('submit',lookup);
    eventInput.addEventListener('input',() => {
      lookupGeneration += 1; lookupLoading = false; submit.disabled = false; eventInput.disabled = false;
      result.hidden = true; resultFields.replaceChildren(); resultIds.replaceChildren(); openRunButton.hidden = true;
      lookupMessage.textContent = '事件标识已更改；需重新核对后查看结果。';
    });
    return {connect,clear};
  }
  window.AgentLineTriggerInspection = {create};
})();
