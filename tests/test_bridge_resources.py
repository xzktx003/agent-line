"""Only identity-bound positive container exit releases an execution lease."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

spec = importlib.util.spec_from_file_location(
    "resource_bridge",
    Path(__file__).resolve().parents[1] / "plugins/agent-line/execution_bridge.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ResourcesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bridge = module.ExecutionBridge.__new__(module.ExecutionBridge)
        self.bridge.container_control = Path("/trusted/control.py")
        self.bridge.workspace = Path("/work")
        self.bridge.ledger = Mock()
        self.bridge.ledger.patch.side_effect = lambda task, **kw: kw
        self.bridge.retire_runtime = AsyncMock(return_value=True)
        self.record = {
            "terminalConfirmed": True,
            "identity": {"codexThreadId": "thread", "codexTurnId": "turn"},
        }
        self.proof = {
            "schemaVersion": 1,
            "resourcesReleased": True,
            "codexThreadId": "thread",
            "codexTurnId": "turn",
            "owner": "a" * 32,
            "containerId": "b" * 64,
            "exitReceipt": {
                "schemaVersion": 1,
                "owner": "a" * 32,
                "containerId": "b" * 64,
                "workspace": "/work",
                "identities": [{"codexThreadId": "thread", "codexTurnId": "turn"}],
                "running": False,
                "pid": 0,
                "status": "exited",
                "finishedAt": "2026-10-21T10:00:00Z",
                "source": "docker-inspect-before-remove",
            },
            "launcherExitProof": {
                "schemaVersion": 1, "source": "procfs-launcher-exit",
                "owner": "a" * 32, "containerId": "b" * 64,
                "pid": 123, "startTicks": "456", "uid": os.getuid(),
                "state": "absent", "observedAt": 1790000000.25,
            },
        }

    async def run_proof(self, proof, code=0):
        process = Mock(returncode=code)
        process.communicate = AsyncMock(return_value=(json.dumps(proof).encode(), b""))
        with patch.object(
            module.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
        ) as create:
            result = await self.bridge.release_resources("task", self.record)
            self.assertIn("--workspace", create.call_args.args)
            self.assertEqual(create.call_args.args[-1], "/work")
            return result

    async def test_positive_bound_exit(self):
        result = await self.run_proof(self.proof)
        self.assertTrue(result["resourcesReleased"])
        self.assertEqual(result["resourceExitReceipt"], self.proof["exitReceipt"])

    async def test_absent_or_untrusted_exit_cannot_release(self):
        variants = [({}, 0), (self.proof, 1)]
        for field, value in [
            ("workspace", "/another"),
            ("running", True),
            ("pid", 10),
            ("owner", "another"),
            ("containerId", "another"),
            ("finishedAt", ""),
            ("identities", []),
            ("source", "native-finished"),
        ]:
            proof = copy.deepcopy(self.proof)
            proof["exitReceipt"][field] = value
            variants.append((proof, 0))
        for proof, code in variants:
            with self.subTest(proof=proof, code=code):
                self.assertFalse(
                    (await self.run_proof(proof, code))["resourcesReleased"]
                )

    async def test_host_or_nonterminal_never_stops_container(self):
        with patch.object(
            module.asyncio, "create_subprocess_exec", AsyncMock()
        ) as create:
            self.record["terminalConfirmed"] = False
            self.assertFalse(
                (await self.bridge.release_resources("task", self.record))[
                    "resourcesReleased"
                ]
            )
            self.record["terminalConfirmed"] = True
            self.bridge.container_control = None
            self.assertFalse(
                (await self.bridge.release_resources("task", self.record))[
                    "resourcesReleased"
                ]
            )
            create.assert_not_called()
