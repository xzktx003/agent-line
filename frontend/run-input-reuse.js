'use strict';
(() => {
  const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const canonical = value => {
    if (value === null || ['string','boolean'].includes(typeof value)) return JSON.stringify(value);
    if (typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= Number.MAX_SAFE_INTEGER) return JSON.stringify(value);
    if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
    if (!plain(value)) throw new Error('原输入不是有效 JSON。');
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  };
  function create({request, scope, targetSignature, canApply, apply, safeText}) {
    const panel = document.getElementById('run-input-reuse');
    const read = document.getElementById('run-input-read');
    const message = document.getElementById('run-input-message');
    const output = document.getElementById('run-input-preview');
    if (!panel || !read || !message || !output) return null;
    let generation = 0, snapshot = null, loading = false;
    function clear(text = '选择原工作流和运行后，显式读取原输入；尚未准备新运行。') {
      generation += 1; snapshot = null; loading = false;
      output.replaceChildren(); message.textContent = text; read.disabled = !scope();
    }
    const sameScope = captured => {
      const current = scope(); return current && canonical(current) === canonical(captured);
    };
    read.addEventListener('click', async () => {
      const captured = scope();
      if (loading || !captured) return;
      clear(); loading = true; read.disabled = true;
      const attempt = generation, target = targetSignature();
      message.textContent = '正在读取本地根提交的原输入，不访问原生执行服务…';
      try {
        const value = await request(`/workflows/${encodeURIComponent(captured.bindingId)}/runs/${encodeURIComponent(captured.runId)}/input`);
        if (generation !== attempt || !sameScope(captured)) return;
        if (targetSignature() !== target) throw new Error('启动表单已变化，请重新读取原输入后核对。');
        if (!plain(value) || value.source !== 'local_run_input' || value.readOnly !== true
            || value.executionSupported !== false || value.bindingId !== captured.bindingId
            || value.projectId !== captured.projectId || value.workflowId !== captured.workflowId
            || value.runId !== captured.runId || !/^[a-f0-9]{64}$/.test(value.inputSha256 || '')
            || !plain(value.input)) throw new Error('原输入回执的身份或格式无法核对，请重新选择原运行。');
        const raw = canonical(value.input);
        if (new TextEncoder().encode(raw).length > 64000) throw new Error('原输入超过复用入口的64,000字节限制。');
        const credentialIn = value => {
          if (typeof value === 'string') return safeText(value) !== value;
          if (Array.isArray(value)) return value.some(credentialIn);
          return plain(value) && Object.keys(value).some(key => safeText(key) !== key || credentialIn(value[key]));
        };
        const credentialPresent = credentialIn(value.input) || safeText(raw) !== raw;
        if (credentialPresent) {message.textContent = '原输入包含当前凭证，禁止展示和回填；请手动准备不含凭证的新输入。'; return;}
        const details = document.createElement('details');
        const summary = document.createElement('summary'); summary.textContent = '查看原始输入 JSON';
        const pre = document.createElement('pre'); pre.className = 'technical';
        pre.textContent = safeText(JSON.stringify(value.input,null,2)); details.append(summary,pre); output.append(details);
        snapshot = {captured,target,raw};
        const label = document.createElement('label'); label.className = 'field-help';
        const consent = document.createElement('input'); consent.type = 'checkbox';
        label.append(consent,document.createTextNode(' 我已核对原输入；允许覆盖启动表单并创建新触发标识。新运行可能再次产生业务副作用。'));
        const prepare = document.createElement('button'); prepare.type = 'button'; prepare.className = 'secondary';
        prepare.textContent = '填入新运行表单'; prepare.disabled = true;
        consent.addEventListener('change', () => {
          prepare.disabled = !consent.checked;
          if (consent.checked && !canApply()) message.textContent = '当前提交尚未取得明确回执，暂不能覆盖启动表单；请先核对原提交。';
        });
        prepare.addEventListener('click', () => {
          if (!consent.checked || !snapshot || generation !== attempt || !sameScope(captured)) return;
          if (targetSignature() !== target) {clear('启动表单已变化，请重新读取原输入后核对。'); return;}
          if (!canApply()) {message.textContent = '当前有提交尚未取得明确回执，请先核对原提交，保留原触发标识。'; return;}
          apply({bindingId:captured.bindingId,input:JSON.parse(raw)});
          clear('已填入原输入并生成新触发标识，旧业务任务关联已清除。尚未启动；请核对目标、输入和副作用后单独启动。');
        });
        output.append(label,prepare);
        message.textContent = '原输入已读取。回填不会恢复旧运行，也不会复制旧输出、审批或提交回执。';
      } catch (error) {
        if (generation !== attempt || !sameScope(captured)) return;
        snapshot = null; output.replaceChildren(); message.textContent = safeText(error?.message || '原输入暂时无法读取。');
      } finally {
        if (generation === attempt) {loading = false; read.disabled = !scope();}
      }
    });
    panel.addEventListener('toggle', () => {read.disabled = loading || !scope();});
    return {clear};
  }
  window.AgentLineRunInputReuse = {create};
})();
