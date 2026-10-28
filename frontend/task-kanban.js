'use strict';
(() => {
  const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const nativeId = value => typeof value === 'string' && /^[A-Za-z0-9_-]{1,256}$/.test(value) ? value : null;
  const utc = value => typeof value === 'string' && /Z$/.test(value) && Number.isFinite(new Date(value).getTime());
  const messages = {
    not_configured:'本项目未登记Kanban只读源，没有发起目录请求。',
    identity_unavailable:'本任务尚无可核对的Codex会话身份，没有发起目录请求。',
    not_observed:'目录中未观察到同一Codex会话、工作区和本地来源的唯一记录；不会自动注册会话。',
    ambiguous:'目录中存在多个匹配会话，无法唯一核对；没有选取或修改其中任何一个。',
    observed:'已读取既有会话目录摘要。摘要可能来自其他回合，不能视为当前任务的执行或验收结论。',
  };
  function render({container,task,request,isCurrent,safeText}) {
    if (!Number.isSafeInteger(task.version) || task.version < 0) return;
    const panel = document.createElement('details'); panel.className = 'task-trace';
    const title = document.createElement('summary'); title.textContent = 'Kanban 会话摘要（只读）';
    const help = document.createElement('p'); help.className = 'field-help';
    help.textContent = '只在项目已登记只读源时查询已有会话目录，不启动、注册、重连或控制会话。会话摘要与本任务当前回合的结果分别核对。';
    const read = document.createElement('button'); read.type = 'button'; read.className = 'secondary'; read.textContent = '读取已有会话摘要';
    const status = document.createElement('p'); status.className = 'field-help'; status.setAttribute('role','status'); status.textContent = '尚未读取。';
    const output = document.createElement('div'); panel.append(title,help,read,status,output); container.append(panel);
    let loading = false;
    read.addEventListener('click', async () => {
      if (loading || !isCurrent() || !panel.isConnected) return;
      loading = true; read.disabled = true; output.replaceChildren(); status.textContent = '正在读取既有目录摘要…';
      try {
        const value = await request(`/node-tasks/${encodeURIComponent(task.taskId)}/kanban?expectedVersion=${task.version}`);
        if (!isCurrent() || !panel.isConnected) return;
        if (!plain(value) || value.source !== 'coding_kanban_readonly' || value.readOnly !== true
          || value.executionSupported !== false || value.taskId !== task.taskId || value.taskVersion !== task.version
          || value.projectId !== task.projectId || value.codexThreadId !== nativeId(task.executionIdentity?.codexThreadId)
          || value.codexTurnId !== nativeId(task.executionIdentity?.codexTurnId)
          || value.coverage !== 'session_directory_snapshot' || value.taskTurnMatched !== null
          || value.resultAuthority !== 'unverified_session_summary' || !Object.hasOwn(messages,value.state)
          || typeof value.readAt !== 'number' || !Number.isFinite(value.readAt) || value.readAt <= 0 || value.readAt > 8_640_000_000_000
          || (['not_configured','identity_unavailable'].includes(value.state) ? value.directoryGeneratedAt !== null : !utc(value.directoryGeneratedAt))) {
          throw new Error('Kanban摘要的任务、身份或来源无法核对。');
        }
        status.textContent = messages[value.state];
        if (value.state !== 'observed') {
          if (value.session !== null) throw new Error('未核对的目录不能包含已确认会话摘要。');
          return;
        }
        const session = value.session;
        if (!plain(session) || !nativeId(session.sessionId)
          || !['online','degraded','offline'].includes(session.connectionState)
          || !['awaiting_input','running','idle','detached','exited'].includes(session.interactionState)
          || !(session.lastAgentMessageSummary === null || typeof session.lastAgentMessageSummary === 'string' && Array.from(session.lastAgentMessageSummary).length <= 4000)
          || !(session.summaryUpdatedAt === null || utc(session.summaryUpdatedAt)) || typeof session.summaryTruncated !== 'boolean') {
          throw new Error('Kanban会话摘要结构无法核对。');
        }
        const connections = {online:'在线',degraded:'连接受限',offline:'离线'};
        const interactions = {awaiting_input:'等待输入',running:'活动中',idle:'空闲',detached:'已脱离',exited:'已退出'};
        const rows = [['会话连接',connections[session.connectionState]],['会话交互',interactions[session.interactionState]],
          ['已记录的Agent摘要',session.lastAgentMessageSummary || '尚无已记录摘要'],
          ['摘要记录时间',session.summaryUpdatedAt ? new Date(session.summaryUpdatedAt).toLocaleString() : '原目录未记录'],
          ['目录生成时间',new Date(value.directoryGeneratedAt).toLocaleString()],
          ['本地读取时间',new Date(value.readAt*1000).toLocaleString()],['当前任务回合匹配','尚未核实']];
        const list = document.createElement('dl'); list.className = 'run-budget-fields';
        for (const [label,text] of rows) {const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=safeText(text);list.append(dt,dd);}
        output.append(list);
        if (session.summaryTruncated) {const note=document.createElement('p');note.className='field-help';note.textContent='摘要超过展示上限，已截断；不是完整执行记录。';output.append(note);}
      } catch (error) {
        if (!isCurrent() || !panel.isConnected) return;
        output.replaceChildren(); status.textContent = safeText(error?.message || 'Kanban摘要尚未核对，请稍后显式重试读取。');
      } finally {
        loading = false; if (isCurrent() && panel.isConnected) read.disabled = false;
      }
    });
  }
  window.AgentLineTaskKanban = {render};
})();
