"""Default guarded and explicit ready expectations must stay distinct."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from probes.gateway.workbench_plugin_probe import check_health, private_json


class WorkbenchPluginProbeTests(unittest.TestCase):
    def test_default_preserves_guarded_requirement(self):
        check_health({'readyForExecution': False, 'controlledExecution': 'unknown'})
        with self.assertRaisesRegex(RuntimeError, 'must_remain_disabled'):
            check_health({'readyForExecution': True, 'controlledExecution': 'confirmed'})

    def test_ready_requires_boolean_and_confirmed_execution(self):
        check_health({'readyForExecution': True, 'controlledExecution': 'confirmed'}, expect_ready=True)
        for health in ({'readyForExecution': False, 'controlledExecution': 'confirmed'},
                       {'readyForExecution': 1, 'controlledExecution': 'confirmed'},
                       {'readyForExecution': True, 'controlledExecution': 'unknown'}, {}):
            with self.assertRaisesRegex(RuntimeError, 'not_confirmed_ready'):
                check_health(health, expect_ready=True)

    def test_private_configuration_rejects_public_file_and_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps({'nonsecret': 'test'}))
            path.chmod(0o600)
            self.assertEqual(private_json(path), {'nonsecret': 'test'})
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'private_regular_configuration_required'):
                private_json(path)
            path.chmod(0o600)
            link = Path(folder) / 'link.json'
            os.symlink(path, link)
            with self.assertRaisesRegex(ValueError, 'private_regular_configuration_required'):
                private_json(link)


if __name__ == '__main__':
    unittest.main()
