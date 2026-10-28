'use strict';
(() => {
  const plain = value => !!value && typeof value === 'object' && !Array.isArray(value);
  const id = value => typeof value === 'string' && value.trim().length > 0 && value.length <= 256 && !/[\u0000-\u001f\u007f]/.test(value);
  const hash = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
  const states = {not_recorded:'快照尚未记录结果',success:'原生工具返回成功',error:'原生工具返回错误',unknown:'工具结果无法核对'};
  function validate(value, task) {
    if (!plain(value) || value.source !== 'cached_native_memory_trace' || value.readOnly !== true
      || value.taskId !== task.taskId || value.roleAgentId !== task.roleAgentId
      || value.roleSessionId !== 'agent-line-' + task.taskId
      || !(value.parentTaskId === null || id(value.parentTaskId))
      || value.memoryInjection !== 'unverified' || value.coverage !== 'cached_parent_snapshot'
      || value.snapshotCapturedAt !== null || typeof value.readAt !== 'number' || !Number.isFinite(value.readAt)
      || value.readAt <= 0 || value.readAt > 8_640_000_000_000) throw new Error('记忆追踪响应的任务或来源无法核对。');
    if (value.state === 'unknown') {
      if (value.snapshotSha256 !== null || value.searches !== null || value.delegations !== null) throw new Error('未知追踪不能包含已确认的调用摘要。');
      return value;
    }
    if (value.state !== 'observed' || !id(value.parentTaskId) || !hash(value.snapshotSha256)
      || !Array.isArray(value.searches) || value.searches.length > 100
      || !Array.isArray(value.delegations) || value.delegations.length > 100) throw new Error('记忆追踪快照结构无法核对。');
    const calls = new Set(), successful = new Set();
    for (const search of value.searches) {
      if (!plain(search) || !id(search.callId) || calls.has(search.callId) || !hash(search.argumentsSha256)
        || !Object.hasOwn(states,search.resultState)
        || (['success','error'].includes(search.resultState) ? !hash(search.outputSha256) : search.outputSha256 !== null)) {
        throw new Error('召回调用标识、状态或指纹无法核对。');
      }
      calls.add(search.callId); if (search.resultState === 'success') successful.add(search.callId);
    }
    for (const delegation of value.delegations) {
      if (!plain(delegation) || !id(delegation.callId) || calls.has(delegation.callId) || !hash(delegation.instructionSha256)
        || !Object.hasOwn(states,delegation.resultState) || ![null,true,false].includes(delegation.executionTaskMatched)
        || (delegation.resultState !== 'success' && delegation.executionTaskMatched !== null)
        || !Array.isArray(delegation.precedingSearchCallIds) || delegation.precedingSearchCallIds.length > 100
        || new Set(delegation.precedingSearchCallIds).size !== delegation.precedingSearchCallIds.length
        || !delegation.precedingSearchCallIds.every(call => successful.has(call))) throw new Error('委派调用及先前召回关联无法核对。');
      calls.add(delegation.callId);
    }
    return value;
  }
  function add(parent, tag, value, className) {
    const element = document.createElement(tag); element.textContent = value;
    if (className) element.className = className;
    parent.append(element); return element;
  }
  function fields(parent, rows) {
    const list = document.createElement('dl'); list.className = 'run-budget-fields';
    for (const [label,value] of rows) { add(list,'dt',label); add(list,'dd',value); }
    parent.append(list);
  }
  function renderEvaluation(parent, trace, task, request, isCurrent, safeText) {
    if (!Number.isSafeInteger(task.version) || task.version < 0 || !id(task.projectId)) return;
    const panel = document.createElement('details'); panel.className = 'technical-details';
    add(panel, 'summary', '核对预期内容（只查已有快照）');
    add(panel, 'p', '输入一段预期应召回的非敏感文本。仅检查此快照的成功工具结果与关联委派指令中是否出现相同文本；不重新检索，不保存输入，也不评判语义相关性。', 'field-help');
    const label = add(panel, 'label', '预期内容（4至256字符，请勿输入密码或凭证）');
    const input = document.createElement('input'); input.type = 'text'; input.minLength = 4; input.maxLength = 256;
    input.autocomplete = 'off'; input.spellcheck = false; label.append(input);
    const button = add(panel, 'button', '核对此快照', 'secondary'); button.type = 'button'; button.disabled = true;
    const status = add(panel, 'p', '结构和顺序证据不等于内容已进入实际 Codex 输入。', 'field-help');
    status.setAttribute('role', 'status');
    const output = document.createElement('div'); panel.append(output); parent.append(panel);
    let loading = false, generation = 0;
    const validInput = () => Array.from(input.value).length >= 4 && Array.from(input.value).length <= 256
      && input.value === input.value.trim() && !/[\u0000-\u001f\u007f]/.test(input.value);
    input.addEventListener('input', () => {
      generation += 1; output.replaceChildren(); status.textContent = '预期内容已改变，请重新核对当前快照。';
      button.disabled = loading || !validInput();
    });
    button.addEventListener('click', async () => {
      if (loading || !validInput() || !isCurrent() || !panel.isConnected) return;
      const expected = input.value;
      if (typeof safeText === 'function' && safeText(expected) !== expected) {
        status.textContent = '预期内容包含当前凭证，请先移除；未发起核对。'; return;
      }
      const attempt = ++generation;
      loading = true; button.disabled = true; output.replaceChildren(); status.textContent = '正在核对此本地快照…';
      try {
        const value = await request(`/node-tasks/${encodeURIComponent(task.taskId)}/memory-retrieval-evaluation`,
          {body:{expectedVersion:task.version, expectedSnapshotSha256:trace.snapshotSha256, expectedText:expected},readOnly:true});
        if (!isCurrent() || !panel.isConnected || generation !== attempt) return;
        const countKeys = ['successfulSearches','textMatchedSearches','matchedSearchesBeforeDelegation','matchedDelegations'];
        if (!plain(value) || value.source !== 'cached_memory_retrieval_evaluation' || value.readOnly !== true
          || value.taskId !== task.taskId || value.projectId !== task.projectId || value.taskVersion !== task.version
          || value.roleAgentId !== task.roleAgentId || value.roleSessionId !== trace.roleSessionId
          || value.parentTaskId !== trace.parentTaskId || value.snapshotSha256 !== trace.snapshotSha256
          || value.coverage !== 'cached_parent_snapshot' || value.snapshotCapturedAt !== null
          || value.criterion !== 'user_supplied_literal_text' || !hash(value.criterionSha256)
          || value.semanticRelevance !== 'unverified' || value.codexInputInjection !== 'unverified'
          || typeof value.readAt !== 'number' || !Number.isFinite(value.readAt) || value.readAt <= 0 || value.readAt > 8_640_000_000_000
          || !countKeys.every(key => Number.isSafeInteger(value[key]) && value[key] >= 0 && value[key] <= 100)
          || value.successfulSearches !== trace.searches.filter(search => search.resultState === 'success').length
          || value.textMatchedSearches > value.successfulSearches
          || value.matchedSearchesBeforeDelegation > value.textMatchedSearches
          || value.matchedDelegations > trace.delegations.filter(item => item.executionTaskMatched === true).length
          || value.matchedDelegations > 0 && value.matchedSearchesBeforeDelegation === 0) {
          throw new Error('核对回执与原任务、快照或计数不一致，请重新读取原任务。');
        }
        fields(output, [['成功召回工具结果数',value.successfulSearches],['结果文本中出现预期内容',value.textMatchedSearches],
          ['出现在当前执行委派之前的命中召回',value.matchedSearchesBeforeDelegation],
          ['此前召回命中且指令含相同文本的委派',value.matchedDelegations],
          ['语义相关性','尚未评估'],['实际 Codex 输入注入','尚未核实'],['本地核对时间',new Date(value.readAt*1000).toLocaleString()]]);
        status.textContent = value.textMatchedSearches ? '此快照观察到字面文本命中；不能据此证明来源正确、记忆相关或实际注入。'
          : '此快照未观察到预期文本；快照可能不完整，不能据此断言原生检索失败。';
      } catch (error) {
        if (!isCurrent() || !panel.isConnected || generation !== attempt) return;
        output.replaceChildren(); status.textContent = typeof safeText === 'function' ? safeText(error.message) : '本次核对未确认，请重新读取原任务和快照。';
      } finally {
        loading = false;
        if (isCurrent() && panel.isConnected) button.disabled = !validInput();
      }
    });
  }
  function render({container,task,request,isCurrent,safeText}) {
    const panel = document.createElement('details'); panel.className = 'task-trace memory-trace';
    add(panel,'summary','原生记忆召回与委派记录');
    add(panel,'p','只读取本地已保存的原生角色快照，不重新召回或展示记忆正文。调用顺序不能证明记忆已注入 Codex。','field-help');
    const status = add(panel,'p','尚未读取；快照可能缺失或不完整。','field-help');
    status.setAttribute('role','status');
    const read = add(panel,'button','读取已有快照','secondary'); read.type = 'button';
    read.disabled = !id(task.taskId) || !id(task.roleAgentId);
    const result = document.createElement('div'); panel.append(result); container.append(panel);
    let loading = false;
    read.addEventListener('click',async () => {
      if (loading || !isCurrent()) return;
      loading = true; read.disabled = true; result.replaceChildren(); status.textContent = '正在读取已有快照…';
      try {
        const response = await request(`/node-tasks/${encodeURIComponent(task.taskId)}/memory-trace`);
        if (!isCurrent() || !panel.isConnected) return;
        const value = validate(response,task);
        if (value.state === 'unknown') {
          status.textContent = '没有可核对的原生角色快照；不能据此认定没有召回或没有委派。'; return;
        }
        const successes = value.searches.filter(search => search.resultState === 'success').length;
        status.textContent = `此快照记录 ${value.searches.length} 次召回调用，其中 ${successes} 次工具返回成功；记录 ${value.delegations.length} 次委派调用。不是完整调用历史或记忆效果结论。`;
        fields(result,[['记忆注入 Codex','尚未核实'],['快照抓取时间','原账本未记录'],
          ['本地读取时间',new Date(value.readAt*1000).toLocaleString()]]);
        for (const [index,search] of value.searches.entries()) {
          const section = document.createElement('details'); section.className = 'technical-details';
          add(section,'summary',`召回 ${index+1} · ${states[search.resultState]}`);
          fields(section,[['调用标识',search.callId],['查询参数指纹',search.argumentsSha256],
            ['工具结果指纹',search.outputSha256 ?? '尚无可核对结果']]); result.append(section);
        }
        for (const [index,delegation] of value.delegations.entries()) {
          const section = document.createElement('details'); section.className = 'technical-details';
          add(section,'summary',`委派 ${index+1} · ${states[delegation.resultState]}`);
          fields(section,[['调用标识',delegation.callId],['委派指令指纹',delegation.instructionSha256],
            ['执行任务关联',delegation.executionTaskMatched === true ? '回执对应当前原生执行任务'
              : delegation.executionTaskMatched === false ? '回执与当前原生执行任务不符' : '尚未核实'],
            ['此前已成功返回的召回调用',delegation.precedingSearchCallIds.join('、') || '此快照中尚无关联记录']]);
          result.append(section);
        }
        const ids = document.createElement('details'); ids.className = 'technical-details'; add(ids,'summary','快照来源标识');
        fields(ids,[['父角色任务',value.parentTaskId],['角色会话',value.roleSessionId],['快照指纹',value.snapshotSha256]]); result.append(ids);
        renderEvaluation(result, value, task, request, isCurrent, safeText);
      } catch (error) {
        if (!isCurrent() || !panel.isConnected) return;
        result.replaceChildren(); status.textContent = `追踪未确认：${error.message || '请稍后只读查询。'}`;
      } finally {
        loading = false; if (isCurrent() && panel.isConnected) read.disabled = false;
      }
    });
  }
  window.AgentLineMemoryTrace = {render};
})();
