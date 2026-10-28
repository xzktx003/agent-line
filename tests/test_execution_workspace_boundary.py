import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1] / 'plugins/agent-line'
spec = importlib.util.spec_from_file_location('execution_workspace_boundary', ROOT / 'execution_bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class ExecutionWorkspaceBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.role = Path(self.tmp.name) / 'role'
        self.role.mkdir()
        self.project = self.role / 'projects' / 'native-memory-roundtrip'
        self.project.mkdir(parents=True)
        self.outside = Path(self.tmp.name) / 'other-project'
        self.outside.mkdir()

    def test_role_workspace_and_project_subdirectory_are_allowed(self):
        self.assertTrue(bridge.execution_workspace_within_role(self.role, self.role))
        self.assertTrue(bridge.execution_workspace_within_role(self.role, self.project))

    def test_parent_sibling_and_symlink_are_refused(self):
        self.assertFalse(bridge.execution_workspace_within_role(self.role, self.role.parent))
        self.assertFalse(bridge.execution_workspace_within_role(self.role, self.outside))
        link = self.role / 'escape'
        link.symlink_to(self.outside, target_is_directory=True)
        self.assertFalse(bridge.execution_workspace_within_role(self.role, link))
        hidden = self.role / 'hidden-link'
        hidden.symlink_to(self.project, target_is_directory=True)
        self.assertFalse(bridge.execution_workspace_within_role(self.role, hidden))

    def test_target_config_accepts_subdirectory_and_rejects_escape(self):
        owner = bridge.ExecutionBridge.__new__(bridge.ExecutionBridge)
        owner.workspace = self.project.resolve()
        owner.target = 'codex'
        owner.container_launcher = None
        inside = SimpleNamespace(backend='codex', workspace_dir=str(self.role), backend_settings={})
        with patch('qwenpaw.config.utils.load_agent_config', return_value=inside):
            self.assertIs(owner.target_config(), inside)
        outside = SimpleNamespace(backend='codex', workspace_dir=str(self.outside), backend_settings={})
        with patch('qwenpaw.config.utils.load_agent_config', return_value=outside):
            with self.assertRaises(bridge.HTTPException) as caught:
                owner.target_config()
        self.assertEqual(caught.exception.status_code, 409)


if __name__ == '__main__':
    unittest.main()
