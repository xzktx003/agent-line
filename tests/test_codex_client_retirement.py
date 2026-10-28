"""Exercise the pinned official lifecycle with delayed EOF and no executor."""
import asyncio
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / 'upstream/qwenpaw/src/qwenpaw/harnesses'


def official_module(name, relative_path):
    """Load the checked-out source; relative support imports use installed QwenPaw."""
    path = HARNESS / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DelayedEOF:
    """Keep the old pipe reader alive until after a new client is admitted."""
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.canceled = asyncio.Event()

    async def readline(self):
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            # Official stop cancels but does not join the reader. Exercise its
            # late finally deterministically, even after cancellation delivery.
            self.canceled.set()
            await self.release.wait()
        return b''


class CodexClientRetirementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = official_module(
            'qwenpaw.harnesses.codex._agent_line_retirement_server',
            'codex/app_server.py')
        self.adapter = official_module(
            'qwenpaw.harnesses.codex._agent_line_retirement_adapter',
            'codex/adapter.py')
        self.runtime = official_module(
            'qwenpaw.harnesses._agent_line_retirement_runtime', 'runtime.py')
        # All lifecycle methods below come from the fixed checkout. Replace
        # only imported constructor bindings in these isolated test modules.
        self.adapter.CodexAppServerClient = self.server.CodexAppServerClient
        self.runtime.create_adapter = lambda provider, state_dir, settings: (
            self.adapter.CodexAdapter(state_dir))
        self.no_process = AsyncMock(side_effect=AssertionError(
            'Retirement regression must not start a process or a model'))
        self.process_patch = patch('asyncio.create_subprocess_exec', self.no_process)
        self.process_patch.start()
        self.addCleanup(self.process_patch.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    async def asyncTearDown(self):
        self.no_process.assert_not_awaited()

    async def old_reader(self, client):
        pipe = DelayedEOF()
        # returncode=0 represents the already-exited/reaped launcher: positive
        # process exit alone does not establish that stdout finally has run.
        client._process = SimpleNamespace(stdout=pipe, returncode=0)
        reader = asyncio.create_task(client._read_stdout())
        client._reader_task = reader
        await asyncio.wait_for(pipe.started.wait(), timeout=2)
        return pipe, reader

    async def test_old_reader_can_fail_new_generation_pending_on_same_client(self):
        client = self.server.CodexAppServerClient()
        pipe, reader = await self.old_reader(client)
        new_pending = asyncio.get_running_loop().create_future()
        # start() replaces the process on an exited client without joining its
        # old reader. Model that post-start state without launching anything.
        client._process = SimpleNamespace(returncode=None)
        client._pending[2] = new_pending
        pipe.release.set()
        await asyncio.wait_for(reader, timeout=2)
        with self.assertRaisesRegex(self.server.CodexAppServerError,
                                    '^Codex app-server exited$'):
            await asyncio.wait_for(new_pending, timeout=2)

    async def test_official_runtime_stop_isolates_next_adapter_from_old_reader(self):
        runtime = self.runtime.HarnessRuntime(Path(self.directory.name))
        previous_adapter = await runtime.adapter('codex', {})
        previous_client = previous_adapter._client
        pipe, reader = await self.old_reader(previous_client)
        try:
            # Use the real public upstream API, including real CodexAdapter.stop
            # and CodexAppServerClient.stop; no private lifecycle method patched.
            await runtime.stop()
            await asyncio.wait_for(pipe.canceled.wait(), timeout=2)
            self.assertFalse(reader.done())
            next_adapter = await runtime.adapter('codex', {})
            next_client = next_adapter._client
            self.assertIsNot(next_adapter, previous_adapter)
            self.assertIsNot(next_client, previous_client)
            self.assertIs(await runtime.adapter('codex', {}), next_adapter)
            next_pending = asyncio.get_running_loop().create_future()
            next_client._pending[2] = next_pending

            pipe.release.set()
            await asyncio.wait_for(reader, timeout=2)
            self.assertFalse(next_pending.done())
            await next_client._dispatch({'id': 2, 'result': {'generation': 'next'}})
            self.assertEqual(await next_pending, {'generation': 'next'})
        finally:
            pipe.release.set()
            await asyncio.wait_for(reader, timeout=2)
            await runtime.stop()


if __name__ == '__main__':
    unittest.main()
