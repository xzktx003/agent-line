"""Validate trusted execution usage separately from model output and billing."""
import copy


TOKEN_KEYS = frozenset(('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens',
                        'reasoning_output_tokens', 'total_tokens'))


def valid_codex_usage(value, identity):
    if (not isinstance(value, dict) or set(value) != {
            'schemaVersion', 'source', 'coverage', 'codexThreadId', 'codexTurnId',
            'tokens', 'samplesObserved'}
            or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
            or value['source'] != 'codex-rollout'
            or value['coverage'] != 'reported-turn-records'
            or any(not isinstance(identity.get(key), str) or not identity[key]
                   or value[key] != identity[key] for key in ('codexThreadId', 'codexTurnId'))
            or type(value['samplesObserved']) is not int
            or not 1 <= value['samplesObserved'] <= 9007199254740991):
        return False
    tokens = value['tokens']
    return (isinstance(tokens, dict) and set(tokens) == TOKEN_KEYS
            and all(type(v) is int and 0 <= v <= 9007199254740991 for v in tokens.values())
            and tokens['cached_input_tokens'] <= tokens['input_tokens']
            and tokens['reasoning_output_tokens'] <= tokens['output_tokens']
            and tokens['total_tokens'] == tokens['input_tokens'] + tokens['output_tokens'])


def task_usage(task):
    result = {'coverage': 'partial', 'tokens': None, 'modelCalls': None,
              'cost': None, 'parentTokens': None, 'codex': None}
    records = [item for item in task['evidence']
               if isinstance(item, dict) and item.get('kind') == 'codex_usage']
    terminals = [item for item in task['evidence']
                 if isinstance(item, dict) and item.get('kind') == 'execution_terminal']
    if (task['executionStatus'] in ('succeeded', 'failed', 'canceled')
            and len(terminals) == 1 and terminals[0].get('confirmed') is True
            and terminals[0].get('executionId') == task['executionIdentity'].get('codexTurnId')
            and len(records) == 1 and set(records[0]) == {'kind', 'executionId', 'usage'}
            and records[0]['executionId'] == task['executionIdentity'].get('codexTurnId')
            and valid_codex_usage(records[0]['usage'], task['executionIdentity'])):
        result['codex'] = copy.deepcopy(records[0]['usage'])
    return result


def unknown_run_usage(scope):
    return {'schemaVersion': 1, 'scope': dict(scope), 'state': 'unknown',
            'localTasks': None, 'observedTasks': None, 'unmeasuredTasks': None,
            'tokens': None, 'parentTokens': None,
            'modelCalls': None, 'cost': None, 'codex': None}


def aggregate_task_usage(tasks, scope):
    """Only exact-run local observations; no inference about absent tasks."""
    result = unknown_run_usage(scope)
    totals = {key: 0 for key in TOKEN_KEYS}
    identities = set()
    observed = samples = 0
    for task in tasks:
        payload, identity = task['payload'], task['executionIdentity']
        if (payload['projectId'] != scope['projectId']
                or payload['workflow']['id'] != scope['workflowId']
                or payload['workflow']['runId'] != scope['runId']
                or not isinstance(identity, dict) or not isinstance(task['evidence'], list)
                or any(not isinstance(item, dict) for item in task['evidence'])
                or any(key in identity and (not isinstance(identity[key], str) or not identity[key].strip())
                       for key in ('codexThreadId', 'codexTurnId'))):
            raise ValueError('Run usage scope or task evidence is invalid')
        thread, turn = identity.get('codexThreadId'), identity.get('codexTurnId')
        if isinstance(thread, str) and thread and isinstance(turn, str) and turn:
            key = (thread, turn)
            if key in identities:
                raise ValueError('Execution identity reused across local tasks')
            identities.add(key)
        entries = [item for item in task['evidence']
                   if isinstance(item, dict) and item.get('kind') == 'codex_usage']
        usage = task_usage(task)['codex']
        if entries and (usage is None or task['executionStatus'] not in ('succeeded', 'failed', 'canceled')):
            raise ValueError('Run contains conflicting usage evidence')
        if usage is None:
            continue
        for key in TOKEN_KEYS:
            totals[key] += usage['tokens'][key]
            if totals[key] > 9007199254740991:
                raise ValueError('Run token counter exceeds safe integer range')
        samples += usage['samplesObserved']
        if samples > 9007199254740991:
            raise ValueError('Run response counter exceeds safe integer range')
        observed += 1
    result.update(state='partial', localTasks=len(tasks), observedTasks=observed,
                  unmeasuredTasks=len(tasks) - observed)
    if observed:
        result['codex'] = {'source': 'codex-rollout', 'coverage': 'reported-turn-records',
                           'tokens': totals, 'samplesObserved': samples}
    return result
