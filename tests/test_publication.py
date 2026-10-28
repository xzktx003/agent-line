import copy
import unittest

import httpx

from agent_line.adapters.sim import SimClient
from agent_line.publication import PublicationUnverified, PublicationVerifier


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.graph = {"blocks": {"human": {"id": "human", "type": "human_in_the_loop_v2", "enabled": True}}, "edges": [], "loops": {}, "parallels": {}}
        self.version = {"id": "deployment", "version": 1, "isActive": True, "state": self.graph}
        self.run = {"runId": "run", "workflowId": "workflow", "deploymentVersionId": "deployment", "workflowState": self.graph}
        self.snapshot = dict(workflowId="workflow", runId="run", revision="1", deploymentVersionId="deployment", humanBlockIds=["human"])
        self.requests = []
        self.response = None
        def handle(request):
            self.requests.append(request)
            if self.response is not None:
                return self.response
            data = self.run if request.url.path == '/api/v2/logs/run' else self.version
            return httpx.Response(200, json={"data": data})
        self.client = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(self.client.close)
        self.verifier = PublicationVerifier(SimClient('http://sim', 'test-only-key', client=self.client))

    def definition(self, **kwargs):
        return self.verifier.check_definition('workflow', '1', 'deployment', ['human'], **kwargs)

    def test_actual_run_proof_uses_only_logs_and_numeric_version(self):
        self.assertIs(self.verifier.verify_run(self.snapshot), True)
        self.assertEqual([r.url.path for r in self.requests], ['/api/v2/logs/run', '/api/v2/workflows/workflow/versions/1'])
        self.assertEqual(self.requests[0].url.params['includeWorkflowState'], 'true')
        self.assertTrue(all(r.method == 'GET' for r in self.requests))

    def test_inactive_historical_run_allowed_but_preflight_rejected(self):
        self.version['isActive'] = False
        self.assertIs(self.verifier.verify_run(self.snapshot), True)
        with self.assertRaisesRegex(PublicationUnverified, 'inactive_version'):
            self.definition(require_active=True)

    def test_run_identity_and_publication_mismatch_never_fallback(self):
        for key in ('runId', 'workflowId', 'deploymentVersionId'):
            for value in (None, 'different'):
                with self.subTest(key=key, value=value):
                    saved = self.run[key]; self.run[key] = value
                    with self.assertRaisesRegex(PublicationUnverified, 'run_version_mismatch'):
                        self.verifier.verify_run(self.snapshot)
                    self.run[key] = saved

    def test_numeric_revision_id_and_type_are_strict(self):
        for key, value in [('id', 'other'), ('version', True), ('version', '1'), ('version', 2)]:
            with self.subTest(key=key, value=value):
                saved = self.version[key]; self.version[key] = value
                with self.assertRaisesRegex(PublicationUnverified, 'version_mismatch'):
                    self.definition()
                self.version[key] = saved

    def test_both_run_and_published_graph_must_prove_human_node(self):
        bad = [None, {}, {'blocks': []}, {'blocks': {}},
               {'blocks': {'human': {'id': 'human', 'type': 'agent', 'enabled': True}}},
               {'blocks': {'human': {'id': 'wrong', 'type': 'human_in_the_loop_v2', 'enabled': True}}},
               {'blocks': {'human': {'id': 'human', 'type': 'human_in_the_loop_v2', 'enabled': False}}}]
        for target, key in ((self.run, 'workflowState'), (self.version, 'state')):
            for graph in bad:
                with self.subTest(key=key, graph=graph):
                    target[key] = graph
                    with self.assertRaises(PublicationUnverified):
                        self.verifier.verify_run(self.snapshot)
                    target[key] = self.graph

    def test_invalid_binding_rejected_before_http(self):
        for key, value in [('revision', 'active'), ('revision', '01'), ('revision', True),
                           ('runId', '../other'), ('deploymentVersionId', None),
                           ('humanBlockIds', []), ('humanBlockIds', ['human', 'human'])]:
            snapshot = copy.deepcopy(self.snapshot); snapshot[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(PublicationUnverified):
                self.verifier.verify_run(snapshot)
        self.assertEqual(self.requests, [])

    def test_http_failure_redirect_bad_envelope_are_bounded(self):
        for response in (httpx.Response(302, headers={'location': 'http://elsewhere'}),
                         httpx.Response(404), httpx.Response(503),
                         httpx.Response(200, text='private-body'),
                         httpx.Response(200, json={'error': 'private-body', 'data': self.version}),
                         httpx.Response(200, json=self.version)):
            self.response = response
            with self.subTest(status=response.status_code), self.assertRaises(PublicationUnverified) as caught:
                self.definition()
            self.assertNotIn('private-body', str(caught.exception))

    def test_transport_timeout_is_unverified(self):
        def timeout(request):
            raise httpx.ReadTimeout('secret upstream detail')
        with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
            verifier = PublicationVerifier(SimClient('http://sim', 'test-only-key', client=client))
            with self.assertRaisesRegex(PublicationUnverified, 'unavailable'):
                verifier.verify_run(self.snapshot)

    def test_conditional_dag_and_unrelated_agent_cycle_allowed(self):
        for node in ('start', 'condition', 'yes', 'no', 'a', 'b'):
            self.graph['blocks'][node] = {'id': node, 'type': 'condition' if node == 'condition' else 'agent'}
        self.graph['edges'] = [{'source': a, 'target': b} for a, b in (
            ('start', 'human'), ('human', 'condition'), ('condition', 'yes'),
            ('condition', 'no'), ('a', 'b'), ('b', 'a'))]
        self.assertIs(self.verifier.verify_run(self.snapshot), True)
        self.graph['edges'].append({'source': 'yes', 'target': 'human'})
        with self.assertRaisesRegex(PublicationUnverified, 'repeated_human_activation'):
            self.verifier.verify_run(self.snapshot)

    def test_human_container_membership_and_nested_parent_rejected(self):
        self.graph['blocks'].update({'outer': {'id': 'outer', 'type': 'loop'},
                                    'inner': {'id': 'inner', 'type': 'parallel'}})
        self.graph['loops'] = {'outer': {'nodes': ['inner']}}
        self.graph['parallels'] = {'inner': {'nodes': ['human']}}
        with self.assertRaisesRegex(PublicationUnverified, 'repeated_human_activation'):
            self.verifier.verify_run(self.snapshot)
        self.graph['parallels']['inner']['nodes'] = []
        self.graph['blocks']['human']['data'] = {'parentId': 'inner'}
        with self.assertRaisesRegex(PublicationUnverified, 'repeated_human_activation'):
            self.verifier.verify_run(self.snapshot)

    def test_unrelated_loop_allowed_but_unknown_shape_rejected(self):
        self.graph['blocks'].update({'loop': {'id': 'loop', 'type': 'loop'},
                                    'agent': {'id': 'agent', 'type': 'agent'}})
        self.graph['loops'] = {'loop': {'nodes': ['agent']}}
        self.assertIs(self.verifier.verify_run(self.snapshot), True)
        self.graph['loops']['loop']['nodes'] = None
        with self.assertRaisesRegex(PublicationUnverified, 'unsupported_topology'):
            self.verifier.verify_run(self.snapshot)

    def test_self_edge_and_container_boundary_return_rejected(self):
        self.graph['edges'] = [{'source': 'human', 'target': 'human'}]
        with self.assertRaisesRegex(PublicationUnverified, 'repeated_human_activation'):
            self.definition()
        self.graph['blocks'].update({'loop': {'id': 'loop', 'type': 'loop'},
                                    'agent': {'id': 'agent', 'type': 'agent'}})
        self.graph['loops'] = {'loop': {'nodes': ['agent']}}
        self.graph['edges'] = [{'source': 'human', 'target': 'loop'}, {'source': 'agent', 'target': 'human'}]
        with self.assertRaisesRegex(PublicationUnverified, 'repeated_human_activation'):
            self.definition()


if __name__ == '__main__':
    unittest.main()
