"""Container reachability must not mask a refused database write transaction."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / 'deploy/sim/health.py'
spec = importlib.util.spec_from_file_location('sim_health_write_tests', SOURCE)
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


class SimDatabaseHealthTests(unittest.TestCase):
    def report(self, probe):
        def run(arguments, **kwargs):
            if arguments[:2] == ['docker', 'inspect']:
                name = arguments[-1]
                state = ({'Status': 'exited', 'ExitCode': 0}
                         if any(part in name for part in ('migrations', 'uploads-init'))
                         else {'Status': 'running', 'Health': {'Status': 'healthy'}}
                         if 'cron' not in name else {'Status': 'running'})
                return SimpleNamespace(returncode=0, stdout=json.dumps(state), stderr='')
            self.assertEqual(arguments[:2], ['docker', 'exec'])
            self.assertIn('ON_ERROR_STOP=1', arguments)
            self.assertIn('-X', arguments)
            self.assertGreater(kwargs['timeout'], 0)
            if isinstance(probe, BaseException):
                raise probe
            return SimpleNamespace(returncode=probe, stdout='private-database-detail',
                                   stderr='private-password-or-query-error')

        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'var/sim').mkdir(parents=True)
            with patch.object(health, '__file__', str(root / 'deploy/sim/health.py')), \
                    patch.object(health.subprocess, 'run', side_effect=run), \
                    patch.object(health.urllib.request, 'build_opener') as opener, \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                opener.return_value.open.return_value = Response()
                with self.assertRaises(SystemExit) as stopped:
                    health.main()
                report = json.loads((root / 'var/sim/health.json').read_text())
                rendered = output.getvalue()
            self.assertNotIn('private-', rendered)
            self.assertNotIn('private-', json.dumps(report))
            return stopped.exception.code, report

    def test_reachable_services_are_unhealthy_when_write_transaction_is_refused(self):
        code, report = self.report(1)
        self.assertEqual(code, 1)
        self.assertFalse(report['healthy'])
        self.assertTrue(any(c.get('service') == 'database_write' and not c['passed']
                            for c in report['checks']))

    def test_write_probe_timeout_is_bounded_unhealthy_and_sanitized(self):
        code, report = self.report(subprocess.TimeoutExpired('private-command', 10,
                                                           output='private-output'))
        self.assertEqual(code, 1)
        self.assertFalse(report['healthy'])

    def test_normal_database_write_admission_keeps_report_healthy(self):
        code, report = self.report(0)
        self.assertEqual(code, 0)
        self.assertTrue(report['healthy'])
        self.assertTrue(any(c.get('service') == 'database_write' and c['passed']
                            for c in report['checks']))
