(() => {
  const kinds = {
    registered: "任务已登记", submission_intent: "执行提交意向已保存", submission_uncertain: "执行提交结果未知",
    accepted: "原生执行身份已绑定", execution_identity_enriched: "原生执行身份已补充",
    succeeded: "已记录执行成功终态", failed: "已记录执行失败终态", canceled: "已记录执行取消终态",
    cancel_requested: "已申请取消执行", canceled_without_execution: "任务未执行即取消",
    execution_interrupted: "执行器已退出，原生终态未知", resources_released: "执行资源已确认退出",
    workspace_lease_recovered: "工作区保护已恢复", resources_quarantined: "执行资源需隔离核对",
    verification_passed: "已记录独立验证通过", verification_failed: "已记录独立验证失败",
    verification_cancel_requested: "已申请停止验收", verification_canceled: "验收已取消",
    memory_pending: "记忆写回待核对", memory_committed: "记忆写入已确认", memory_failed: "记忆写回失败",
    memory_needs_review: "记忆待审核", memory_deleting: "记忆删除中", memory_deleted: "记忆删除已确认",
    execution_observed: "已取得原执行记录", execution_observation_unknown: "最近执行核对结果未知",
    submission_rejected: "提交被明确拒绝，尚未受理",
    submission_rejection_released: "已记录修正后的重新提交",
    role_revision_required: "角色版本尚未固定", role_revision_mismatch: "角色配置与固定版本不符",
    role_revision_unavailable: "角色版本暂时无法核对", role_revision_ready: "角色版本阻塞已解除",
    execution_readiness_stale: "执行准入证据已过期",
    role_started: "角色已启动", memory_recalled: "记忆已召回", delegation_started: "委派已开始",
    approval_created: "审批已创建", approval_decided: "审批已处理",
    artifact_recorded: "产物已记录",
  };
  const categories = {
    task_registered: "任务登记", submission_accepted: "提交受理", role_started: "角色启动",
    memory_recalled: "记忆召回", delegation_started: "委派开始", native_execution: "原生执行",
    approval_created: "审批创建", approval_decided: "审批处理", artifact_recorded: "产物生成",
    verification_recorded: "结果验证", memory_candidate: "记忆候选", memory_writeback: "回写完成",
    recovery_checked: "恢复核对", terminal: "终态",
  };

  const eventLabel = kind => Object.hasOwn(kinds, kind) ? kinds[kind] : "其他已记录事件";
  const categoryLabel = category => Object.hasOwn(categories, category) ? categories[category] : "";
  const validTime = value => typeof value === "number" && Number.isFinite(value)
    && value >= 0 && value <= 8_640_000_000_000;
  const formatTime = value => validTime(value) ? new Date(value * 1000).toLocaleString("zh-CN", {hour12: false}) : "尚无记录";
  function rows(activity) {
    if (!activity || typeof activity !== "object" || Array.isArray(activity)) {
      return [["登记时间", "尚无记录"], ["最近记录", "尚无记录"]];
    }
    const last = activity.lastEvent;
    const validEvent = last && typeof last === "object" && !Array.isArray(last)
      && Number.isSafeInteger(last.sequence) && last.sequence > 0
      && typeof last.kind === "string" && last.kind.length > 0 && last.kind.length <= 160
      && validTime(last.recordedAt);
    const lines = [["登记时间", formatTime(activity.registeredAt)]];
    if (Object.hasOwn(activity, "startedAt")) {
      lines.push(["开始时间", formatTime(activity.startedAt)]);
    }
    lines.push(["最近记录", validEvent ? `${formatTime(last.recordedAt)} · ${eventLabel(last.kind)}` : "尚无记录"]);
    return lines;
  }
  const queueReasons = {
    project_concurrency_limit: "项目执行名额已满，等待已有任务确认退出后再启动",
    awaiting_checks: "已登记，等待执行准入检查",
    admission_closed: "执行准入当前关闭，请等待维护或恢复核对",
    workspace_in_use: "工作区被占用，等待原任务释放资源",
    role_in_use: "角色被占用，等待原任务释放资源",
    earlier_registered_task: "前序登记任务尚未处理，按串行队列等待",
    business_attempt_limit: "本节点在此运行中的提交次数已达上限，需人工处理",
    run_submission_limit: "本次流程运行的提交额度已用尽，需人工处理",
    run_execution_deadline: "本次流程运行的执行时限已到，需人工处理",
    state_unknown: "等待原因尚无法核对，请查询原任务",
    submission_rejected: "提交被明确拒绝且尚未受理，原因已保留。修正前不会自动重发",
  };
  const submissionBlocks = {
    role_revision_required: "角色版本尚未固定，任务尚未启动，请核对角色绑定",
    role_revision_mismatch: "角色配置与固定版本不符，任务尚未启动",
    role_revision_unavailable: "角色版本暂时无法核对，任务尚未启动",
    execution_readiness_stale: "执行准入证据已过期，任务尚未启动",
    submission_rejected: "提交被明确拒绝且尚未受理。原因已保留，修正前不会自动重发",
  };
  function attention(task) {
    if (!task || typeof task !== "object") return "任务状态待核对";
    const status = task.executionStatus;
    if (status === "registered") {
      if (Object.hasOwn(task, "submissionBlock")) {
        return typeof task.submissionBlock === "string" && Object.hasOwn(submissionBlocks, task.submissionBlock)
          ? submissionBlocks[task.submissionBlock] : "任务启动前检查原因待核对，请查询原任务";
      }
      const queue = task.executionQueue;
      if (queue && typeof queue === "object" && !Array.isArray(queue)
          && queue.policy === "serial_fifo" && queue.state === "waiting"
          && typeof queue.reason === "string" && Object.hasOwn(queueReasons, queue.reason)) return queueReasons[queue.reason];
      return "已登记，执行准入原因待核对，请查询原任务";
    }
    if (status === "submitting" || status === "uncertain") return "提交受理结果待核对，请查询原任务，避免重复提交";
    if (status === "cancel_requested") return "已申请取消，等待原执行与资源退出确认";
    if (status === "accepted") {
      return task.executionObservation?.status === "unknown"
        ? "原生执行身份已绑定，但最近核对未取得结果，请继续查询原任务"
        : "原生执行身份已绑定，尚未取得终态记录";
    }
    if (!["succeeded", "failed", "canceled"].includes(status)) return "执行状态待核对，请查询原任务";
    if (task.resourcesReleased !== true) return "执行已结束，资源退出尚未确认";
    if (permissionDenied(task)) return "原生权限已被拒绝。有失败分支时回传一次原状态；没有则保持原任务并转人工。沙箱和审批策略不变，不会因此重新查询、重发执行或当成新的尝试";
    if (confirmedExecutionFailure(task)) return "执行已失败。有失败分支时回传一次原状态；没有则保持原任务并转人工，不会因此重新查询、重发执行或当成新的尝试";
    if (confirmedInterruption(task)) return "执行器已退出且没有原生终态。有失败分支时回传一次原状态；没有则保持原任务并转人工，不会因此重新查询、重发执行或当成新的尝试";
    if (status !== "succeeded") return "";
    if (task.verificationCancellationRequested === true && task.verificationStatus !== "canceled") return "已申请停止验收，等待退出确认";
    if (task.outputValidation?.state === "invalid") return "结果结构无法整理，已转人工；不会自动重新执行，也不会把它当成传输重试";
    if (task.outputValidation?.state === "unsupported") return "结果结构未通过核对，需要人工处理；不会自动重新执行";
    if (task.verificationStatus === "pending") return "执行结果尚未取得独立验收结论";
    if (task.verificationStatus === "failed") return "独立验收未通过。有修复分支时送入该分支；没有则保持原任务并转人工，不会因此重新查询、重发执行或当成传输重试";
    if (task.verificationStatus === "canceled") return "验收已取消，编码结果与产物仍保留";
    if (task.verificationStatus !== "passed") return "独立验收状态待核对";
    const memory = {pending: "记忆回写结果待核对", needs_review: "记忆内容等待人工审核",
      failed: "长期记忆写入失败。只重试记忆阶段，编码结果和产物保留，不会重新执行代码任务", deleting: "记忆删除尚未确认完成"};
    if (Object.hasOwn(memory, task.memoryStatus)) return memory[task.memoryStatus];
    return ["not_requested", "committed", "deleted"].includes(task.memoryStatus) ? "" : "记忆状态待核对";
  }
  function sameIdentity(left, right) {
    if (!left || !right || typeof left !== "object" || typeof right !== "object"
        || Array.isArray(left) || Array.isArray(right)) return false;
    const leftKeys = Object.keys(left);
    const rightKeys = Object.keys(right);
    if (leftKeys.length !== rightKeys.length || !leftKeys.every(key => rightKeys.includes(key) && left[key] === right[key])) return false;
    return typeof left.codexThreadId === "string" && left.codexThreadId.length > 0
      && typeof left.codexTurnId === "string" && left.codexTurnId.length > 0;
  }
  function confirmedInterruption(task) {
    if (!task || typeof task !== "object" || Array.isArray(task)) return false;
    if (task.executionStatus !== "failed" || task.resourcesReleased !== true) return false;
    const identity = task.executionIdentity;
    if (!identity || typeof identity !== "object" || Array.isArray(identity)
        || typeof identity.codexThreadId !== "string" || !identity.codexThreadId
        || typeof identity.codexTurnId !== "string" || !identity.codexTurnId) return false;
    const evidence = Array.isArray(task.evidence) ? task.evidence : [];
    const interrupted = evidence.filter(item => item && typeof item === "object" && !Array.isArray(item)
      && item.kind === "execution_interrupted");
    if (interrupted.length !== 1) return false;
    const event = interrupted[0];
    if (event.taskId !== task.id || !sameIdentity(event.executionIdentity, identity)
        || event.reason !== "owned_executor_exited_without_native_terminal"
        || event.nativeStatus !== "unknown" || event.cancellationRequested !== false) return false;
    const exits = evidence.filter(item => item && typeof item === "object" && !Array.isArray(item)
      && item.kind === "resource_exit");
    if (exits.length !== 1) return false;
    const exited = exits[0];
    const receipt = exited.receipt;
    return exited.taskId === task.id && sameIdentity(exited.executionIdentity, identity)
      && receipt && typeof receipt === "object" && !Array.isArray(receipt)
      && receipt.source === "docker-inspect-before-remove" && receipt.running === false
      && receipt.pid === 0 && (receipt.status === "exited" || receipt.status === "dead");
  }
  function confirmedExecutionFailure(task) {
    if (!task || typeof task !== "object" || Array.isArray(task)) return false;
    if (task.executionStatus !== "failed" || task.resourcesReleased !== true) return false;
    const identity = task.executionIdentity;
    if (!identity || typeof identity !== "object" || Array.isArray(identity)) return false;
    const executionId = (typeof identity.codexTurnId === "string" && identity.codexTurnId)
      || (typeof identity.upstreamTaskId === "string" && identity.upstreamTaskId);
    if (!executionId) return false;
    const evidence = Array.isArray(task.evidence) ? task.evidence : [];
    return evidence.some(item => item && typeof item === "object" && !Array.isArray(item)
      && item.kind === "execution_terminal" && item.executionId === executionId && item.confirmed === true);
  }
  function permissionDenied(task) {
    if (!task || typeof task !== "object" || Array.isArray(task)) return false;
    if (!["succeeded", "failed"].includes(task.executionStatus) || task.resourcesReleased !== true) return false;
    const ids = [task.id, task.taskId].filter(value => typeof value === "string" && value.length > 0);
    if (ids.length < 1 || ids.some(value => value !== ids[0])) return false;
    const decisions = task.approvalDecisions;
    if (!Array.isArray(decisions)) return false;
    const identity = task.executionIdentity;
    const matches = decisions.filter(item => item && typeof item === "object" && !Array.isArray(item)
      && Object.keys(item).length === 4 && item.decision === "reject" && item.taskId === ids[0]
      && item.deliveryStatus === "accepted" && sameIdentity(item.executionIdentity, identity));
    if (matches.length !== 1) return false;
    const evidence = Array.isArray(task.evidence) ? task.evidence : [];
    const policies = evidence.filter(item => item && typeof item === "object" && !Array.isArray(item)
      && item.kind === "execution_policy");
    if (policies.length !== 1) return false;
    const policy = policies[0];
    const controls = policy.controls;
    return Object.keys(policy).length === 4 && policy.source === "qwenpaw-plugin-task-binding"
      && policy.executionId === identity.codexTurnId
      && controls && typeof controls === "object" && !Array.isArray(controls)
      && Object.keys(controls).length === 2 && controls.sandbox === "read-only"
      && controls.approval_policy === "never";
  }
  function waitingReason(task) {
    if (!task || typeof task !== "object") return null;
    const status = task.executionStatus;
    if (status === "registered") {
      if (Object.hasOwn(task, "submissionBlock")) {
        return typeof task.submissionBlock === "string" && Object.hasOwn(submissionBlocks, task.submissionBlock)
          ? submissionBlocks[task.submissionBlock] : "任务启动前检查原因待核对，请查询原任务";
      }
      const queue = task.executionQueue;
      if (queue && typeof queue === "object" && !Array.isArray(queue)
          && queue.policy === "serial_fifo" && queue.state === "waiting") {
        return typeof queue.reason === "string" && Object.hasOwn(queueReasons, queue.reason)
          ? queueReasons[queue.reason] : "已登记，执行准入原因待核对，请查询原任务";
      }
      return null;
    }
    if (status === "submitting" || status === "uncertain") return "提交受理结果待核对，请查询原任务，避免重复提交";
    if (status === "cancel_requested") return "已申请取消，等待原执行与资源退出确认";
    return null;
  }
  function namedBlock(task) {
    if (!task || typeof task !== "object" || Array.isArray(task)) return null;
    const status = task.executionStatus;
    if (status === "waiting_approval") return "审批";
    if (status === "waiting_input") return "等待回答";
    if ((status === "succeeded" || status === "failed") && Object.hasOwn(task, "outputValidation")) {
      const validation = task.outputValidation;
      if (validation && typeof validation === "object" && !Array.isArray(validation)
          && (validation.state === "invalid" || validation.state === "unsupported")) return "输出错误";
    }
    const queue = task.executionQueue;
    if (status === "registered" && queue && typeof queue === "object" && !Array.isArray(queue)
        && queue.policy === "serial_fifo" && queue.state === "waiting") {
      if (queue.reason === "project_concurrency_limit" || queue.reason === "workspace_in_use" || queue.reason === "role_in_use")
        return "资源不足";
      if (queue.reason === "state_unknown") return "执行未知";
    }
    if (status === "uncertain" || status === "submitting") return "执行未知";
    const observation = task.executionObservation;
    if (observation && typeof observation === "object" && !Array.isArray(observation) && observation.status === "unknown")
      return "执行未知";
    return null;
  }
  function businessLines(value, projectId) {
    if (value === undefined) return [];
    if (!value || typeof value !== "object" || Array.isArray(value)
        || typeof value.projectId !== "string" || !value.projectId
        || (projectId && value.projectId !== projectId)
        || typeof value.workItemId !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(value.workItemId)
        || typeof value.definitionSha256 !== "string" || !/^[a-f0-9]{64}$/.test(value.definitionSha256)
        || typeof value.title !== "string" || !value.title.trim() || value.title.length > 160
        || typeof value.goal !== "string" || !value.goal.trim() || value.goal.length > 4000
        || !Array.isArray(value.acceptanceCriteria) || value.acceptanceCriteria.length < 1
        || value.acceptanceCriteria.length > 8
        || value.acceptanceCriteria.some(item => typeof item !== "string" || !item.trim() || item.length > 500))
      return [["业务任务", "关联摘要无法核对，请查询原登记记录。"]];
    return [["业务任务", value.title], ["固定目标", value.goal],
      ["验收条件（仍需独立验收）", value.acceptanceCriteria.join("\n")]];
  }
  const businessSchemas = ["coding-result-v1", "review-result-v1", "fields-result-v1"];
  function outputShapeNote(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return "输出结构核对结果未知，请查询原任务。";
    if (value.state === "valid" && value.schemaId === "task-result"
        && value.reason === "declared_bridge_shape_matched") {
      return "符合已声明的结果结构；产物和独立测试结论仍以验证状态为准。";
    }
    if (value.state === "valid" && businessSchemas.includes(value.schemaId)
        && value.reason === "same_body_fence_unwrapped") {
      return "同一结果只去掉外层单一 Markdown 围栏后符合声明结构。原文保留，未补字段、未改事实、未新建尝试。这仍不代替独立测试和人工审批。";
    }
    if (value.state === "valid" && businessSchemas.includes(value.schemaId)
        && value.reason === "declared_business_shape_matched") {
      return "符合已声明的业务结果结构。字段表达本次业务结果或 Agent 意见，不代替独立测试和人工审批。";
    }
    if (value.state === "not_ready" && value.reason === "execution_not_succeeded") return "尚未取得成功执行结果，未校验输出结构。";
    if (value.state === "unsupported" && value.reason === "output_schema_unsupported") return "声明的结果结构尚不支持，需修正配置；不会新提交或按成功结果回传。";
    if (value.state === "unknown" && value.reason === "output_schema_not_declared") return "历史任务未声明结果结构，不能确认已通过结构校验。";
    if (value.state === "invalid") {
      const reasons = {output_shape_mismatch: "字段缺失、类型错误或原生完成标记不符",
        output_exceeds_limit: "结果超过大小上限", output_not_finite_json: "结果不是有效的有限 JSON",
        business_result_invalid: "业务结果不是符合声明字段、类型和大小约束的完整 JSON"};
      if (Object.hasOwn(reasons, value.reason)) return `${reasons[value.reason]}。编码结果保留，已转人工；不会自动重新编码、传输重试或成功回传。`;
    }
    return "输出结构核对结果未知，请查询原任务。";
  }
  function showsBusinessFields(value) {
    return Boolean(value && typeof value === "object" && !Array.isArray(value)
      && value.state === "valid" && businessSchemas.includes(value.schemaId)
      && (value.reason === "declared_business_shape_matched" || value.reason === "same_body_fence_unwrapped"));
  }
  function workflowRevision(workflow) {
    if (!workflow || typeof workflow !== "object" || Array.isArray(workflow) || !Object.hasOwn(workflow, "revision")) return null;
    const value = workflow.revision;
    if (typeof value !== "string" || value.length < 1 || value.length > 256 || value.trim() !== value || value.includes("\0")) return "";
    return value;
  }
  function responsibleAgent(task) {
    if (!task || typeof task !== "object" || Array.isArray(task) || !Object.hasOwn(task, "roleAgentId")) return null;
    const value = task.roleAgentId;
    if (typeof value !== "string" || value.length < 1 || value.length > 256 || value.trim() !== value || value.includes("\0")) return "";
    return value;
  }
  window.AgentLineActivity = Object.freeze({eventLabel, categoryLabel, rows, attention, waitingReason, namedBlock, businessLines, workflowRevision, responsibleAgent, outputShapeNote, showsBusinessFields});
})();
