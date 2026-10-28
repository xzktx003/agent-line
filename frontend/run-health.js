'use strict';
(() => {
  const countNames = ['total','queued','unsettled','resourcesAwaitingExit','verificationPending',
    'verificationFailed','memoryPending','memoryFailed','observationUnknown','observationNotRecorded',
    'quietUnsettled','activityUnknown','workspaceLeases'];
  const plain = value => !!value && typeof value === 'object' && !Array.isArray(value);
  const time = value => typeof value === 'number' && Number.isFinite(value) && value > 0 && value <= 8_640_000_000_000;
  function valid(value, expected) {
    if (!plain(value) || value.schemaVersion !== 1 || value.bindingId !== expected.bindingId
      || !plain(value.scope) || value.scope.runId !== expected.runId
      || typeof value.scope.projectId !== 'string' || !value.scope.projectId
      || typeof value.scope.workflowId !== 'string' || !value.scope.workflowId
      || (expected.projectId && expected.projectId !== value.scope.projectId)
      || (expected.workflowId && expected.workflowId !== value.scope.workflowId)
      || value.quietWindowSeconds !== 300 || !time(value.observedAt)
      || value.nativeProgress !== 'unknown' || value.codexActivity !== 'unknown'
      || !plain(value.counts) || Object.keys(value.counts).sort().join(',') !== countNames.slice().sort().join(',')) return false;
    if (value.state === 'unknown') return value.lastTaskEventAt === null && countNames.every(name => value.counts[name] === null);
    if (value.state !== 'known' || !(value.lastTaskEventAt === null || (time(value.lastTaskEventAt) && value.lastTaskEventAt <= value.observedAt))
      || !countNames.every(name => Number.isSafeInteger(value.counts[name]) && value.counts[name] >= 0)
      || !countNames.filter(name => name !== 'workspaceLeases').every(name => value.counts[name] <= value.counts.total)
      || value.counts.queued + value.counts.unsettled > value.counts.total
      || value.counts.resourcesAwaitingExit > value.counts.unsettled
      || value.counts.observationNotRecorded > value.counts.unsettled
      || value.counts.quietUnsettled + value.counts.activityUnknown > value.counts.total) return false;
    return true;
  }
  function text(parent, tag, value, className) {
    const element = document.createElement(tag); element.textContent = value;
    if (className) element.className = className;
    parent.append(element); return element;
  }
  function render(container, value, expected) {
    document.getElementById('run-health-summary')?.remove();
    if (value === undefined) return;
    const panel = document.createElement('details'); panel.id = 'run-health-summary'; panel.className = 'run-health-summary';
    text(panel,'summary','本地运行检查');
    if (!valid(value, expected) || value.state === 'unknown') {
      text(panel,'p','本地任务记录暂时无法核对，不能据此判断没有待处理任务或资源已退出。','field-help');
    } else {
      const counts = value.counts;
      text(panel,'p',counts.total === 0
        ? '此运行暂无本地节点任务记录；不代表原生工作流尚未运行或已完成。'
        : `当前运行有 ${counts.total} 条本地节点任务记录。各项可能重叠，不能相加作为任务总数。`,'field-help');
      const rows = [['排队等待提交',counts.queued],['执行或资源收尾待确认',counts.unsettled],
        ['已提交但资源退出未确认',counts.resourcesAwaitingExit],['成功执行后的验收待处理',counts.verificationPending],
        ['独立验收失败',counts.verificationFailed],['记忆等待核对、审核或删除',counts.memoryPending],
        ['记忆写回失败',counts.memoryFailed],['最近一次执行核对结果未知',counts.observationUnknown],
        ['待收尾任务尚无执行核对记录',counts.observationNotRecorded],
        ['执行或验收待处理项超过5分钟无新本地事件',counts.quietUnsettled],
        ['执行或验收待处理项事件时间无法核对',counts.activityUnknown],['持有的工作区租约',counts.workspaceLeases]];
      const fields = document.createElement('dl'); fields.className = 'run-budget-fields';
      for (const [label,count] of rows) { text(fields,'dt',label); text(fields,'dd',String(count)); }
      text(fields,'dt','最近本地任务事件');
      text(fields,'dd',value.lastTaskEventAt === null ? '尚无可核对记录' : new Date(value.lastTaskEventAt * 1000).toLocaleString());
      text(fields,'dt','本地读取时间'); text(fields,'dd',new Date(value.observedAt * 1000).toLocaleString());
      panel.append(fields);
    }
    text(panel,'p','“无新本地事件”按5分钟展示窗口计算，不证明原生执行停滞；执行核对记录的时间也不是最近巡检时间。Codex 是否活动、原生节点进展仍未知。查看不会调用模型、取消或重新提交任务。','field-help');
    container.append(panel);
  }
  window.AgentLineRunHealth = {render};
})();
