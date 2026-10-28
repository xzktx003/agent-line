(() => {
  const byId = id => document.getElementById(id);
  const projectSelect = byId("overview-project");
  const statusSelect = byId("overview-status");
  const sizeSelect = byId("overview-size");
  const servicesList = byId("overview-services");
  const tasksList = byId("overview-tasks");
  const notice = byId("overview-notice");
  const message = byId("overview-message");
  const previousButton = byId("overview-prev");
  const nextButton = byId("overview-next");
  const pageLabel = byId("overview-page");

  let generation = 0;
  let page = 0;
  let nextCursor = null;
  let cursors = [null];
  let pageSize = 25;
  let hasLoaded = false;

  const valueOrNull = value => typeof value === "string" && value ? value : null;
  const label = (value, fallback = "尚无记录") => typeof value === "string" && value ? value : fallback;

  const executionLabels = {registered: "已登记", submitting: "提交中", uncertain: "结果待核对",
    accepted: "已接收", succeeded: "执行成功", failed: "执行失败", cancel_requested: "已申请取消", canceled: "已取消"};
  const verificationLabels = {pending: "等待验证", passed: "验证通过", failed: "验证失败", canceled: "已取消验收"};
  const memoryLabels = {not_requested: "未请求回写", pending: "记忆等待核对", committed: "已写入",
    failed: "记忆写回失败", needs_review: "需要审核", deleting: "删除处理中", deleted: "已删除"};
  const statusLabel = (labels, value) => Object.hasOwn(labels, value) ? labels[value] : "状态待核对";

  function setNotice(text, isError = false) {
    notice.textContent = text;
    notice.hidden = !text;
    notice.className = isError ? "notice error" : "notice";
  }

  function updateControls(loading = false) {
    previousButton.disabled = loading || page === 0;
    nextButton.disabled = loading || !nextCursor;
    byId("overview-refresh").disabled = false;
    projectSelect.disabled = false;
    statusSelect.disabled = false;
    sizeSelect.disabled = false;
    pageLabel.textContent = `第 ${page + 1} 页`;
  }

  function clearResults() {
    servicesList.replaceChildren();
    tasksList.replaceChildren();
    nextCursor = null;
    updateControls();
  }

  function exactFilters(filters, expected) {
    return filters !== null && typeof filters === "object" && !Array.isArray(filters)
      && Object.keys(filters).length === 2
      && Object.hasOwn(filters, "projectId") && Object.hasOwn(filters, "status")
      && filters.projectId === expected.projectId && filters.status === expected.status;
  }

  function validateOverview(data, expected, limit, after) {
    if (!data || typeof data !== "object" || Array.isArray(data)
      || data.source !== "local_instance_summary" || data.readOnly !== true
      || !exactFilters(data.filters, expected)
      || !Array.isArray(data.projects) || !Array.isArray(data.services) || !Array.isArray(data.tasks)) {
      throw new Error("总览响应结构或筛选回显无法核对，请刷新重试。");
    }
    if (data.tasks.length > limit) throw new Error("总览返回任务数超过请求页长，已清除当前列表。");
    if (!(data.nextCursor === null || (typeof data.nextCursor === "string" && data.nextCursor.length > 0 && data.nextCursor.length <= 128))) {
      throw new Error("总览分页游标无法核对，请刷新重试。");
    }
    if (data.nextCursor !== null && (!data.tasks.length || data.nextCursor !== data.tasks[data.tasks.length - 1].taskId || data.nextCursor === after)) {
      throw new Error("总览分页游标与当前页不一致，请刷新重试。");
    }

    const projectIds = new Set();
    for (const project of data.projects) {
      if (!project || typeof project.projectId !== "string" || !project.projectId || typeof project.name !== "string" || projectIds.has(project.projectId)) {
        throw new Error("总览项目标识无法核对，请刷新重试。");
      }
      projectIds.add(project.projectId);
    }
    for (const service of data.services) {
      if (!service || typeof service.id !== "string" || !service.id || typeof service.name !== "string"
        || !["available", "configured"].includes(service.state) || typeof service.description !== "string"
        || (Object.hasOwn(service, "count") && (!Number.isSafeInteger(service.count) || service.count < 0))) {
        throw new Error("总览服务信息无法核对，请刷新重试。");
      }
    }
    const taskIds = new Set();
    for (const task of data.tasks) {
      const workflow = task?.workflow;
      if (!task || typeof task.taskId !== "string" || !task.taskId || taskIds.has(task.taskId)
        || !projectIds.has(task.projectId)
        || (expected.projectId !== null && task.projectId !== expected.projectId)
        || (expected.status !== null && task.executionStatus !== expected.status)
        || !(task.roleBindingId === null || typeof task.roleBindingId === "string")
        || !workflow || typeof workflow !== "object" || Array.isArray(workflow)
        || typeof task.executionStatus !== "string" || typeof task.verificationStatus !== "string"
        || typeof task.memoryStatus !== "string" || typeof task.resourcesReleased !== "boolean") {
        throw new Error("总览任务记录不完整或重复，已清除当前列表。");
      }
      taskIds.add(task.taskId);
    }
    return data;
  }

  function projectName(projectId, projects) {
    const project = projects.find(item => item.projectId === projectId);
    return project ? project.name : projectId;
  }

  function renderServices(services) {
    const rows = services.map(service => {
      const item = document.createElement("li");
      item.className = "service-card";
      const heading = document.createElement("h3");
      heading.textContent = service.name;
      const state = document.createElement("p");
      state.className = "task-context";
      state.textContent = service.state === "available" ? "适配服务可响应" : "已配置";
      const description = document.createElement("p");
      description.textContent = service.description;
      item.append(heading, state, description);
      if (Object.hasOwn(service, "count")) {
        const count = document.createElement("p");
        count.className = "task-context";
        count.textContent = `已记录：${service.count}`;
        item.append(count);
      }
      return item;
    });
    servicesList.replaceChildren(...rows);
  }

  function renderTasks(tasks, projects) {
    const rows = tasks.map(task => {
      const item = document.createElement("li");
      const row = document.createElement("div");
      row.className = "task-row overview-task-row";
      const workflow = task.workflow;
      const title = document.createElement("strong");
      title.textContent = task.roleBindingId || "节点任务";
      const context = document.createElement("span");
      context.className = "task-context";
      context.textContent = `项目：${projectName(task.projectId, projects)} · 节点：${label(workflow.nodeId)}`;
      const states = document.createElement("span");
      states.className = "task-states";
      for (const [name, value] of [["执行", statusLabel(executionLabels, task.executionStatus)], ["验证", statusLabel(verificationLabels, task.verificationStatus)], ["记忆", statusLabel(memoryLabels, task.memoryStatus)]]) {
        const status = document.createElement("span");
        status.textContent = `${name}：${value}`;
        states.append(status);
      }
      row.append(title, context, states);
      const revision = window.AgentLineActivity.workflowRevision(workflow);
      if (revision !== null) {
        const line = document.createElement("span");
        line.className = "task-context";
        line.textContent = `固定流程版本：${revision || "尚无记录"}`;
        row.append(line);
      }
      const agent = window.AgentLineActivity.responsibleAgent(task);
      if (agent !== null) {
        const line = document.createElement("span");
        line.className = "task-context";
        line.textContent = `负责人 Agent：${agent || "原记录未提供"}`;
        row.append(line);
      }
      for (const [name, value] of window.AgentLineActivity.rows(task.activity)) {
        const line = document.createElement("span");
        line.className = "task-context";
        line.textContent = `${name}：${value}`;
        row.append(line);
      }

      const waiting = window.AgentLineActivity.waitingReason(task);
      if (waiting) {
        const line = document.createElement("span");
        line.className = "task-context";
        line.textContent = `等待原因：${waiting}`;
        row.append(line);
      }
      const blockCategory = window.AgentLineActivity.namedBlock(task);
      if (blockCategory) {
        const line = document.createElement("span");
        line.className = "task-context";
        line.textContent = `阻塞类别：${blockCategory}`;
        row.append(line);
      }
      const attention = window.AgentLineActivity.attention(task);
      if (attention && attention !== waiting) {
        const line = document.createElement("span");
        line.className = "task-context";
        line.textContent = `需要关注：${attention}`;
        row.append(line);
      }

      const details = document.createElement("details");
      details.className = "task-row-identifiers";
      const summary = document.createElement("summary");
      summary.textContent = "查看任务标识";
      details.append(summary);
      const identifiers = [
        ...(typeof task.workItemId === 'string' && task.workItemId ? [["业务任务", task.workItemId]] : []),
        ["任务", task.taskId], ["流程", workflow.id], ["运行", workflow.runId],
        ["节点", workflow.nodeId], ["节点激活", workflow.nodeExecutionId], ["尝试", workflow.attemptId],
      ];
      for (const [name, value] of identifiers) {
        const line = document.createElement("p");
        line.className = "task-context";
        line.textContent = `${name}：${label(value)}`;
        details.append(line);
      }
      item.append(row, details);
      if (/^[A-Za-z0-9_-]{1,128}$/.test(task.taskId)) {
        const view = document.createElement('a');
        view.className = 'overview-action';
        view.href = `./runs.html#task=${encodeURIComponent(task.taskId)}`;
        view.textContent = '查看任务详情';
        item.append(view);
      }
      return item;
    });
    tasksList.replaceChildren(...rows);
  }

  async function loadOverview({ reset = false } = {}) {
    if (reset) {
      page = 0;
      cursors = [null];
    }
    pageSize = [10, 25, 50].includes(Number(sizeSelect.value)) ? Number(sizeSelect.value) : 25;
    const expected = { projectId: valueOrNull(projectSelect.value), status: valueOrNull(statusSelect.value) };
    const after = cursors[page] || null;
    const requestGeneration = ++generation;
    const query = new URLSearchParams({ limit: String(pageSize) });
    if (expected.projectId !== null) query.set("projectId", expected.projectId);
    if (expected.status !== null) query.set("status", expected.status);
    if (after !== null) query.set("after", after);

    servicesList.replaceChildren();
    tasksList.replaceChildren();
    message.textContent = hasLoaded ? "正在更新总览…" : "正在读取已建立的任务与服务…";
    setNotice("");
    updateControls(true);

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(`/extension/v1/overview?${query.toString()}`, {
        method: "GET",
        credentials: "omit",
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`总览读取失败（HTTP ${response.status}），请刷新重试。`);
      const data = validateOverview(await response.json(), expected, pageSize, after);
      if (requestGeneration !== generation) return;

      projectSelect.replaceChildren(new Option("全部项目", ""), ...data.projects.map(project => new Option(project.name, project.projectId)));
      projectSelect.value = expected.projectId || "";
      renderServices(data.services);
      renderTasks(data.tasks, data.projects);
      nextCursor = data.nextCursor;
      if (nextCursor) cursors[page + 1] = nextCursor;
      else cursors.length = page + 1;
      hasLoaded = true;
      message.textContent = data.tasks.length ? `本页 ${data.tasks.length} 条任务。` : "当前筛选下没有可访问的任务。";
      updateControls();
    } catch (error) {
      if (requestGeneration !== generation) return;
      clearResults();
      message.textContent = "暂时无法读取本机服务，请稍后刷新。";
      setNotice(error.name === "AbortError" ? "读取超时，请刷新重试。" : (error.message || "读取失败，请刷新重试。"), true);
      updateControls();
    } finally {
      clearTimeout(timeout);
    }
  }

  byId("overview-refresh").addEventListener("click", () => loadOverview());
  projectSelect.addEventListener("change", () => loadOverview({ reset: true }));
  statusSelect.addEventListener("change", () => loadOverview({ reset: true }));
  sizeSelect.addEventListener("change", () => loadOverview({ reset: true }));
  previousButton.addEventListener("click", () => {
    if (page > 0) {
      page -= 1;
      loadOverview();
    }
  });
  nextButton.addEventListener("click", () => {
    if (nextCursor) {
      page += 1;
      loadOverview();
    }
  });
  const linkedBinding = new URLSearchParams(window.location.search).get("binding");
  const manageLink = byId("overview-manage");
  if (manageLink && linkedBinding && /^[A-Za-z0-9_-]{1,160}$/.test(linkedBinding)) {
    const target = new URL("./runs.html", window.location.href);
    target.searchParams.set("binding", linkedBinding);
    manageLink.href = target.href;
  }
  loadOverview({ reset: true });
})();
