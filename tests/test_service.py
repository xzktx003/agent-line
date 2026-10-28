import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agent_line.service import ServiceConfig, create_app
from agent_line.store import TaskStore


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = TaskStore.initialize(self.root / "tasks.db")
        self.config = ServiceConfig(
            tokens={"test-owner-token": {"a"}, "test-other-token": {"b"}},
            projects={
                "a": {"workspace": str(self.root)},
                "b": {"workspace": str(self.root)},
            },
            bindings={"developer": {"projectId": "a", "roleAgentId": "stable-role"}},
            controlled_execution="confirmed",
        )
        self.client = TestClient(create_app(self.store, self.config))
        self.headers = {"Authorization": "Bearer test-owner-token"}
        self.body = {
            "schemaVersion": "1.0",
            "idempotencyKey": "run:activation:attempt",
            "workflow": {
                "id": "wf",
                "revision": "rev",
                "runId": "run",
                "nodeId": "dev",
                "nodeExecutionId": "activation",
                "attemptId": "attempt",
            },
            "projectId": "a",
            "roleBindingId": "developer",
            "input": {"requirement": "test only"},
            "memoryPolicyId": "policy",
            "outputSchemaId": "task-result",
            "limits": {"maxExecutionSeconds": 30, "maxBusinessAttempts": 1},
        }

    def register(self):
        response = self.client.post(
            "/extension/v1/node-tasks", json=self.body, headers=self.headers
        )
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["taskId"]

    def test_overview_reports_stored_role_agent_without_inventing_one(self):
        import json
        task_id = self.register()
        response = self.client.get("/extension/v1/overview")
        self.assertEqual(response.status_code, 200, response.text)
        task = next(item for item in response.json()["tasks"] if item["taskId"] == task_id)
        self.assertEqual(task["roleAgentId"], "stable-role")
        with self.store.connect() as db:
            row = db.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone()
            payload = json.loads(row["payload"])
            payload["roleAgentId"] = " bad"
            db.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(payload), task_id))
        response = self.client.get("/extension/v1/overview")
        self.assertEqual(response.status_code, 200, response.text)
        task = next(item for item in response.json()["tasks"] if item["taskId"] == task_id)
        self.assertIsNone(task["roleAgentId"])
        self.assertNotIn(" bad", response.text)

    def test_shared_host_role_is_rejected_before_readiness(self):
        shared = ServiceConfig(
            tokens={"test-owner-token": {"a", "b"}},
            projects={
                "a": {"workspace": str(self.root)},
                "b": {"workspace": str(self.root)},
            },
            bindings={
                "developer": {"projectId": "a", "roleAgentId": "stable-role"},
                "other": {"projectId": "b", "roleAgentId": "stable-role"},
            },
            controlled_execution="unknown",
        )
        client = TestClient(create_app(self.store, shared))
        response = client.post("/extension/v1/node-tasks", json=self.body, headers=self.headers)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"], "原生角色不能跨项目共用记忆")
        other = {
            **self.body,
            "projectId": "b",
            "roleBindingId": "other",
            "idempotencyKey": "other-project",
            "workflow": {**self.body["workflow"], "nodeExecutionId": "other-project"},
        }
        response = client.post("/extension/v1/node-tasks", json=other, headers=self.headers)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.store.list_tasks(frozenset({"a", "b"}))["tasks"], [])

    def test_readonly_database_health_blocks_admission_but_preserves_history(self):
        task_id = self.register()
        before = self.store.get(task_id)
        self.store.path.chmod(0o400)
        try:
            health = self.client.get('/extension/v1/health', headers=self.headers)
            self.assertEqual(health.status_code, 503, health.text)
            self.assertNotIn(str(self.root), health.text)
            response = self.client.get('/extension/v1/node-tasks/' + task_id, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.store.get(task_id), before)
            response = self.client.post('/extension/v1/node-tasks', headers=self.headers,
                json={**self.body, 'idempotencyKey': 'another',
                      'workflow': {**self.body['workflow'], 'nodeExecutionId': 'another'}})
            self.assertEqual(response.status_code, 503)
        finally:
            # SQLite sidecars created during a readonly open inherit DB mode.
            # Simulate the operator restoring the whole storage permission set.
            for path in self.root.glob('tasks.db*'):
                path.chmod(0o600)
        self.assertEqual(self.client.get('/extension/v1/health', headers=self.headers).status_code, 200)
        self.assertEqual(self.store.get(task_id), before)

    def test_health_probe_does_not_change_tasks_or_events(self):
        task_id = self.register()
        before = self.store.get(task_id)
        events = self.store.events(task_id)
        for _ in range(3):
            health = self.client.get('/extension/v1/health', headers=self.headers)
            self.assertEqual(health.status_code, 200)
            self.assertTrue(health.json()['readyForExecution'])
        self.assertEqual(self.store.get(task_id), before)
        self.assertEqual(self.store.events(task_id), events)

    def test_auth_required_and_unknown_capability_rejected(self):
        path = "/extension/v1/node-tasks"
        for headers in [
            {},
            {"Authorization": "Bearer bad"},
            {"Authorization": "Basic test-owner-token"},
        ]:
            self.assertEqual(
                self.client.post(path, json=self.body, headers=headers).status_code, 401
            )
        unknown = ServiceConfig(
            tokens=self.config.tokens,
            projects=self.config.projects,
            bindings=self.config.bindings,
        )
        client = TestClient(create_app(self.store, unknown))
        self.assertEqual(
            client.post(path, json=self.body, headers=self.headers).status_code, 503
        )
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)

    def test_registration_is_not_execution_and_replays_are_idempotent(self):
        task_id = self.register()
        stored = self.store.get(task_id)
        self.assertEqual(stored["payload"]["roleAgentId"], "stable-role")
        self.assertEqual(stored["payload"]["workspace"], str(self.root.resolve()))
        response = self.client.post(
            "/extension/v1/node-tasks", json=self.body, headers=self.headers
        )
        self.assertEqual(response.json()["taskId"], task_id)
        self.assertTrue(response.json()["reusedExistingTask"])
        self.assertEqual(response.json()["executionStatus"], "registered")
        self.assertEqual(response.json()["upstreamSubmissionStatus"], "pending")
        changed = {**self.body, "input": {"requirement": "different"}}
        self.assertEqual(
            self.client.post(
                "/extension/v1/node-tasks", json=changed, headers=self.headers
            ).status_code,
            409,
        )

    def test_cross_project_access_and_client_selected_roles_are_rejected(self):
        task_id = self.register()
        other = {"Authorization": "Bearer test-other-token"}
        for suffix in ["", "/events"]:
            self.assertEqual(
                self.client.get(
                    f"/extension/v1/node-tasks/{task_id}{suffix}", headers=other
                ).status_code,
                404,
            )
        self.assertEqual(
            self.client.post(
                f"/extension/v1/node-tasks/{task_id}/cancel", headers=other
            ).status_code,
            404,
        )
        for key, value in [
            ("workspace", "/tmp"),
            ("roleAgentId", "arbitrary"),
            ("executorAgentId", "arbitrary"),
        ]:
            self.assertEqual(
                self.client.post(
                    "/extension/v1/node-tasks",
                    json={**self.body, key: value},
                    headers=self.headers,
                ).status_code,
                422,
            )
        self.assertEqual(
            self.client.post(
                "/extension/v1/node-tasks",
                json={**self.body, "projectId": "b"},
                headers=self.headers,
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/extension/v1/node-tasks",
                json={**self.body, "projectId": "b"},
                headers=other,
            ).status_code,
            403,
        )
        read = self.client.get(
            f"/extension/v1/node-tasks/{task_id}", headers=self.headers
        )
        self.assertNotIn(str(self.root), read.text)

    def test_resource_evidence_retains_identity_without_internal_workspace(self):
        task_id = self.register()
        task = self.store.get(task_id)
        self.store.claim(task_id, self.root)
        identity = {"codexThreadId": "thread", "codexTurnId": "turn"}
        self.store.accept(task_id, identity)
        self.store.finish(
            task_id,
            "succeeded",
            {},
            [{"kind": "execution_terminal", "executionId": "turn", "confirmed": True}],
        )
        receipt = {
            "source": "docker-inspect-before-remove",
            "workspace": task["payload"]["workspace"],
            "owner": "owner",
            "containerId": "container",
            "running": False,
            "pid": 0,
            "status": "exited",
            "finishedAt": "2026-10-21T10:00:00Z",
            "identities": [identity],
        }
        self.store.confirm_resources_released(task_id, "turn", receipt)
        response = self.client.get(
            f"/extension/v1/node-tasks/{task_id}", headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(str(self.root), response.text)
        body = response.json()
        self.assertTrue(body["resourcesReleased"])
        self.assertEqual(body["evidence"][1]["executionIdentity"], identity)
        self.assertNotIn("workspace", body["evidence"][1]["receipt"])
        self.assertEqual(self.store.get(task_id)["evidence"][1]["receipt"], receipt)

    def test_cancel_and_cursor_events(self):
        task_id = self.register()
        path = f"/extension/v1/node-tasks/{task_id}"
        first = self.client.get(path + "/events?limit=1", headers=self.headers).json()
        self.assertEqual(len(first["events"]), 1)
        canceled = self.client.post(path + "/cancel", headers=self.headers)
        self.assertEqual(canceled.json()["executionStatus"], "canceled")
        self.assertFalse(
            self.client.post(path + "/cancel", headers=self.headers).json()["changed"]
        )
        next_page = self.client.get(
            path + f'/events?after={first["nextCursor"]}', headers=self.headers
        ).json()
        self.assertEqual([e["kind"] for e in next_page["events"]], ["canceled"])
        for query in ["after=-1", "limit=501", "after=notint"]:
            self.assertEqual(
                self.client.get(
                    path + "/events?" + query, headers=self.headers
                ).status_code,
                422,
            )
        for method in ["finish", "accept", "resources_released"]:
            self.assertEqual(
                self.client.post(path + "/" + method, headers=self.headers).status_code,
                404,
            )

    def test_current_events_expose_the_contract_without_changing_storage(self):
        task_id = self.register()
        before = self.store.events(task_id)
        page = self.client.get(
            f"/extension/v1/node-tasks/{task_id}/events", headers=self.headers
        ).json()
        event = page["events"][0]
        self.assertEqual(event["kind"], "registered")
        self.assertEqual(event["category"], "task_registered")
        self.assertEqual(event["run_id"], "run")
        self.assertEqual(event["source_identity"], "task-store")
        self.assertEqual(event["task_version"], 1)
        self.assertEqual(event["task_id"], task_id)
        self.assertIsInstance(event["event_id"], str)
        self.assertTrue(event["event_id"])
        self.assertIsInstance(event["occurred_at"], float)
        self.assertIsInstance(event["received_at"], float)
        self.assertEqual(event["payload"], {})
        self.assertEqual(self.store.events(task_id), before)
        again = self.client.get(
            f"/extension/v1/node-tasks/{task_id}/events", headers=self.headers
        ).json()
        self.assertEqual(again["events"], page["events"])

    def test_version_two_events_leave_unrecorded_contract_fields_null(self):
        from tests.test_recovery_records import write_version_two

        path = self.root / "legacy.sqlite"
        write_version_two(path)
        store = TaskStore(path)
        task, _reused = store.register("legacy", {
            "projectId": "a",
            "roleAgentId": "stable-role",
            "workspace": str(self.root),
            "workflow": {
                "runId": "run",
                "nodeExecutionId": "activation",
                "attemptId": "attempt",
            },
            "input": {"requirement": "test only"},
        })
        client = TestClient(create_app(store, self.config))
        response = client.get(
            f"/extension/v1/node-tasks/{task['id']}/events", headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        event = response.json()["events"][0]
        self.assertEqual(event["kind"], "registered")
        self.assertEqual(event["task_id"], task["id"])
        for key in (
            "event_id", "run_id", "occurred_at", "received_at",
            "source_identity", "task_version", "payload", "category",
        ):
            self.assertIsNone(event[key])
        self.assertEqual(store.events(task["id"])[0]["kind"], "registered")

    def test_concurrent_http_replays_create_one_task(self):
        def submit(_):
            with TestClient(create_app(self.store, self.config)) as client:
                return client.post(
                    "/extension/v1/node-tasks", json=self.body, headers=self.headers
                )

        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(submit, range(6)))
        self.assertEqual({r.status_code for r in responses}, {202})
        self.assertEqual(len({r.json()["taskId"] for r in responses}), 1)
        self.assertEqual(sum(not r.json()["reusedExistingTask"] for r in responses), 1)

    def test_missing_database_is_unavailable_not_success(self):
        self.store.path.unlink()
        response = self.client.post(
            "/extension/v1/node-tasks", json=self.body, headers=self.headers
        )
        self.assertEqual(response.status_code, 503)
        self.assertFalse(self.store.path.exists())

    def test_active_cancel_is_only_requested(self):
        task_id = self.register()
        self.store.claim(task_id, self.root)
        result = self.client.post(
            f"/extension/v1/node-tasks/{task_id}/cancel", headers=self.headers
        )
        self.assertEqual(result.json()["executionStatus"], "cancel_requested")
        self.assertFalse(self.store.get(task_id)["resourcesReleased"])

    def test_task_catalog_is_scoped_paginated_and_does_not_expose_payload(self):
        first = self.register()
        second_body = {
            **self.body,
            "idempotencyKey": "second",
            "workflow": {**self.body["workflow"], "attemptId": "2"},
        }
        second = self.client.post(
            "/extension/v1/node-tasks", json=second_body, headers=self.headers
        ).json()["taskId"]
        response = self.client.get(
            "/extension/v1/node-tasks?limit=1", headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        page = response.json()
        self.assertEqual(len(page["tasks"]), 1)
        self.assertIsNotNone(page["nextCursor"])
        following = self.client.get(
            "/extension/v1/node-tasks",
            params={"limit": 1, "after": page["nextCursor"]},
            headers=self.headers,
        ).json()
        self.assertEqual(
            {page["tasks"][0]["taskId"], following["tasks"][0]["taskId"]},
            {first, second},
        )
        self.assertIsNone(following["nextCursor"])
        self.assertNotIn("workspace", response.text)
        self.assertNotIn("requirement", response.text)
        hidden = self.client.get(
            "/extension/v1/node-tasks",
            headers={"Authorization": "Bearer test-other-token"},
        )
        self.assertEqual(hidden.json()["tasks"], [])
        self.assertEqual(self.client.get("/extension/v1/node-tasks").status_code, 401)

    def test_task_catalog_filters_execution_status_without_changing_records(self):
        task = self.register()
        self.client.post(
            f"/extension/v1/node-tasks/{task}/cancel", headers=self.headers
        )
        result = self.client.get(
            "/extension/v1/node-tasks?status=canceled", headers=self.headers
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["tasks"][0]["taskId"], task)
        self.assertEqual(
            self.client.get(
                "/extension/v1/node-tasks?status=registered", headers=self.headers
            ).json()["tasks"],
            [],
        )
        self.assertEqual(
            self.client.get(
                "/extension/v1/node-tasks?status=made-up", headers=self.headers
            ).status_code,
            422,
        )

    def test_maintenance_rejects_new_admission_and_preserves_existing_replay(self):
        task = self.register()
        owner = self.store.enter_maintenance()
        repeat = self.client.post(
            "/extension/v1/node-tasks", json=self.body, headers=self.headers
        )
        self.assertEqual(repeat.status_code, 202)
        self.assertEqual(repeat.json()["taskId"], task)
        changed = {
            **self.body,
            "idempotencyKey": "maintenance-new",
            "workflow": {**self.body["workflow"], "attemptId": "2"},
        }
        self.assertEqual(
            self.client.post(
                "/extension/v1/node-tasks", json=changed, headers=self.headers
            ).status_code,
            503,
        )
        self.store.leave_maintenance(owner)
        self.assertEqual(
            self.client.post(
                "/extension/v1/node-tasks", json=changed, headers=self.headers
            ).status_code,
            202,
        )

    def test_queue_projection_is_scoped_read_only_and_has_no_contender_identity(self):
        import copy
        # A different project's earlier task shares the same physical workspace.
        payload={'projectId':'b','roleAgentId':'private-role','roleBindingId':'private-binding',
                 'workspace':str(self.root),'input':{'private':'must not disclose'},
                 'workflow':{'id':'private-workflow','runId':'private-run','nodeExecutionId':'node','attemptId':'1'}}
        private=self.store.register('private-queue',payload)[0]
        task_id=self.register()
        before=copy.deepcopy(self.store.get(task_id))
        events=self.store.events(task_id)
        detail=self.client.get('/extension/v1/node-tasks/'+task_id,headers=self.headers)
        self.assertEqual(detail.status_code,200)
        self.assertEqual(detail.json()['executionQueue'],{'policy':'serial_fifo','state':'waiting','reason':'earlier_registered_task'})
        for secret in [private['id'],'private-role','private-binding','private-workflow','must not disclose']:
            self.assertNotIn(secret,detail.text)
        listing=self.client.get('/extension/v1/node-tasks',headers=self.headers)
        self.assertEqual(listing.status_code,200)
        self.assertEqual(len(listing.json()['tasks']),1)
        self.assertEqual(listing.json()['tasks'][0]['executionQueue'],detail.json()['executionQueue'])
        self.assertEqual(self.store.get(task_id),before)
        self.assertEqual(self.store.events(task_id),events)
        self.assertEqual(self.client.get('/extension/v1/node-tasks/'+private['id'],headers=self.headers).status_code,404)
        self.assertEqual(self.client.get('/extension/v1/node-tasks/'+task_id).status_code,401)

    def test_cancel_queued_task_preserves_history_and_hides_queue(self):
        task_id=self.register()
        canceled=self.client.post('/extension/v1/node-tasks/'+task_id+'/cancel',headers=self.headers)
        self.assertEqual(canceled.status_code,200)
        self.assertEqual(canceled.json()['executionStatus'],'canceled')
        self.assertNotIn('executionQueue',canceled.json())
        self.assertTrue(canceled.json()['resourcesReleased'])
        self.assertEqual(self.client.get('/extension/v1/node-tasks/'+task_id,headers=self.headers).status_code,200)

    def test_public_task_explains_unwritten_native_identity_without_rewriting_it(self):
        from agent_line.service import execution_identity_availability
        missing = execution_identity_availability({}, 'stable-role')
        self.assertEqual(missing['fields']['roleSessionId']['state'], 'not_written')
        self.assertEqual(missing['fields']['executorAgentId']['state'], 'not_written')
        self.assertIn('roleSessionId', missing['fields']['roleSessionId']['reason'])
        self.assertIn('executorAgentId', missing['fields']['executorAgentId']['reason'])
        self.assertEqual(missing['verifiable'], {
            'roleAgentId': 'recorded_outside_execution_identity',
            'upstreamTaskId': 'not_recorded',
        })
        self.assertFalse(missing['mutatesStoredIdentity'])
        dirty = execution_identity_availability({'roleSessionId': ' padded', 'executorAgentId': ''}, None)
        self.assertEqual(dirty['fields']['roleSessionId']['state'], 'unavailable')
        self.assertEqual(dirty['fields']['executorAgentId']['state'], 'unavailable')
        self.assertEqual(dirty['verifiable']['roleAgentId'], 'unavailable')
        self.assertEqual(execution_identity_availability('not-a-dict', None)['fields']['roleSessionId']['state'], 'not_written')

        task_id = self.register()
        empty = self.client.get(f'/extension/v1/node-tasks/{task_id}', headers=self.headers)
        self.assertEqual(empty.status_code, 200)
        body = empty.json()
        self.assertEqual(body['executionIdentity'], {})
        self.assertEqual(body['roleAgentId'], 'stable-role')
        self.assertEqual(body['executionIdentityAvailability']['fields']['roleSessionId']['state'], 'not_written')
        self.assertEqual(body['executionIdentityAvailability']['fields']['executorAgentId']['state'], 'not_written')
        self.assertNotIn('agent-line-', empty.text)
        self.assertEqual(self.store.get(task_id)['executionIdentity'], {})

        identity = {'upstreamTaskId': 'native-task', 'codexThreadId': 'thread', 'codexTurnId': 'turn'}
        self.store.claim(task_id, self.root)
        self.assertTrue(self.store.accept(task_id, identity))
        accepted = self.client.get(f'/extension/v1/node-tasks/{task_id}', headers=self.headers).json()
        self.assertEqual(accepted['executionIdentity'], identity)
        self.assertEqual(accepted['executionIdentityAvailability']['verifiable']['upstreamTaskId'], 'recorded')
        self.assertEqual(accepted['executionIdentityAvailability']['fields']['roleSessionId']['state'], 'not_written')
        self.assertEqual(accepted['executionIdentityAvailability']['fields']['executorAgentId']['state'], 'not_written')
        self.assertEqual(self.store.get(task_id)['executionIdentity'], identity)
        listed = self.client.get('/extension/v1/node-tasks', headers=self.headers).json()
        visible = next(item for item in listed['tasks'] if item['taskId'] == task_id)
        self.assertEqual(visible['executionIdentity'], identity)
        self.assertEqual(visible['executionIdentityAvailability']['fields']['executorAgentId']['state'], 'not_written')

        stored = {**identity, 'roleSessionId': 'already-stored', 'executorAgentId': 'already-executor'}
        self.assertTrue(self.store.accept(task_id, stored))
        before = self.store.get(task_id)['executionIdentity']
        read = self.client.get(f'/extension/v1/node-tasks/{task_id}', headers=self.headers).json()
        self.assertEqual(read['executionIdentity'], stored)
        self.assertEqual(read['executionIdentityAvailability']['fields']['roleSessionId']['state'], 'recorded')
        self.assertEqual(read['executionIdentityAvailability']['fields']['executorAgentId']['state'], 'recorded')
        self.assertNotIn('already-stored', str(read['executionIdentityAvailability']))
        self.assertEqual(self.store.get(task_id)['executionIdentity'], before)

    def test_capabilities_explain_unwritten_native_identity_without_writing(self):
        from agent_line.service import native_execution_identity_capability
        declared = native_execution_identity_capability()
        self.assertEqual(declared['roleSessionId']['state'], 'not_written')
        self.assertEqual(declared['executorAgentId']['state'], 'not_written')
        self.assertFalse(declared['mutatesStoredIdentity'])
        self.assertEqual(declared['verifiable'], {
            'roleAgentId': 'task_field',
            'upstreamTaskId': 'recorded_execution_identity_only',
        })
        self.assertNotIn('agent-line-', str(declared))
        task_id = self.register()
        before = self.store.get(task_id)
        events = self.store.events(task_id)
        denied = self.client.get('/extension/v1/capabilities')
        self.assertEqual(denied.status_code, 401)
        response = self.client.get('/extension/v1/capabilities', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get('cache-control'), 'no-store')
        body = response.json()
        self.assertEqual(body['nativeExecutionIdentity'], declared)
        self.assertNotIn('roleSessionId', body['execution'])
        self.assertNotIn('executorAgentId', body['execution'])
        self.assertNotIn('agent-line-', response.text)
        self.assertEqual(self.store.get(task_id), before)
        self.assertEqual(self.store.events(task_id), events)
        self.assertEqual(self.store.list_tasks(frozenset({'a'}))['tasks'][0]['executionIdentity'], {})

    def test_public_activity_started_at_comes_only_from_role_started(self):
        task_id = self.register()
        first = self.client.get(f'/extension/v1/node-tasks/{task_id}', headers=self.headers)
        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertIsNone(body['activity']['startedAt'])
        self.assertIsNotNone(body['activity']['registeredAt'])
        self.store.note_delegation_started(task_id, 'deleg-only')
        delegated = self.client.get(f'/extension/v1/node-tasks/{task_id}', headers=self.headers).json()
        self.assertIsNone(delegated['activity']['startedAt'])
        self.assertEqual(delegated['executionIdentity'], body['executionIdentity'])
        noted = self.store.note_role_started(task_id)
        second = self.client.get(f'/extension/v1/node-tasks/{task_id}', headers=self.headers).json()
        self.assertEqual(second['activity']['startedAt'], noted['occurredAt'])
        self.assertNotEqual(second['activity']['startedAt'], second['activity']['registeredAt'])
        self.assertEqual(second['executionIdentity'], body['executionIdentity'])
        self.assertNotIn('roleSessionId', second['executionIdentity'])
        self.assertNotIn('executorAgentId', second['executionIdentity'])
        listed = self.client.get('/extension/v1/node-tasks', headers=self.headers).json()
        visible = next(item for item in listed['tasks'] if item['taskId'] == task_id)
        self.assertEqual(visible['activity']['startedAt'], noted['occurredAt'])

