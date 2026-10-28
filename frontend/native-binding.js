(() => {
  "use strict";

  const ID = /^[A-Za-z0-9_-]{1,160}$/;

  function plain(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function validWait(value) {
    return plain(value) && ID.test(value.id || "") && typeof value.name === "string"
      && value.name.length > 0 && value.name.length <= 120 && !value.name.includes("\0")
      && (value.taskInput === null || value.taskInput === "object" || value.taskInput === "string")
      && Object.keys(value).every(key => ["id", "name", "taskInput"].includes(key));
  }

  function create(options) {
    const container = options.container;
    const list = container.querySelector("#native-binding-list");
    const status = container.querySelector("#native-binding-status");
    const button = container.querySelector("#native-binding-publish");
    let waits = null;
    let scope = null;

    function roleOptions(selected) {
      const select = document.createElement("select");
      select.append(new Option("选择既有角色", ""));
      for (const role of options.roles()) {
        if (!role || !ID.test(role.roleBindingId || "")) continue;
        const label = role.details && typeof role.details.displayName === "string"
          ? role.details.displayName : role.roleBindingId;
        select.add(new Option(label, role.roleBindingId));
      }
      if ([...select.options].some(option => option.value === selected)) select.value = selected;
      return select;
    }

    function render() {
      const previous = new Map([...list.querySelectorAll("[data-wait-id]")].map(row => [row.dataset.waitId, {
        kind: row.querySelector("[data-kind]").value,
        role: row.querySelector("[data-role]")?.value || "",
        instruction: row.querySelector("[data-instruction]")?.value || "",
        memory: row.querySelector("[data-memory]")?.checked === true,
        failure: row.querySelector("[data-failure]")?.checked === true,
      }]));
      list.replaceChildren();
      if (!Array.isArray(waits)) {
        container.hidden = true;
        return;
      }
      container.hidden = false;
      if (!waits.length) {
        status.textContent = "原版图没有可绑定的人工等待节点。图结构请在 Sim 原编辑器修改。";
        button.hidden = true;
        return;
      }
      if (waits === null) return;
      button.hidden = false;
      status.textContent = "每个等待节点都要明确选择既有角色任务或人工确认。发布不覆盖原图，也不调用模型。";
      for (const wait of waits) {
        const saved = previous.get(wait.id) || {};
        const row = document.createElement("fieldset");
        row.dataset.waitId = wait.id;
        const title = document.createElement("legend");
        title.textContent = wait.name === wait.id ? wait.id : wait.name + " · " + wait.id;
        row.append(title);
        const kind = document.createElement("select");
        kind.dataset.kind = "true";
        kind.append(new Option("请选择用途", ""));
        if (wait.taskInput) kind.append(new Option("交给既有角色（taskInput）", "agent"));
        kind.append(new Option("人工确认", "human"));
        if (saved.kind && [...kind.options].some(option => option.value === saved.kind)) kind.value = saved.kind;
        const note = document.createElement("p");
        note.className = "muted";
        note.textContent = wait.taskInput ? "此节点有唯一 taskInput，可以作为角色任务。" : "没有唯一 taskInput，只能标为人工确认。";
        row.append(kind, note);
        const agent = document.createElement("div");
        agent.hidden = kind.value !== "agent";
        const role = roleOptions(saved.role);
        role.dataset.role = "true";
        const instruction = document.createElement("textarea");
        instruction.dataset.instruction = "true";
        instruction.rows = 3;
        instruction.maxLength = 4000;
        instruction.placeholder = "交给既有角色的任务说明";
        instruction.value = saved.instruction || "";
        const memory = document.createElement("label");
        memory.className = "policy-option";
        const memoryInput = document.createElement("input");
        memoryInput.type = "checkbox";
        memoryInput.dataset.memory = "true";
        memoryInput.checked = saved.memory === true;
        memory.append(memoryInput, document.createTextNode("等待记忆提交后再回传结果"));
        const failure = document.createElement("label");
        failure.className = "policy-option";
        const failureInput = document.createElement("input");
        failureInput.type = "checkbox";
        failureInput.dataset.failure = "true";
        failureInput.checked = saved.failure === true;
        failure.append(failureInput, document.createTextNode("允许已确认失败结果进入后续分支"));
        agent.append(role, instruction, memory, failure);
        kind.addEventListener("change", () => { agent.hidden = kind.value !== "agent"; });
        row.append(agent);
        list.append(row);
      }
    }

    function show(next) {
      scope = options.scope();
      if (!Array.isArray(next) || next.length > 200 || next.some(item => !validWait(item))
          || new Set(next.map(item => item.id)).size !== next.length) {
        waits = Array.isArray(next) ? null : null;
        if (Array.isArray(next)) status.textContent = "原版等待节点无法安全识别，未提供绑定。请核对 Sim 图后再读取。";
        container.hidden = !Array.isArray(next);
        button.hidden = true;
        list.replaceChildren();
        if (!Array.isArray(next)) container.hidden = true;
        return;
      }
      waits = next.map(item => ({id: item.id, name: item.name, taskInput: item.taskInput}));
      render();
    }

    function clear() {
      waits = null;
      scope = null;
      list.replaceChildren();
      status.textContent = "";
      container.hidden = true;
      button.hidden = true;
    }

    function payload() {
      const nativeBindings = {};
      const humanBlockIds = [];
      for (const row of list.querySelectorAll("[data-wait-id]")) {
        const id = row.dataset.waitId;
        const wait = waits.find(item => item.id === id);
        const kind = row.querySelector("[data-kind]").value;
        if (!wait || (kind !== "agent" && kind !== "human")) throw new Error("请为每个等待节点选择用途。");
        if (kind === "human") {
          humanBlockIds.push(id);
          continue;
        }
        if (!wait.taskInput) throw new Error("缺少 taskInput 的节点不能交给角色。");
        const roleBindingId = row.querySelector("[data-role]").value;
        const instruction = row.querySelector("[data-instruction]").value.trim();
        if (!options.roles().some(role => role.roleBindingId === roleBindingId)) throw new Error("请选择本项目既有角色。");
        if (!instruction || instruction.length > 4000) throw new Error("请填写不超过 4000 字的任务说明。");
        nativeBindings[id] = {
          roleBindingId,
          instruction,
          requireMemoryCommit: row.querySelector("[data-memory]").checked === true,
          allowFailureBranch: row.querySelector("[data-failure]").checked === true,
        };
      }
      if (Object.keys(nativeBindings).length + humanBlockIds.length !== waits.length) {
        throw new Error("等待节点绑定不完整。");
      }
      const current = options.scope();
      if (!scope || !current || current.projectId !== scope.projectId || current.workflowId !== scope.workflowId
          || current.epoch !== scope.epoch || current.digest !== scope.digest) {
        throw new Error("草稿已变化，请重新读取后再发布。");
      }
      const body = {expectedStateSha256: current.digest, nativeBindings, humanBlockIds};
      const encoded = JSON.stringify(body);
      if (options.token() && encoded.includes(options.token())) throw new Error("任务说明含当前编辑凭证，请移除后再发布。");
      return body;
    }

    button.addEventListener("click", async () => {
      try {
        await options.publish(payload());
      } catch (error) {
        status.textContent = error.message || "原生绑定未发布。";
      }
    });

    return {
      show, clear,
      rolesUpdated() { if (Array.isArray(waits)) render(); },
      sync(disabled) {
        button.disabled = disabled || !Array.isArray(waits) || waits.length === 0;
        list.querySelectorAll("input,select,textarea").forEach(control => { control.disabled = disabled; });
      },
    };
  }

  window.AgentLineNativeBinding = {create};
})();
