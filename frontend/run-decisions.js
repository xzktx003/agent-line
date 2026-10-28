'use strict';
(() => {
  const plain = value => !!value && typeof value === 'object' && !Array.isArray(value);
  const safeId = (value, max=128) => typeof value === 'string' && value.length > 0
    && value.length <= max && /^[A-Za-z0-9_-]+$/.test(value);
  const unixSeconds = value => typeof value === 'number' && Number.isFinite(value)
    && value >= 0 && value <= 8_640_000_000_000;
  const identityFields = ['bindingId','projectId','workflowId','runId','revision','graphSha256'];
  const identityKey = value => plain(value) ? JSON.stringify(identityFields.map(key=>value[key])) : null;
  const sameIdentity = (left,right) => plain(left) && plain(right)
    && Object.keys(left).length === identityFields.length
    && identityFields.every(key=>Object.hasOwn(left,key)&&left[key]===right[key]);

  function validate(data, identity, graph, after) {
    if (!plain(data) || data.source !== 'fixed_publication_run_condition_decisions' || data.readOnly !== true
      || !sameIdentity(data.identity,identity) || !unixSeconds(data.observedAt)
      || data.scope !== 'native_current_block_outputs' || !Array.isArray(data.conditions)
      || data.conditions.length > 20 || !(data.nextCursor === null || safeId(data.nextCursor))) {
      throw new Error('条件记录响应无法核验。');
    }
    const conditions = graph.nodes.filter(node=>node.type==='condition');
    const conditionById = new Map(conditions.map(node=>[node.id,node]));
    const nodes = new Map(graph.nodes.map(node=>[node.id,node]));
    const seen = new Set(); let previous = after || '';
    for (const item of data.conditions) {
      const node = conditionById.get(item?.nodeId);
      const expectedItemKeys = item?.state === 'matched'
        ? ['nodeId','executionNodeId','state','decision'] : ['nodeId','executionNodeId','state'];
      if (!plain(item) || !safeId(item.nodeId) || !node || item.executionNodeId !== node.executionNodeId
        || Object.keys(item).length !== expectedItemKeys.length || !expectedItemKeys.every(key=>Object.hasOwn(item,key))
        || seen.has(item.nodeId) || item.nodeId <= previous
        || !['matched','unobserved','unverified'].includes(item.state)) {
        throw new Error('条件记录节点或排序无法核验。');
      }
      seen.add(item.nodeId); previous = item.nodeId;
      if (item.state !== 'matched') {
        if (Object.hasOwn(item,'decision')) throw new Error('未命中状态包含不应显示的决定记录。');
        continue;
      }
      const decision = item.decision;
      const decisionKeys = ['ruleId','edgeId','targetNodeId','isDefault','conditionResult'];
      if (!plain(decision) || Object.keys(decision).length !== decisionKeys.length
        || !decisionKeys.every(key=>Object.hasOwn(decision,key))
        || !safeId(decision.ruleId) || !safeId(decision.edgeId,200) || !safeId(decision.targetNodeId)
        || typeof decision.isDefault !== 'boolean' || typeof decision.conditionResult !== 'boolean'
        || decision.isDefault !== (decision.ruleId === 'else')
        || decision.conditionResult !== !decision.isDefault || !nodes.has(decision.targetNodeId)) {
        throw new Error('条件命中决定结构无法核验。');
      }
      const branchEdges = graph.edges.filter(edge=>edge.source===node.id && edge.branch===decision.ruleId);
      if (branchEdges.length !== 1 || branchEdges[0].id !== decision.edgeId
        || branchEdges[0].target !== decision.targetNodeId) {
        throw new Error('条件命中连线与固定版本图不匹配。');
      }
    }
    if (data.nextCursor !== null && (data.conditions.length !== 20
      || data.nextCursor !== data.conditions[data.conditions.length-1]?.nodeId
      || data.nextCursor === after)) throw new Error('条件记录分页游标无法核验。');
    if (after && data.conditions.length && data.conditions[0].nodeId <= after)
      throw new Error('条件记录分页顺序无法核验。');
    return data;
  }

  function create({container,request,identity,graph}) {
    if (!container || typeof request !== 'function' || typeof identity !== 'function' || typeof graph !== 'function')
      throw new TypeError('条件记录需要容器、授权请求与固定运行图。');
    const button=container.querySelector('[data-decision-read]');
    const previous=container.querySelector('[data-decision-prev]');
    const next=container.querySelector('[data-decision-next]');
    const message=container.querySelector('[data-decision-message]');
    const list=container.querySelector('[data-decision-list]');
    const pageLabel=container.querySelector('[data-decision-page]');
    let generation=0,loading=false,contextIdentity=null,contextGraph=null,contextKey=null;
    let cursors=[null],page=0,nextCursor=null,blocked=false;

    function clearRecords(note='尚未读取条件命中记录。') {
      generation++; loading=false; cursors=[null]; page=0; nextCursor=null;
      list.replaceChildren(); pageLabel.textContent='第 1 页'; message.textContent=note;
      refreshControls(blocked);
    }
    function clear() {
      contextIdentity=null; contextGraph=null; contextKey=null; container.hidden=true; clearRecords();
    }
    function current() {
      const liveIdentity=identity(),liveGraph=graph();
      return !!contextIdentity && identityKey(liveIdentity)===contextKey
        && identityKey(contextIdentity)===contextKey && JSON.stringify(liveGraph)===JSON.stringify(contextGraph);
    }
    function refreshControls(isBlocked=blocked) {
      blocked=!!isBlocked;
      if (contextIdentity && !current()) {
        contextIdentity=null; contextGraph=null; contextKey=null; container.hidden=true;
        generation++; loading=false; cursors=[null]; page=0; nextCursor=null;
        list.replaceChildren(); pageLabel.textContent='第 1 页'; message.textContent='运行身份或固定版本图已变化，条件记录已清除。';
      }
      const available=!!contextIdentity&&!!contextGraph&&current();
      button.disabled=blocked||loading||!available;
      previous.disabled=blocked||loading||!available||page===0;
      next.disabled=blocked||loading||!available||nextCursor===null;
    }
    function setContext(newIdentity,newGraph) {
      const nextKey=identityKey(newIdentity),graphJson=JSON.stringify(newGraph);
      if (!nextKey || !plain(newGraph) || !Array.isArray(newGraph.nodes) || !Array.isArray(newGraph.edges)) {
        clear(); return;
      }
      if (nextKey!==contextKey || JSON.stringify(contextGraph)!==graphJson) clearRecords();
      contextIdentity={...newIdentity}; contextGraph=newGraph; contextKey=nextKey;
      container.hidden=false; refreshControls(blocked);
    }
    function labelFor(item) {
      const node=contextGraph.nodes.find(value=>value.id===item.nodeId);
      if (item.state==='unobserved') return `${node?.name || '条件节点'}：当前块尚无可核验条件输出；这不能说明节点未执行。`;
      if (item.state==='unverified') return `${node?.name || '条件节点'}：当前块输出无法核验。`;
      const decision=item.decision, target=contextGraph.nodes.find(value=>value.id===decision.targetNodeId);
      return `${node?.name || '条件节点'}：${decision.isDefault?'否则分支（前置规则均未匹配）':'条件成立（规则 '+decision.ruleId+'）'}；选择目标：${target?.name || '目标节点'}。`;
    }
    function render(data) {
      list.replaceChildren();
      for (const item of data.conditions) {
        const row=document.createElement('li'); row.className='run-decision-row';
        const text=document.createElement('p'); text.textContent=labelFor(item); row.append(text); list.append(row);
      }
      nextCursor=data.nextCursor;
      if (nextCursor!==null) cursors[page+1]=nextCursor;
      pageLabel.textContent=`第 ${page+1} 页`;
      message.textContent=`当前块输出范围 · ${data.conditions.length} 条条件记录 · 读取于 ${new Date(data.observedAt*1000).toLocaleString()}。这不是完整激活历史或整条路径结论。`;
      refreshControls(blocked);
    }
    async function readPage(targetPage=page) {
      if (loading) return;
      if (!current()) { refreshControls(blocked); return; }
      const identitySnapshot={...contextIdentity}, graphSnapshot=contextGraph, key=contextKey;
      const after=cursors[targetPage] ?? null, requestGeneration=++generation;
      const query=new URLSearchParams({graphSha256:identitySnapshot.graphSha256,limit:'20'});
      if (after!==null) query.set('after',after);
      loading=true; refreshControls(blocked); message.textContent='正在读取当前块条件记录…';
      try {
        const response=await request(`/workflows/${encodeURIComponent(identitySnapshot.bindingId)}/runs/${encodeURIComponent(identitySnapshot.runId)}/view/decisions?${query}`);
        if (requestGeneration!==generation || !current() || contextKey!==key
          || JSON.stringify(contextGraph)!==JSON.stringify(graphSnapshot)) return;
        validate(response,identitySnapshot,graphSnapshot,after);
        page=targetPage; render(response);
      } catch (_) {
        if (requestGeneration!==generation) return;
        clearRecords('条件命中记录无法核验，已清除此视图；固定版本运行图保留。请重新读取。');
      } finally {
        if (requestGeneration===generation) { loading=false; refreshControls(blocked); }
      }
    }
    button.addEventListener('click',()=>readPage(page));
    previous.addEventListener('click',()=>{if(!loading&&page>0)readPage(page-1);});
    next.addEventListener('click',()=>{if(!loading&&nextCursor!==null)readPage(page+1);});
    container.addEventListener('toggle',()=>refreshControls(blocked));
    clear();
    return {clear,setContext,refreshControls};
  }
  window.AgentLineRunDecisions={create};
})();
