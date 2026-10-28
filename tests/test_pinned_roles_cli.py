"""Publication probe selects an isolated directory and never replays old state."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from probes.full_workflow import pinned_roles as probe
from probes.sim.authoring_probe import write


class PinnedRolesCLITests(unittest.TestCase):
    def test_new_mode_requires_commands_and_rejects_failure_and_cancel(self):
        invalid = [dict(publication_capabilities=True)]
        invalid.extend(dict(publication_capabilities=True, test_commands=True, **{flag: True})
                       for flag in ('test_failure', 'test_cancel'))
        for options in invalid:
            with self.subTest(options=options):
                with self.assertRaisesRegex(ValueError, 'Publication capabilities'):
                    probe.probe_directory(Path('/fixture'), **options)

    def test_new_mode_uses_unique_directory_and_preserves_each_existing_mode(self):
        modes = [({}, 'pinned-role-flow'),
                 ({'test_commands': True}, 'pinned-test-flow'),
                 ({'test_commands': True, 'reuse_known_template': True}, 'pinned-test-flow-known-template'),
                 ({'test_commands': True, 'reuse_known_template': True, 'test_failure': True},
                  'pinned-test-failure-known-template'),
                 ({'test_commands': True, 'reuse_known_template': True, 'test_cancel': True},
                  'pinned-test-cancel-known-template'),
                 ({'test_commands': True, 'publication_capabilities': True}, 'publication-ready-flow'),
                 ({'test_commands': True, 'publication_capabilities': True, 'reuse_known_template': True},
                  'publication-ready-known-template')]
        for options, name in modes:
            with self.subTest(options=options):
                self.assertEqual(probe.probe_directory('/fixture', **options), Path('/fixture/var') / name)

    def test_original_invalid_mode_constraints_remain(self):
        for options in ({'reuse_known_template': True}, {'test_failure': True}, {'test_cancel': True},
                        {'test_commands': True, 'test_cancel': True},
                        {'test_commands': True, 'reuse_known_template': True, 'test_cancel': True,
                         'test_failure': True}):
            with self.assertRaises(ValueError):
                probe.probe_directory('/fixture', **options)

    def test_cli_explicit_mode_and_invalid_flags_fail_before_auth_or_directory_io(self):
        self.assertEqual(probe.arguments(['--test-commands', '--publication-capabilities']), {
            'test_commands': True, 'reuse_known_template': False, 'test_failure': False,
            'test_cancel': False, 'publication_capabilities': True})
        self.assertTrue(probe.arguments(['--test-commands','--publication-capabilities',
                                         '--reuse-known-template'])['reuse_known_template'])
        for argv in (['--publication-capabilities'],
                     ['--test-commands', '--publication-capabilities', '--test-failure'],
                     ['--test-commands', '--publication-capabilities', '--test-cancel']):
            with redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as exited:
                    probe.arguments(argv)
            self.assertEqual(exited.exception.code, 2)
        with patch.object(probe, 'SimClient') as native:
            with self.assertRaises(ValueError):
                probe.main(publication_capabilities=True)
            native.assert_not_called()

    def test_existing_new_mode_state_only_observes_original_run_and_never_opens_host(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        directory = root / 'var/publication-ready-flow'; directory.mkdir(parents=True, mode=0o700)
        auth_dir = root / 'var/sim'; auth_dir.mkdir(mode=0o700)
        write(auth_dir / 'probe-auth.json', {'baseUrl': 'http://sim.fixture', 'apiKey': 'fixture-secret'})
        state = {'workflowId': 'original-owned-workflow',
                 'cases': {'pass': {'runId': 'original-unknown-run'}}}
        write(directory / 'state.json', state)
        before = (directory / 'state.json').read_bytes()
        sim = Mock()
        sim._client.event_hooks = {'request': [], 'response': []}
        sim.observe.return_value = {'status': 'unknown'}
        with patch.object(probe, 'DATA', probe.DATA), patch.object(probe, 'ROOT', root), \
                patch.object(probe, 'SimClient', return_value=sim), \
                patch.object(probe, 'quiet_host') as quiet, patch.object(probe.httpx, 'Client') as host, \
                redirect_stdout(StringIO()):
            self.assertEqual(probe.main(test_commands=True, publication_capabilities=True), 0)
        sim.observe.assert_called_once_with('original-owned-workflow', 'original-unknown-run')
        sim.execute.assert_not_called(); sim.resume.assert_not_called()
        quiet.assert_not_called(); host.assert_not_called(); sim.close.assert_called_once()
        self.assertEqual((directory / 'state.json').read_bytes(), before)
        observation = json.loads((directory / 'observation.json').read_text())
        self.assertEqual(observation['newSubmissions'], 0)
        self.assertEqual(observation['nativeWritesThisInvocation'], 0)

    def test_partial_existing_new_directory_never_creates_fresh_state_or_contacts_native(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root=Path(temporary.name)
        directory=root/'var/publication-ready-flow'; directory.mkdir(parents=True,mode=0o700)
        with patch.object(probe,'DATA',probe.DATA),patch.object(probe,'ROOT',root), \
                patch.object(probe,'SimClient') as native:
            with self.assertRaisesRegex(RuntimeError,'requires_reconciliation'):
                probe.main(test_commands=True,publication_capabilities=True)
            native.assert_not_called()
        self.assertEqual(list(directory.iterdir()),[])

    def test_candidate_reuses_actual_bindings_and_secrets_paths_without_granting_readiness(self):
        bindings={probe.BINDING:{'projectId':probe.PROJECT,'roleAgentId':probe.ROLE,
                                'roleRevision':'qwenpaw-role-v1:'+'a'*64,
                                'verificationPolicy':{'schemaVersion':2,'policyId':'actual-policy'}}}
        settings=probe.candidate_settings('/fixture','/fixture/var/publication-ready-flow',
            workspace='/fixture/workspace',base='http://127.0.0.1:52403',bindings=bindings,
            image='sha256:'+'b'*64,memory_query='当前原生召回查询',author_token='test-author',
            runner_token='test-runner',sim_workspace='existing-sim-workspace',installation='actual-installation')
        self.assertEqual(set(settings),{'service.json','execution.json','authoring.json'})
        service=settings['service.json']; execution=settings['execution.json']['gateways'][0]
        self.assertEqual(service['bindings'],bindings)
        self.assertNotIn('readinessFile',service)
        self.assertNotIn('controlled_execution',service)
        self.assertEqual(service['sim']['installationId'],'actual-installation')
        self.assertEqual(service['sim']['journal'],'sim.sqlite')
        self.assertEqual(service['taskFlowFile'],'task-flow.json')
        self.assertEqual(service['verification']['testExecutor']['imageId'],'sha256:'+'b'*64)
        self.assertEqual(execution['roleBindingId'],probe.BINDING)
        self.assertEqual(execution['roleAgentId'],probe.ROLE)
        self.assertEqual(execution['hostCredentialsFile'],'/fixture/var/qwenpaw-secrets/local-login.json')
        self.assertEqual(execution['serviceTokenFile'],'/fixture/var/qwenpaw-secrets/execution-service-token')
        self.assertNotIn('password',json.dumps(settings))
        self.assertEqual(settings['authoring.json']['workspaces'],{probe.PROJECT:'existing-sim-workspace'})

    def test_publication_reuse_checks_three_original_sources_without_changing_other_modes(self):
        root=Path('/fixture')
        original,sources=probe.historical_sources(root,publication_capabilities=True)
        self.assertEqual(original,root/'var/pinned-role-flow')
        self.assertEqual(sources,[root/'var'/name for name in (
            'pinned-role-flow','pinned-test-flow-known-template','pinned-test-failure-known-template')])
        self.assertEqual(probe.historical_sources(root),(original,[original]))
        self.assertEqual(probe.historical_sources(root,test_failure=True),
                         (sources[1],[sources[0],sources[1]]))
        self.assertEqual(probe.historical_sources(root,test_cancel=True),
                         (sources[2],sources))

    def test_each_historical_parent_requires_matching_independent_proof_and_completed_child(self):
        temporary=tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root=Path(temporary.name)
        _,sources=probe.historical_sources(root,publication_capabilities=True)
        workflow='fc628016-3198-4921-ae23-ad3e35c3fd51'
        for index,source in enumerate(sources):
            source.mkdir(parents=True,mode=0o700)
            case={'result':'passed_live','runId':'parent-'+str(index),'finalRunId':'child-'+str(index)}
            write(source/'state.json',{'result':'passed_live','workflowId':workflow,'cases':{'one':case}})
            write(source/'report.json',{'status':'passed_live','cases':{'one':case}})
            write(source/'root-check.json',{'status':'passed_read_only','workflowId':workflow,
                'rootRunId':case['runId'],'childRunId':case['finalRunId']})
        native=Mock(); native.observe.return_value={'status':'completed'}
        self.assertEqual(probe.checked_historical_parents(sources,native),{'parent-0','parent-1','parent-2'})
        self.assertEqual(native.observe.call_count,3)
        native.execute.assert_not_called(); native.resume.assert_not_called()
        native.observe.return_value={'status':'paused'}
        with self.assertRaisesRegex(RuntimeError,'child_not_completed'):
            probe.checked_historical_parents(sources,native)
        native.observe.return_value={'status':'completed'}
        check=json.loads((sources[2]/'root-check.json').read_text());check['childRunId']='wrong-child'
        write(sources[2]/'root-check.json',check)
        with self.assertRaisesRegex(RuntimeError,'run_identity_mismatch'):
            probe.checked_historical_parents(sources,native)

    def test_known_history_accepts_only_proved_paused_parents_and_existing_terminal_cancel(self):
        workflow='owned-workflow'; parents={'parent-0','parent-1','parent-2'}
        history=[{'workflowId':workflow,'runId':parent,'status':'paused'} for parent in sorted(parents)]
        history.append({'workflowId':workflow,'runId':'cancel-root','status':'cancelled'})
        probe.check_known_history(history,workflow,parents)
        history.append({'workflowId':workflow,'runId':'unknown','status':'paused'})
        with self.assertRaisesRegex(RuntimeError,'unresolved_run'):
            probe.check_known_history(history,workflow,parents)
        history.pop();history[0]['status']='running'
        with self.assertRaisesRegex(RuntimeError,'unresolved_run'):
            probe.check_known_history(history,workflow,parents)


if __name__ == '__main__':
    unittest.main()
