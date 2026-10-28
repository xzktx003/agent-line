"""Read-only host/session diagnosis; only sanitized timing/tool-count evidence is saved."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
TASK='task-8603a783ff9b4617aae7e12c07cf83a1'
record=json.loads((ROOT/'var/executor/host-restart/attempt-4/receipt.json').read_text())
path=ROOT/'var/qwenpaw/workspaces/agent-line-g0-parent/sessions/console'/('agent-line-service_agent-line-'+TASK+'.json')
agent=json.loads(path.read_text())['agent']
log=(ROOT/'var/qwenpaw/service.log').read_text(errors='replace').splitlines()
start=next(i for i,line in enumerate(log) if 'built agent for session=agent-line-'+TASK in line)
end=next(i for i in range(start,len(log)) if 'cancel-save:' in log[i] and TASK in log[i])
window=log[start:end+1]
scroll=agent['scroll']
evidence={'taskId':TASK,'parentTaskId':record['parent']['parentTaskId'],
          'officialParentResult':record['parent']['observation']['result'],
          'firstRuntimeEvent':window[0],'lastRuntimeEvent':window[-1],
          'persistedToolResultCount':len(scroll.get('seen_tool_result_ids',[])),
          'persistedReasoningBlockCount':len(scroll.get('seen_thinking_block_ids',[])),
          'providerRetryWarningCount':sum('LLM stream failed' in line for line in window),
          'usageEventCount':sum('Usage for session agent-line-'+TASK in line for line in window),
          'knownBoundary':'Before any persisted model/tool output; no memory or delegate result. Outer console task timeout canceled parent.',
          'unresolved':'Logs do not distinguish rate-limiter wait from provider request/header latency; no measured 429 or network fault should be asserted.',
          'sourceFacts':{'llmAcquireTimeoutDefaultSeconds':300,'consoleProbeTimeoutSeconds':180,
              'streamWatchdog':'retry_chat_model.py __call__ awaits inner before wrapping returned async stream; first-content watchdog is not an end-to-end request deadline.'}}
output=ROOT/'var/executor/host-restart/attempt-4/timeout-diagnosis.json'
output.write_text(json.dumps(evidence,indent=2));output.chmod(0o600)
print(json.dumps({k:evidence[k] for k in ('persistedToolResultCount','persistedReasoningBlockCount','providerRetryWarningCount','usageEventCount','knownBoundary','unresolved')}))
