"use strict";

(() => {
  const services = ["workflows", "authoring", "approvals", "memories", "triggers"];
  const serviceLabels = { workflows: "工作流运行", authoring: "流程搭建", approvals: "人工审批", memories: "记忆审核", triggers: "自动入口" };
  const executionLabels = { confirmed: "已确认", unknown: "未知", unsupported: "不支持" };
  const blockLabels = { none: "未报告阻断原因", compatibility_blocked: "兼容检查阻止执行", readiness_unconfirmed: "执行就绪尚未确认", readiness_stale: "执行就绪信息已过期", reconciliation_required: "需要恢复核对" };
  function create({ request, context, safe }) {
    const button = document.getElementById("service-capabilities-read");
    const message = document.getElementById("service-capabilities-message");
    const results = document.getElementById("service-capabilities-results");
    const versionDisclosure = document.getElementById("service-capabilities-versions");
    const versionDetails = document.getElementById("service-capabilities-version-rows");
    let generation = 0;
    let inFlight = false;
    function clear() {
      generation += 1; inFlight = false; results.replaceChildren(); versionDetails.replaceChildren();
      versionDisclosure.open = false;
      message.textContent = "授权后，手动读取此服务提供的只读能力摘要。";
      button.disabled = !context().authorized; button.textContent = "读取能力摘要";
    }
    function sync() {
      button.disabled = !context().authorized || inFlight;
      if (!context().authorized) clear();
    }
    function valid(data) {
      const exactKeys = (value, keys) => value && typeof value === "object" && !Array.isArray(value)
        && Object.keys(value).sort().join("\0") === [...keys].sort().join("\0");
      const rootKeys = ["schemaVersion", "source", "readOnly", "observedAt", "apiContractVersion", "execution", "services", "versions", "versionSource"];
      if (Object.hasOwn(data || {}, "nativeExecutionIdentity")) rootKeys.push("nativeExecutionIdentity");
      if (!data || typeof data !== "object" || Array.isArray(data)
          || !exactKeys(data, rootKeys)
          || data.schemaVersion !== "1.0" || data.source !== "authorized_service_capabilities"
          || data.readOnly !== true || typeof data.observedAt !== "number" || !Number.isFinite(data.observedAt)
          || data.observedAt <= 0 || data.observedAt > 8640000000000
          || data.observedAt > Date.now() / 1000 + 300
          || data.apiContractVersion !== "1.0" || data.versionSource !== "not_observed") return false;
      const execution = data.execution;
      if (!exactKeys(execution, ["controlledExecution", "readyForExecution", "reconciliationRequired", "blockReason"])
          || !Object.hasOwn(executionLabels, execution.controlledExecution)
          || typeof execution.readyForExecution !== "boolean" || typeof execution.reconciliationRequired !== "boolean"
          || !Object.hasOwn(blockLabels, execution.blockReason)
          || (execution.readyForExecution && (execution.controlledExecution !== "confirmed" || execution.reconciliationRequired || execution.blockReason !== "none"))
          || (execution.blockReason === "none" && !execution.readyForExecution)
          || (execution.blockReason === "reconciliation_required" && (!execution.reconciliationRequired || execution.readyForExecution))
          || (execution.reconciliationRequired && (!["reconciliation_required", "compatibility_blocked"].includes(execution.blockReason) || execution.readyForExecution))
          || (execution.blockReason !== "none" && execution.readyForExecution)) return false;
      if (!Array.isArray(data.services) || data.services.length !== services.length) return false;
      const seen = new Set();
      for (const item of data.services) {
        if (!exactKeys(item, ["id", "state"]) || !services.includes(item.id)
            || seen.has(item.id) || !["configured", "not_configured"].includes(item.state)) return false;
        seen.add(item.id);
      }
      if (!(seen.size === services.length && exactKeys(data.versions, ["qwenpaw", "sim"])
        && data.versions.qwenpaw === null && data.versions.sim === null)) return false;
      if (!Object.hasOwn(data, "nativeExecutionIdentity")) return true;
      const identity = data.nativeExecutionIdentity;
      const field = (value) => exactKeys(value, ["state", "reason"]) && value.state === "not_written"
        && typeof value.reason === "string" && value.reason.trim() === value.reason
        && value.reason.length > 0 && value.reason.length <= 400
        && !value.reason.includes("agent-line-") && !value.reason.includes("\0");
      return exactKeys(identity, ["readOnly", "mutatesStoredIdentity", "roleSessionId", "executorAgentId", "verifiable"])
        && identity.readOnly === true && identity.mutatesStoredIdentity === false
        && field(identity.roleSessionId) && field(identity.executorAgentId)
        && exactKeys(identity.verifiable, ["roleAgentId", "upstreamTaskId"])
        && identity.verifiable.roleAgentId === "task_field"
        && identity.verifiable.upstreamTaskId === "recorded_execution_identity_only";
    }
    function row(parent, label, value) {
      const name = document.createElement("dt"); name.textContent = safe(label);
      const result = document.createElement("dd"); result.textContent = safe(value);
      parent.append(name, result);
    }
    function render(data) {
      results.replaceChildren();
      const summary = document.createElement("dl"); summary.className = "run-budget-fields";
      row(summary, "受控节点执行", executionLabels[data.execution.controlledExecution]);
      row(summary, "新节点任务", data.execution.readyForExecution ? "就绪已确认" : "未开放");
      row(summary, "恢复核对", data.execution.reconciliationRequired ? "需要核对" : "未报告需要核对");
      row(summary, "执行阻断", blockLabels[data.execution.blockReason]);
      results.append(summary);
      const list = document.createElement("dl"); list.className = "run-budget-fields";
      for (const id of services) {
        const item = data.services.find(service => service.id === id);
        row(list, serviceLabels[id], item.state === "configured" ? "已配置" : "未配置");
      }
      results.append(list);
      if (data.nativeExecutionIdentity) {
        const identity = document.createElement("dl"); identity.className = "run-budget-fields";
        row(identity, "角色会话", data.nativeExecutionIdentity.roleSessionId.reason);
        row(identity, "执行者", data.nativeExecutionIdentity.executorAgentId.reason);
        row(identity, "可核对替代", "任务上的角色 ID；上游任务号只在已记录时有效");
        results.append(identity);
      }
      versionDetails.replaceChildren();
      const versions = document.createElement("dl"); versions.className = "run-budget-fields";
      row(versions, "QwenPaw 实际运行版本", "未知");
      row(versions, "Sim 实际运行版本", "未知");
      row(versions, "API 契约版本", data.apiContractVersion);
      row(versions, "观察时间", new Date(data.observedAt * 1000).toLocaleString());
      versionDetails.append(versions);
      message.textContent = "摘要仅描述服务配置与已报告的节点执行状态。模块已配置不代表当前身份获准执行编辑、审批或记忆操作；就绪状态仅适用于新节点任务。实际上游版本未知。";
    }
    async function read() {
      if (!context().authorized || inFlight) return;
      const started = context(), current = ++generation;
      results.replaceChildren(); versionDetails.replaceChildren();
      inFlight = true; button.disabled = true; button.textContent = "正在读取…"; message.textContent = "正在读取能力摘要。";
      try {
        const data = await request("/capabilities", { readOnly: true });
        const latest = context();
        if (current !== generation || latest.epoch !== started.epoch || !latest.authorized) return;
        if (!valid(data)) { message.textContent = "服务响应不符合能力摘要契约，状态无法核对。"; return; }
        render(data);
      } catch (error) {
        const latest = context();
        if (current !== generation || latest.epoch !== started.epoch || !latest.authorized) return;
        message.textContent = error?.status === 404 ? "当前服务版本不提供能力摘要。" : "能力摘要暂时不可用，请稍后手动重试。";
      } finally {
        const latest = context();
        if (current === generation && latest.epoch === started.epoch && latest.authorized) {
          inFlight = false; button.disabled = false; button.textContent = "重新读取能力摘要";
        }
      }
    }
    button.addEventListener("click", read); clear(); return { clear, sync };
  }
  window.AgentLineServiceCapabilities = { create };
})();
