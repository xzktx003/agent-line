"""Managed memory_search filtering. No live QwenPaw process is restarted."""
import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_line.memory_applicability import MemoryApplicability

ROOT = Path(__file__).resolve().parents[1] / "plugins/agent-line"


def load_plugin():
    spec = importlib.util.spec_from_file_location("applicability_plugin_under_test", ROOT / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(text):
    return hashlib.sha256(text.strip().encode()).hexdigest()


class GuardFilterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = load_plugin()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MemoryApplicability.initialize(Path(self.tmp.name) / "applicability.db")
        self.body = "verified project constraint"
        self.store.classify(
            role_id="role-1", project_id="project-a", experience_id="exp-1",
            content_sha256=digest(self.body), state="deleted", reason="user deleted",
        )
        self.contract = self.plugin.DelegationContract({
            "managed_role_ids": ["role-1"], "codex_target_agent_id": "codex",
        })
        self.contract.applicability = self.store
        self.contract.role_projects = {"role-1": "project-a"}

    def test_unmapped_role_keeps_the_original_search_behavior(self):
        guard = self.contract.middleware(SimpleNamespace(agent_id="role-1"), None)
        self.assertIs(guard.applicability, self.store)
        other = self.plugin.DelegationContract({
            "managed_role_ids": ["role-2"], "codex_target_agent_id": "codex",
        })
        other.applicability = self.store
        other.role_projects = {"role-1": "project-a"}
        guard = other.middleware(SimpleNamespace(agent_id="role-2"), None)
        self.assertIsNone(guard.applicability)
        self.assertIsNone(guard.project_id)

    async def test_memory_search_result_drops_deleted_section_only(self):
        guard = self.contract.middleware(SimpleNamespace(agent_id="role-1"), None)
        answer = "\n".join([
            "========== memory/old.md:1-1 [score=1] ==========",
            self.body,
            "========== memory/new.md:1-1 [score=1] ==========",
            "native unmarked",
        ])
        chunk = SimpleNamespace(content=[SimpleNamespace(text=answer)])

        async def downstream():
            yield chunk

        items = [item async for item in guard.on_acting(None, {"tool_call": {"name": "memory_search"}}, downstream)]
        self.assertEqual(items, [chunk])
        self.assertNotIn(self.body, chunk.content[0].text)
        self.assertIn("native unmarked", chunk.content[0].text)

    async def test_broken_ledger_denies_search_before_the_native_call(self):
        self.contract.applicability = self.plugin.UnavailableApplicability()
        guard = self.contract.middleware(SimpleNamespace(agent_id="role-1"), None)
        called = []

        async def downstream():
            called.append(True)
            yield "secret"

        with self.assertRaises(PermissionError):
            async for _ in guard.on_acting(None, {"tool_call": {"name": "memory_search"}}, downstream):
                pass
        self.assertEqual(called, [])

    async def test_auto_injected_memory_search_output_is_rewritten(self):
        guard = self.contract.middleware(SimpleNamespace(agent_id="role-1"), None)
        part = SimpleNamespace(text=self.body)
        message = SimpleNamespace(
            name="memory_search",
            content=[SimpleNamespace(name="memory_search", output=[part], text=None)],
        )
        kept = guard._without_forbidden_exact([message, SimpleNamespace(name="user", content="prefix " + self.body)])
        self.assertEqual(part.text, "(no memory results)")
        self.assertEqual(len(kept), 2)
        self.assertEqual(kept[1].content, "prefix " + self.body)

    async def test_without_ledger_configuration_search_text_is_unchanged(self):
        plain = self.plugin.ManagedRoleToolGuard(None, "role-1")
        chunk = SimpleNamespace(content=[SimpleNamespace(text=self.body)])

        async def downstream():
            yield chunk

        items = [item async for item in plain.on_acting(None, {"tool_call": {"name": "memory_search"}}, downstream)]
        self.assertEqual(items, [chunk])
        self.assertEqual(chunk.content[0].text, self.body)
