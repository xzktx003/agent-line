(() => {
  "use strict";

  const ID = /^[A-Za-z0-9_-]{1,160}$/;
  const TASK_ID = /^task-[0-9a-f]{32}$/;
  const SHA256 = /^[0-9a-f]{64}$/;
  const MAX_QUEUE_BYTES = 256 * 1024;
  const MAX_CONTENT_LENGTH = 128000;
  const PYTHON_EDGE_SPACE = /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/gu;

  function createDecisionId() {
    const cryptoApi = globalThis.crypto;
    if (cryptoApi && typeof cryptoApi.randomUUID === "function") return cryptoApi.randomUUID();
    if (!cryptoApi || typeof cryptoApi.getRandomValues !== "function") {
      throw new Error("当前环境无法安全生成审核标识。");
    }
    const bytes = cryptoApi.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  function hasLoneSurrogate(value) {
    for (let index = 0; index < value.length; index += 1) {
      const code = value.charCodeAt(index);
      if (code >= 0xd800 && code <= 0xdbff) {
        const next = value.charCodeAt(index + 1);
        if (!(next >= 0xdc00 && next <= 0xdfff)) return true;
        index += 1;
      } else if (code >= 0xdc00 && code <= 0xdfff) return true;
    }
    return false;
  }

  function validateEntry(entry) {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)
        || typeof entry.key !== "string" || !entry.key || entry.key.length > 512
        || !ID.test(entry.bindingId || "") || !ID.test(entry.projectId || "")
        || !ID.test(entry.roleAgentId || "") || !TASK_ID.test(entry.taskId || "")
        || typeof entry.content !== "string" || entry.content.length > MAX_CONTENT_LENGTH
        || entry.content.includes("\0") || hasLoneSurrogate(entry.content)
        || !SHA256.test(entry.expectedEvidenceSha256 || "")) {
      throw new Error("候选信息无效，请重新读取原任务后再加入。");
    }
    const content = entry.content.replace(PYTHON_EDGE_SPACE, "");
    if (!content || content.length > MAX_CONTENT_LENGTH || encoderBytes(content) > MAX_CONTENT_LENGTH) {
      throw new Error("审核正文为空或超过 128000 字节。");
    }
    return {
      key: entry.key, bindingId: entry.bindingId, projectId: entry.projectId,
      roleAgentId: entry.roleAgentId, taskId: entry.taskId,
      content, expectedEvidenceSha256: entry.expectedEvidenceSha256,
    };
  }

  function encoderBytes(value) {
    return new TextEncoder().encode(value).length;
  }

  function create({ container, request, isCurrent, onAttempt, onOpen, onBusy, safe }) {
    if (!(container instanceof Element) || typeof request !== "function"
        || typeof isCurrent !== "function" || typeof onAttempt !== "function") {
      throw new TypeError("记忆批量审核初始化参数无效。");
    }

    const entries = [];
    let generation = 0;
    let operationId = 0;
    let operationBusy = false;
    let parentBlocked = false;
    let notice = null;
    let list = null;
    let submitButton = null;
    let queryButton = null;
    let confirmed = null;

    const shown = (value) => {
      let result = value;
      try {
        if (typeof safe === "function") result = safe(value);
      } catch {
        result = "[内容已隐藏]";
      }
      return typeof result === "string" ? result : String(result ?? "");
    };
    const current = (entry) => {
      try { return isCurrent(entry) === true; } catch { return false; }
    };
    const busyNow = () => operationBusy || parentBlocked;

    const setNotice = (message, tone = "neutral") => {
      if (!notice) return;
      notice.textContent = message;
      notice.dataset.tone = tone;
    };

    function build() {
      container.replaceChildren();
      const details = document.createElement("details");
      details.className = "memory-batch-panel";
      const summary = document.createElement("summary");
      summary.textContent = "批量审核记忆候选";
      details.append(summary);

      const explanation = document.createElement("p");
      explanation.className = "field-help";
      explanation.textContent = "候选仅暂存在当前页面。提交会按加入顺序调用原逐项审核接口，不会自动重发；结果未知时只能查询原记录。所有已发送项目都确认写入后，才能继续提交未发送项目。";
      details.append(explanation);

      const queueSummary = document.createElement("p");
      queueSummary.className = "memory-batch-count";
      details.append(queueSummary);

      list = document.createElement("div");
      list.className = "memory-batch-list";
      details.append(list);

      confirmed = document.createElement("input");
      confirmed.type = "checkbox";
      confirmed.id = "memory-batch-confirm";
      const confirmLabel = document.createElement("label");
      confirmLabel.className = "memory-batch-confirm";
      confirmLabel.append(confirmed, document.createTextNode("我已逐项核对正文、来源任务和证据指纹，确认按顺序提交。"));
      details.append(confirmLabel);

      const actions = document.createElement("div");
      actions.className = "memory-batch-actions";
      submitButton = document.createElement("button");
      submitButton.type = "button";
      submitButton.textContent = "按顺序提交未发送项";
      submitButton.addEventListener("click", submitUnsent);
      queryButton = document.createElement("button");
      queryButton.type = "button";
      queryButton.textContent = "只查询已发送记录";
      queryButton.addEventListener("click", querySent);
      actions.append(submitButton, queryButton);
      details.append(actions);

      notice = document.createElement("p");
      notice.className = "field-help memory-batch-notice";
      notice.setAttribute("role", "status");
      details.append(notice);
      container.append(details);
      confirmed.addEventListener("change", refreshControls);
      render();
    }

    function render() {
      if (!list) return;
      list.replaceChildren();
      const count = container.querySelector(".memory-batch-count");
      if (count) count.textContent = `暂存 ${entries.length} 项；每项目最多 10 项，队列正文上限 256 KiB。`;
      for (const entry of entries) {
        const card = document.createElement("article");
        card.className = "memory-batch-item";
        const title = document.createElement("h4");
        title.textContent = `${entry.sent ? "已发送" : "未发送"} · ${shown(entry.taskId)}`;
        card.append(title);
        const identity = document.createElement("p");
        identity.className = "field-help";
        identity.textContent = `项目 ${shown(entry.projectId)} · 角色绑定 ${shown(entry.bindingId)} · 原生角色 ${shown(entry.roleAgentId)} · 证据 SHA-256 ${shown(entry.expectedEvidenceSha256)}`;
        card.append(identity);
        const decision = document.createElement("p");
        decision.className = "field-help";
        decision.textContent = `审核标识 ${shown(entry.decisionId)}`;
        card.append(decision);
        const state = document.createElement("p");
        state.className = "memory-batch-state";
        state.textContent = entry.stateText;
        card.append(state);
        const preview = document.createElement("pre");
        preview.className = "memory-batch-content";
        preview.textContent = shown(entry.content);
        card.append(preview);

        const row = document.createElement("div");
        row.className = "memory-batch-item-actions";
        const open = document.createElement("button");
        open.type = "button";
        open.textContent = "打开原任务详情";
        open.disabled = busyNow() || typeof onOpen !== "function";
        open.addEventListener("click", () => {
          if (busyNow() || !current(entry) || typeof onOpen !== "function") return;
          try { onOpen(Object.freeze({ ...entry })); } catch { setNotice("无法打开原任务详情，请返回任务列表重新选择。", "error"); }
        });
        row.append(open);
        if (!entry.sent || entry.state === "committed") {
          const remove = document.createElement("button");
          remove.type = "button";
          remove.textContent = entry.sent ? "清除已确认项的本页正文" : "移除未发送项";
          remove.disabled = busyNow();
          remove.addEventListener("click", () => {
            if (busyNow() || (entry.sent && entry.state !== "committed")) return;
            const sent = entry.sent;
            entry.content = ""; entry.decisionId = "";
            entries.splice(entries.indexOf(entry), 1);
            if (confirmed) confirmed.checked = false;
            setNotice(sent ? "已清除本页暂存内容，原审核和记忆保留。" : "已移除未发送的暂存项。", "neutral");
            render();
          });
          row.append(remove);
        }
        card.append(row);
        list.append(card);
      }
      refreshControls();
    }

    function refreshControls(blocked) {
      if (typeof blocked === "boolean") parentBlocked = blocked;
      const unavailable = busyNow();
      if (confirmed) confirmed.disabled = unavailable || entries.length === 0;
      const sent = entries.filter((entry) => entry.sent);
      const unresolvedSent = sent.some((entry) => entry.state !== "committed");
      if (submitButton) submitButton.disabled = unavailable || entries.length === 0
        || !confirmed?.checked || unresolvedSent || !entries.some((entry) => !entry.sent);
      if (queryButton) queryButton.disabled = unavailable || sent.length === 0;
      if (list) {
        for (const button of list.querySelectorAll("button")) button.disabled = unavailable
          || button.textContent === "打开原任务详情" && typeof onOpen !== "function";
      }
    }

    function pathFor(entry) {
      return `/${encodeURIComponent(entry.bindingId)}/${encodeURIComponent(entry.taskId)}`;
    }

    function sameIdentity(record, entry) {
      return record && typeof record === "object" && !Array.isArray(record)
        && record.bindingId === entry.bindingId && record.projectId === entry.projectId
        && record.roleAgentId === entry.roleAgentId && record.taskId === entry.taskId
        && record.decisionId === entry.decisionId && record.source
        && typeof record.source === "object" && !Array.isArray(record.source)
        && record.source.evidenceSha256 === entry.expectedEvidenceSha256
        && SHA256.test(record.contentSha256 || "");
    }

    function statusText(status) {
      const labels = {
        committed: "已确认写入",
        review_recorded: "审核已记录，记忆写入尚未确认；请只查询原记录",
        pending: "写入处理中；请只查询原记录",
        failed: "写入失败；请只查询原记录核对",
        needs_review: "需要进一步核对；请只查询原记录",
        deleting: "记忆删除处理中，不能作为完成",
        deleted: "记录已删除，不能作为完成",
      };
      return Object.hasOwn(labels, status) ? labels[status] : "状态未确认；请只查询原记录";
    }

    function setUnknown(entry, message) {
      entry.state = "unknown";
      entry.stateText = message;
      render();
    }

    async function runOperation(action, work) {
      if (busyNow()) return;
      const myGeneration = generation;
      const myOperation = ++operationId;
      operationBusy = true;
      try { onBusy?.(true); } catch { /* Parent lock is best-effort; local controls remain locked. */ }
      render();
      try {
        await work(myGeneration);
      } finally {
        if (generation === myGeneration && operationId === myOperation) {
          operationBusy = false;
          try { onBusy?.(false); } catch { /* Never surface callback details. */ }
          refreshControls();
        }
      }
    }

    async function submitUnsent() {
      if (busyNow()) return;
      if (!confirmed?.checked) {
        setNotice("请先确认已逐项核对正文、任务和来源指纹。", "error");
        return;
      }
      if (entries.some((entry) => entry.sent && entry.state !== "committed")) {
        setNotice("仍有已发送项目未确认写入；请先只查询这些原记录。", "error");
        return;
      }
      confirmed.checked = false;
      refreshControls();
      await runOperation("提交", async (myGeneration) => {
        for (const entry of entries.slice()) {
          if (entry.sent) continue;
          if (generation !== myGeneration || !current(entry)) return;
          const payload = Object.freeze({
            decisionId: entry.decisionId,
            content: entry.content,
            expectedEvidenceSha256: entry.expectedEvidenceSha256,
          });
          try {
            onAttempt(Object.freeze({ ...entry }), payload);
          } catch (error) {
            entry.state = "not_sent";
            const expired = error?.code === 'candidate_deadline';
            entry.stateText = expired ? "候选写入期限已到、格式无效或服务不支持，未发送本项。" : "与原审核操作冲突，未发送本项；请先核对原任务。";
            setNotice(expired ? "候选期限无法通过核对，已停止队列；未发送本项及后续项目。" : "审核操作冲突，已停止队列；未发送本项及后续项目。", "error");
            render();
            return;
          }
          entry.sent = true;
          entry.state = "sending";
          entry.stateText = "正在提交原审核请求…";
          render();
          let receipt;
          try {
            if (generation !== myGeneration || !current(entry)) return;
            receipt = await request(`${pathFor(entry)}/review`, payload);
          } catch {
            if (generation !== myGeneration || !current(entry)) return;
            setUnknown(entry, "提交结果未知；禁止再次提交，只能查询原记录。");
            setNotice("未收到可核对的审核回执，队列已停止。请只查询已发送记录。", "error");
            return;
          }
          if (generation !== myGeneration || !current(entry)) return;
          if (!sameIdentity(receipt, entry)) {
            setUnknown(entry, "回执身份或来源指纹不匹配；禁止再次提交，只能查询原记录。");
            setNotice("审核回执无法核对，队列已停止。", "error");
            return;
          }
          if (receipt.status === "committed" && receipt.content === entry.content) {
            entry.state = "committed";
            entry.stateText = "已确认写入。";
            render();
            continue;
          }
          entry.state = "not_committed";
          entry.stateText = statusText(receipt.status);
          setNotice("本项尚未确认写入，队列已停止；请查询原记录后再决定后续操作。", "error");
          render();
          return;
        }
        setNotice("本轮按顺序处理完成；如有未发送项目，请重新明确确认后继续。", "success");
      });
    }

    async function querySent() {
      const sent = entries.filter((entry) => entry.sent);
      if (!sent.length || busyNow()) return;
      if (confirmed) confirmed.checked = false;
      await runOperation("查询", async (myGeneration) => {
        for (const entry of sent) {
          if (generation !== myGeneration || !current(entry)) return;
          entry.stateText = "正在查询已发送记录…";
          render();
          let record;
          try {
            if (generation !== myGeneration || !current(entry)) return;
            record = await request(pathFor(entry));
          } catch {
            if (generation !== myGeneration || !current(entry)) return;
            entry.state = "unknown";
            entry.stateText = "查询未能确认结果；保留原标识，只能再次查询。";
            setNotice("至少一条已发送记录仍未确认；没有重新提交任何项目。", "error");
            render();
            continue;
          }
          if (generation !== myGeneration || !current(entry)) return;
          if (!sameIdentity(record, entry)) {
            entry.state = "unknown";
            entry.stateText = "查询记录身份或来源不匹配；仍未确认。";
            setNotice("查询记录无法核对；没有重新提交任何项目。", "error");
            render();
            continue;
          }
          if (record.status === "committed" && record.content !== entry.content) {
            entry.state = "unknown";
            entry.stateText = "已写入记录的正文与审核内容不一致；仍未确认。";
          } else if (record.status === "committed") {
            entry.state = "committed";
            entry.stateText = "已确认写入。";
          } else {
            entry.state = "not_committed";
            entry.stateText = statusText(record.status);
          }
          render();
        }
        if (sent.every((entry) => entry.state === "committed")) {
          setNotice("所有已发送记录均已确认正文一致且写入完成。", "success");
        }
      });
    }

    function stage(entry) {
      const copy = validateEntry(entry);
      if (shown(copy.content) !== copy.content) throw new Error("正文包含不能按原文预览的内容，请先移除后再加入。");
      if (busyNow() || !current(copy)) throw new Error("审核身份或操作状态已变化。");
      if (entries.some(item => item.projectId !== copy.projectId)) throw new Error("请在同一项目中暂存候选。");
      if (entries.some((item) => item.key === copy.key)) throw new Error("此候选已在批量队列中。");
      const projectCount = entries.filter((item) => item.projectId === copy.projectId).length;
      if (projectCount >= 10) throw new Error("同一项目的批量队列最多暂存 10 项。");
      if (entries.some((item) => item.bindingId === copy.bindingId && item.taskId === copy.taskId)) {
        throw new Error("同一审核来源已在队列中。");
      }
      const totalBytes = entries.reduce((total, item) => total + encoderBytes(item.content), 0)
        + encoderBytes(copy.content);
      if (totalBytes > MAX_QUEUE_BYTES) throw new Error("批量正文总量超过 256 KiB。");
      const decisionId = createDecisionId();
      entries.push({ ...copy, decisionId, sent: false, state: "staged", stateText: "已暂存，尚未提交。" });
      if (confirmed) confirmed.checked = false;
      setNotice("候选已暂存；没有发送审核请求。", "neutral");
      render();
      return decisionId;
    }

    function clear() {
      generation += 1;
      operationId += 1;
      operationBusy = false;
      for (const entry of entries) {entry.content = ""; entry.decisionId = "";}
      entries.splice(0, entries.length);
      build();
      setNotice("队列已清空。", "neutral");
      refreshControls();
    }

    function has(key) {
      return entries.some((entry) => entry.key === key);
    }

    build();
    return { stage, clear, refreshControls, has };
  }

  window.AgentLineMemoryBatch = { create };
})();
