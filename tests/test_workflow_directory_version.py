"""Live deployment metadata must not confuse newest history with active version."""
import unittest
from unittest.mock import patch
import httpx
from agent_line.sim_authoring import AuthoringError
from tests import test_sim_authoring as transport_fixture


class DeploymentTransportTests(unittest.TestCase):
    def make_client(self, payload, workspace='workspace'):
        helper = transport_fixture.TransportTests(methodName='runTest')
        self.addCleanup(helper.doCleanups)
        self.calls = []

        def handler(request):
            self.calls.append(request)
            if request.url.path.endswith('/deployment'):
                return httpx.Response(200, json={'data': payload})
            return httpx.Response(200, json={'data': {'id': 'wf', 'name': '流程',
                                                     'workspaceId': workspace}})
        return helper.setup_client(handler)

    def test_exact_read_path_and_native_current_version(self):
        payload = {'id': 'wf', 'isDeployed': True, 'needsRedeployment': True,
                   'activeDeployment': {'deploymentVersionId': 'original-v2', 'version': 2}}
        client = self.make_client(payload)
        self.assertEqual(client.get_deployment('wf'), payload)
        self.assertEqual([(r.method, r.url.path) for r in self.calls],
                         [('GET', '/api/v2/workflows/wf'), ('GET', '/api/v2/workflows/wf/deployment')])

    def test_scope_mismatch_prevents_deployment_read(self):
        client = self.make_client({}, workspace='someone-else')
        with self.assertRaises(AuthoringError):
            client.get_deployment('wf')
        self.assertEqual(len(self.calls), 1)

    def test_invalid_and_contradictory_states_reject(self):
        valid = {'id': 'wf', 'isDeployed': True, 'needsRedeployment': False,
                 'activeDeployment': {'deploymentVersionId': 'v2', 'version': 2}}
        for patch_value in ({'id': 'other'}, {'isDeployed': False}, {'needsRedeployment': None},
                            {'activeDeployment': None},
                            {'activeDeployment': {'deploymentVersionId': 'v', 'version': True}},
                            {'activeDeployment': {'deploymentVersionId': '', 'version': 2}}):
            with self.subTest(patch=patch_value):
                with self.assertRaises(AuthoringError):
                    self.make_client(valid | patch_value).get_deployment('wf')
        draft = valid | {'isDeployed': False, 'activeDeployment': None}
        self.assertEqual(self.make_client(draft).get_deployment('wf'), draft)


class DirectoryVersionTests(unittest.TestCase):
    def setUp(self):
        from tests.test_authoring_api import AuthoringAPITests
        self.fixture = AuthoringAPITests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.wid = self.fixture.owned()

    def rows(self):
        response = self.fixture.request('GET', '/projects/one/workflows')
        self.assertEqual(response.status_code, 200)
        return response.json()['workflows']

    def test_current_version_comes_from_remote_not_local_publication_receipt(self):
        with patch.object(self.fixture.remote, 'get_deployment', return_value={
                'id': self.wid, 'isDeployed': True, 'needsRedeployment': True,
                'activeDeployment': {'deploymentVersionId': 'rolled-back', 'version': 2}}):
            row = self.rows()[0]
        self.assertEqual(row['currentVersion'], 2)
        self.assertEqual(row['deploymentStatus'], 'deployed')
        self.assertTrue(row['needsRedeployment'])
        self.assertTrue(row['isDeployed'])
        self.assertEqual(self.fixture.remote.count('deploy'), 0)

    def test_native_undeploy_overrides_stale_detail_and_failed_read_stays_unknown(self):
        self.fixture.remote.details[self.wid]['isDeployed'] = True
        row = self.rows()[0]
        self.assertEqual(row['deploymentStatus'], 'draft')
        self.assertIsNone(row['currentVersion'])
        self.assertFalse(row['isDeployed'])
        with patch.object(self.fixture.remote, 'get_deployment', side_effect=AuthoringError('upstream_read_failed')):
            row = self.rows()[0]
        self.assertEqual(row['deploymentStatus'], 'unknown')
        self.assertIsNone(row['currentVersion'])
        self.assertIsNone(row['needsRedeployment'])
