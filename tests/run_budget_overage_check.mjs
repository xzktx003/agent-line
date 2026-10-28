import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const app = readFileSync(path.join(root, 'frontend/app.js'), 'utf8');
const start = app.indexOf('  function renderRunBudget(');
const end = app.indexOf('  function usageMessage(');
assert.ok(start >= 0 && end > start, 'run budget renderer must stay in app.js');
const source = app.slice(start, end);

function element(tag) {
  return {
    tag,
    id: '',
    className: '',
    textContent: '',
    children: [],
    setAttribute() {},
    append(...nodes) { this.children.push(...nodes); },
    remove() {},
  };
}

function render(value) {
  const nodes = { 'run-result': element('div') };
  const document = {
    getElementById(id) { return nodes[id] || null; },
    createElement: element,
  };
  const fn = new Function('document', `
    const byId = (id) => document.getElementById(id);
    ${source}
    return renderRunBudget;
  `);
  fn(document)({ budget: value }, 'binding_1', 'run_1', {
    projectId: 'project', workflowId: 'workflow',
  });
  const section = nodes['run-result'].children[0];
  assert.equal(section.id, 'run-budget-summary');
  const texts = [];
  const walk = (node) => {
    if (node.textContent) texts.push(node.textContent);
    for (const child of node.children) walk(child);
  };
  walk(section);
  return texts;
}

function budget(overrides) {
  const observedAt = 1_700_000_000;
  return {
    schemaVersion: 1,
    bindingId: 'binding_1',
    scope: { projectId: 'project', workflowId: 'workflow', runId: 'run_1' },
    state: 'configured',
    maxSubmissions: 2,
    submissionsUsed: 1,
    remainingSubmissions: 1,
    maxExecutionSeconds: 3600,
    deadlineAt: observedAt + 100,
    budgetAllowsSubmission: true,
    observedAt,
    usage: { tokens: null, modelCalls: null, cost: null },
    ...overrides,
  };
}

const within = render(budget({}));
assert.ok(within.some((text) => text.includes('尚有剩余')));
assert.ok(within.includes('1 次'));
assert.equal(within.includes('超出提交次数'), false);

const exact = render(budget({
  submissionsUsed: 2,
  remainingSubmissions: 0,
  budgetAllowsSubmission: false,
}));
assert.ok(exact.some((text) => text.includes('已耗尽或时限已到')));
assert.ok(exact.some((text) => text.includes('不能据此判定活动进程已停止')));
assert.ok(exact.includes('0 次'));

const overdue = render(budget({
  deadlineAt: 1_700_000_000 - 10,
  budgetAllowsSubmission: false,
}));
assert.ok(overdue.some((text) => text.includes('已耗尽或时限已到')));
assert.ok(overdue.includes('1 次'));
assert.equal(overdue.includes('超出提交次数'), false);

const over = render(budget({
  maxSubmissions: 2,
  submissionsUsed: 3,
  remainingSubmissions: -1,
  budgetAllowsSubmission: false,
}));
assert.ok(over.some((text) => text.includes('已超出上限 1 次')));
assert.ok(over.some((text) => text.includes('不能据此把活动进程标成已停止')));
assert.equal(over.some((text) => text.includes('已耗尽')), false);
assert.ok(over.includes('超出提交次数'));
assert.ok(over.includes('1 次'));
assert.equal(over.includes('0 次'), false);

const clamped = render(budget({
  maxSubmissions: 2,
  submissionsUsed: 3,
  remainingSubmissions: 0,
  budgetAllowsSubmission: false,
}));
assert.ok(clamped.some((text) => text.includes('暂时无法核对')));
assert.equal(clamped.some((text) => text.includes('已耗尽')), false);
