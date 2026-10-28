"""Static Human branches use native identities, never the other branch's summary."""
import copy
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import httpx

from agent_line.adapters.sim import SimClient
from agent_line.adapters.sim_wait import SimWaitClient, WaitInputUnavailable
from agent_line.completion_relay import CompletionRelay, ResumeBinding
from agent_line.pause_bindings import PauseBindings
from agent_line.sim_journal import SimJournal
from agent_line.store import Conflict


class ParallelWaitRelayTests(unittest.TestCase):
    def setUp(self):
        self.run = {'workflowId': 'workflow', 'runId': 'run', 'status': 'paused',
                    'paused': {'blockedOnBlockId': 'left', 'contextId': 'left',
                               'pauseKind': 'human', 'pausePointCount': 2, 'resumedCount': 0}}
        self.details = {'workflowId': 'workflow', 'executionId': 'run',
                        'totalPauseCount': 2, 'resumedCount': 0,
                        'pausePoints': [self.point('left'), self.point('right')]}
        self.calls = []
        self.posts = []
        self.details_status = 200
        self.uncertain = False
        transport = httpx.Client(transport=httpx.MockTransport(self.request))
        self.addCleanup(transport.close)
        self.client = SimClient('http://sim.test', 'PRIVATE_TOKEN', client=transport)
        self.wait = SimWaitClient(self.client)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'journal.sqlite'
        self.journal = SimJournal.initialize(self.path)
        self.relay = CompletionRelay(self.client, self.journal)

    @staticmethod
    def point(block):
        return {'contextId': block, 'blockId': block, 'resumeStatus': 'paused',
                'snapshotReady': True, 'pauseKind': 'human'}

    def request(self, request):
        self.calls.append(request)
        self.assertEqual(request.headers['x-api-key'], 'PRIVATE_TOKEN')
        if request.method == 'POST':
            self.assertEqual(request.url.path, '/api/v2/workflows/workflow/runs/run/resume')
            body = json.loads(request.content)
            self.posts.append(body)
            if self.uncertain:
                raise httpx.ReadTimeout('PRIVATE_TOKEN', request=request)
            return httpx.Response(200, json={'runId': 'child-' + body['contextId'],
                'workflowId': 'workflow', 'contextId': body['contextId']},
                headers={'X-Run-Id': 'child-' + body['contextId']})
        if request.url.path == '/api/resume/workflow/run':
            return httpx.Response(self.details_status, json=self.details)
        self.assertEqual(request.url.path, '/api/v2/workflows/workflow/runs/run')
        data = copy.deepcopy(self.run)
        selector = request.url.params.get('selectedOutputs')
        if selector:
            self.assertIn(selector, ('left.taskInput', 'right.taskInput'))
            data['blockOutputs'] = {selector: {'branch': selector.split('.')[0]}}
        return httpx.Response(200, json={'data': data})

    @staticmethod
    def binding(block):
        return ResumeBinding('task-' + block, 'workflow', 'run', block, block,
                             'external_task_result', allow_multiple=True)

    @staticmethod
    def task(block):
        return {'id': 'task-' + block,
                'payload': {'workflow': {'id': 'workflow', 'runId': 'run',
                                         'nodeId': block, 'nodeExecutionId': block,
                                         'attemptId': 'attempt-1'}},
                'executionStatus': 'succeeded', 'verificationStatus': 'passed',
                'resourcesReleased': True, 'memoryStatus': 'committed',
                'executionIdentity': {'codexThreadId': 'thread-' + block,
                                      'codexTurnId': 'turn-' + block},
                'output': {'branch': block}, 'evidence': []}

    def test_explicit_static_inputs_read_their_own_projection(self):
        for block in ('left', 'right'):
            result = self.wait.read_input('workflow', 'run', block, allow_multiple=True)
            self.assertEqual(result['input'], {'branch': block})
            self.assertEqual(result['run']['contextId'], block)
        self.assertEqual([r.url.params['selectedOutputs'] for r in self.calls
                          if 'selectedOutputs' in r.url.params],
                         ['left.taskInput', 'right.taskInput'])
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.posts, [])

    def test_default_reader_keeps_the_single_pause_boundary(self):
        with self.assertRaises(WaitInputUnavailable):
            self.wait.read_input('workflow', 'run', 'left')
        self.assertEqual(len(self.calls), 1)

    def test_last_static_branch_still_requires_native_ready_state(self):
        self.run['paused'].update(contextId='right', blockedOnBlockId='right', resumedCount=1)
        self.details['resumedCount'] = 1
        self.details['pausePoints'][0]['resumeStatus'] = 'resumed'
        for state in ('resuming', 'queued', 'failed'):
            self.details['pausePoints'][1]['resumeStatus'] = state
            with self.subTest(state=state):
                result = self.relay.deliver(self.task('right'), self.binding('right'))
                self.assertEqual(result['status'], 'waiting')
        self.assertEqual(self.posts, [])

    def test_multiple_input_does_not_allow_loop_or_nonboolean_flag(self):
        for kwargs in ({'allow_multiple': True, 'context_id': 'left_loop0'},
                       {'allow_multiple': 1}, {'allow_multiple': 'yes'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(WaitInputUnavailable):
                self.wait.read_input('workflow', 'run', 'left', **kwargs)
        self.assertEqual(self.calls, [])

    def invalid_details(self):
        return [lambda d: d.update(workflowId='other'),
                lambda d: d.update(executionId='other'),
                lambda d: d.update(totalPauseCount=3),
                lambda d: d.update(resumedCount=1),
                lambda d: d['pausePoints'][1].update(resumeStatus='resuming'),
                lambda d: d['pausePoints'][1].update(resumeStatus='failed'),
                lambda d: d['pausePoints'][1].update(resumeStatus='queued'),
                lambda d: d['pausePoints'][1].update(resumeStatus='resumed'),
                lambda d: d['pausePoints'][1].update(snapshotReady=False),
                lambda d: d['pausePoints'][1].update(contextId='other'),
                lambda d: d['pausePoints'][1].update(parallelScope={'id': 'scope'}),
                lambda d: d['pausePoints'][1].update(loopScope={'id': 'loop'}),
                lambda d: d['pausePoints'][1].update(pauseKind='timer'),
                lambda d: d['pausePoints'].append(self.point('right'))]

    def test_input_refuses_changed_identity_status_scope_or_counter(self):
        original = copy.deepcopy(self.details)
        for mutation in self.invalid_details():
            self.details = copy.deepcopy(original)
            mutation(self.details)
            with self.subTest(mutation=mutation), self.assertRaises(WaitInputUnavailable):
                self.wait.read_input('workflow', 'run', 'right', allow_multiple=True)
        self.assertEqual(self.posts, [])

    def test_input_requires_valid_current_run_and_bounded_counts(self):
        original = copy.deepcopy(self.run)
        mutations = [lambda r: r.update(status='completed'),
                     lambda r: r.update(workflowId='other'),
                     lambda r: r['paused'].update(pausePointCount=201),
                     lambda r: r['paused'].update(pausePointCount=True),
                     lambda r: r['paused'].update(resumedCount=-1),
                     lambda r: r['paused'].update(resumedCount=2),
                     lambda r: r['paused'].update(resumedCount=False)]
        for mutation in mutations:
            self.run = copy.deepcopy(original)
            mutation(self.run)
            with self.subTest(mutation=mutation), self.assertRaises(WaitInputUnavailable):
                self.wait.read_input('workflow', 'run', 'right', allow_multiple=True)
        self.assertEqual(self.posts, [])

    def test_native_api_errors_are_sanitized_and_never_mutate(self):
        self.details = {'error': 'PRIVATE_TOKEN'}
        for code in (302, 401, 403, 404, 500):
            self.details_status = code
            with self.assertRaises(WaitInputUnavailable) as error:
                self.wait.read_input('workflow', 'run', 'right', allow_multiple=True)
            self.assertNotIn('PRIVATE_TOKEN', str(error.exception))
            self.assertEqual(self.relay.deliver(self.task('right'), self.binding('right'))['status'],
                             'waiting')
        self.assertEqual(self.posts, [])

    def test_two_results_resume_only_their_original_context(self):
        for block in ('right', 'left'):
            result = self.relay.deliver(self.task(block), self.binding(block))
            self.assertEqual(result['status'], 'accepted')
        self.assertEqual([p['contextId'] for p in self.posts], ['right', 'left'])
        for post in self.posts:
            self.assertEqual(post['input']['taskId'], 'task-' + post['contextId'])
            self.assertEqual(post['input']['nodeExecutionId'], post['contextId'])
            self.assertEqual(post['input']['attemptId'], 'attempt-1')
            self.assertEqual(post['input']['output']['branch'], post['contextId'])

    def test_relay_waits_for_native_point_with_matching_counts(self):
        original = copy.deepcopy(self.details)
        for mutation in self.invalid_details():
            self.details = copy.deepcopy(original)
            mutation(self.details)
            with self.subTest(mutation=mutation):
                result = self.relay.deliver(self.task('right'), self.binding('right'))
                self.assertEqual(result['status'], 'waiting')
        self.assertEqual(self.posts, [])

    def test_single_remaining_point_can_differ_from_stale_summary(self):
        self.run['paused']['resumedCount'] = 1
        self.details['resumedCount'] = 1
        self.details['pausePoints'][0]['resumeStatus'] = 'resumed'
        result = self.relay.deliver(self.task('right'), self.binding('right'))
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(self.posts[0]['contextId'], 'right')

    def test_already_resumed_bound_point_is_not_replaced_by_other_live_branch(self):
        self.run['paused']['resumedCount'] = 1
        self.details['resumedCount'] = 1
        self.details['pausePoints'][1]['resumeStatus'] = 'resumed'
        with self.assertRaises(WaitInputUnavailable):
            self.wait.read_input('workflow', 'run', 'right', allow_multiple=True)
        self.assertEqual(self.relay.deliver(self.task('right'), self.binding('right'))['status'],
                         'waiting')
        self.assertEqual(self.posts, [])

    def test_unknown_resume_survives_reconstruction_without_second_send(self):
        self.uncertain = True
        task, binding = self.task('right'), self.binding('right')
        self.assertEqual(self.relay.deliver(task, binding)['status'], 'uncertain')
        self.assertEqual(len(self.posts), 1)
        before = len(self.calls)
        rebuilt = CompletionRelay(self.client, SimJournal(self.path))
        for _ in range(2):
            self.assertEqual(rebuilt.deliver(task, binding)['status'], 'uncertain')
        self.assertEqual(len(self.calls), before)
        self.assertEqual(len(self.posts), 1)

    def test_result_policy_and_missing_native_transport_never_bypass(self):
        for change in ({'verificationStatus': 'pending'}, {'resourcesReleased': False},
                       {'memoryStatus': 'pending'}, {'cancellationRequested': True}):
            result = self.relay.deliver({**self.task('right'), **change}, self.binding('right'))
            self.assertEqual(result['status'], 'waiting')
        client = Mock(spec=['observe', 'resume'])
        client.observe.return_value = {'response': {'data': self.run}}
        result = CompletionRelay(client, self.journal).deliver(self.task('right'), self.binding('right'))
        self.assertEqual(result['status'], 'waiting')
        client.resume.assert_not_called()
        self.assertEqual(self.posts, [])

    def test_native_transport_failure_and_bad_body_stay_read_only(self):
        original_request = self.request
        def timeout(request):
            if request.url.path == '/api/resume/workflow/run':
                raise httpx.ReadTimeout('PRIVATE_TOKEN', request=request)
            return original_request(request)
        transport = httpx.Client(transport=httpx.MockTransport(timeout))
        self.addCleanup(transport.close)
        client = SimClient('http://sim.test', 'PRIVATE_TOKEN', client=transport)
        with self.assertRaises(WaitInputUnavailable) as error:
            SimWaitClient(client).read_input('workflow', 'run', 'right', allow_multiple=True)
        self.assertEqual(str(error.exception), 'upstream_transport_failed')
        self.assertEqual(CompletionRelay(client, self.journal).deliver(
            self.task('right'), self.binding('right'))['status'], 'waiting')
        for body in ([], {'data': []}, {'data': {'paused': 'PRIVATE_TOKEN'}},
                     {'data': {**self.run, 'paused': 'PRIVATE_TOKEN'}}):
            fake = Mock(spec=['observe', 'resume'])
            fake.observe.return_value = {'response': body}
            with self.subTest(body=body):
                self.assertEqual(CompletionRelay(fake, self.journal).deliver(
                    self.task('right'), self.binding('right'))['status'], 'waiting')
            fake.resume.assert_not_called()
        self.assertEqual(self.posts, [])

    def test_mismatched_run_identity_counters_never_reach_resume(self):
        original = copy.deepcopy(self.run)
        for field, value in (('workflowId', 'other'), ('runId', 'other'),
                             ('status', 'completed')):
            self.run = {**copy.deepcopy(original), field: value}
            self.assertEqual(self.relay.deliver(self.task('right'), self.binding('right'))['status'],
                             'waiting')
        for total, resumed in ((201, 0), (2, 3), (True, 0), (2, True), (2, -1), (2, 2)):
            self.run = copy.deepcopy(original)
            self.run['paused'].update(pausePointCount=total, resumedCount=resumed)
            self.assertEqual(self.relay.deliver(self.task('right'), self.binding('right'))['status'],
                             'waiting')
        self.assertEqual(self.posts, [])

    def test_old_binding_cannot_gain_multiple_pause_capability(self):
        for block in ('left', 'right'):
            binding = replace(self.binding(block), allow_multiple=False)
            self.assertEqual(self.relay.deliver(self.task(block), binding)['status'], 'waiting')
        self.assertFalse(any(r.url.path.startswith('/api/resume/') for r in self.calls))
        self.assertEqual(self.posts, [])

    def test_old_json_defaults_false_and_cannot_change_frozen_policy(self):
        bindings = PauseBindings.initialize(self.path.with_name('bindings.sqlite'))
        old = replace(self.binding('right'), allow_multiple=False)
        bindings.bind(old)
        with bindings.connect() as db:
            raw = asdict(old)
            raw.pop('allow_multiple')
            db.execute('UPDATE bindings SET payload=?', (json.dumps(raw),))
        reopened = PauseBindings(bindings.path)
        self.assertIs(reopened.get(old.task_id).allow_multiple, False)
        self.assertFalse(reopened.bind(old))
        with self.assertRaises(Conflict):
            reopened.bind(self.binding('right'))
        self.assertIs(reopened.for_context('workflow', 'run', 'right').allow_multiple, False)

    def test_multiple_binding_flag_requires_boolean_before_delivery(self):
        for value in (1, 0, None, 'true', [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.relay.deliver(self.task('right'),
                                   replace(self.binding('right'), allow_multiple=value))
        self.assertEqual(self.calls, [])
