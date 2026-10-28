'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const plain = value => !!value && typeof value === 'object' && !Array.isArray(value);
  const limit = 256 * 1024;
  const bytes = value => new TextEncoder().encode(value).length;
  function inspect(value) {
    if (!plain(value) || Object.keys(value).sort().join(',') !== 'document,format,roleSlots,schemaVersion'
      || value.format !== 'agent-line-workflow-template' || value.schemaVersion !== 1
      || !plain(value.document) || value.document.schemaVersion !== 1
      || !Array.isArray(value.document.nodes) || value.document.nodes.length < 2 || value.document.nodes.length > 200
      || !Array.isArray(value.document.edges) || value.document.edges.length > 1000
      || !Array.isArray(value.roleSlots) || value.roleSlots.length > 200
      || bytes(JSON.stringify(value)) > limit) throw new Error('模板格式或大小不受支持。');
    const slots = new Set();
    for (const slot of value.roleSlots) {
      if (!plain(slot) || Object.keys(slot).sort().join(',') !== 'id,name'
        || typeof slot.id !== 'string' || !/^slot_[1-9][0-9]{0,2}$/.test(slot.id)
        || slots.has(slot.id) || typeof slot.name !== 'string' || !slot.name.trim() || slot.name.length > 80) {
        throw new Error('模板角色槽位无法核对。');
      }
      slots.add(slot.id);
    }
    return value;
  }
  function create({context, request, applyDocument}) {
    let generation = 0, key = '', loading = false, recipe = null, exported = null;
    const message = $('portable-message'), mappings = $('portable-role-mappings');
    const fileInput = $('portable-file');
    function identity(ctx) {
      return JSON.stringify([ctx.epoch,ctx.projectId,ctx.workflowId,ctx.document,ctx.formDirty,ctx.rolesReady,ctx.roles.map(role => [role.roleBindingId,role.roleAgentId])]);
    }
    function available(ctx) {
      return !!ctx.projectId && !!ctx.workflowId && ctx.editable && !!ctx.document && !ctx.blocked && !ctx.formDirty;
    }
    function clear() {
      generation += 1; loading = false; recipe = null; exported = null;
      fileInput.value = ''; mappings.replaceChildren();
      $('portable-export-preview').hidden = true; $('portable-import-preview').hidden = true;
      $('portable-export-content').value = ''; $('portable-export-reviewed').checked = false;
      $('portable-import-summary').textContent = '';
      message.textContent = '模板只在当前页面暂存；先打开可编辑草稿。';
    }
    function sync() {
      const ctx = context(), currentKey = identity(ctx);
      if (key !== currentKey) { clear(); key = currentKey; }
      const ready = available(ctx) && !loading;
      $('portable-export').disabled = !ready;
      fileInput.disabled = !ready || !ctx.rolesReady;
      $('portable-download').disabled = !ready || !exported || !$('portable-export-reviewed').checked;
      const selects = [...mappings.querySelectorAll('select')];
      selects.forEach(select => { select.disabled = !ready || !ctx.rolesReady; });
      $('portable-apply').disabled = !ready || !ctx.rolesReady || !recipe || selects.some(select => !select.value);
    }
    function stillCurrent(captured, expectedKey) {
      return captured === generation && identity(context()) === expectedKey;
    }
    async function prepareExport() {
      sync(); const ctx = context();
      if (!available(ctx) || loading) return;
      const captured = ++generation, expectedKey = key;
      exported = null; loading = true; $('portable-export-preview').hidden = true; sync();
      message.textContent = '正在校验当前草稿的可分享结构…';
      try {
        const result = inspect(await request('/templates/export', {document:ctx.document}));
        if (!stillCurrent(captured, expectedKey)) return;
        exported = JSON.stringify(result, null, 2);
        if (bytes(exported) > limit) exported = JSON.stringify(result);
        $('portable-export-content').value = exported;
        $('portable-export-reviewed').checked = false; $('portable-export-preview').hidden = false;
        message.textContent = '已准备模板。请核对节点文本、条件值和固定输出，再下载；不会导出运行记录或记忆。';
      } catch (error) {
        if (stillCurrent(captured, expectedKey)) message.textContent = `导出未完成：${error.message || '请核对草稿后重试。'}`;
      } finally {
        if (stillCurrent(captured, expectedKey)) { loading = false; sync(); }
      }
    }
    async function readFile() {
      const file = fileInput.files?.[0];
      if (!file) return;
      sync(); const ctx = context();
      if (!available(ctx) || !ctx.rolesReady || loading) return;
      const captured = ++generation, expectedKey = key;
      recipe = null; mappings.replaceChildren(); $('portable-import-preview').hidden = true;
      loading = true; sync(); message.textContent = '正在读取本地模板；尚未上传或修改草稿…';
      try {
        if (file.size > limit) throw new Error('文件超过 256 KiB，请缩小模板。');
        const text = await file.text();
        if (!stillCurrent(captured, expectedKey)) return;
        if (bytes(text) > limit) throw new Error('文件超过 256 KiB。');
        recipe = inspect(JSON.parse(text));
        for (const slot of recipe.roleSlots) {
          const label = document.createElement('label'), select = document.createElement('select');
          label.textContent = `${slot.name} · 选择本项目既有角色`;
          select.dataset.slot = slot.id; select.add(new Option('请明确选择角色', ''));
          for (const role of ctx.roles) select.add(new Option(role.details?.displayName || role.roleBindingId, role.roleBindingId));
          select.addEventListener('change', () => { generation += 1; loading = false; sync(); });
          label.append(select); mappings.append(label);
        }
        $('portable-import-summary').textContent = `${recipe.document.nodes.length} 个节点，${recipe.document.edges.length} 条连线，${recipe.roleSlots.length} 个角色槽位。绑定相同角色不会增加权限；独立审查要求仍须由你核对。`;
        $('portable-import-preview').hidden = false;
        message.textContent = '文件已在本地读取。选择角色后，点击校验并载入；尚未修改或保存草稿。';
      } catch (error) {
        if (stillCurrent(captured, expectedKey)) {
          recipe = null; message.textContent = `模板未载入：${error instanceof SyntaxError ? '文件不是有效 JSON。' : error.message}`;
        }
      } finally {
        if (stillCurrent(captured, expectedKey)) { loading = false; fileInput.value = ''; sync(); }
      }
    }
    async function apply() {
      sync(); const ctx = context();
      if (!available(ctx) || !ctx.rolesReady || !recipe || loading) return;
      const roleBindings = Object.fromEntries([...mappings.querySelectorAll('select')].map(select => [select.dataset.slot,select.value]));
      if (Object.values(roleBindings).some(value => !value)) return;
      const captured = ++generation, expectedKey = key, candidate = recipe;
      loading = true; sync(); message.textContent = '正在校验模板及本项目角色；尚未保存或发布…';
      try {
        const result = await request('/templates/import', {template:candidate,roleBindings});
        if (!stillCurrent(captured, expectedKey) || !available(context())) return;
        if (!plain(result) || result.source !== 'validated_workflow_template' || result.projectId !== ctx.projectId
          || !plain(result.document) || result.document.schemaVersion !== 1 || !Array.isArray(result.document.nodes)
          || !Array.isArray(result.document.edges) || bytes(JSON.stringify(result.document)) > limit) {
          throw new Error('模板校验响应身份或内容无法核对。');
        }
        if (applyDocument(result.document)) {
          clear(); key = identity(context());
          message.textContent = '模板已载入本地画布，尚未保存；请核对节点，再点击原“保存草稿”。';
        } else message.textContent = '已保留原画布，未载入模板。';
      } catch (error) {
        if (stillCurrent(captured, expectedKey)) message.textContent = `模板未载入，原草稿保留：${error.message || '请核对后重试。'}`;
      } finally {
        if (stillCurrent(captured, expectedKey)) loading = false;
        sync();
      }
    }
    $('portable-export').addEventListener('click',prepareExport);
    $('portable-clear').addEventListener('click',() => { clear(); sync(); });
    fileInput.addEventListener('change',readFile);
    $('portable-apply').addEventListener('click',apply);
    $('portable-export-reviewed').addEventListener('change',sync);
    $('portable-download').addEventListener('click',() => {
      sync(); if (!exported || $('portable-download').disabled) return;
      const url = URL.createObjectURL(new Blob([exported],{type:'application/json;charset=utf-8'}));
      const anchor = document.createElement('a'); anchor.href = url; anchor.download = 'agent-line-workflow.json';
      document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url),1000);
    });
    return {sync,clear};
  }
  window.AgentLinePortableTemplates = {create};
})();
