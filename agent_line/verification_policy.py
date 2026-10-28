"""Versioned deployment policy; never inferred from model output or memory."""
import hashlib
import json
import re

from .adapters.sim import segment
from .verification import ArtifactPolicy


def validate_policy(value):
    base = {'schemaVersion', 'policyId', 'kind', 'requireTests', 'artifacts'}
    if not isinstance(value, dict) or type(value.get('schemaVersion')) is not int:
        raise ValueError('Invalid versioned verification policy')
    version = value['schemaVersion']
    if version == 1:
        if set(value) != base or value['kind'] != 'artifact_sha256' or value['requireTests'] is not False:
            raise ValueError('Schema one supports artifact verification only')
    elif version == 2:
        if (set(value) != base | {'tests', 'testEnvironmentId'} or value['kind'] != 'command_and_artifacts'
                or value['requireTests'] is not True or not isinstance(value['testEnvironmentId'], str)
                or re.fullmatch(r'docker-test-v1:[0-9a-f]{64}', value['testEnvironmentId']) is None):
            raise ValueError('Invalid isolated test verification policy')
    else:
        raise ValueError('Unsupported verification policy version')
    if not isinstance(value['artifacts'], list) or not (1 if version == 1 else 0) <= len(value['artifacts']) <= 32:
        raise ValueError('Invalid declared artifacts')
    segment(value['policyId'])
    artifacts = []
    paths = set()
    total = 0
    for item in value['artifacts']:
        if (not isinstance(item, dict) or not {'path', 'sha256'} <= set(item)
                <= {'path', 'sha256', 'exactSize', 'maxBytes'}):
            raise ValueError('Invalid declared artifact policy')
        policy = ArtifactPolicy(item['path'], item['sha256'], item.get('exactSize'),
                                item.get('maxBytes', 10_000_000))
        if policy.relative_path in paths:
            raise ValueError('Duplicate declared artifact path')
        paths.add(policy.relative_path)
        total += policy.max_bytes
        if total > 64_000_000:
            raise ValueError('Total declared verification read bound exceeded')
        artifacts.append({'path': policy.relative_path, 'sha256': policy.expected_sha256,
                          'exactSize': policy.exact_size, 'maxBytes': policy.max_bytes})
    result = {'schemaVersion': version, 'policyId': value['policyId'], 'kind': value['kind'],
              'requireTests': value['requireTests'], 'artifacts': artifacts}
    if version == 2:
        tests = value['tests']
        if not isinstance(tests, list) or not 1 <= len(tests) <= 8:
            raise ValueError('One to eight tests required')
        normalized = []; ids = set(); duration = 0; output = 0
        for test in tests:
            if (not isinstance(test, dict) or not {'id', 'argv'} <= set(test)
                    <= {'id', 'argv', 'timeoutSeconds', 'maxOutputBytes'}):
                raise ValueError('Invalid declared test')
            identifier = segment(test['id'])
            argv = test['argv']
            if (identifier in ids or not isinstance(argv, list) or not 1 <= len(argv) <= 64
                    or any(not isinstance(arg, str) or not arg or '\0' in arg for arg in argv)
                    or sum(len(arg) for arg in argv) > 16384):
                raise ValueError('Invalid test identity or argv')
            try:
                encoded_length = sum(len(arg.encode('utf-8')) for arg in argv)
            except UnicodeError:
                raise ValueError('Test argv must be valid UTF-8') from None
            if encoded_length > 16384:
                raise ValueError('Test argv byte budget exceeded')
            timeout = test.get('timeoutSeconds', 60)
            bound = test.get('maxOutputBytes', 65536)
            if type(timeout) is not int or not 1 <= timeout <= 300 or type(bound) is not int or not 1 <= bound <= 1048576:
                raise ValueError('Invalid test resource bound')
            ids.add(identifier); duration += timeout; output += bound
            if duration > 900 or output > 4194304:
                raise ValueError('Total test resource bound exceeded')
            normalized.append({'id': identifier, 'argv': list(argv), 'timeoutSeconds': timeout, 'maxOutputBytes': bound})
        result.update(tests=normalized, testEnvironmentId=value['testEnvironmentId'])
    return result


def policy_hash(value):
    policy = validate_policy(value)
    encoded = json.dumps(policy, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def validate_test_check(check, test, policy, task_id, execution_identity):
    """Validate a trusted executor receipt against the frozen command and identity.

    This is a binding/exit validator, not authentication of arbitrary JSON. Only
    the trusted checker calls Store.record_verification; no model/HTTP route does.
    """
    from datetime import datetime
    from pathlib import PurePosixPath
    check_keys = {'kind', 'testId', 'argv', 'timeoutSeconds', 'maxOutputBytes', 'environmentId',
                  'exitCode', 'status', 'reason', 'containerId', 'resourceExitReceipt',
                  'stdout', 'stderr', 'outputTruncated'}
    if not isinstance(check, dict) or set(check) != check_keys:
        raise ValueError('Exact test check fields required')
    expected = {'kind': 'test_command', 'testId': test['id'], 'argv': test['argv'],
                'timeoutSeconds': test['timeoutSeconds'], 'maxOutputBytes': test['maxOutputBytes'],
                'environmentId': policy['testEnvironmentId']}
    if any(json.dumps(check.get(key), sort_keys=True) != json.dumps(value, sort_keys=True)
           for key, value in expected.items()):
        raise ValueError('Test check differs from frozen command')
    if (type(check.get('exitCode')) is not int or not 0 <= check['exitCode'] <= 255
            or type(check.get('outputTruncated')) is not bool
            or not isinstance(check.get('stdout'), str) or not isinstance(check.get('stderr'), str)
            or len((check['stdout'] + check['stderr']).encode('utf-8')) > test['maxOutputBytes']):
        raise ValueError('Invalid bounded test output')
    container = check.get('containerId')
    if not isinstance(container, str) or re.fullmatch('[0-9a-f]{64}', container) is None:
        raise ValueError('Full test container identity required')
    proof = check.get('resourceExitReceipt')
    proof_keys = {'source', 'containerId', 'taskId', 'executionIdentity', 'policySha256', 'testId',
                  'environmentId', 'exitCode', 'running', 'pid', 'status', 'oomKilled', 'owner',
                  'workspace', 'startedAt', 'finishedAt'}
    if not isinstance(proof, dict) or set(proof) != proof_keys:
        raise ValueError('Exact independent test executor exit fields required')
    bindings = {'source': 'docker-inspect-before-remove', 'containerId': container,
                'taskId': task_id, 'executionIdentity': execution_identity,
                'policySha256': policy_hash(policy), 'testId': test['id'],
                'environmentId': policy['testEnvironmentId'], 'exitCode': check['exitCode']}
    if any(json.dumps(proof.get(key), sort_keys=True) != json.dumps(value, sort_keys=True)
           for key, value in bindings.items()):
        raise ValueError('Test exit identity mismatch')
    if (proof.get('running') is not False or type(proof.get('pid')) is not int or proof['pid'] != 0
            or proof.get('status') != 'exited' or type(proof.get('oomKilled')) is not bool
            or not isinstance(proof.get('owner'), str) or re.fullmatch('[0-9a-f]{32}', proof['owner']) is None):
        raise ValueError('Test executor exit not positively confirmed')
    workspace = proof.get('workspace')
    if (not isinstance(workspace, str) or not workspace or '\0' in workspace
            or len(workspace) > 4096 or workspace.startswith('//')
            or not PurePosixPath(workspace).is_absolute() or '..' in PurePosixPath(workspace).parts
            or str(PurePosixPath(workspace)) != workspace):
        raise ValueError('Test workspace identity absent')
    if any(not isinstance(proof.get(key), str) or len(proof[key]) > 64 for key in ('startedAt', 'finishedAt')):
        raise ValueError('Bounded test timestamps required')
    try:
        started = datetime.fromisoformat(proof['startedAt'].replace('Z', '+00:00'))
        finished = datetime.fromisoformat(proof['finishedAt'].replace('Z', '+00:00'))
        if started.tzinfo is None or finished.tzinfo is None or finished < started:
            raise ValueError('Invalid test timestamps')
    except (KeyError, AttributeError, TypeError, ValueError):
        raise ValueError('Test start/exit timestamps required') from None
    reason = ('output_limit_exceeded' if check['outputTruncated'] else 'oom_killed' if proof['oomKilled']
              else 'command_exit_zero' if check['exitCode'] == 0 else 'command_exit_nonzero')
    status = 'passed' if reason == 'command_exit_zero' else 'failed'
    if check.get('reason') != reason or check.get('status') != status:
        raise ValueError('Test summary disagrees with actual exit/output')
    return check


def validate_cancellation_receipt(receipt, policy, task_id, identity, workspace):
    """Trusted runner's per-test cancellation facts; never invented native exits."""
    import math
    from datetime import datetime
    policy = validate_policy(policy)
    keys = {'kind', 'schemaVersion', 'taskId', 'executionIdentity', 'policySha256',
            'canceledAt', 'tests', 'evidenceFile'}
    if (policy['schemaVersion'] != 2 or not isinstance(receipt, dict) or set(receipt) != keys
            or receipt['kind'] != 'verification_cancellation' or type(receipt['schemaVersion']) is not int
            or receipt['schemaVersion'] != 1 or receipt['taskId'] != task_id
            or receipt['executionIdentity'] != identity or receipt['policySha256'] != policy_hash(policy)
            or type(receipt['canceledAt']) not in (int, float) or not math.isfinite(receipt['canceledAt'])
            or receipt['canceledAt'] <= 0 or not isinstance(receipt['evidenceFile'], str)
            or re.fullmatch(r'cancellation-[0-9a-f]{64}\.json', receipt['evidenceFile']) is None
            or not isinstance(receipt['tests'], list) or len(receipt['tests']) != len(policy['tests'])):
        raise ValueError('Invalid test cancellation receipt')
    common = {'testId', 'disposition', 'taskId', 'executionIdentity', 'policySha256', 'environmentId'}
    for proof, test in zip(receipt['tests'], policy['tests']):
        if not isinstance(proof, dict):
            raise ValueError('Test cancellation proof required')
        disposition = proof.get('disposition')
        expected_keys = common if disposition == 'not_started' else common | {'containerId', 'resourceExitReceipt', 'check'}
        if (disposition not in ('not_started', 'exited') or set(proof) != expected_keys
                or proof['testId'] != test['id'] or proof['taskId'] != task_id or proof['executionIdentity'] != identity
                or proof['policySha256'] != receipt['policySha256'] or proof['environmentId'] != policy['testEnvironmentId']):
            raise ValueError('Test cancellation differs from frozen identity/order')
        if disposition == 'exited':
            check = validate_test_check(proof['check'], test, policy, task_id, identity)
            if (proof['containerId'] != check['containerId'] or proof['resourceExitReceipt'] != check['resourceExitReceipt']
                    or proof['resourceExitReceipt']['workspace'] != workspace
                    or datetime.fromisoformat(proof['resourceExitReceipt']['finishedAt'].replace('Z', '+00:00')).timestamp() > receipt['canceledAt']):
                raise ValueError('Test cancellation exit mismatch')
    return receipt
