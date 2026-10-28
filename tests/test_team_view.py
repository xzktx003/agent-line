"""Project-scoped, durable team organization metadata API tests."""
from concurrent.futures import ThreadPoolExecutor
import json
import unittest

from agent_line.store import canonical


class TeamViewTests(unittest.TestCase):
    def setUp(self):
        from tests.test_authoring_api import AuthoringAPITests
        self.fixture = AuthoringAPITests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.api = self.fixture.api
        self.api.role_bindings.update({
            'alpha-binding': {'projectId': 'one', 'roleAgentId': 'agent-alpha', 'enabled': True,
                              'roleRevision': 'rev-1'},
            'alpha-disabled': {'projectId': 'one', 'roleAgentId': 'agent-alpha', 'enabled': False},
            'beta-binding': {'projectId': 'one', 'roleAgentId': 'agent-beta', 'enabled': False},
            'other-binding': {'projectId': 'two', 'roleAgentId': 'agent-other', 'enabled': True},
        })

    def request(self, method, path, *, token='edit', body=None):
        headers = {} if token is None else {'Authorization': 'Bearer ' + token}
        return self.fixture.http.request(method, '/extension/v1/authoring' + path,
                                         headers=headers, json=body)

    def get_team(self, project='one', token='edit'):
        return self.request('GET', f'/projects/{project}/team', token=token)

    def post_team(self, body, project='one', token='edit'):
        return self.request('POST', f'/projects/{project}/team', token=token, body=body)

    def body(self, operation='team-1', revision=0, edges=None):
        return {'operationId': operation, 'expectedRevision': revision, 'edges': edges or []}

    def test_get_projects_configured_roles_edges_and_availability_without_journal_write(self):
        with self.fixture.journal.connect() as db:
            before = db.execute('SELECT count(*) FROM operations').fetchone()[0]
        response = self.get_team()
        self.assertEqual(response.status_code, 200)
        value = response.json()
        self.assertEqual(value['revision'], 0)
        self.assertEqual(value['edges'], [])
        self.assertEqual(value['roles'], [
            {'roleAgentId': 'agent-alpha', 'bindingIds': ['alpha-binding', 'alpha-disabled'],
             'enabled': True, 'roleRevision': 'rev-1'},
            {'roleAgentId': 'agent-beta', 'bindingIds': ['beta-binding'],
             'enabled': False},
        ])
        self.assertEqual(value['tasksAvailable'], True)
        self.assertEqual(value['activeTasks'], [])
        with self.fixture.journal.connect() as db:
            after = db.execute('SELECT count(*) FROM operations').fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(self.fixture.remote.calls, [])

    def test_post_canonicalizes_persists_and_replay_keeps_original_receipt(self):
        edges = [{'sourceAgentId': 'agent-beta', 'targetAgentId': 'agent-alpha'},
                 {'sourceAgentId': 'agent-alpha', 'targetAgentId': 'agent-beta'}]
        response = self.post_team(self.body(edges=edges))
        self.assertEqual(response.status_code, 200)
        expected = sorted(edges, key=lambda edge: (edge['sourceAgentId'], edge['targetAgentId']))
        receipt = {'projectId': 'one', 'operationId': 'team-1', 'revision': 1, 'edges': expected}
        self.assertEqual(response.json(), receipt)
        self.assertEqual(self.get_team().json()['edges'], expected)
        self.assertEqual(self.post_team(self.body('later', 1, [])).json()['revision'], 2)
        replay = self.post_team(self.body(edges=edges))
        self.assertEqual(replay.json(), receipt)
        self.assertEqual(self.get_team().json()['revision'], 2)
        self.assertEqual(self.request('GET', '/projects/one/team/operations/team-1').json(), receipt)
        self.assertEqual(self.request('GET', '/projects/one/team/operations/unknown').status_code, 404)
        with self.fixture.journal.connect() as db:
            count = db.execute("SELECT count(*) FROM operations WHERE context='team-view'").fetchone()[0]
        self.assertEqual(count, 2)

    def test_same_operation_changed_actor_or_request_conflicts(self):
        self.assertEqual(self.post_team(self.body()).status_code, 200)
        changed_body = self.post_team(self.body(edges=[{'sourceAgentId': 'agent-alpha',
                                                        'targetAgentId': 'agent-beta'}]))
        self.assertEqual(changed_body.status_code, 409)
        changed_actor = self.post_team(self.body(), token='publish')
        self.assertEqual(changed_actor.status_code, 409)

    def test_concurrent_cas_has_one_winner(self):
        barrier = __import__('threading').Barrier(2)

        def write(operation, source):
            barrier.wait()
            target = 'agent-beta' if source == 'agent-alpha' else 'agent-alpha'
            return self.post_team(self.body(operation, 0, [{'sourceAgentId': source,
                                                            'targetAgentId': target}]))

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda args: write(*args),
                                    [('team-a', 'agent-alpha'), ('team-b', 'agent-beta')]))
        self.assertEqual(sorted(response.status_code for response in results), [200, 409])
        self.assertEqual(self.get_team().json()['revision'], 1)

    def test_auth_scope_revocation_and_maintenance_guard(self):
        self.assertEqual(self.get_team(token=None).status_code, 401)
        self.assertEqual(self.get_team(project='one', token='other').status_code, 404)
        self.fixture.access.revoke('edit')
        self.assertEqual(self.get_team().status_code, 401)
        self.fixture.access.replace_credentials({'edit': self.fixture.credentials['edit']})
        owner = self.fixture.tasks.enter_maintenance()
        try:
            self.assertEqual(self.post_team(self.body()).status_code, 503)
        finally:
            self.fixture.tasks.leave_maintenance(owner)
        self.assertEqual(self.get_team().status_code, 200)

    def test_unknown_new_edges_rejected_deleted_binding_becomes_orphan_and_can_be_removed(self):
        dangling = {'sourceAgentId': 'agent-beta', 'targetAgentId': 'agent-alpha'}
        self.assertEqual(self.post_team(self.body(edges=[dangling])).status_code, 200)
        self.api.role_bindings.pop('beta-binding')
        self.api.role_bindings.pop('alpha-binding')
        self.api.role_bindings.pop('alpha-disabled')
        view = self.get_team().json()
        self.assertEqual(view['orphanAgentIds'], ['agent-alpha', 'agent-beta'])
        # Existing dangling metadata may be retained, but new edges to unknown IDs fail.
        self.assertEqual(self.post_team(self.body('keep', 1, [dangling])).status_code, 200)
        self.assertEqual(self.post_team(self.body('new', 2, [{'sourceAgentId': 'agent-x',
                                                             'targetAgentId': 'agent-y'}])).status_code, 422)
        self.assertEqual(self.post_team(self.body('remove', 2, [])).status_code, 200)
        self.assertEqual(self.get_team().json()['orphanAgentIds'], [])

    def test_deleted_binding_active_task_role_is_orphan_and_task_projection_is_private_read_only(self):
        payload = {'projectId': 'one', 'roleAgentId': 'agent-gone',
                   'workflow': {'runId': 'workflow-run'}, 'workspace': '/secret/path',
                   'privateToken': 'secret'}
        identity = {'taskId': 'task-1', 'codexThreadId': 'thread-1',
                    'codexTurnId': 'turn-1', 'upstreamTaskId': 'upstream-1', 'secret': 'hidden'}
        with self.fixture.tasks.connect() as db:
            db.execute("INSERT INTO tasks(id,idempotency_key,activation_key,input_hash,payload,execution_status,"
                       "submission_status,created_at,execution_identity,output,evidence,verification_status,"
                       "memory_status,resources_released) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       ('task-1', 'key', 'activation', 'hash', canonical(payload), 'accepted', 'accepted',
                        1, canonical(identity), canonical({'result': 'private'}), canonical([{'secret': 'evidence'}]),
                        'pending', 'not_requested', 0))
            db.execute("INSERT INTO tasks(id,idempotency_key,activation_key,input_hash,payload,execution_status,"
                       "submission_status,created_at,execution_identity,output,evidence,verification_status,"
                       "memory_status,resources_released) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       ('task-two', 'key-two', 'activation-two', 'hash', canonical({**payload, 'projectId': 'two'}),
                        'accepted', 'accepted', 1, '{}', None, '[]', 'pending', 'not_requested', 0))
            before = db.execute('SELECT count(*) FROM tasks').fetchone()[0]
        response = self.get_team()
        self.assertEqual(response.status_code, 200)
        value = response.json()
        self.assertEqual(value['orphanAgentIds'], ['agent-gone'])
        self.assertEqual(len(value['activeTasks']), 1)
        task = value['activeTasks'][0]
        self.assertEqual(task, {'taskId': 'task-1', 'roleAgentId': 'agent-gone',
                                'workflowRunId': 'workflow-run', 'executionStatus': 'accepted',
                                'verificationStatus': 'pending', 'memoryStatus': 'not_requested',
                                'resourcesReleased': False,
                                'executionIdentity': {'taskId': 'task-1', 'codexThreadId': 'thread-1',
                                                      'codexTurnId': 'turn-1',
                                                      'upstreamTaskId': 'upstream-1'}})
        encoded = json.dumps(value)
        for private in ('secret', '/secret/path', 'private', 'evidence', 'task-two'):
            self.assertNotIn(private, encoded)
        with self.fixture.tasks.connect() as db:
            after = db.execute('SELECT count(*) FROM tasks').fetchone()[0]
        self.assertEqual(after, before)

    def test_restart_reads_same_journal_and_corrupt_history_fails_closed(self):
        self.assertEqual(self.post_team(self.body(edges=[{'sourceAgentId': 'agent-alpha',
                                                         'targetAgentId': 'agent-beta'}])).status_code, 200)
        from agent_line.sim_journal import SimJournal
        from agent_line.team_view import TeamView
        restarted = TeamView(SimJournal(self.fixture.journal.path), self.api.projects,
                             self.api.role_bindings, self.fixture.tasks)
        self.assertEqual(restarted.get('one')['revision'], 1)
        with self.fixture.journal.connect() as db:
            db.execute("UPDATE operations SET receipt='{}' WHERE context='team-view'")
        self.assertEqual(self.get_team().status_code, 503)

    def test_strict_body_rejects_duplicates_self_edges_and_more_than_200(self):
        duplicate = {'sourceAgentId': 'agent-alpha', 'targetAgentId': 'agent-beta'}
        self.assertEqual(self.post_team(self.body(edges=[duplicate, duplicate])).status_code, 422)
        self.assertEqual(self.post_team(self.body(edges=[{'sourceAgentId': 'agent-alpha',
                                                         'targetAgentId': 'agent-alpha'}])).status_code, 422)
        self.assertEqual(self.post_team(self.body(edges=[duplicate] * 201)).status_code, 422)


if __name__ == '__main__':
    unittest.main()
