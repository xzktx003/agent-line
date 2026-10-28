#!/usr/bin/env python3
"""Dedicated native loop proof; no model, role, memory, or production mutations."""
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent_line.adapters.sim import SimClient
from probes.sim.authoring_probe import write
from agent_line.sim_authoring import SimAuthoringClient,compile_document,decode_document,native_id,loop_policies
from agent_line.adapters.sim_wait import SimWaitClient
from agent_line.publication import PublicationVerifier

DATA = ROOT / 'var/sim-finite-loop-guard'


def main():
    DATA.mkdir(mode=0o700, exist_ok=True)
    path = DATA / ('state-revision2.json' if '--revision2' in sys.argv or (DATA/'state-revision2.json').exists() else 'state.json')
    authpath = ROOT / 'var/sim/probe-auth.json'
    if authpath.stat().st_mode & 0o077:
        raise RuntimeError('credential_permissions')
    auth = json.loads(authpath.read_text())
    sim = SimClient(auth['baseUrl'], auth['apiKey']);api=SimAuthoringClient(sim,auth['workspaceId']);verifier=PublicationVerifier(sim)
    calls = []
    sim._client.event_hooks['request'].append(lambda r: calls.append({'method': r.method, 'path': r.url.path}))
    existed = path.exists()
    state = json.loads(path.read_text()) if existed else {'steps': {}, 'runs': {}, 'prefix': 'fl_' + uuid.uuid4().hex, 'workspaceId': auth['workspaceId']}
    if not existed and '--revision2' in sys.argv:
        prior=json.loads((DATA/'state.json').read_text())
        state.update(workflowId=prior['workflowId'],steps={'create':prior['steps']['create']},purpose='explicit revision2 corrected guard result reference',previousEvidence='state.json')
    def persist():write(path, state)
    def request(method, suffix, body=None, params=None):
        r = sim._client.request(method, sim.base_url + suffix, headers={'x-api-key': sim._key}, json=body, params=params, follow_redirects=False)
        if r.status_code not in (200, 201, 202):
            raise RuntimeError('native_response_' + str(r.status_code))
        value = r.json()
        if type(value) is not dict or 'data' not in value:raise RuntimeError('native_envelope_invalid')
        return value['data']
    def once(key, payload, operation):
        old = state['steps'].get(key)
        if old:
            if old['payload'] != payload:raise RuntimeError('unknown_intent_no_resubmit')
            if old['status']=='received':return old['result']
            if old['status']!='proven_no_write':raise RuntimeError('unknown_intent_no_resubmit')
        state['steps'][key] = {'status': 'intent', 'payload': payload};persist()
        result = operation()
        state['steps'][key].update(status='received', result=result);persist()
        return result
    try:
        if existed and '--continue-known' not in sys.argv:
            observations = {}
            for name, run in state['runs'].items():
                observations[name] = {rid: sim.observe(state['workflowId'], rid) for rid in dict.fromkeys([run['runId']] + run.get('children', []))}
            write(DATA / 'observation.json', {'observations': observations, 'nativeCalls': calls, 'nativeWrites': 0, 'priorResult': state.get('result')})
            print(json.dumps({'mode': 'observation_only', 'nativeWrites': 0, 'priorResult': state.get('result')}));return 0
        persist()
        check = subprocess.run(['docker', 'ps', '--filter', 'label=com.docker.compose.project=agent-line', '--format', '{{.Names}} {{.Status}}'], capture_output=True, text=True, check=True).stdout
        if 'agent-line-simstudio-1' not in check or 'healthy' not in check:raise RuntimeError('sim_health_unconfirmed')
        state['processCheck'] = check;persist()
        payload = {'name':'agent-line-finite-loop-guard-probe','description':'Compiled bounded loop guard, no model'}
        created=once('create',payload,lambda:api.create(**payload))
        wid=created['id'];state['workflowId']=wid;base='/api/v2/workflows/'+wid
        def node(i,t,c,x):return {'id':i,'type':t,'name':{'start':'开始','repair':'修复循环','agent':'执行','end':'结果'}[i],'position':{'x':x,'y':0},'config':c}
        cfg={'roleBindingId':'probe-unbound','instruction':'本流程仅验证原生循环，不调用模型。','inputFrom':{'nodeId':'start','field':'input'},'memoryPolicyId':'none','outputSchemaId':'probe','requireMemoryCommit':False,'allowFailureBranch':True}
        doc={'schemaVersion':1,'nodes':[node('start','start',{},0),node('repair','loop',{'body':'agent','maxIterations':3},200),node('agent','agent',cfg,50),node('end','end',{'resultFrom':'agent'},750)],'edges':[{'id':'a','source':'start','target':'repair'},{'id':'b','source':'repair','target':'agent','branch':'body'},{'id':'c','source':'repair','target':'end','branch':'done'}],'variables':{}}
        state['document']=doc;graph=compile_document(doc,wid);state['graph']=graph;persist()
        once('save',graph,lambda:api.save_state(wid,graph))
        deployment=once('publish',{},lambda:api.deploy(wid))
        version=api.get_version(wid,deployment['version']);state['published']=version;persist()
        decoded=decode_document(version['state'])
        if compile_document(decoded,wid)!=graph:raise RuntimeError('published_decode_mismatch')
        bid=native_id(wid,'agent');policies=loop_policies(doc,wid)
        proof={'workflowId':wid,'revision':str(deployment['version']),'deploymentVersionId':version['id'],'humanBlockIds':[bid],'loopPolicies':policies}
        state['publicationDefinitionVerified']=verifier.check_definition(wid,proof['revision'],version['id'],[bid],require_active=True,loop_policies=policies);persist()
        cases=[('exhausted',[{'executionStatus':'succeeded','verificationStatus':'failed'}]*3,None),('early_success',[{'executionStatus':'succeeded','verificationStatus':'failed'},{'executionStatus':'succeeded','verificationStatus':'passed'}],None),('missing_execution',[{'verificationStatus':'passed'}],'loop_execution_invalid'),('missing_verification',[{'executionStatus':'succeeded'}],'loop_verification_invalid'),('wrong_verification',[{'executionStatus':'succeeded','verificationStatus':True}],'loop_verification_invalid')]
        for name, results, expected_error in cases:
            rounds=len(results)
            run = state['runs'].setdefault(name, {'runId': str(uuid.uuid4()), 'children': [], 'pauses': []});persist()
            rid = run['runId'];inputs = {'input': {}}
            once(name + '-execute', {'runId': rid, 'input': inputs}, lambda: sim.execute(wid, rid, inputs))
            for iteration in range(rounds):
                context = bid + '_loop' + str(iteration)
                key = name + '-resume-' + str(iteration)
                if key in state['steps']:
                    if state['steps'][key]['status'] != 'received':raise RuntimeError('unknown_intent_no_resubmit')
                    continue
                for _ in range(80):
                    snapshot = request('GET', base + '/runs/' + rid)
                    pause = snapshot.get('paused') or {}
                    if snapshot['status'] in ('failed', 'completed'):raise RuntimeError('premature_terminal')
                    if pause.get('contextId') == context:break
                    time.sleep(0.25)
                else:raise RuntimeError('next_pause_not_observed')
                resolved=SimWaitClient(sim).read_input(wid,rid,bid,context_id=context)
                if resolved['input']!={'input':{},'iteration':iteration}:raise RuntimeError('activation_input_mismatch')
                run_proof={**proof,'runId':rid}
                if verifier.verify_run(run_proof) is not True:raise RuntimeError('publication_run_unverified')
                run['pauses'].append({'iteration':iteration,'contextId':context,'input':resolved['input'],'snapshot':snapshot,'publicationVerified':True});persist()
                supplied={**results[iteration],'output':{'case':name,'iteration':iteration}}
                receipt = once(key, {'runId': rid, 'contextId': context, 'input': supplied}, lambda: sim.resume(wid, rid, context, supplied))
                run['children'].append(receipt['resumedRunId']);persist()
                print(json.dumps({'case': name, 'iteration': iteration, 'contextVerified': True, 'resumeAccepted': True}), flush=True)
            final_id = state['steps'][name + '-resume-' + str(rounds - 1)]['result']['resumedRunId']
            for _ in range(80):
                final = sim.observe(wid, final_id)
                if final['status'] in ('completed', 'failed', 'paused'):break
                time.sleep(0.25)
            run['final'] = final;run['parentFinal'] = sim.observe(wid, rid);persist()
            if expected_error:
                message=final['response']['data'].get('error',{}).get('message','')
                if final['status']!='failed' or expected_error not in message or final['response']['data'].get('output') is not None:raise RuntimeError('guard_did_not_fail')
                run['guardError']=message;persist()
            else:
                expected={'agentLineResult':{**results[-1],'output':{'case':name,'iteration':rounds-1}}}
                if final['status']!='completed' or final['response']['data'].get('output',{}).get('data')!=expected:raise RuntimeError('loop_terminal_mismatch')
        state['result'] = 'passed_live';persist()
        report = {'status': state['result'], 'workflowId': wid, 'nativeWritesThisInvocation': sum(x['method'] != 'GET' for x in calls), 'nativeCalls': calls, 'cumulativeAcknowledgedWrites':len(state['steps']), 'steps': {k: v['status'] for k, v in state['steps'].items()}, 'scope': 'native loop; manually supplied results; no model or product loop support claim'}
        write(DATA / 'report.json', report);print(json.dumps({'status': report['status'], 'nativeWrites': report['nativeWritesThisInvocation']}));return 0
    except Exception as exc:
        error = {'status': 'incomplete', 'code': str(exc) if isinstance(exc, RuntimeError) else 'probe_' + type(exc).__name__, 'nativeCalls': calls}
        write(DATA / ('failure-' + uuid.uuid4().hex + '.json'), error);print(json.dumps({'status': error['status'], 'code': error['code']}));return 1
    finally:sim.close()


if __name__ == '__main__':raise SystemExit(main())
