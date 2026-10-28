import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from probes.upgrade.qwenpaw_semantics import once, run


class UpgradeSemanticsTests(unittest.TestCase):
    def test_write_intent_survives_lost_receipt_and_never_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'state.json'
            state = {'intents': {}}
            calls = []
            def lost():
                calls.append(1)
                raise TimeoutError()
            with self.assertRaises(TimeoutError): once(state, path, 'create', lost)
            reopened = json.loads(path.read_text())
            with self.assertRaises(RuntimeError): once(reopened, path, 'create', lost)
            self.assertEqual(calls, [1])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_received_write_is_not_repeated(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'state.json'
            state = {'intents': {}}
            self.assertEqual(once(state, path, 'save', lambda: {'outcome': 'saved'}), {'outcome': 'saved'})
            with self.assertRaises(RuntimeError): once(state, path, 'save', lambda: self.fail('Repeated'))

    def test_existing_attempt_only_observes_saved_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root/'var/upgrade-runtime/existing'
            directory.mkdir(parents=True)
            (directory/'report.json').write_text(json.dumps({'phase': 'unknown', 'verified': False}))
            with patch('probes.upgrade.qwenpaw_semantics.ROOT', root), patch('probes.upgrade.qwenpaw_semantics.Runtime') as runtime:
                self.assertFalse(run('existing')['verified'])
                runtime.assert_not_called()

    def test_installed_frontend_must_match_current_plugin(self):
        from types import SimpleNamespace
        from probes.upgrade.qwenpaw_semantics import plugin_hashes
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            expected = root/'plugins/agent-line/dist/index.js'
            installed = root/'data/plugins/agent-line/dist/index.js'
            for p in (expected, installed):
                p.parent.mkdir(parents=True)
                p.write_text('same frontend')
            with patch('probes.upgrade.qwenpaw_semantics.ROOT', root):
                self.assertEqual(len(plugin_hashes(SimpleNamespace(data=root/'data'))['frontendSha256']), 64)
                installed.write_text('old frontend')
                with self.assertRaises(AssertionError): plugin_hashes(SimpleNamespace(data=root/'data'))
