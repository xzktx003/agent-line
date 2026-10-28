"""Stopping the adapter must not signal a process group."""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy' / 'executor'))
import launcher


class AdapterStopScopeTests(unittest.TestCase):
    def test_executor_client_is_outside_the_service_group(self):
        child = Mock()
        child.pid = 4242
        with patch.object(launcher.subprocess, 'Popen', return_value=child) as popen, \
             patch.object(launcher, 'start_ticks', return_value='101'):
            started = launcher.launch_executor(['docker', 'run'])
        self.assertIs(started, child)
        self.assertEqual(popen.call_args.args, (['docker', 'run'],))
        self.assertEqual(popen.call_args.kwargs['stdin'], subprocess.PIPE)
        self.assertEqual(popen.call_args.kwargs['stdout'], subprocess.PIPE)
        self.assertIs(popen.call_args.kwargs['start_new_session'], True)
        self.assertEqual(child.agent_line_start, '101')

    def test_stop_signals_only_the_same_client_pid(self):
        child = Mock()
        child.pid = 4242
        child.agent_line_start = '101'
        child.poll.return_value = None
        child.wait.side_effect = subprocess.TimeoutExpired(['docker'], 5)
        with patch.object(launcher, 'start_ticks', return_value='101'), \
             patch.object(launcher.os, 'killpg', side_effect=AssertionError('process group targeted')):
            launcher.stop_owned_executor(child)
        child.terminate.assert_called_once_with()
        child.kill.assert_called_once_with()
        child.wait.assert_called_once_with(timeout=5)

    def test_reused_or_unproven_pid_is_not_signaled(self):
        child = Mock()
        child.pid = 4242
        child.poll.return_value = None
        child.agent_line_start = '101'
        with patch.object(launcher, 'start_ticks', return_value='202'), \
             patch.object(launcher.os, 'killpg', side_effect=AssertionError('process group targeted')):
            launcher.stop_owned_executor(child)
        child.terminate.assert_not_called()
        child.kill.assert_not_called()
        child.agent_line_start = None
        with patch.object(launcher, 'start_ticks', return_value='101'), \
             patch.object(launcher.os, 'killpg', side_effect=AssertionError('process group targeted')):
            launcher.stop_owned_executor(child)
        child.terminate.assert_not_called()
        launcher.stop_owned_executor(None)

    def test_timeout_does_not_signal_a_changed_pid(self):
        child = Mock()
        child.pid = 4242
        child.agent_line_start = '101'
        child.poll.side_effect = [None, None]
        child.wait.side_effect = subprocess.TimeoutExpired(['docker'], 5)
        ticks = iter(['101', '303'])
        with patch.object(launcher, 'start_ticks', side_effect=lambda pid: next(ticks)), \
             patch.object(launcher.os, 'killpg', side_effect=AssertionError('process group targeted')):
            launcher.stop_owned_executor(child)
        child.terminate.assert_called_once_with()
        child.kill.assert_not_called()

    def test_shutdown_uses_the_owned_client_stop(self):
        source = (ROOT / 'deploy' / 'executor' / 'launcher.py').read_text()
        self.assertIn('child=launch_executor(command(sys.argv[1:],owner=owner))', source)
        self.assertIn('stop_owned_executor(child)', source)
        self.assertNotIn('os.killpg', source)


if __name__ == '__main__':
    unittest.main()
