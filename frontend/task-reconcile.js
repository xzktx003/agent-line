(() => {
  "use strict";
  const outcomes = {
    observed: "已取得原执行的有效记录；执行、验收和资源状态仍分别显示。",
    unknown: "原执行仍无法确认，已保留原身份和资源保护。",
    rate_limited: "原服务限流，本次未能确认；请稍后查询原任务。",
    unavailable: "原服务暂时不可用，本次未能确认。",
    authentication: "原执行服务认证未通过，请修复配置后核对原任务。不会自动更换模型或执行路径。",
    model_missing: "配置的执行模型不可用。请修复原模型配置后再核对。不会自动换成其他模型或执行路径，也不会重新提交。",
    not_queried: "本次没有发起原生执行查询，请查看当前任务与后端配置。",
  };
  const active = new Set(["submitting", "uncertain", "accepted", "cancel_requested"]);
  const terminal = new Set(["succeeded", "failed", "canceled"]);
  function render({container, task, request, isCurrent, onTask, notify}) {
    if (!Number.isSafeInteger(task.version) || task.version < 0
        || typeof task.taskId !== "string" || !/^[A-Za-z0-9_-]{1,160}$/.test(task.taskId)
        || !(active.has(task.executionStatus) || terminal.has(task.executionStatus) && task.resourcesReleased === false)) return;
    const button = document.createElement("button"); button.type = "button"; button.className = "secondary";
    button.textContent = "主动核对原执行";
    const message = document.createElement("p"); message.className = "field-help";
    message.setAttribute("role", "status"); message.setAttribute("aria-live", "polite");
    message.textContent = "核对原任务，不重新提交编码；可能同步处理该任务已有的取消、超时或资源收尾。";
    const finding = document.createElement("input");
    finding.type = "text"; finding.maxLength = 500; finding.autocomplete = "off";
    finding.setAttribute("aria-label", "核对发现");
    finding.placeholder = "核对发现，单行，最多500字";
    const decision = document.createElement("input");
    decision.type = "text"; decision.maxLength = 500; decision.autocomplete = "off";
    decision.setAttribute("aria-label", "人工裁决");
    decision.placeholder = "人工裁决，单行，最多500字";
    const allow = document.createElement("input");
    allow.type = "checkbox";
    const allowLabel = document.createElement("label");
    allowLabel.append(allow, document.createTextNode("记录为允许以后另开尝试。这只写裁决，不会创建尝试。"));
    const save = document.createElement("button");
    save.type = "button"; save.className = "secondary";
    save.textContent = "保存人工裁决";
    let running = false;
    function sameTask(result) {
      const updated = result?.task;
      return result?.source === "original_execution_reconciliation" && result.taskId === task.taskId
        && result.submissionAttempted === false && Object.hasOwn(outcomes, result.outcome)
        && updated && updated.taskId === task.taskId && updated.projectId === task.projectId
        && updated.roleBindingId === task.roleBindingId
        && Number.isSafeInteger(updated.version) && updated.version >= task.version
        && typeof updated.resourcesReleased === "boolean"
        && [...active, ...terminal, "registered"].includes(updated.executionStatus)
        && task.workflow && updated.workflow
        && ["id", "revision", "runId", "nodeId", "nodeExecutionId", "attemptId"].every(key => updated.workflow[key] === task.workflow[key]);
    }
    function explain(error) {
      const detail = typeof error?.message === "string" ? error.message : "";
      if (error?.status === 409 && (detail.includes("版本 2") || detail.includes("恢复裁决"))) return detail;
      if (error?.status === 409) return "任务状态或身份需要重新核对，请先查询原记录；未重新提交编码。";
      if (error?.status === 422) return detail || "发现和裁决必须是不含控制字符的单行文本。";
      if (error?.status === 404) return "当前服务不提供此核对入口，或原任务已不可访问。";
      return "未取得明确核对回执。请查询原任务记录，不要重新提交编码。";
    }
    button.addEventListener("click", async () => {
      if (running || !isCurrent()) return;
      running = true; button.disabled = true; save.disabled = true; message.textContent = "正在核对原执行身份与状态…";
      try {
        const result = await request(`/recovery/${encodeURIComponent(task.taskId)}/reconcile`,
          {body:{expectedVersion:task.version}});
        if (!isCurrent()) return;
        if (!sameTask(result) || Object.hasOwn(result, "recoveryRecord")) {
          message.textContent = "核对回执身份或结构不一致，请查询原任务；未自动重发。"; return;
        }
        onTask(result.task);
        notify(outcomes[result.outcome], result.outcome === "observed" ? "ok" : "warn");
      } catch (error) {
        if (!isCurrent()) return;
        message.textContent = explain(error);
      } finally {
        running = false;
        if (isCurrent()) { button.disabled = false; save.disabled = false; button.textContent = "再次核对原执行"; }
      }
    });
    save.addEventListener("click", async () => {
      if (running || !isCurrent()) return;
      const found = finding.value.trim();
      const ruled = decision.value.trim();
      if (!found || !ruled || found !== finding.value || ruled !== decision.value) {
        message.textContent = "发现和裁决必须是不含首尾空白的单行文本。未发起核对。"; return;
      }
      running = true; button.disabled = true; save.disabled = true;
      message.textContent = "正在核对原执行并保存人工裁决；不会创建新尝试…";
      try {
        const result = await request(`/recovery/${encodeURIComponent(task.taskId)}/reconcile`,
          {body:{expectedVersion:task.version, finding:found, decision:ruled, allowNewAttempt:allow.checked}});
        if (!isCurrent()) return;
        const record = result?.recoveryRecord;
        if (!sameTask(result) || result.recoveryRecorded !== true || !record
            || record.taskId !== task.taskId || record.action !== "reconcile"
            || record.finding !== found || record.decision !== ruled
            || record.allowNewAttempt !== allow.checked || record.submissionAttempted !== false) {
          message.textContent = "裁决回执身份或结构不一致。请查询原任务；未自动重发，也不要另开尝试。"; return;
        }
        onTask(result.task);
        notify(allow.checked ? "已记录允许以后另开尝试的裁决。本次没有创建尝试。" : "已记录保持原尝试的裁决。本次没有创建尝试。", "ok");
        message.textContent = outcomes[result.outcome];
      } catch (error) {
        if (!isCurrent()) return;
        message.textContent = explain(error);
      } finally {
        running = false;
        if (isCurrent()) { button.disabled = false; save.disabled = false; }
      }
    });
    container.append(button, finding, decision, allow, allowLabel, save, message);
  }
  window.AgentLineTaskReconcile = {render};
})();
