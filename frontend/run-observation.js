'use strict';

const RUN_ID = /^[A-Za-z0-9_-]{1,160}$/;

function observationSearch(currentSearch, bindingId, runId) {
  if (!RUN_ID.test(bindingId) || !RUN_ID.test(runId)) return null;
  const params = new URLSearchParams(typeof currentSearch === 'string' ? currentSearch : '');
  params.set('binding', bindingId);
  params.set('run', runId);
  return `?${params.toString()}`;
}

function linkedObservation(search) {
  const params = new URLSearchParams(typeof search === 'string' ? search : '');
  const bindingId = params.get('binding');
  const runId = params.get('run');
  if (!RUN_ID.test(bindingId || '') || !RUN_ID.test(runId || '')) return null;
  if (params.getAll('binding').length !== 1 || params.getAll('run').length !== 1) return null;
  return {bindingId, runId};
}

function browserDiscardRequest() {
  return null;
}

const api = {observationSearch, linkedObservation, browserDiscardRequest};
if (typeof window !== 'undefined') window.AgentLineRunObservation = api;
if (typeof module !== 'undefined' && module.exports) module.exports = api;
