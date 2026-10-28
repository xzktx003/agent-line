#!/usr/bin/env python3
"""Real Docker test jobs; native execution metadata is explicitly synthetic."""
import argparse
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent_line.test_executor import DockerTestExecutor
from agent_line.verification_policy import validate_policy

def save(path, value):
    from agent_line.verification import _persist
    _persist(path.parent, dict(value, evidenceFile=path.name))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--attempt', type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.attempt <= 10:
        raise ValueError('Bounded explicit attempt required')
    DATA = ROOT/('var/test-executor-native-'+str(args.attempt))
    DATA.mkdir(mode=0o700, exist_ok=True)
    report_path = DATA/'report.json'
    if report_path.exists():
        report = json.loads(report_path.read_text())
        print(json.dumps({'mode':'observation_only', 'status':report['status'], 'newTestJobs':0}))
        return 0
    workspace = DATA/'workspace'; workspace.mkdir(mode=0o700, exist_ok=True)
    evidence = DATA/'evidence'; evidence.mkdir(mode=0o700, exist_ok=True)
    image = json.loads((ROOT/'var/executor/config.json').read_text())['image']
    executor = DockerTestExecutor(evidence, image)
    state_path = DATA/'state.json'
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if state['environmentId'] != executor.environment_id:
            raise RuntimeError('Original test environment changed; no new jobs')
    else:
        scripts = {
            'pass': ("import unittest\nclass T(unittest.TestCase):\n def test_add(self): self.assertEqual(2+3,5)\nunittest.main()", 5, 4096),
            'fail': ("import sys; print('intentional assertion failure'); sys.exit(7)", 5, 4096),
            'timeout_children': ("import subprocess,time; subprocess.Popen(['python3','-c','import os,time;os.setsid();time.sleep(60)']);time.sleep(60)", 1, 4096),
            'readonly': ("from pathlib import Path;Path('forbidden.txt').write_text('not permitted')", 5, 4096),
            'bounded_logs': ("print('X'*10000)", 5, 128),
        }
        cases = {}
        for name, (script, timeout, bound) in scripts.items():
            policy = validate_policy(dict(schemaVersion=2, policyId='native-'+name,
                kind='command_and_artifacts', requireTests=True, artifacts=[],
                testEnvironmentId=executor.environment_id, tests=[dict(id=name,
                argv=['python3','-c',script], timeoutSeconds=timeout, maxOutputBytes=bound)]))
            task = dict(id='test-executor-native-'+str(args.attempt)+'-'+name, executionStatus='succeeded', resourcesReleased=True,
                executionIdentity={'upstreamTaskId':'explicitly-synthetic-'+name}, payload={'workspace':str(workspace)})
            cases[name] = dict(policy=policy, task=task, status='pending')
        state = dict(environmentId=executor.environment_id, imageId=image, cases=cases,
                     nativeExecutionMetadata='explicitly synthetic; only Docker jobs are real')
        save(state_path, state)
    for name, case in state['cases'].items():
        if case['status'] == 'completed':
            continue
        deadline = time.monotonic()+30
        while time.monotonic() < deadline:
            result = executor.check(case['task'], case['policy'], case['policy']['tests'][0])
            case['lastObservation'] = result; save(state_path, state)
            if result['status'] == 'blocked':
                print(json.dumps({'case':name, **result})); return 1
            if result['status'] == 'completed':
                case.update(status='completed', check=result['check']); save(state_path, state); break
            time.sleep(.1)
        else:
            print(json.dumps({'case':name, 'status':'waiting_on_original_job'})); return 1
        check = case['check']
        assert check['status'] == ('passed' if name=='pass' else 'failed')
        assert check['resourceExitReceipt']['pid']==0 and check['resourceExitReceipt']['running'] is False
        if name=='fail': assert check['exitCode']==7
        if name=='timeout_children': assert check['exitCode']!=0
        if name=='readonly': assert not (workspace/'forbidden.txt').exists()
        if name=='bounded_logs':
            assert check['reason']=='output_limit_exceeded' and check['outputTruncated'] is True
        assert len((check['stdout']+check['stderr']).encode()) <= case['policy']['tests'][0]['maxOutputBytes']
        print(json.dumps({'case':name,'status':check['status'],'exitCode':check['exitCode']}), flush=True)
    # A new Python executor instance must reuse the stored result without Docker.
    replay = DockerTestExecutor(evidence, image)
    with patch('agent_line.test_executor._command', side_effect=AssertionError('unexpected Docker replay')):
        for case in state['cases'].values():
            result = replay.check(case['task'],case['policy'],case['policy']['tests'][0])
            assert result['status']=='completed' and result['check']==case['check']
    report = dict(status='passed_real_docker', environmentId=executor.environment_id,
        imageId=image, cases=state['cases'], replayDockerCalls=0,
        nativeExecutionMetadata='explicitly synthetic; not QwenPaw/Codex end-to-end evidence',
        configurationOrUpstreamChanges=False)
    save(report_path, report)
    print(json.dumps({'status':report['status'],'realDockerCases':len(state['cases']),'replayDockerCalls':0}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
