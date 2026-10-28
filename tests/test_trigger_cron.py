"""Generated cron invokes the existing ingress without credentials in argv."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location('trigger_cron', Path(__file__).resolve().parents[1] / 'deploy/sim/trigger_cron.py')
cron = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cron)


class TriggerCronTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.json'
        self.token = 'private-test-token-' + 'a' * 40
        self.write(self.root / 'token', self.token)
        self.original = self.root / 'official-crontab'
        self.original.write_bytes(b'# official retained\nSHELL=/bin/sh\n*/1 * * * * existing-job\n')
        self.data = {'schemaVersion': 1, 'adapterBaseUrl': 'http://127.0.0.1:3193',
                     'tokenFile': 'token', 'jobs': [{'triggerId': 'daily', 'cron': '*/5 1-22/2 * 1,6 0-6',
                                                    'input': {'message': '中文\n"$(touch /tmp/nope)"', 'n': 1}}]}
        self.output = self.root / 'generated'
        self.save()

    def write(self, path, value):
        path.write_text(value)
        path.chmod(0o600)

    def save(self):
        self.write(self.config, json.dumps(self.data))

    def prepare(self, **kwargs):
        return cron.prepare(self.config, self.output, self.original, **kwargs)

    def test_preserves_official_bytes_and_generates_private_files_with_manifest(self):
        self.prepare()
        combined = (self.output / 'crontab').read_bytes()
        self.assertTrue(combined.startswith(self.original.read_bytes()))
        self.assertIn(b'*/5 1-22/2 * 1,6 0-6 /bin/sh /agent-line-triggers/send.sh 0\n', combined)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        manifest = json.loads((self.output / 'manifest.json').read_text())
        self.assertEqual(set(manifest['files']), {p.name for p in self.output.iterdir()} - {'manifest.json'})
        for path in self.output.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.name == 'send.sh' else 0o600)
            if path.name != 'manifest.json':
                self.assertEqual(manifest['files'][path.name], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertNotIn(self.token, (self.output / 'manifest.json').read_text())
        self.assertNotIn(self.token, (self.output / 'crontab').read_text())
        self.assertNotIn(self.token, (self.output / 'send.sh').read_text())

    def fake_environment(self):
        tools = self.root / 'bin'; tools.mkdir()
        (tools / 'date').write_text('#!/bin/sh\nprintf "%s\\n" "$FAKE_SLOT"\n')
        (tools / 'curl').write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE_ARGS"\ncat > "$CAPTURE_BODY"\nprintf "%s" "${FAKE_HTTP_STATUS:-202}"\nexit "${FAKE_CURL_EXIT:-0}"\n')
        for path in tools.iterdir(): path.chmod(0o700)
        return dict(os.environ, PATH=str(tools) + ':' + os.environ['PATH'],
                    FAKE_SLOT='2026-10-22T08:14:00Z', CAPTURE_ARGS=str(self.root / 'args'),
                    CAPTURE_BODY=str(self.root / 'body'))

    def test_shell_posts_stable_slot_and_input_without_argv_secret_or_network(self):
        self.prepare(); env = self.fake_environment()
        command = ['/bin/sh', str(self.output / 'send.sh'), '0']
        first = subprocess.run(command, env=env, capture_output=True, check=True)
        body = (self.root / 'body').read_bytes()
        self.assertEqual(json.loads(body), {'slot': env['FAKE_SLOT'], 'input': self.data['jobs'][0]['input']})
        args = (self.root / 'args').read_text().splitlines()
        self.assertEqual(args, ['-q', '--config', str(self.output / '0.curl'), '--data-binary', '@-', '--write-out', '%{http_code}'])
        self.assertNotIn(self.token, ' '.join(args))
        self.assertEqual(first.stdout + first.stderr, b'')
        subprocess.run(command, env=env, capture_output=True, check=True)
        self.assertEqual(body, (self.root / 'body').read_bytes())
        config = (self.output / '0.curl').read_text()
        self.assertIn('max-time = 20', config)
        self.assertIn('retry = 0', config)
        self.assertNotIn('location', config)

    def test_once_slot_skips_other_minutes_and_sends_only_matching_minute(self):
        self.data['jobs'][0]['onceSlot'] = '2026-10-22T08:15:00Z'; self.save(); self.prepare()
        env = self.fake_environment(); command = ['/bin/sh', str(self.output / 'send.sh'), '0']
        subprocess.run(command, env=env, check=True)
        self.assertFalse((self.root / 'args').exists())
        env['FAKE_SLOT'] = self.data['jobs'][0]['onceSlot']
        subprocess.run(command, env=env, check=True)
        self.assertEqual(json.loads((self.root / 'body').read_text())['slot'], env['FAKE_SLOT'])

    def test_curl_failure_is_nonzero_without_retry(self):
        self.prepare(); env = self.fake_environment(); env['FAKE_CURL_EXIT'] = '22'
        result = subprocess.run(['/bin/sh', str(self.output / 'send.sh'), '0'], env=env, capture_output=True)
        self.assertEqual(result.returncode, 22)
        self.assertEqual(result.stdout + result.stderr, b'')

    def test_redirect_or_unexpected_success_is_not_a_delivery_receipt(self):
        self.prepare(); env = self.fake_environment()
        for status in ('301', '200', '503'):
            env['FAKE_HTTP_STATUS'] = status
            result = subprocess.run(['/bin/sh', str(self.output / 'send.sh'), '0'], env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout + result.stderr, b'')

    def test_rejects_cron_injection_and_out_of_range_fields(self):
        for value in ('* * * * *;touch bad', '* * * * *\ncommand', '60 * * * *', '* 24 * * *',
                      '* * 0 * *', '* * * 13 *', '* * * * 7', '*/0 * * * *',
                      '10-2 * * * *', '@hourly', '*  * * * *', '* * * * */99', '1/2 * * * *'):
            with self.subTest(value=value), self.assertRaises(ValueError): cron.cron(value)

    def test_rejects_untrusted_or_non_origin_urls_and_allows_explicit_host(self):
        for value in ('http://evil.example:123', 'https://user:pass@trusted', 'https://trusted/',
                      'https://trusted?q=1', 'https://trusted#x', 'https://trusted?', 'ftp://trusted',
                      'https://trusted\nheader="bad"', 'http://127.0.0.1:0', 'https://{evil,other}.example', 'https://bad%0ahost'):
            with self.subTest(value=value), self.assertRaises(ValueError): cron.origin(value, ())
        self.assertEqual(cron.origin('http://172.18.0.1:3193', ('172.18.0.1',)), 'http://172.18.0.1:3193')
        self.assertEqual(cron.origin('https://trusted:443', ()), 'https://trusted:443')

    def test_refuses_existing_output_or_symlink_without_modifying_it(self):
        self.output.mkdir(); sentinel = self.output / 'mine'; sentinel.write_text('preserve')
        with self.assertRaises(FileExistsError): self.prepare()
        self.assertEqual(sentinel.read_text(), 'preserve')
        linked = self.root / 'linked'; linked.symlink_to(self.output, target_is_directory=True)
        with self.assertRaises(FileExistsError): cron.prepare(self.config, linked, self.original)

    def test_private_config_and_token_permissions_regular_files_and_owner(self):
        for path in (self.config, self.root / 'token'):
            path.chmod(0o644)
            with self.assertRaises(ValueError): self.prepare()
            path.chmod(0o600)
        with patch.object(cron.os, 'getuid', return_value=os.getuid() + 1):
            with self.assertRaises(ValueError): self.prepare()
        linked = self.root / 'linked-config'; linked.symlink_to(self.config)
        with self.assertRaises(ValueError): cron.prepare(linked, self.output, self.original)
        with self.assertRaises(ValueError): cron.private_read(self.root)

    def test_rejects_duplicate_schema_jobs_token_injection_or_non_object_input(self):
        self.write(self.config, '{"schemaVersion":1,"schemaVersion":1}')
        with self.assertRaises(ValueError): self.prepare()
        self.save()
        for bad in ('short', self.token + '\n', self.token + '"\nurl=evil', self.token + '\x00'):
            self.write(self.root / 'token', bad)
            with self.assertRaises(ValueError): self.prepare()
        self.write(self.root / 'token', self.token)
        cases = [dict(self.data, schemaVersion=True), dict(self.data, unknown=1), dict(self.data, jobs=[]),
                 dict(self.data, jobs=self.data['jobs'] * 2)]
        for value in cases:
            self.write(self.config, json.dumps(value))
            with self.assertRaises(ValueError): self.prepare()
        for name, bad in (('triggerId', 'bad;command'), ('input', []), ('onceSlot', '2026-02-31T01:00:00Z')):
            self.data['jobs'][0][name] = bad; self.save()
            with self.assertRaises(ValueError): self.prepare()
            self.setUpReplacementJob()

    def setUpReplacementJob(self):
        self.data['jobs'] = [{'triggerId': 'daily', 'cron': '* * * * *', 'input': {}}]


if __name__ == '__main__':
    unittest.main()
