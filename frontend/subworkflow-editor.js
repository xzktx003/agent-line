(() => {
  "use strict";

  const SHA256 = /^[0-9a-f]{64}$/;
  const SEGMENT = /^[A-Za-z0-9_-]{1,160}$/;
  const IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;
  const RESERVED = new Set(["__proto__", "constructor", "prototype"]);
  const PAGE_SIZE = 30;

  const plain = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const canonical = (value) => JSON.stringify(value, (_key, child) => {
    if (!plain(child)) return child;
    const result = Object.create(null);
    for (const key of Object.keys(child).sort()) result[key] = child[key];
    return result;
  });
  const safeSegment = (value) => typeof value === "string" && SEGMENT.test(value);

  function create({ container, request, scope, isCurrent, getDraft, canInsert, canRemove,
    supported, applyDocument, removeGroup, safe }) {
    if (!(container instanceof Element) || typeof request !== "function"
        || typeof scope !== "function" || typeof isCurrent !== "function" || typeof getDraft !== "function"
        || typeof canInsert !== "function" || typeof canRemove !== "function"
        || typeof supported !== "function" || typeof applyDocument !== "function"
        || typeof removeGroup !== "function") {
      throw new TypeError("子流程编辑面板初始化参数无效。");
    }

    const $ = (id) => container.querySelector(`#${id}`);
    const controls = ["subworkflow-load", "subworkflow-search", "subworkflow-search-submit",
      "subworkflow-next", "subworkflow-version", "subworkflow-version-next", "subworkflow-edge",
      "subworkflow-input", "subworkflow-input-map-add", "subworkflow-output-map-add",
      "subworkflow-preview", "subworkflow-apply", "subworkflow-remove", "subworkflow-remove-button"];
    let generation = 0;
    let operation = 0;
    let busy = false;
    let blocked = false;
    let candidates = [];
    let candidateCursor = null;
    let selectedWorkflow = null;
    let versionCursor = null;
    let selectedReference = null;
    let preview = null;
    let previewInputs = null;
    let inputRows = [];
    let outputRows = [];
    let search = "";
    let candidateIds = new Set();
    let candidateCursors = new Set();
    let versionRevisions = new Set();
    let versionDeployments = new Set();
    let versionCursors = new Set();

    const display = (value) => {
      let result = value;
      try { if (typeof safe === "function") result = safe(value); } catch { result = "[内容已隐藏]"; }
      return typeof result === "string" ? result : String(result ?? "");
    };
    const nodeLabel = (node) => typeof node?.name === "string" && node.name.trim()
      ? display(node.name) : node?.type === "start" ? "开始" : node?.type === "end" ? "结束" : "节点";
    const current = (capturedGeneration, op, capturedScope) => generation === capturedGeneration
      && operation === op && isCurrent(capturedScope) === true;

    function say(message, tone = "neutral") {
      const status = $("subworkflow-message");
      if (status) { status.textContent = message; status.dataset.tone = tone; }
    }

    function clearPreview() {
      preview = null;
      previewInputs = null;
      $("subworkflow-preview-summary").replaceChildren();
      $("subworkflow-preview-summary").hidden = true;
    }

    function invalidate() {
      generation += 1;
      operation += 1;
      busy = false;
      clearPreview();
    }

    function sync(blockedValue) {
      if (typeof blockedValue === "boolean") blocked = blockedValue;
      if (previewInputs) {
        try {
          const draft = getDraft();
          if (!draft || draft.stateSha256 !== previewInputs.stateSha256
              || canonical(draft.document) !== previewInputs.document
              || isCurrent(previewInputs.scope) !== true) {
            clearPreview();
            say("父草稿已变化，旧预览已作废；请重新预览。", "neutral");
          }
        } catch { clearPreview(); }
      }
      renderRemovals();
      const insertAllowed = !blocked && !busy && supported() === true && canInsert() === true;
      const removeAllowed = !blocked && !busy && canRemove() === true;
      for (const id of controls) {
        const element = $(id);
        if (element) element.disabled = true;
      }
      $("subworkflow-load").disabled = !insertAllowed;
      $("subworkflow-search").disabled = !insertAllowed;
      $("subworkflow-search-submit").disabled = !insertAllowed;
      $("subworkflow-next").disabled = !insertAllowed || !candidateCursor;
      $("subworkflow-version").disabled = !insertAllowed || !selectedWorkflow;
      $("subworkflow-version-next").disabled = !insertAllowed || !versionCursor;
      const hasReference = Boolean(selectedReference);
      for (const id of ["subworkflow-edge", "subworkflow-input", "subworkflow-input-map-add",
        "subworkflow-output-map-add", "subworkflow-preview"]) $(id).disabled = !insertAllowed || !hasReference;
      $("subworkflow-apply").disabled = !insertAllowed || !preview || !previewInputs;
      $("subworkflow-remove").disabled = !removeAllowed || !removeOptions.length;
      $("subworkflow-remove-button").disabled = !removeAllowed || !$("subworkflow-remove").value;
      for (const button of container.querySelectorAll("[data-subworkflow-remove-row]")) button.disabled = !insertAllowed;
      for (const button of container.querySelectorAll("[data-subworkflow-candidate]")) button.disabled = !insertAllowed;
      for (const field of container.querySelectorAll("[data-subworkflow-mapping]")) field.disabled = !insertAllowed;
    }

    let removeOptions = [];
    function renderRemovals() {
      const select = $("subworkflow-remove");
      const selected = select.value;
      select.replaceChildren(new Option("选择一个已加入的子流程", ""));
      removeOptions = [];
      let draft;
      try { draft = getDraft(); } catch { draft = null; }
      const document = draft?.document;
      if (plain(document) && Array.isArray(document.nodes)) {
        const seen = new Set();
        for (const node of document.nodes) {
          if (node?.type !== "subflow_input" || !plain(node.config)
              || typeof node.config.callId !== "string" || seen.has(node.config.callId)) continue;
          seen.add(node.config.callId);
          const ref = node.config.reference;
          const revision = Number.isSafeInteger(ref?.revision) ? `版本 ${ref.revision}` : "固定版本";
          const option = new Option(`子流程 ${removeOptions.length + 1} · ${revision}`, node.config.callId);
          select.add(option);
          removeOptions.push(node.config.callId);
        }
      }
      if (!removeOptions.length) select.replaceChildren(new Option("当前没有已识别的子流程", ""));
      else if (removeOptions.includes(selected)) select.value = selected;
    }

    function resetVersionAndPreview() {
      selectedReference = null;
      versionCursor = null;
      $("subworkflow-version").replaceChildren(new Option("请读取并明确选择版本", ""));
      $("subworkflow-edge").replaceChildren(new Option("选择要替换的已有连线", ""));
      $("subworkflow-input").replaceChildren(new Option("选择子流程输入来源", ""));
      clearPreview();
      renderMappings();
      sync();
    }

    function renderCandidates() {
      const list = $("subworkflow-candidates");
      list.replaceChildren();
      const activeScope = scope();
      const visible = candidates.filter((item) => item.id !== activeScope?.workflowId && item.archivePending !== true);
      if (!visible.length) {
        const empty = document.createElement("p"); empty.className = "muted";
        empty.textContent = "此页没有其他可选择的流程。"; list.append(empty);
      }
      for (const item of visible) {
        const row = document.createElement("article"); row.className = "workflow-row";
        const title = document.createElement("strong"); title.textContent = display(item.name || "未命名流程");
        const detail = document.createElement("p"); detail.className = "muted";
        detail.textContent = item.isDeployed === true ? "已发布，可选择固定历史版本。" : "当前未发布；只有服务端认可的版本可以继续预览。";
        const button = document.createElement("button"); button.type = "button";
        button.className = "secondary"; button.textContent = "选择此流程的版本";
        button.dataset.subworkflowCandidate = "true";
        button.disabled = busy || blocked || !supported() || !canInsert();
        button.addEventListener("click", () => loadVersions(item));
        row.append(title, detail, button); list.append(row);
      }
      sync();
    }

    function renderVersions(items, append = false) {
      const select = $("subworkflow-version");
      if (!append) select.replaceChildren(new Option("明确选择一个历史版本", ""));
      const known = new Set([...select.options].map((option) => option.value));
      for (const item of items) {
        if (!Number.isSafeInteger(item.revision) || item.revision < 1
            || !safeSegment(item.deploymentVersionId) || typeof item.isActive !== "boolean"
            || known.has(String(item.revision))) continue;
        select.add(new Option(`版本 ${item.revision}${item.isActive ? " · 读取时活动" : ""}`, String(item.revision)));
      }
      sync();
    }

    function selectedEdgeAndInput() {
      const draft = getDraft();
      const document = draft?.document;
      if (!plain(document) || !Array.isArray(document.nodes) || !Array.isArray(document.edges)) return null;
      const ids = new Map(document.nodes.map((node) => [node.id, node]));
      const protectedIds = new Set(Array.isArray(draft.protectedIds) ? draft.protectedIds : []);
      const entryIds = new Set(Array.isArray(draft.entryIds) ? draft.entryIds : []);
      const outputIds = new Set(Array.isArray(draft.outputIds) ? draft.outputIds : []);
      const edgeSelect = $("subworkflow-edge");
      const selectedEdge = edgeSelect.value;
      edgeSelect.replaceChildren(new Option("选择要替换的已有连线", ""));
      for (const edge of document.edges) {
        const source = ids.get(edge.source), target = ids.get(edge.target);
        if (!source || !target || protectedIds.has(edge.source) && !outputIds.has(edge.source)
            || protectedIds.has(edge.target) && !entryIds.has(edge.target)) continue;
        edgeSelect.add(new Option(`${nodeLabel(source)} → ${nodeLabel(target)}`, edge.id));
      }
      if ([...edgeSelect.options].some((option) => option.value === selectedEdge)) edgeSelect.value = selectedEdge;
      const inputSelect = $("subworkflow-input");
      const selectedInput = inputSelect.value;
      inputSelect.replaceChildren(new Option("选择子流程输入来源", ""));
      for (const node of document.nodes) {
        const allowed = node.type === "start" || node.type === "agent" || node.type === "join"
          || outputIds.has(node.id);
        if (!allowed || protectedIds.has(node.id) && !outputIds.has(node.id)) continue;
        const field = node.type === "start" ? "input" : "output";
        inputSelect.add(new Option(`${nodeLabel(node)} · ${field}`, `${node.id}\u0000${field}`));
      }
      if ([...inputSelect.options].some((option) => option.value === selectedInput)) inputSelect.value = selectedInput;
      return { draft, document, edgeSelect, inputSelect };
    }

    function renderMappings() {
      const groups = [["input", inputRows], ["output", outputRows]];
      for (const [kind, rows] of groups) {
        const host = $(`subworkflow-${kind}-mapping`); host.replaceChildren();
        if (rows.length) {
          const heading = document.createElement("p"); heading.className = "muted";
          heading.textContent = kind === "input" ? "输入映射：投影父输入后传给子流程。" : "返回映射：投影子流程返回后交给后续节点。";
          host.append(heading);
        }
        for (const [index, row] of rows.entries()) {
          const line = document.createElement("div"); line.className = "editor-actions";
          const name = document.createElement("input"); name.placeholder = "目标字段"; name.maxLength = 64;
          name.dataset.subworkflowMapping = "true";
          name.value = row.name; name.setAttribute("aria-label", "映射目标字段名");
          name.addEventListener("input", () => { rows[index].name = name.value; clearPreview(); sync(); });
          const mode = document.createElement("select"); mode.setAttribute("aria-label", "映射来源类型");
          mode.dataset.subworkflowMapping = "true";
          mode.add(new Option("字段路径", "path")); mode.add(new Option("JSON 固定值", "literal"));
          mode.value = row.kind;
          mode.addEventListener("change", () => { rows[index].kind = mode.value; rows[index].value = ""; clearPreview(); renderMappings(); sync(); });
          const value = document.createElement(row.kind === "literal" ? "textarea" : "input");
          value.dataset.subworkflowMapping = "true";
          value.placeholder = row.kind === "literal" ? '例如 "中文" 或 3' : "例如 requirements.owner";
          value.value = row.value; value.setAttribute("aria-label", row.kind === "literal" ? "JSON 固定值" : "点分字段路径");
          value.addEventListener("input", () => { rows[index].value = value.value; clearPreview(); sync(); });
          const remove = document.createElement("button"); remove.type = "button"; remove.className = "quiet";
          remove.textContent = "删除"; remove.dataset.subworkflowRemoveRow = "true";
          remove.addEventListener("click", () => { rows.splice(index, 1); clearPreview(); renderMappings(); sync(); });
          line.append(name, mode, value, remove); host.append(line);
        }
      }
    }

    function mapping(rows) {
      if (!rows.length) return undefined;
      if (rows.length > 32) throw new Error("每种映射最多 32 个字段。");
      const names = new Set();
      const fields = rows.map((row) => {
        const name = row.name.trim();
        if (!IDENTIFIER.test(name) || RESERVED.has(name) || names.has(name)) throw new Error("映射目标字段须为不重复的英文字段名。");
        names.add(name);
        if (row.kind === "path") {
          const path = row.value.trim().split(".");
          if (path.length < 1 || path.length > 8 || path.some((part) => !IDENTIFIER.test(part) || RESERVED.has(part))) {
            throw new Error("字段路径须为 1–8 层点分英文字段名。");
          }
          return { name, kind: "path", path };
        }
        let value;
        try { value = JSON.parse(row.value); }
        catch { throw new Error("固定值必须是有效 JSON；字符串需使用双引号。"); }
        return { name, kind: "literal", value };
      });
      return { schemaVersion: 1, fields };
    }

    function clear() {
      invalidate();
      candidates = []; candidateCursor = null; selectedWorkflow = null; versionCursor = null;
      selectedReference = null; inputRows = []; outputRows = []; search = "";
      candidateIds = new Set(); candidateCursors = new Set(); versionRevisions = new Set();
      versionDeployments = new Set(); versionCursors = new Set();
      $("subworkflow-search").value = "";
      $("subworkflow-candidates").replaceChildren();
      $("subworkflow-candidates").append(Object.assign(document.createElement("p"), {className:"muted", textContent:"保存父草稿后读取候选流程。"}));
      $("subworkflow-version").replaceChildren(new Option("先选择流程", ""));
      $("subworkflow-edge").replaceChildren(new Option("先读取草稿连线", ""));
      $("subworkflow-input").replaceChildren(new Option("先读取草稿来源", ""));
      renderMappings(); renderRemovals();
      say("已清除候选版本、字段映射和预览。", "neutral");
      sync();
    }

    async function run(work) {
      sync();
      if (busy || blocked || !supported() || !canInsert()) return;
      const capturedScope = scope();
      if (!plain(capturedScope) || !safeSegment(capturedScope.projectId)
          || !safeSegment(capturedScope.workflowId)
          || !isCurrent(capturedScope)) {
        say("请先选择有效项目和父流程。", "error"); return;
      }
      const draft = getDraft();
      if (!draft || !plain(draft.document) || !SHA256.test(draft.stateSha256 || "")) {
        say("请先保存并重新读取有效的父流程草稿。", "error"); return;
      }
      const originalDocument = canonical(draft.document);
      const captured = generation, op = ++operation;
      busy = true; sync();
      try { await work(draft, captured, op, capturedScope, originalDocument); }
      catch (error) { if (current(captured, op, capturedScope)) say(`操作未完成：${error?.message || "请核对后重试。"}`, "error"); }
      finally { if (current(captured, op, capturedScope)) { busy = false; sync(); } }
    }

    async function loadCandidates(after = "") {
      return run(async (_draft, captured, op, capturedScope) => {
        if (after && (!/^[A-Za-z0-9_-]{1,128}$/.test(after) || !candidateCursors.has(after))) {
          throw new Error("流程列表游标无效或已使用。");
        }
        const query = new URLSearchParams({limit:String(PAGE_SIZE), search:search.trim()});
        if (after) query.set("after", after);
        const result = await request(`/workflows?${query}`, {method:"GET"});
        if (!current(captured, op, capturedScope)) return;
        if (!plain(result) || !Array.isArray(result.workflows)
            || !(result.nextCursor === null || typeof result.nextCursor === "string")) throw new Error("流程列表响应无法核对。");
        if (result.workflows.length > PAGE_SIZE) throw new Error("流程列表页超过请求上限。");
        const next = result.nextCursor;
        if (next !== null && (!/^[A-Za-z0-9_-]{1,128}$/.test(next) || next === after || candidateCursors.has(next))) {
          throw new Error("流程列表游标无效、重复或未前进。");
        }
        const checked = result.workflows;
        const pageIds = new Set();
        for (const item of checked) {
          if (!plain(item) || !safeSegment(item.id) || typeof item.name !== "string"
              || typeof item.archivePending !== "boolean" || pageIds.has(item.id)
              || candidateIds.has(item.id)) throw new Error("流程列表身份重复或字段无效。");
          pageIds.add(item.id);
        }
        for (const id of pageIds) candidateIds.add(id);
        candidates = after ? candidates.concat(checked) : checked;
        candidateCursor = next;
        if (next !== null) candidateCursors.add(next);
        renderCandidates();
        say(`已读取本项目流程：本页 ${checked.length} 个。归档待处理和当前父流程已排除；可手动读取下一页。`);
      });
    }

    async function loadVersions(item) {
      invalidate();
      selectedWorkflow = item;
      resetVersionAndPreview();
      versionCursor = null;
      versionRevisions = new Set(); versionDeployments = new Set(); versionCursors = new Set();
      $("subworkflow-version").replaceChildren(new Option("正在读取版本…", ""));
      await fetchVersions("");
    }

    async function fetchVersions(after) {
      if (!selectedWorkflow) return;
      const workflowId = selectedWorkflow.id;
      return run(async (_draft, captured, op, capturedScope) => {
        if (after && (!versionCursors.has(after) || after.length > 1024)) throw new Error("版本列表游标无效或已使用。");
        const query = new URLSearchParams({limit:"20"}); if (after) query.set("after", after);
        const result = await request(`/workflows/${encodeURIComponent(workflowId)}/versions?${query}`, {method:"GET"});
        if (!current(captured, op, capturedScope)) return;
        if (!plain(result) || result.source !== "native_workflow_versions" || result.readOnly !== true
            || result.projectId !== capturedScope.projectId || result.workflowId !== workflowId
            || !Array.isArray(result.versions) || result.versions.length > 20
            || !(result.nextCursor === null || typeof result.nextCursor === "string")) {
          throw new Error("历史版本列表身份无法核对。");
        }
        const next = result.nextCursor;
        if (next !== null && (next.length < 1 || next.length > 1024 || next === after || versionCursors.has(next))) {
          throw new Error("版本列表游标无效、重复或未前进。");
        }
        const pageRevisions = new Set(), pageDeployments = new Set();
        for (const item of result.versions) {
          if (!plain(item) || !Number.isSafeInteger(item.revision) || item.revision < 1
              || !safeSegment(item.deploymentVersionId) || typeof item.isActive !== "boolean"
              || pageRevisions.has(item.revision) || pageDeployments.has(item.deploymentVersionId)
              || versionRevisions.has(item.revision) || versionDeployments.has(item.deploymentVersionId)) {
            throw new Error("版本列表包含重复身份或无效字段。");
          }
          pageRevisions.add(item.revision); pageDeployments.add(item.deploymentVersionId);
        }
        for (const revision of pageRevisions) versionRevisions.add(revision);
        for (const deployment of pageDeployments) versionDeployments.add(deployment);
        versionCursor = next;
        if (next !== null) versionCursors.add(next);
        renderVersions(result.versions, Boolean(after));
        say("版本列表来自本项目原生只读历史；请明确选择版本后读取固定快照。" );
      });
    }

    async function readSelectedVersion() {
      return run(async (_draft, captured, op, capturedScope) => {
        const revision = Number($("subworkflow-version").value);
        const workflowId = selectedWorkflow?.id;
        if (!workflowId || !Number.isSafeInteger(revision) || revision < 1) throw new Error("请先明确选择版本。");
        clearPreview(); selectedReference = null;
        $("subworkflow-edge").replaceChildren(new Option("选择要替换的已有连线", ""));
        $("subworkflow-input").replaceChildren(new Option("选择子流程输入来源", ""));
        sync();
        const result = await request(`/workflows/${encodeURIComponent(workflowId)}/versions/${revision}`, {method:"GET"});
        if (!current(captured, op, capturedScope)) return;
        if (!plain(result) || result.source !== "native_workflow_version_snapshot" || result.readOnly !== true
            || result.projectId !== capturedScope.projectId || result.workflowId !== workflowId
            || result.revision !== revision || !safeSegment(result.deploymentVersionId)
            || !SHA256.test(result.graphSha256 || "") || !plain(result.document)
            || !Array.isArray(result.document.nodes) || result.document.nodes.length > 200
            || result.document.nodes.some((node) => !plain(node) || typeof node.type !== "string")
            || !Array.isArray(result.document.edges) || result.document.edges.length > 1000
            || result.document.edges.some((edge) => !plain(edge) || typeof edge.id !== "string"
              || typeof edge.source !== "string" || typeof edge.target !== "string")
            || new TextEncoder().encode(JSON.stringify(result.document)).length > 262144) {
          throw new Error("固定版本快照的项目、流程、版本、部署标识或图摘要无法核对。");
        }
        selectedReference = {projectId:result.projectId, workflowId:result.workflowId,
          revision:result.revision, deploymentVersionId:result.deploymentVersionId, graphSha256:result.graphSha256};
        selectedEdgeAndInput();
        say(`已明确选择版本 ${revision}；部署标识与图摘要已核对。请选择已有连线和输入来源。`);
        sync();
      });
    }

    function renderPreview(result) {
      const host = $("subworkflow-preview-summary"); host.replaceChildren(); host.hidden = false;
      const facts = document.createElement("p"); facts.className = "muted";
      facts.textContent = `只读预览 · ${result.nodeCount} 个节点 · ${result.edgeCount} 条连线。`;
      const labels = {start:"开始", end:"结束", agent:"Agent", condition:"条件", human:"人工确认",
        parallel:"并行", join:"汇合", loop:"循环", subflow_input:"子流程入口", subflow_output:"子流程出口"};
      const types = new Map();
      for (const node of result.document.nodes) types.set(node.type, (types.get(node.type) || 0) + 1);
      const summary = document.createElement("p"); summary.className = "muted";
      summary.textContent = `节点类型：${[...types].map(([type, count]) => `${labels[type] || "其他"} ${count}`).join("、") || "无"}。`;
      const boundary = document.createElement("p"); boundary.className = "muted";
      boundary.textContent = "预览不会运行流程。加入后仅修改本地草稿，仍需原保存与发布流程。";
      const identity = document.createElement("details"); identity.className = "technical-details";
      const summaryIdentity = document.createElement("summary"); summaryIdentity.textContent = "查看版本与图标识";
      const identifier = document.createElement("p"); identifier.className = "technical";
      identifier.textContent = `版本 ${result.reference.revision} · 部署标识 ${display(result.reference.deploymentVersionId)} · 图 SHA-256 ${result.graphSha256}`;
      identity.append(summaryIdentity, identifier);
      host.append(facts, summary, boundary, identity);
    }

    async function preparePreview() {
      return run(async (draft, captured, op, capturedScope, originalDocument) => {
        const selection = selectedEdgeAndInput();
        const edgeId = selection?.edgeSelect.value;
        const rawInput = selection?.inputSelect.value;
        if (!selectedReference || !edgeId || !rawInput) throw new Error("请先选择固定版本、已有连线和输入来源。");
        const [nodeId, field] = rawInput.split("\u0000");
        const inputFrom = {nodeId, field};
        const inputMapping = mapping(inputRows), outputMapping = mapping(outputRows);
        const callId = `subflow_${randomSuffix()}`;
        const body = {parentWorkflowId:capturedScope.workflowId, reference:selectedReference,
          expectedParentStateSha256:draft.stateSha256, callId, edgeId, inputFrom,
          ...(inputMapping ? {inputMapping} : {}), ...(outputMapping ? {outputMapping} : {})};
        clearPreview();
        const result = await request("/subworkflows/preview", {method:"POST", body:JSON.stringify(body)});
        if (!current(captured, op, capturedScope)) return;
        const currentDraft = getDraft();
        if (!currentDraft || currentDraft.stateSha256 !== draft.stateSha256
            || canonical(currentDraft.document) !== originalDocument) {
          clearPreview(); say("父草稿在预览期间发生变化，响应已丢弃；请重新预览。", "error"); return;
        }
        const size = plain(result) && plain(result.document)
          ? new TextEncoder().encode(JSON.stringify(result.document)).length : Infinity;
        if (!plain(result) || result.source !== "validated_subworkflow_expansion_preview" || result.readOnly !== true
            || result.executionSupported !== false || result.projectId !== capturedScope.projectId
            || result.parentWorkflowId !== capturedScope.workflowId || result.parentStateSha256 !== draft.stateSha256
            || canonical(result.reference) !== canonical(selectedReference) || result.callId !== callId
            || canonical(result.inputFrom) !== canonical(inputFrom) || !SHA256.test(result.graphSha256 || "")
            || !plain(result.document) || !Array.isArray(result.document.nodes) || result.document.nodes.length > 200
            || result.document.nodes.some((node) => !plain(node) || typeof node.type !== "string")
            || !Array.isArray(result.document.edges) || result.document.edges.length > 1000
            || result.document.edges.some((edge) => !plain(edge) || typeof edge.id !== "string"
              || typeof edge.source !== "string" || typeof edge.target !== "string")
            || result.nodeCount !== result.document.nodes.length || result.edgeCount !== result.document.edges.length
            || size > 1_000_000
            || Object.hasOwn(result, "inputMapping") !== Boolean(inputMapping)
            || inputMapping && canonical(result.inputMapping) !== canonical(inputMapping)
            || Object.hasOwn(result, "outputMapping") !== Boolean(outputMapping)
            || outputMapping && canonical(result.outputMapping) !== canonical(outputMapping)) {
          throw new Error("预览响应来源、身份、映射、摘要或图规模无法核对。");
        }
        preview = clone(result.document);
        previewInputs = {document:originalDocument, stateSha256:draft.stateSha256,
          scope:clone(capturedScope),
          reference:clone(selectedReference), callId, edgeId, inputFrom:clone(inputFrom),
          inputMapping:inputMapping ? clone(inputMapping) : undefined,
          outputMapping:outputMapping ? clone(outputMapping) : undefined, graphSha256:result.graphSha256};
        renderPreview(result);
        say("预览已核对。检查节点和连线后，可将结果加入当前本地草稿。", "success");
        sync();
      });
    }

    function randomSuffix() {
      const bytes = new Uint8Array(16);
      try {
        if (globalThis.crypto?.getRandomValues) crypto.getRandomValues(bytes);
        else for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
      } catch { for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256); }
      return [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
    }

    function applyPreview() {
      sync();
      if (!preview || !previewInputs || blocked || busy || !supported() || !canInsert()) return;
      const draft = getDraft();
      if (!draft || draft.stateSha256 !== previewInputs.stateSha256
          || canonical(draft.document) !== previewInputs.document) {
        clearPreview(); sync(); say("父草稿已变化，预览已作废；请重新选择并预览。", "error"); return;
      }
      if (isCurrent(previewInputs.scope) !== true) {
        clearPreview(); sync(); say("项目或父流程已切换，旧预览已作废。", "error"); return;
      }
      const metadata = {scope:clone(previewInputs.scope), parentStateSha256:previewInputs.stateSha256,
        originalDocument:clone(draft.document)};
      let applied = false;
      try { applied = applyDocument(clone(preview), metadata) === true; }
      catch (error) { say(`未加入草稿：${error?.message || "父编辑器拒绝了变更。"}`, "error"); return; }
      if (!applied) { say("父编辑器未接受预览，原草稿保留。", "error"); return; }
      clear();
      say("子流程已加入本地草稿，尚未保存。请核对后使用原“保存草稿”和发布操作。", "success");
    }

    function addMapping(kind) {
      const rows = kind === "input" ? inputRows : outputRows;
      if (rows.length >= 32) return;
      rows.push({name:"", kind:"path", value:""}); clearPreview(); renderMappings(); sync();
    }

    function startCandidateSearch() {
      search = $("subworkflow-search").value.trim(); candidates = []; candidateCursor = null;
      candidateIds = new Set(); candidateCursors = new Set(); loadCandidates("");
    }
    $("subworkflow-load").addEventListener("click", startCandidateSearch);
    $("subworkflow-search-submit").addEventListener("click", startCandidateSearch);
    $("subworkflow-search").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); $("subworkflow-search-submit").click(); } });
    $("subworkflow-next").addEventListener("click", () => { if (candidateCursor) loadCandidates(candidateCursor); });
    $("subworkflow-version-next").addEventListener("click", () => { if (versionCursor) fetchVersions(versionCursor); });
    $("subworkflow-version").addEventListener("change", readSelectedVersion);
    $("subworkflow-edge").addEventListener("change", () => { clearPreview(); sync(); });
    $("subworkflow-input").addEventListener("change", () => { clearPreview(); sync(); });
    $("subworkflow-input-map-add").addEventListener("click", () => addMapping("input"));
    $("subworkflow-output-map-add").addEventListener("click", () => addMapping("output"));
    $("subworkflow-preview").addEventListener("click", preparePreview);
    $("subworkflow-apply").addEventListener("click", applyPreview);
    $("subworkflow-remove").addEventListener("change", sync);
    $("subworkflow-remove-button").addEventListener("click", () => {
      sync(); const callId = $("subworkflow-remove").value;
      if (!callId || blocked || busy || !canRemove()) return;
      try {
        if (removeGroup(callId) === true) {
          renderRemovals(); say("已从本地草稿整体移除子流程组；请检查后续节点的输入来源，再使用原保存操作。", "success");
        } else say("父编辑器未移除该子流程组，原草稿保留。", "error");
      } catch (error) { say(`未移除子流程：${error?.message || "父编辑器拒绝了变更。"}`, "error"); }
      sync();
    });

    renderRemovals(); renderMappings(); sync();
    return {clear, sync};
  }

  window.AgentLineSubworkflows = {create};
})();
