"""Read persisted Human wait inputs and bounded native pause identities."""
import json
import math
import re

import httpx
from .sim import segment


class WaitInputUnavailable(ValueError):
    def __init__(self,code):
        self.code=code
        super().__init__(code)


def _require(value,code):
    if not value:raise WaitInputUnavailable(code)


def _plain(value,depth=0):
    _require(depth<=16,'input_too_deep')
    if isinstance(value,str):
        # The pinned secret projector uses {{NAME}} and explicit bracket markers.
        # HTML/comparison syntax in actual task data is not such a marker.
        _require(not re.search(r'\{\{|\[(?:REDACTED|TRUNCATED|UNAVAILABLE|HIDDEN|RUNTIME_BINDING)[^\]]*\]',value,re.I),'input_projection_unavailable')
    elif type(value) in (int,float):
        _require(abs(value)<=9_007_199_254_740_991 and (type(value) is int or math.isfinite(value)),'input_invalid_number')
    elif value is None or type(value) is bool:pass
    elif type(value) is list:
        for item in value:_plain(item,depth+1)
    elif type(value) is dict:
        for key,item in value.items():
            _require(isinstance(key,str) and key not in ('__simLargeValueRef','__largeValueRef'),'input_projection_unavailable')
            _plain(key,depth+1);_plain(item,depth+1)
    else:raise WaitInputUnavailable('input_invalid_type')


class SimWaitClient:
    def __init__(self,sim_client):self.sim=sim_client

    def read_pauses(self, workflow_id, run_id):
        """Read identities of distinct top-level waits, without snapshot data.

        This does not authorize dispatch or resume. Callers must independently
        prove the published graph and each activation before taking any action.
        Replicated/container activations retain their existing unsupported fence.
        """
        try:
            workflow_id = segment(workflow_id)
            run_id = segment(run_id)
        except ValueError:
            raise WaitInputUnavailable('identity_invalid') from None
        try:
            response = self.sim._client.get(
                self.sim.base_url + f'/api/resume/{workflow_id}/{run_id}',
                headers={'x-api-key': self.sim._key}, follow_redirects=False)
            _require(response.status_code == 200, 'upstream_read_failed')
            _require(len(response.content) <= 1048576, 'pause_details_too_large')
            data = response.json()
            _require(type(data) is dict, 'response_invalid')
            _require(data.get('workflowId') == workflow_id
                     and data.get('executionId') == run_id, 'run_identity_mismatch')
            points = data.get('pausePoints')
            _require(type(points) is list and 1 <= len(points) <= 200, 'pause_unsupported')
            total, resumed = data.get('totalPauseCount'), data.get('resumedCount')
            _require(type(total) is int and total == len(points)
                     and type(resumed) is int and 0 <= resumed <= total, 'pause_unsupported')
            result, contexts, blocks = [], set(), set()
            statuses = {'paused', 'resumed', 'failed', 'queued', 'resuming'}
            for point in points:
                _require(type(point) is dict, 'pause_unsupported')
                context, block = segment(point.get('contextId')), segment(point.get('blockId'))
                _require(context == block and context not in contexts and block not in blocks,
                         'activation_unsupported')
                _require(point.get('parallelScope') is None and point.get('loopScope') is None,
                         'activation_unsupported')
                _require(point.get('pauseKind') == 'human'
                         and type(point.get('snapshotReady')) is bool
                         and isinstance(point.get('resumeStatus'), str)
                         and point['resumeStatus'] in statuses, 'pause_unsupported')
                contexts.add(context); blocks.add(block)
                result.append({key: point[key] for key in (
                    'contextId', 'blockId', 'resumeStatus', 'snapshotReady', 'pauseKind')})
            _require(resumed == sum(p['resumeStatus'] == 'resumed' for p in result),
                     'pause_unsupported')
            return {'workflowId': workflow_id, 'runId': run_id, 'totalPauseCount': total,
                    'resumedCount': resumed, 'pausePoints': result}
        except WaitInputUnavailable:
            raise
        except httpx.HTTPError:
            raise WaitInputUnavailable('upstream_transport_failed') from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise WaitInputUnavailable('response_invalid') from None

    def read_input(self,workflow_id,run_id,block_id,*,context_id=None,allow_multiple=False):
        # An explicit activation only binds the input read, never authorizes a
        # loop or task. The caller must separately prove the published topology.
        try:
            workflow_id=segment(workflow_id);run_id=segment(run_id);block_id=segment(block_id)
            context_id=block_id if context_id is None else segment(context_id)
        except ValueError:raise WaitInputUnavailable('identity_invalid') from None
        activation=context_id!=block_id
        _require(type(allow_multiple) is bool, 'pause_unsupported')
        _require(not (allow_multiple and activation), 'activation_unsupported')
        selector=block_id if activation else block_id+'.taskInput'
        try:
            response=self.sim._client.get(self.sim.base_url+f'/api/v2/workflows/{workflow_id}/runs/{run_id}',params={'selectedOutputs':selector},headers={'x-api-key':self.sim._key},follow_redirects=False)
            _require(response.status_code==200,'upstream_read_failed')
            envelope=response.json()
            _require(type(envelope) is dict and set(envelope)=={'data'},'response_invalid')
            data=envelope['data'];_require(type(data) is dict,'response_invalid')
            _require(data.get('workflowId')==workflow_id and data.get('runId')==run_id,'run_identity_mismatch')
            _require(data.get('status')=='paused','run_not_paused')
            pause=data.get('paused');_require(type(pause) is dict,'pause_missing')
            if not allow_multiple:
                _require(pause.get('blockedOnBlockId')==block_id and pause.get('contextId')==context_id,'pause_identity_mismatch')
            _require(pause.get('pauseKind')=='human' and type(pause.get('pausePointCount')) is int and pause['pausePointCount']>=1,'pause_unsupported')
            # Nonparallel contexts only; publication proof remains the caller's responsibility.
            # resumedCount is global to the paused execution, not this block activation.
            _require(type(pause.get('resumedCount')) is int and pause['resumedCount']>=0,'activation_unsupported')
            if allow_multiple:
                _require(pause['pausePointCount'] <= 200
                         and pause['resumedCount'] < pause['pausePointCount'], 'pause_unsupported')
                details = self.read_pauses(workflow_id, run_id)
                _require(details['totalPauseCount'] == pause['pausePointCount']
                         and details['resumedCount'] == pause['resumedCount'], 'pause_changed')
                matching = [point for point in details['pausePoints']
                            if point['contextId'] == context_id and point['blockId'] == block_id]
                _require(len(matching) == 1 and matching[0]['resumeStatus'] == 'paused'
                         and matching[0]['snapshotReady'] is True, 'bound_pause_not_ready')
            else:
                _require(pause['pausePointCount']-pause['resumedCount']==1,'pause_unsupported')
            _require(not any(data.get(k) for k in ('executionDataTruncated','truncated','redacted','hidden')),'input_projection_unavailable')
            outputs=data.get('blockOutputs');_require(type(outputs) is dict and set(outputs)=={selector},'input_missing')
            value=outputs[selector]
            if activation:
                _require(type(value) is dict,'activation_output_missing')
                response_value=value.get('response')
                _require(type(response_value) is dict,'activation_output_missing')
                resume=response_value.get('resume')
                _require(type(resume) is dict and resume.get('contextId')==context_id,'activation_context_mismatch')
                _require('taskInput' in value,'input_missing')
                value=value['taskInput']
            encoding='object'
            if isinstance(value,str):
                _require(len(value.encode())<=65536,'input_too_large')
                def pairs(items):
                    result={}
                    for key,item in items:
                        _require(key not in result,'input_duplicate_key');result[key]=item
                    return result
                def invalid_constant(value):raise WaitInputUnavailable('input_invalid_number')
                try:value=json.loads(value,object_pairs_hook=pairs,parse_constant=invalid_constant)
                except ValueError as exc:
                    if isinstance(exc,WaitInputUnavailable):raise
                    raise WaitInputUnavailable('input_not_object') from None
                encoding='json-object-string'
            _require(type(value) is dict,'input_not_object')
            _require(len(json.dumps(value,allow_nan=False).encode())<=65536,'input_too_large')
            _plain(value)
            return {'input':value,'encoding':encoding,'run':{'workflowId':workflow_id,'runId':run_id,'status':'paused','blockId':block_id,'contextId':context_id,'pauseKind':'human','pausePointCount':pause['pausePointCount'],'resumedCount':pause['resumedCount']}}
        except WaitInputUnavailable:raise
        except httpx.HTTPError:raise WaitInputUnavailable('upstream_transport_failed') from None
        except (ValueError,TypeError,KeyError,RecursionError):raise WaitInputUnavailable('response_invalid') from None
