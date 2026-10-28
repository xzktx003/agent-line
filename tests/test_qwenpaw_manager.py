import importlib.util
import errno
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('qwen_manager', Path(__file__).resolve().parents[1] / 'deploy/qwenpaw/manage.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class ManagerPortTests(unittest.TestCase):
    def server_time_wait_port(self, *, reuse=True):
        """Make the temporary server actively close an accepted connection."""
        with socket.socket() as listener, socket.socket() as client:
            listener.settimeout(2)
            client.settimeout(2)
            if reuse:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            client.connect(('127.0.0.1', port))
            accepted, _ = listener.accept()
            with accepted:
                accepted.shutdown(socket.SHUT_WR)
                self.assertEqual(client.recv(1), b'')
            # Client closes after the server FIN; the server owns TIME_WAIT.
        return port

    def test_first_start_allocates_and_restart_reuses_port(self):
        port = module.available_port()
        self.assertGreater(port, 0)
        self.assertEqual(module.available_port({'port': port}), port)

    def test_occupied_recorded_port_fails_instead_of_changing_endpoint(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            with self.assertRaises(OSError):
                module.available_port({'port': sock.getsockname()[1]})

    def test_restart_reuses_real_loopback_time_wait_port(self):
        port = self.server_time_wait_port()
        # Reproduce the old manager's bare-bind failure with all sockets closed.
        with socket.socket() as probe:
            with self.assertRaises(OSError) as caught:
                probe.bind(('127.0.0.1', port))
        self.assertEqual(caught.exception.errno, errno.EADDRINUSE)
        self.assertEqual(module.available_port({'port': port}), port)

    def test_live_reuse_listener_still_blocks_original_endpoint(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            with self.assertRaises(OSError) as caught:
                module.available_port({'port': listener.getsockname()[1]})
            self.assertEqual(caught.exception.errno, errno.EADDRINUSE)

    def test_nonreuse_time_wait_still_blocks_original_endpoint(self):
        port = self.server_time_wait_port(reuse=False)
        with self.assertRaises(OSError) as caught:
            module.available_port({'port': port})
        self.assertEqual(caught.exception.errno, errno.EADDRINUSE)

    def test_invalid_record_never_silently_allocates_new_endpoint(self):
        for port in (None, 0, -1, 65536, True, '1234'):
            with self.subTest(port=port), self.assertRaises(ValueError):
                module.available_port({'port': port})


class ConfiguredProjectPortTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'var/adapter').mkdir(parents=True)
        self.root_patch = patch.object(module, 'ROOT', self.root)
        self.root_patch.start(); self.addCleanup(self.root_patch.stop)
        self.marker = self.root / 'var/single-project-setup.json'
        self.spec = self.root / 'var/adapter/project-spec.json'

    def write(self, path, value):
        path.write_text(json.dumps(value)); path.chmod(0o600)

    def configured(self, origin='http://127.0.0.1:52405'):
        self.write(self.marker, {'phase': 'configured'})
        self.write(self.spec, {'qwenpaw': {'baseURL': origin}})

    def test_legacy_installs_keep_original_first_start_and_restart_semantics(self):
        self.assertIsNone(module.project_endpoint(None))
        old = {'port': 12345, 'pid': 1}
        self.assertIs(module.project_endpoint(old), old)

    def test_fresh_project_first_start_uses_configured_endpoint_and_reuses_record(self):
        self.configured()
        self.assertEqual(module.project_endpoint(None), {'port': 52405})
        old = {'port': 52405, 'host': '127.0.0.1', 'pid': 1}
        self.assertIs(module.project_endpoint(old), old)

    def test_previous_endpoint_conflict_never_moves_old_host(self):
        self.configured()
        for previous in ({'port': 12345}, {'port': 52405, 'host': '0.0.0.0'}):
            with self.assertRaises(ValueError): module.project_endpoint(previous)

    def test_incomplete_or_missing_private_project_configuration_is_not_startable(self):
        for phase in ('installing', 'incomplete', None):
            self.write(self.marker, {'phase': phase})
            with self.assertRaises(ValueError): module.project_endpoint(None)
        self.configured(); self.spec.chmod(0o644)
        with self.assertRaises(ValueError): module.project_endpoint(None)

    def test_unsupported_listener_is_not_silently_normalized(self):
        for origin in ('http://localhost:5000', 'http://127.0.0.1', 'http://127.0.0.1:0',
                       'https://127.0.0.1:5000', 'http://user:pass@127.0.0.1:5000',
                       'http://127.0.0.1:5000/api'):
            self.configured(origin)
            with self.subTest(origin=origin), self.assertRaises(ValueError): module.project_endpoint(None)
