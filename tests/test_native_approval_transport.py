import json
import unittest
import httpx

from agent_line.store import Conflict
from tests import test_role_dispatch as fixtures


class NativeApprovalTransportTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.RoleDispatchTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.requests = []
        def handler(request):
            self.requests.append(request)
            return httpx.Response(200, json={'taskId': 'task-one', 'approvals': []})
        self.gateway = self.f.gateway(handler=handler)
        self.f.ledger.claim('task-one', {'roleAgentId': 'fixed-parent',
            'roleSessionId': 'agent-line-task-one', 'deploymentId': 'installation-test'})

    def test_exact_task_routes_use_only_host_and_service_auth(self):
        self.gateway.execution_approvals('task-one')
        payload = {'decisionId': 'decision', 'decision': 'reject', 'decidedBy': 'human',
                   'expectedIdentity': {'upstreamTaskId': 'native', 'codexThreadId': 'thread', 'codexTurnId': 'turn'}}
        self.gateway.decide_execution_approval('task-one', 'request-one', payload)
        self.assertEqual([(r.method, r.url.path) for r in self.requests], [
            ('GET', '/api/agent-line/executions/task-one/approvals'),
            ('POST', '/api/agent-line/executions/task-one/approvals/request-one/decision')])
        self.assertEqual(json.loads(self.requests[1].content), payload)
        for request in self.requests:
            self.assertEqual(request.headers['Authorization'], 'Bearer host-secret')
            self.assertEqual(request.headers['X-Agent-Line-Service-Token'], 'service-secret')
            self.assertEqual(request.headers['X-Agent-Id'], 'fixed-parent')
            self.assertNotIn('secret', str(request.url))

    def test_unbound_or_changed_role_cannot_query_or_deliver(self):
        with self.assertRaises(KeyError):
            self.gateway.execution_approvals('unknown')
        other = self.f.gateway(role='other')
        with self.assertRaises(Conflict):
            other.execution_approvals('task-one')
        with self.assertRaises(ValueError):
            self.gateway.decide_execution_approval('task-one', '../request', {})
        self.assertFalse(self.requests)
