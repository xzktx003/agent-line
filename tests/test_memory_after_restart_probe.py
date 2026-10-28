import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from probes.qwenpaw_memory import after_restart as probe
from probes.qwenpaw_memory.native_roundtrip import submit_once
from agent_line.store import canonical


class SourceMemoryProofTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.marker = 'ONLY_RECALLED_MARKER'; self.content = 'reviewed experience ' + self.marker
        self.sha = hashlib.sha256(self.content.encode()).hexdigest()
        self.patch = patch.object(probe, 'SOURCE_SHA', self.sha); self.patch.start(); self.addCleanup(self.patch.stop)
        identity = {'upstreamTaskId': 'native-A', 'codexThreadId': 'thread-A', 'codexTurnId': 'turn-A'}
        artifact = 'original-A.txt'; expected = (self.marker + '\n').encode(); digest = hashlib.sha256(expected).hexdigest()
        verification = {'kind': 'verification', 'status': 'passed', 'taskId': probe.SOURCE_TASK,
            'executionIdentity': identity, 'evidenceFile': 'original.json', 'checks': [{
                'kind': 'artifact_sha256', 'status': 'passed', 'path': artifact,
                'expectedSha256': digest, 'actualSha256': digest,
                'expectedSize': len(expected), 'actualSize': len(expected)}]}
        evidence = [{'kind': 'resource_exit', 'executionIdentity': identity, 'receipt': {
            'running': False, 'pid': 0, 'workspace': '/workspace', 'source': 'docker-inspect-before-remove',
            'identities': [{**identity, 'nativeTerminal': True}]}}, verification]
        payload = {'projectId': probe.PROJECT, 'roleAgentId': probe.ROLE,
                   'roleBindingId': probe.BINDING, 'workflow': {'id': 'original'}, 'workspace': '/workspace'}
        self.state = {'verified': True, 'marker': self.marker, 'query': 'source query',
                      'roleWorkspace': '/original-native-role', 'A': {'taskId': probe.SOURCE_TASK, 'artifact': artifact}}
        self.write('state-attempt-4.json', self.state)
        (self.directory / 'artifacts').mkdir(); self.write('artifacts/original.json', verification)
        self.parent = {'status': 'finished', 'result': {'status': 'completed',
            'session_id': 'agent-line-' + probe.SOURCE_TASK, 'completed_at': '1970-01-01T00:05:00+00:00',
            'output': [{'type': 'plugin_call', 'content': [{'data': {'name': 'agent_line_delegate', 'call_id': 'd1'}}]},
                       {'type': 'plugin_call_output', 'content': [{'data': {'name': 'agent_line_delegate',
                        'call_id': 'd1', 'state': 'success', 'output': {'taskId': probe.SOURCE_TASK,
                        'identity': {'upstreamTaskId': identity['upstreamTaskId']}}}}]}]}}
        self.write('attempt4-A-parent-final.json', self.parent)
        self.write('attempt4-report.json', {'tasks': {'A': {'id': probe.SOURCE_TASK, 'parentTaskId': 'parent-A', 'executionIdentity': identity}}})
        source = {'workflow': payload['workflow'], 'executionStatus': 'succeeded', 'verificationStatus': 'passed',
                  'evidenceSha256': hashlib.sha256(canonical(evidence).encode()).hexdigest()}
        with sqlite3.connect(self.directory / 'tasks.sqlite') as db:
            db.execute('CREATE TABLE tasks(id,execution_status,verification_status,resources_released,payload,evidence,execution_identity)')
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)', (probe.SOURCE_TASK, 'succeeded', 'passed', 1,
                       json.dumps(payload), json.dumps(evidence), json.dumps(identity)))
        with sqlite3.connect(self.directory / 'commits.sqlite') as db:
            db.execute('CREATE TABLE memory_commits(role_id,task_id,status,content,content_hash,decision_id)')
            db.execute('CREATE TABLE memory_events(role_id,task_id,kind,created_at)')
            db.execute('INSERT INTO memory_commits VALUES(?,?,?,?,?,?)',
                       (probe.ROLE, probe.SOURCE_TASK, 'committed', self.content, self.sha, 'review-1'))
            db.execute('INSERT INTO memory_events VALUES(?,?,?,?)', (probe.ROLE, probe.SOURCE_TASK, 'committed', 500))
        with sqlite3.connect(self.directory / 'reviews.sqlite') as db:
            db.execute('CREATE TABLE memory_reviews(role_id,task_id,project_id,content_hash,decision_id,actor,source,created_at)')
            db.execute('INSERT INTO memory_reviews VALUES(?,?,?,?,?,?,?,?)',
                       (probe.ROLE, probe.SOURCE_TASK, probe.PROJECT, self.sha, 'review-1', 'reviewer', json.dumps(source), 400))
        with sqlite3.connect(self.directory / 'roles.sqlite') as db:
            db.execute('CREATE TABLE deliveries(task_id,payload,parent_id)')
            db.execute('INSERT INTO deliveries VALUES(?,?,?)', (probe.SOURCE_TASK, json.dumps({'roleAgentId': probe.ROLE, 'roleSessionId': 'agent-line-' + probe.SOURCE_TASK}), 'parent-A'))

    def write(self, name, value):
        (self.directory / name).write_text(json.dumps(value))

    def prove(self, birth=1000, content=None):
        return probe.source_memory_proof(self.directory, birth, self.content if content is None else content)

    def sql(self, name, sql):
        with sqlite3.connect(self.directory / name) as db: db.execute(sql)

    def test_real_rows_and_committed_event_prove_restart_and_no_files_change(self):
        before = {p.name: p.read_bytes() for p in self.directory.glob('*.sqlite')}
        result = self.prove()
        self.assertEqual(result['committedAt'], 500); self.assertEqual(result['hostBirth'], 1000)
        self.assertEqual(result['originalParentTaskId'], 'parent-A')
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.directory.glob('*.sqlite')})

    def test_old_json_and_file_mtime_cannot_replace_actual_commit(self):
        self.sql('commits.sqlite', 'DELETE FROM memory_events')
        with self.assertRaisesRegex(RuntimeError, 'real_restart_timeline'): self.prove()

    def test_commit_must_precede_current_host_and_follow_review(self):
        for birth in (500, 499):
            with self.assertRaises(RuntimeError): self.prove(birth=birth)
        self.sql('reviews.sqlite', 'UPDATE memory_reviews SET created_at=501')
        with self.assertRaises(RuntimeError): self.prove()

    def test_readback_hash_status_and_review_binding_cannot_be_forged_in_state(self):
        changes = [('commits.sqlite', "UPDATE memory_commits SET status='intent'"),
                   ('commits.sqlite', "UPDATE memory_commits SET content='changed'"),
                   ('reviews.sqlite', "UPDATE memory_reviews SET decision_id='other'"),
                   ('reviews.sqlite', "UPDATE memory_reviews SET project_id='other'"),
                   ('reviews.sqlite', "UPDATE memory_reviews SET source='{}'")]
        originals = {name: (self.directory / name).read_bytes() for name, _ in changes}
        for name, sql in changes:
            with self.subTest(sql=sql):
                self.sql(name, sql)
                with self.assertRaises(RuntimeError): self.prove()
                (self.directory / name).write_bytes(originals[name])
        with self.assertRaises(RuntimeError): self.prove(content='other official content')

    def test_source_failure_missing_exit_and_tampered_independent_artifact_rejected(self):
        self.sql('tasks.sqlite', "UPDATE tasks SET execution_status='failed'")
        with self.assertRaises(RuntimeError): self.prove()
        self.sql('tasks.sqlite', "UPDATE tasks SET execution_status='succeeded',resources_released=0")
        with self.assertRaises(RuntimeError): self.prove()
        self.sql('tasks.sqlite', 'UPDATE tasks SET resources_released=1')
        self.write('artifacts/original.json', {'status': 'passed'})
        with self.assertRaisesRegex(RuntimeError, 'independent_artifact'): self.prove()

    def test_finished_parent_must_have_original_session_before_commit(self):
        for change in ({'status': 'running'}, {'result': {'status': 'completed', 'session_id': 'other'}}):
            self.write('attempt4-A-parent-final.json', {**self.parent, **change})
            with self.assertRaises(RuntimeError): self.prove()
        parent = copy.deepcopy(self.parent); parent['result']['completed_at'] = '1970-01-01T00:20:00+00:00'
        self.write('attempt4-A-parent-final.json', parent)
        with self.assertRaises(RuntimeError): self.prove()

    def test_parent_may_finish_after_memory_commit_but_before_host_restart(self):
        parent = copy.deepcopy(self.parent); parent['result']['completed_at'] = '1970-01-01T00:10:00+00:00'
        self.write('attempt4-A-parent-final.json', parent)
        self.assertTrue(self.prove()['originalParentFinished'])

    def test_parent_narrative_without_linked_real_delegation_rejected(self):
        parent = copy.deepcopy(self.parent); parent['result']['output'] = []
        self.write('attempt4-A-parent-final.json', parent)
        with self.assertRaisesRegex(RuntimeError, 'actual_delegation'): self.prove()

    def test_missing_database_never_initializes_it(self):
        (self.directory / 'commits.sqlite').unlink()
        with self.assertRaises(FileNotFoundError): self.prove()
        self.assertFalse((self.directory / 'commits.sqlite').exists())


class RestartProbeSafetyTests(unittest.TestCase):
    def test_default_is_zero_io_without_login_or_directory_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'absent'
            with patch.object(probe, 'main_snapshot', side_effect=AssertionError), patch.object(probe.httpx, 'Client', side_effect=AssertionError):
                result = probe.run_probe(directory=target)
            self.assertEqual(result['newSubmissions'], 0); self.assertFalse(target.exists())

    def test_existing_state_even_execute_only_observes_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp); (target / 'state.json').write_text('{"phase":"prepared","task":{}}')
            with patch.object(probe, 'observe_existing', return_value={'mode': 'observation_only'}) as observe, patch.object(probe, 'quiet_host', side_effect=AssertionError):
                self.assertEqual(probe.run_probe(execute=True, directory=target)['mode'], 'observation_only')
                observe.assert_called_once()

    def test_partial_state_with_database_and_no_task_identity_stays_readonly(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp); state = {'task': {}, 'phase': 'prepared'}
            (target / 'state.json').write_text(json.dumps(state)); (target / 'tasks.sqlite').write_bytes(b'incomplete')
            before = {p.name: p.read_bytes() for p in target.iterdir()}
            with patch.object(probe.httpx, 'Client', side_effect=AssertionError):
                result = probe.run_probe(execute=True, directory=target)
            self.assertEqual(result['observation'], 'missing_task_identity')
            self.assertEqual(before, {p.name: p.read_bytes() for p in target.iterdir()})

    def test_existing_observation_uses_get_and_readonly_databases_without_reconcile(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp); state = {'phase': 'stopped', 'task': {'taskId': 'original'}, 'baseURL': 'http://127.0.0.1:1'}
            (target / 'state.json').write_text(json.dumps(state))
            (target / 'host-auth.json').write_text(json.dumps({'Authorization': 'Bearer private'}))
            with sqlite3.connect(target / 'tasks.sqlite') as db:
                db.execute('CREATE TABLE tasks(id)'); db.execute("INSERT INTO tasks VALUES('original')")
                db.execute('CREATE TABLE workspace_leases(task_id)')
            with sqlite3.connect(target / 'roles.sqlite') as db:
                db.execute('CREATE TABLE deliveries(task_id,parent_id)')
                db.execute("INSERT INTO deliveries VALUES('original','original-parent')")
            before = {p.name: p.read_bytes() for p in target.iterdir()}
            client = Mock(); client.get.return_value.status_code = 200
            client.get.return_value.json.return_value = {'status': 'finished'}
            context = MagicMock(); context.__enter__.return_value = client
            with patch.object(probe.httpx, 'Client', return_value=context), patch.object(probe, 'Dispatcher', side_effect=AssertionError), patch.object(probe, 'TaskStore', side_effect=AssertionError):
                result = probe.run_probe(execute=True, directory=target)
            self.assertEqual(result['parent']['status'], 'finished')
            client.get.assert_called_once(); client.post.assert_not_called()
            self.assertTrue(client.get.call_args.args[0].endswith('/original-parent'))
            self.assertEqual(before, {p.name: p.read_bytes() for p in target.iterdir()})

    def test_preexisting_directory_without_state_cannot_adopt_or_submit(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(probe, 'quiet_host', side_effect=AssertionError):
            with self.assertRaisesRegex(RuntimeError, 'preexisting_unowned'): probe.run_probe(execute=True, directory=Path(tmp))

    def test_input_marker_must_only_arrive_via_recalled_memory(self):
        proof = {'query': 'historical lookup', 'marker': 'SECRET'}
        self.assertNotIn('SECRET', probe.instruction_for(proof, 'fresh.txt'))
        with self.assertRaises(RuntimeError): probe.instruction_for(proof, 'SECRET.txt')
        with self.assertRaises(RuntimeError): probe.instruction_for({'query': 'SECRET lookup', 'marker': 'SECRET'}, 'fresh.txt')

    def test_unknown_submit_intent_never_retries(self):
        dispatcher = Mock(); dispatcher.submit.side_effect = TimeoutError
        state = {'task': {'taskId': 'original'}}; persist = Mock()
        with self.assertRaises(TimeoutError): submit_once(dispatcher, state, 'task', persist)
        submit_once(dispatcher, state, 'task', persist)
        dispatcher.submit.assert_called_once_with('original')

    def test_timeout_keeps_original_cancel_and_never_submits_replacement(self):
        dispatcher = Mock(); store = Mock(); ledger = Mock()
        dispatcher.reconcile.return_value = {'resourcesReleased': True}
        with self.assertRaisesRegex(RuntimeError, 'original_cancel_intent'):
            probe.wait_task(dispatcher, store, ledger, 'original', 900, Mock(), clock=Mock(side_effect=[0, 1201]), sleep=Mock())
        store.request_cancel.assert_called_once_with('original'); dispatcher.submit.assert_not_called()
        dispatcher.reconcile.assert_called_once_with('original')

    def test_native_rollout_requires_real_user_input_not_assistant_narrative(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp); (home / 'sessions').mkdir(); rollout = home / 'sessions/abc-thread.jsonl'
            task = {'executionIdentity': {'codexThreadId': 'thread', 'codexTurnId': 'turn'}}
            event = {'type': 'response_item', 'timestamp': 'then', 'payload': {
                'type': 'message', 'role': 'assistant', 'text': 'MARK fresh.txt',
                'internal_chat_message_metadata_passthrough': {'turn_id': 'turn'}}}
            events = self.rollout_events(event)
            rollout.write_text('\n'.join(json.dumps(e) for e in events))
            with self.assertRaises(RuntimeError): probe.native_input_proof(home, task, 'MARK', 'fresh.txt')
            event['payload']['role'] = 'user'
            rollout.write_text('\n'.join(json.dumps(e) for e in events))
            proof = probe.native_input_proof(home, task, 'MARK', 'fresh.txt')
            self.assertEqual(proof['nativeInputTimestamps'], ['then'])
            self.assertEqual(proof['codexTurnId'], 'turn')
            self.assertTrue(proof['matchingSessionAndCompletedTurn'])

    @staticmethod
    def rollout_events(message):
        return [{'type': 'session_meta', 'payload': {'id': 'thread'}},
                {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn'}},
                message, {'type': 'turn_context', 'payload': {'turn_id': 'turn'}},
                {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'turn'}}]

    def test_marker_from_wrong_session_turn_or_missing_terminal_cannot_pass(self):
        message = {'type': 'response_item', 'timestamp': 'then', 'payload': {
            'type': 'message', 'role': 'user', 'text': 'MARK fresh.txt',
            'internal_chat_message_metadata_passthrough': {'turn_id': 'turn'}}}
        originals = self.rollout_events(message)
        changed = []
        for index, field in ((0, 'id'), (1, 'turn_id'), (3, 'turn_id'), (4, 'turn_id')):
            events = copy.deepcopy(originals); events[index]['payload'][field] = 'another'
            changed.append(events)
        events = copy.deepcopy(originals)
        events[2]['payload']['internal_chat_message_metadata_passthrough']['turn_id'] = 'earlier-turn'
        changed.append(events)
        changed.append(originals[:-1])
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp); (home / 'sessions').mkdir(); rollout = home / 'sessions/abc-thread.jsonl'
            task = {'executionIdentity': {'codexThreadId': 'thread', 'codexTurnId': 'turn'}}
            for index, events in enumerate(changed):
                with self.subTest(case=index):
                    rollout.write_text('\n'.join(json.dumps(e) for e in events))
                    with self.assertRaises(RuntimeError): probe.native_input_proof(home, task, 'MARK', 'fresh.txt')

    def test_other_turn_user_message_does_not_prove_requested_turn_input(self):
        old = {'type': 'response_item', 'timestamp': 'old', 'payload': {
            'type': 'message', 'role': 'user', 'text': 'MARK fresh.txt',
            'internal_chat_message_metadata_passthrough': {'turn_id': 'old'}}}
        events = self.rollout_events(old)
        events[1]['payload']['turn_id'] = 'old'
        events[3]['payload']['turn_id'] = 'old'
        events[4]['payload']['turn_id'] = 'old'
        events.extend(self.rollout_events({'type': 'response_item', 'payload': {
            'type': 'message', 'role': 'user', 'text': 'different content',
            'internal_chat_message_metadata_passthrough': {'turn_id': 'turn'}}})[1:])
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp); (home / 'sessions').mkdir()
            (home / 'sessions/abc-thread.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
            task = {'executionIdentity': {'codexThreadId': 'thread', 'codexTurnId': 'turn'}}
            with self.assertRaises(RuntimeError): probe.native_input_proof(home, task, 'MARK', 'fresh.txt')


if __name__ == '__main__': unittest.main()
