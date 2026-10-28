(() => {
  "use strict";

  const SHA256 = /^[0-9a-f]{64}$/;
  const SEGMENT = /^[A-Za-z0-9_-]{1,160}$/;
  const MAX_REVISION = Number.MAX_SAFE_INTEGER;
  const PAGE_SIZE = 20;

  function plainObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function validScope(value) {
    return plainObject(value) && SEGMENT.test(value.projectId || "")
      && SEGMENT.test(value.workflowId || "")
      && (typeof value.epoch === "string" || Number.isSafeInteger(value.epoch));
  }

  function documentShape(value) {
    if (!plainObject(value) || Object.keys(value).length !== 4
        || value.schemaVersion !== 1 || !Array.isArray(value.nodes)
        || value.nodes.length < 2 || value.nodes.length > 200
        || !Array.isArray(value.edges) || value.edges.length > 1000
        || !plainObject(value.variables)) return false;
    const nodeIds = new Set();
    for (const node of value.nodes) {
      if (!plainObject(node) || Object.keys(node).length !== 5
          || typeof node.id !== "string" || !/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(node.id)
          || nodeIds.has(node.id)
          || !["start", "end", "agent", "condition", "human", "loop", "parallel", "join", "subflow_input", "subflow_output"].includes(node.type)
          || typeof node.name !== "string" || !node.name || node.name.length > 255
          || !plainObject(node.position) || Object.keys(node.position).length !== 2
          || !["x", "y"].every((key) => typeof node.position[key] === "number"
            && Number.isFinite(node.position[key]))
          || !plainObject(node.config)) return false;
      nodeIds.add(node.id);
    }
    const edgeIds = new Set();
    for (const edge of value.edges) {
      if (!plainObject(edge) || !(Object.keys(edge).length === 3 || Object.keys(edge).length === 4)
          || !/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(edge.id || "") || edgeIds.has(edge.id)
          || !nodeIds.has(edge.source) || !nodeIds.has(edge.target)
          || Object.hasOwn(edge, "branch") && (typeof edge.branch !== "string" || edge.branch.length > 128)) return false;
      edgeIds.add(edge.id);
    }
    return true;
  }

  function validVersion(value) {
    return plainObject(value) && Number.isSafeInteger(value.revision)
      && value.revision > 0 && value.revision <= MAX_REVISION
      && SEGMENT.test(value.deploymentVersionId || "") && typeof value.isActive === "boolean";
  }

  function serialize(value) {
    return JSON.stringify(value, (_key, child) => {
      if (!plainObject(child)) return child;
      const sorted = Object.create(null);
      for (const key of Object.keys(child).sort()) sorted[key] = child[key];
      return sorted;
    });
  }

  function nodeEdgeDiff(before, after) {
    const result = { nodes: { added: 0, removed: 0, changed: 0 }, edges: { added: 0, removed: 0, changed: 0 } };
    for (const key of ["nodes", "edges"]) {
      const oldValues = new Map(before[key].map((item) => [item.id, serialize(item)]));
      const newValues = new Map(after[key].map((item) => [item.id, serialize(item)]));
      if (oldValues.size !== before[key].length || newValues.size !== after[key].length) return null;
      for (const [id, value] of newValues) {
        if (!oldValues.has(id)) result[key].added += 1;
        else if (oldValues.get(id) !== value) result[key].changed += 1;
      }
      for (const id of oldValues.keys()) if (!newValues.has(id)) result[key].removed += 1;
    }
    result.variablesChanged = serialize(before.variables) !== serialize(after.variables);
    return result;
  }

  function create({ container, request, scope, isCurrent, getDraft, safe }) {
    if (!(container instanceof Element) || typeof request !== "function"
        || typeof scope !== "function" || typeof isCurrent !== "function"
        || typeof getDraft !== "function") {
      throw new TypeError("版本历史模块初始化参数无效。");
    }

    let generation = 0;
    let operation = 0;
    let busy = false;
    let parentBlocked = false;
    let selectedScope = null;
    let cursor = null;
    let pageAfter;
    let cursorStack = [];
    let pageHistory = [];
    let versions = [];
    let status = null;
    let list = null;
    let snapshotRegion = null;
    let loadButton = null;
    let previousButton = null;
    let nextButton = null;
    let activeSnapshot = null;

    const escaped = (value) => {
      let result = value;
      try { if (typeof safe === "function") result = safe(value); }
      catch { result = "[内容已隐藏]"; }
      return typeof result === "string" ? result : String(result ?? "");
    };
    const locked = () => busy || parentBlocked;
    const sameScope = (a, b) => a && b && a.projectId === b.projectId
      && a.workflowId === b.workflowId && a.epoch === b.epoch;
    const current = (captured) => {
      try { return isCurrent(captured) === true && sameScope(scope(), captured); }
      catch { return false; }
    };

    function say(message, tone = "neutral") {
      if (!status) return;
      status.textContent = message;
      status.dataset.tone = tone;
    }

    function showSnapshot(value, diff, draftKnown) {
      activeSnapshot = value;
      snapshotRegion.replaceChildren();
      if (!value) return;
      const heading = document.createElement("h4");
      heading.textContent = `版本 ${value.revision} · ${value.isActive ? "读取时为活动版本" : "读取时非活动版本"}`;
      snapshotRegion.append(heading);
      const facts = document.createElement("p");
      facts.className = "field-help";
      facts.textContent = `部署版本标识 ${escaped(value.deploymentVersionId)} · 图 SHA-256 ${escaped(value.graphSha256)} · 节点 ${value.document.nodes.length} 个 · 连线 ${value.document.edges.length} 条。历史快照只读，读取时的活动标记不表示现在仍活动，也不表示旧版本可执行。`;
      snapshotRegion.append(facts);

      const overview = document.createElement("p");
      overview.className = "version-history-diff";
      if (!draftKnown || !diff) {
        overview.textContent = "与当前草稿的差异未知：读取时的草稿无法安全序列化或结构无法核对。";
      } else {
        overview.textContent = `与发起读取时本页已应用的草稿相比：节点新增 ${diff.nodes.added}、删除 ${diff.nodes.removed}、修改 ${diff.nodes.changed}；连线新增 ${diff.edges.added}、删除 ${diff.edges.removed}、修改 ${diff.edges.changed}；变量${diff.variablesChanged ? "有变化" : "无变化"}。`;
      }
      snapshotRegion.append(overview);

      const disclosure = document.createElement("details");
      disclosure.className = "version-history-json";
      const summary = document.createElement("summary");
      summary.textContent = "查看完整脱敏版本图 JSON";
      const pre = document.createElement("pre");
      pre.textContent = escaped(JSON.stringify(value.document, null, 2));
      disclosure.append(summary, pre);
      snapshotRegion.append(disclosure);
    }

    function clearSnapshot() {
      activeSnapshot = null;
      if (snapshotRegion) snapshotRegion.replaceChildren();
    }

    function renderVersions() {
      if (!list) return;
      list.replaceChildren();
      if (!versions.length) {
        const empty = document.createElement("p");
        empty.className = "field-help";
        empty.textContent = "此页没有可显示的版本记录。";
        list.append(empty);
      }
      for (const item of versions) {
        const row = document.createElement("article");
        row.className = "version-history-row";
        const title = document.createElement("h4");
        title.textContent = `版本 ${item.revision}${item.isActive ? " · 读取时活动" : ""}`;
        row.append(title);
        const id = document.createElement("p");
        id.className = "field-help";
        id.textContent = `部署版本标识 ${escaped(item.deploymentVersionId)}`;
        row.append(id);
        const select = document.createElement("button");
        select.type = "button";
        select.textContent = "只读查看此版本";
        select.disabled = locked();
        select.addEventListener("click", () => readVersion(item));
        row.append(select);
        list.append(row);
      }
      refreshControls();
    }

    function refreshControls(blocked) {
      if (typeof blocked === "boolean") parentBlocked = blocked;
      const disabled = locked();
      if (loadButton) loadButton.disabled = disabled;
      if (previousButton) previousButton.disabled = disabled || cursorStack.length === 0;
      if (nextButton) nextButton.disabled = disabled || !cursor;
      if (list) for (const button of list.querySelectorAll("button")) button.disabled = disabled;
    }

    async function run(work) {
      if (locked()) return;
      const captured = scope();
      if (!validScope(captured) || !current(captured)) {
        say("请先选择有效项目和工作流。", "error");
        return;
      }
      const currentGeneration = generation;
      const currentOperation = ++operation;
      busy = true;
      refreshControls();
      try {
        await work(captured, currentGeneration);
      } finally {
        if (generation === currentGeneration && operation === currentOperation) {
          busy = false;
          refreshControls();
        }
      }
    }

    function versionsPath(captured, after) {
      const base = `/extension/v1/authoring/projects/${encodeURIComponent(captured.projectId)}`
        + `/workflows/${encodeURIComponent(captured.workflowId)}/versions?limit=${PAGE_SIZE}`;
      return after ? `${base}&after=${encodeURIComponent(after)}` : base;
    }

    async function loadPage(after, previous = false) {
      await run(async (captured, currentGeneration) => {
        const targetIndex = previous ? cursorStack.length - 1 : pageAfter === undefined ? 0 : cursorStack.length + 1;
        const priorPages = pageHistory.slice(0, targetIndex);
        const priorRevisions = new Set(priorPages.flatMap(page => page.revisions));
        const priorDeployments = new Set(priorPages.flatMap(page => page.deployments));
        const priorCursors = new Set(priorPages.map(page => page.nextCursor).filter(value => value !== null));
        clearSnapshot();
        say("正在读取原生版本目录…");
        try {
          if (currentGeneration !== generation || !current(captured)) return;
          const response = await request(versionsPath(captured, after));
          if (currentGeneration !== generation || !current(captured)) return;
          if (!response || response.source !== "native_workflow_versions" || response.readOnly !== true
              || response.projectId !== captured.projectId || response.workflowId !== captured.workflowId
              || !Array.isArray(response.versions) || response.versions.length > PAGE_SIZE
              || !(response.nextCursor === null || typeof response.nextCursor === "string"
                && response.nextCursor.length > 0 && response.nextCursor.length <= 1024)) {
            throw new Error("invalid");
          }
          const pageSeen = new Set();
          const pageDeployments = new Set();
          for (const item of response.versions) {
            if (!validVersion(item) || pageSeen.has(item.revision)
                || pageDeployments.has(item.deploymentVersionId)
                || priorRevisions.has(item.revision)
                || priorDeployments.has(item.deploymentVersionId)) throw new Error("invalid");
            pageSeen.add(item.revision);
            pageDeployments.add(item.deploymentVersionId);
          }
          if (response.nextCursor !== null && (!response.versions.length || response.nextCursor === after
              || priorCursors.has(response.nextCursor))) throw new Error("invalid");
          if (previous) cursorStack.pop();
          else if (pageAfter !== undefined) cursorStack.push(pageAfter);
          pageAfter = after;
          cursor = response.nextCursor;
          pageHistory = [...priorPages, {revisions:[...pageSeen], deployments:[...pageDeployments], nextCursor:cursor}];
          versions = response.versions.map((item) => ({ ...item }));
          selectedScope = { ...captured };
          renderVersions();
          say(`已读取 ${versions.length} 条原生版本记录。版本只用于查看，不会覆盖当前草稿。`, "success");
        } catch {
          if (currentGeneration !== generation || !current(captured)) return;
          versions = [];
          cursor = null;
          pageAfter = undefined;
          cursorStack = [];
          pageHistory = [];
          selectedScope = null;
          renderVersions();
          say("版本目录不可用或响应无法核对；当前草稿未改变。", "error");
        }
      });
    }

    async function readVersion(item) {
      await run(async (captured, currentGeneration) => {
        if (!sameScope(selectedScope, captured)) {
          say("版本列表不属于当前项目或工作流，请重新读取历史。", "error");
          return;
        }
        clearSnapshot();
        say(`正在读取版本 ${item.revision} 的固定快照…`);
        let draft = null;
        let draftKnown = false;
        try {
          draft = JSON.parse(JSON.stringify(getDraft()));
          draftKnown = documentShape(draft);
        } catch {
          draftKnown = false;
        }
        try {
          if (currentGeneration !== generation || !current(captured)) return;
          const path = `/extension/v1/authoring/projects/${encodeURIComponent(captured.projectId)}`
            + `/workflows/${encodeURIComponent(captured.workflowId)}/versions/${item.revision}`;
          const response = await request(path);
          if (currentGeneration !== generation || !current(captured)) return;
          if (!response || response.source !== "native_workflow_version_snapshot"
              || response.readOnly !== true || response.projectId !== captured.projectId
              || response.workflowId !== captured.workflowId || response.revision !== item.revision
              || response.deploymentVersionId !== item.deploymentVersionId
              || typeof response.isActive !== "boolean" || !SHA256.test(response.graphSha256 || "")
              || !documentShape(response.document)) throw new Error("invalid");
          const snapshot = { ...response };
          let diff = null;
          if (draftKnown) {
            try { diff = nodeEdgeDiff(draft, response.document); }
            catch { diff = null; }
          }
          showSnapshot(snapshot, diff, draftKnown);
          say(`已读取版本 ${item.revision} 的只读快照；活动状态仅对应本次读取。`, "success");
        } catch {
          if (currentGeneration !== generation || !current(captured)) return;
          clearSnapshot();
          say("所选版本快照暂不可用或身份无法核对；当前草稿未改变。", "error");
        }
      });
    }

    function build() {
      container.replaceChildren();
      const disclosure = document.createElement("details");
      disclosure.className = "version-history-panel";
      const summary = document.createElement("summary");
      summary.textContent = "版本历史（只读）";
      disclosure.append(summary);
      const explanation = document.createElement("p");
      explanation.className = "field-help";
      explanation.textContent = "显式读取原生版本与固定图快照。历史图不会覆盖、保存、发布或执行；比较不含尚未应用的节点表单。活动标记仅代表读取时状态。";
      disclosure.append(explanation);
      const actions = document.createElement("div");
      actions.className = "version-history-actions";
      loadButton = document.createElement("button");
      loadButton.type = "button";
      loadButton.textContent = "读取版本历史";
      loadButton.addEventListener("click", () => {
        if (locked()) return;
        cursor = null;
        pageAfter = undefined;
        cursorStack = [];
        pageHistory = [];
        loadPage(null);
      });
      previousButton = document.createElement("button");
      previousButton.type = "button";
      previousButton.textContent = "上一页";
      previousButton.addEventListener("click", () => {
        const previousCursor = cursorStack[cursorStack.length - 1] ?? null;
        if (cursorStack.length) loadPage(previousCursor, true);
      });
      nextButton = document.createElement("button");
      nextButton.type = "button";
      nextButton.textContent = "下一页";
      nextButton.addEventListener("click", () => { if (cursor) loadPage(cursor); });
      actions.append(loadButton, previousButton, nextButton);
      disclosure.append(actions);
      status = document.createElement("p");
      status.className = "field-help version-history-status";
      status.setAttribute("role", "status");
      disclosure.append(status);
      list = document.createElement("div");
      list.className = "version-history-list";
      disclosure.append(list);
      snapshotRegion = document.createElement("section");
      snapshotRegion.className = "version-history-snapshot";
      disclosure.append(snapshotRegion);
      container.append(disclosure);
      say("尚未读取版本历史。", "neutral");
      refreshControls();
    }

    function clear() {
      generation += 1;
      operation += 1;
      busy = false;
      selectedScope = null;
      cursor = null;
      pageAfter = undefined;
      cursorStack = [];
      pageHistory = [];
      versions = [];
      activeSnapshot = null;
      build();
    }

    build();
    return { clear, refreshControls };
  }

  window.AgentLineVersionHistory = { create };
})();
