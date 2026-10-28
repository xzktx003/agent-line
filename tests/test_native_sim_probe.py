"""Independent live attempts must not collide or replace persisted identities."""
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from probes.sim.workflow_probe import Probe, graph


class NativeSimProbeTests(unittest.TestCase):
    def test_independent_attempts_and_restart_preserve_native_identities(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'auth.json'
            config.write_text(json.dumps({'workspaceId': 'workspace', 'baseUrl': 'http://sim.invalid', 'apiKey': 'fixture'}))
            config.chmod(0o600)
            names, node_ids, states, posts = set(), set(), {}, []

            def native(request):
                path = request.url.path
                body = json.loads(request.content) if request.content else None
                if request.method == 'POST' and path == '/api/v2/workflows':
                    if body['name'] in names:
                        return httpx.Response(409, json={'error': 'name conflict'})
                    names.add(body['name'])
                    workflow = f'workflow-{len(names)}'
                    posts.append(workflow)
                    return httpx.Response(200, json={'data': {'id': workflow}})
                workflow = path.split('/')[4]
                if request.method == 'PUT':
                    self.assertFalse(set(body['blocks']) & node_ids)
                    node_ids.update(body['blocks'])
                    states[workflow] = body
                    for edge in body['edges']:
                        self.assertIn(edge['source'], body['blocks'])
                        self.assertIn(edge['target'], body['blocks'])
                    return httpx.Response(200, json={'data': {}})
                if path.endswith('/deploy'):
                    return httpx.Response(200, json={'data': {}})
                return httpx.Response(200, json={'data': states[workflow]})

            def open_probe(path):
                probe = Probe(config, path)
                probe.client.close()
                probe.client = httpx.Client(base_url='http://sim.invalid', transport=httpx.MockTransport(native))
                self.addCleanup(probe.client.close)
                return probe

            first_path, second_path = root / 'first.json', root / 'second.json'
            first = open_probe(first_path).prepare('condition')
            second = open_probe(second_path).prepare('condition')
            self.assertNotEqual(first, second)
            self.assertEqual(open_probe(first_path).prepare('condition'), first)
            self.assertEqual(len(posts), 2)
            self.assertEqual(len(node_ids), 8)

            # Old files have no attemptId: observe their original native graph.
            states['legacy'] = graph()
            legacy_path = root / 'legacy.json'
            legacy_path.write_text(json.dumps({'workspaceId': 'workspace', 'runs': {}, 'steps': {
                'condition-create': {'status': 'received', 'response': {'id': 'legacy'}},
                'condition-state': {'status': 'received', 'response': {}},
                'condition-deploy': {'status': 'received', 'response': {}},
            }}))
            self.assertEqual(open_probe(legacy_path).prepare('condition'), 'legacy')
            self.assertEqual(len(posts), 2)
            unknown_path = root / 'unknown.json'
            unknown_path.write_text(json.dumps({'workspaceId': 'workspace', 'runs': {}, 'steps': {
                'condition-create': {'status': 'intent'}}}))
            with self.assertRaisesRegex(RuntimeError, 'outcome unknown'):
                open_probe(unknown_path).prepare('condition')
            self.assertEqual(len(posts), 2)
