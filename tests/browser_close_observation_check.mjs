import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const observation = require(path.join(root, 'frontend/run-observation.js'));

assert.equal(observation.browserDiscardRequest(), null);
assert.deepEqual(observation.linkedObservation('?binding=binding_1&run=run_1'), {
  bindingId: 'binding_1', runId: 'run_1',
});
assert.equal(observation.linkedObservation('?binding=binding_1'), null);
assert.equal(observation.linkedObservation('?binding=binding_1&run=run_1&run=run_2'), null);
assert.equal(observation.linkedObservation('?binding=bad%20id&run=run_1'), null);
assert.equal(
  observation.observationSearch('?extra=1&binding=old', 'binding_1', 'run_1'),
  '?extra=1&binding=binding_1&run=run_1',
);
assert.equal(observation.observationSearch('', 'binding_1', 'not an id'), null);
assert.deepEqual(
  observation.linkedObservation(observation.observationSearch('', 'binding_1', 'run_1')),
  { bindingId: 'binding_1', runId: 'run_1' },
);

const app = readFileSync(path.join(root, 'frontend/app.js'), 'utf8');
const discarded = app.match(/addEventListener\('pagehide', \(\) => \{[\s\S]*?\}\);/);
assert.ok(discarded, 'closing the page must keep background tasks');
assert.match(discarded[0], /browserDiscardRequest\(\)/);
assert.doesNotMatch(discarded[0], /request\(|fetch\(|stop|cancel/);
const requestFn = app.slice(app.indexOf('async function request'), app.indexOf('function clearRunControl'));
assert.ok(requestFn.includes('pageDiscarded'));
assert.ok(requestFn.indexOf('pageDiscarded') < requestFn.indexOf('fetch('));
assert.match(app, /function rememberObservation/);
assert.match(app, /function restoreLinkedObservation/);
assert.match(app, /await restoreLinkedObservation\(\)/);
assert.match(readFileSync(path.join(root, 'frontend/runs.html'), 'utf8'), /run-observation\.js/);
