import importlib.util
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "cancel_bridge_test", ROOT / "plugins/agent-line/execution_bridge.py"
)
bridge_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge_module)


class BridgeCancelTests(unittest.IsolatedAsyncioTestCase):
    def bridge(self, terminal=False):
        bridge = bridge_module.ExecutionBridge.__new__(bridge_module.ExecutionBridge)
        bridge.target = "fixed-role"
        bridge.workspace = Path("/fixture/workspace")
        record = {
            "terminalConfirmed": terminal,
            "resourcesReleased": False,
            "identity": {"upstreamTaskId": "native"},
            "status": "accepted",
        }
        bridge.ledger = Mock()
        bridge.ledger.get.return_value = (
            {
                "targetAgentId": "fixed-role",
                "workspace": "/fixture/workspace",
                "roleSessionId": "agent-line-task",
            },
            record,
        )
        bridge.observe = AsyncMock(return_value=record)
        bridge.target_config = Mock()
        return bridge

    async def test_stop_targets_bound_session_and_ack_is_not_completion(self):
        bridge = self.bridge()
        request = Request({"type": "http", "headers": [], "state": {}})
        with patch(
            "qwenpaw.app.routers.console.post_console_chat_stop", new_callable=AsyncMock
        ) as stop:
            stop.return_value = {"stopped": True}
            result = await bridge.cancel("task", request)
            self.assertEqual(stop.call_args.kwargs["chat_id"], "agent-line-task")
            self.assertEqual(stop.call_args.args[0].state.agent_id, "fixed-role")
            self.assertFalse(result["terminalConfirmed"])
            self.assertFalse(result["resourcesReleased"])
            bridge.ledger.patch.assert_any_call("task", cancellationRequested=True)

    async def test_terminal_task_is_not_interrupted(self):
        bridge = self.bridge(terminal=True)
        with patch(
            "qwenpaw.app.routers.console.post_console_chat_stop", new_callable=AsyncMock
        ) as stop:
            await bridge.cancel(
                "task", Request({"type": "http", "headers": [], "state": {}})
            )
            stop.assert_not_called()
