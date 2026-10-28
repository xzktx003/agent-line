"""Guardrails for the live read-only browser probe; no browser or live service."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from probes.sim.publication_ready_browser import check_document, check_results, load_configuration, private_json


class PublicationReadyBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = dict(baseUrl='http://127.0.0.1:3197', serviceFile='service.json', projectId='project',
                           workflowId='flow', bindingId='published-flow', revision=1, runId='run', taskId='task',
                           roleBindingId='role', roleAgentId='native-role')
        self.service = dict(authoringFile='authoring.json', tokens={'service-private': ['project']},
                            bindings={'role': dict(projectId='project', roleAgentId='native-role')})
        self.authoring = dict(credentials=[dict(token='editor-private', projectScopes=['project'],
                                                capabilities=['edit_workflows'], expiresAt=time.time() + 60)])
        self.save('service.json', self.service)
        self.save('authoring.json', self.authoring)
        self.path = self.save('browser-config.json', self.config)

    def save(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    def test_private_project_credentials_and_relative_files(self):
        config, editor, service, output = load_configuration(self.path)
        self.assertEqual(config, self.config)
        self.assertEqual((editor, service), ('editor-private', 'service-private'))
        self.assertEqual(output, self.root / 'browser')

    def test_external_or_credential_bearing_origin_rejected(self):
        for base in ('https://example.com', 'http://10.30.0.28:3197', 'http://secret@127.0.0.1:3197',
                     'http://127.0.0.1:3197/path', 'http://127.0.0.1:3197?token=secret'):
            self.save('browser-config.json', dict(self.config, baseUrl=base))
            with self.assertRaisesRegex(ValueError, 'loopback_origin_required'):
                load_configuration(self.path)

    def test_config_must_be_private_regular_file(self):
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'private_regular_configuration_required'):
            private_json(self.path)
        self.path.chmod(0o600)
        link = self.root / 'link.json'
        os.symlink(self.path, link)
        with self.assertRaisesRegex(ValueError, 'private_regular_configuration_required'):
            private_json(link)

    def test_ambiguous_or_expired_authoring_credentials_rejected(self):
        self.authoring['credentials'].append(copy.deepcopy(self.authoring['credentials'][0]))
        self.save('authoring.json', self.authoring)
        with self.assertRaisesRegex(ValueError, 'unique_project_credentials_required'):
            load_configuration(self.path)
        self.authoring['credentials'] = [dict(self.authoring['credentials'][0], expiresAt=1)]
        self.save('authoring.json', self.authoring)
        with self.assertRaisesRegex(ValueError, 'unique_project_credentials_required'):
            load_configuration(self.path)

    def test_wrong_native_role_identity_rejected(self):
        self.service['bindings']['role']['roleAgentId'] = 'new-role'
        self.save('service.json', self.service)
        with self.assertRaisesRegex(ValueError, 'configured_role_identity_changed'):
            load_configuration(self.path)

    def test_document_requires_exact_published_version_and_native_memory(self):
        detail = dict(workflowId='flow', isDeployed=True, stateSha256='a'*64,
                      publishedBindings=[dict(bindingId='published-flow', revision=1)],
                      document=dict(nodes=[dict(id='agent', type='agent', config=dict(
                          roleBindingId='role', memoryPolicyId='native-role'))], edges=[]))
        _, digest = check_document(self.config, detail)
        self.assertEqual(len(digest), 64)
        for altered in (dict(detail, isDeployed=False), dict(detail, publishedBindings=[])):
            with self.assertRaises(ValueError):
                check_document(self.config, altered)
        detail['document']['nodes'][0]['config']['memoryPolicyId'] = 'replacement'
        with self.assertRaisesRegex(ValueError, 'native_role_policy_changed'):
            check_document(self.config, detail)

    def test_terminal_run_requires_independent_verification_and_resource_exit(self):
        task = dict(taskId='task', projectId='project', roleBindingId='role', executionStatus='succeeded',
                    verificationStatus='passed', memoryStatus='not_requested', resourcesReleased=True)
        run = dict(execution=dict(status='paused'), resumption=dict(execution=dict(status='completed')))
        self.assertEqual(check_results(self.config, run, task)['runStatus'], 'completed')
        for altered in (dict(task, verificationStatus='pending'), dict(task, resourcesReleased=False),
                        dict(task, executionStatus='accepted'), dict(task, taskId='other')):
            with self.assertRaises(ValueError):
                check_results(self.config, run, altered)
        with self.assertRaisesRegex(ValueError, 'flow_not_completed'):
            check_results(self.config, dict(execution=dict(status='paused')), task)


if __name__ == '__main__':
    unittest.main()
