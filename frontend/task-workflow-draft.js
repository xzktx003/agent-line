(() => {
  "use strict";
  const isObject = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const canonical = value => {
    if (value === null || typeof value === "string" || typeof value === "boolean") return JSON.stringify(value);
    if (typeof value === "number" && Number.isFinite(value)) return JSON.stringify(value);
    if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
    if (!isObject(value)) throw new Error("草稿不是有效的 JSON 对象。");
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  };
  function render({container, task, request, isCurrent, safeText}) {
    const source = task?.structuredOutput;
    if (task?.executionStatus !== "succeeded" || task.resourcesReleased !== true
        || task.outputValidation?.schemaId !== "workflow-draft-v1" || task.outputValidation?.state !== "valid"
        || !isObject(source) || !Number.isSafeInteger(task.version) || task.version < 0
        || typeof task.taskId !== "string" || typeof task.projectId !== "string"
        || typeof task.verificationStatus !== "string") return;

    const details = document.createElement("details");
    details.className = "task-trace workflow-draft-review";
    const summary = document.createElement("summary"); summary.textContent = "生成的流程草稿";
    const help = document.createElement("p"); help.className = "field-help";
    help.textContent = "模型生成内容仍是候选草稿；结构核对不代表独立验收或人工批准。核对后可手动导入流程编辑器并选择角色。此操作不会保存、发布或执行流程。";
    const check = document.createElement("button"); check.type = "button"; check.className = "secondary";
    check.textContent = "核对草稿";
    const status = document.createElement("p"); status.className = "field-help";
    status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
    status.textContent = "点击后只读核对当前任务版本；不会再次调用模型。";
    const output = document.createElement("div");
    details.append(summary, help, check, status, output);
    container.append(details);
    const captured = task;
    let loading = false;
    check.addEventListener("click", async () => {
      if (loading || !isCurrent() || captured !== task) return;
      loading = true; check.disabled = true;
      output.replaceChildren();
      status.textContent = "正在只读核对当前任务版本…";
      try {
        const result = await request(`/node-tasks/${encodeURIComponent(task.taskId)}/workflow-draft`,
          {body: {expectedVersion: task.version}, readOnly: true});
        if (!isCurrent()) return;
        const template = result?.template;
        if (result?.source !== "validated_model_workflow_draft" || result.readOnly !== true
            || result.executionSupported !== false || result.saved !== false || result.published !== false
            || result.projectId !== task.projectId || result.taskId !== task.taskId
            || result.taskVersion !== task.version || result.verificationStatus !== task.verificationStatus
            || !/^[a-f0-9]{64}$/.test(result.templateSha256 || "") || !isObject(template)
            || template.format !== "agent-line-workflow-template" || template.schemaVersion !== 1
            || !isObject(template.document) || template.document.schemaVersion !== 1
            || !Array.isArray(template.document.nodes) || !Array.isArray(template.document.edges)
            || !Array.isArray(template.roleSlots)
            || template.document.nodes.length > 200 || template.document.edges.length > 1000
            || template.roleSlots.length > 200
            || !Number.isSafeInteger(result.nodeCount) || result.nodeCount !== template.document.nodes.length
            || !Number.isSafeInteger(result.edgeCount) || result.edgeCount !== template.document.edges.length
            || !Number.isSafeInteger(result.roleSlotCount) || result.roleSlotCount !== template.roleSlots.length
            || canonical(template) !== canonical(source)) {
          throw new Error("核对回执与原任务草稿、身份或规模不一致，已丢弃结果；请重新查询原任务。");
        }
        const raw = JSON.stringify(template);
        if (new TextEncoder().encode(raw).length > 64000) throw new Error("草稿超过 64,000 字节限制，已拒绝展示和下载。");
        const credentialPresent = typeof safeText === "function" && safeText(raw) !== raw;
        const counts = document.createElement("p"); counts.className = "field-help";
        counts.textContent = `已核对结构：${result.nodeCount} 个节点 · ${result.edgeCount} 条边 · ${result.roleSlotCount} 个角色槽。${credentialPresent ? "正文含当前凭证，已脱敏展示且禁止下载。" : ""}`;
        const jsonDetails = document.createElement("details"); jsonDetails.className = "technical-details";
        const jsonSummary = document.createElement("summary"); jsonSummary.textContent = "查看完整 JSON";
        const pre = document.createElement("pre"); pre.className = "technical";
        const display = JSON.stringify(template, null, 2);
        pre.textContent = typeof safeText === "function" ? safeText(display) : display;
        jsonDetails.append(jsonSummary, pre);
        output.append(counts, jsonDetails);
        if (credentialPresent) {
          status.textContent = "草稿已与原任务逐字段核对。正文含当前凭证，已隐藏对应文本并禁用下载。";
          return;
        }
        const confirmation = document.createElement("label"); confirmation.className = "field-help";
        const confirm = document.createElement("input"); confirm.type = "checkbox";
        confirmation.append(confirm, document.createTextNode(" 我已核对角色、分支和指令；仅下载未发布草稿"));
        const download = document.createElement("button"); download.type = "button"; download.className = "secondary";
        download.textContent = "下载未发布草稿 JSON"; download.disabled = true;
        confirm.addEventListener("change", () => { download.disabled = !confirm.checked || !isCurrent(); });
        download.addEventListener("click", () => {
          if (!confirm.checked || !isCurrent()) return;
          const blob = new Blob([raw], {type: "application/json;charset=utf-8"});
          const url = URL.createObjectURL(blob), link = document.createElement("a");
          link.href = url; link.download = `workflow-draft-${task.taskId.replace(/[^A-Za-z0-9_-]/g, "_")}.json`;
          link.hidden = true; document.body.append(link); link.click(); link.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
          status.textContent = "已下载未发布的可移植模板。请在流程编辑器中手动导入并选择角色；未保存、发布或执行。";
        });
        output.append(confirmation, download);
        status.textContent = "草稿已与原任务逐字段核对。请完整检查节点、分支和指令；核对通过不代表批准。";
      } catch (error) {
        if (!isCurrent()) return;
        status.textContent = error?.message || "未能核对草稿，请查询原任务后重试。";
      } finally {
        loading = false;
        if (isCurrent()) check.disabled = false;
      }
    });
  }
  window.AgentLineTaskWorkflowDraft = {render};
})();
