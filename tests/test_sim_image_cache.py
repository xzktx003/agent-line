"""Preparing the first image must not create what looks like an existing install."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / 'deploy/sim/pull-via-proxy.py'
spec = importlib.util.spec_from_file_location('sim_image_cache_tests', SOURCE)
puller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(puller)


class SimImageCacheTests(unittest.TestCase):
    def test_fresh_image_preparation_does_not_create_installation_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(puller, '__file__', str(root / 'deploy/sim/pull-via-proxy.py')), \
                    patch('sys.argv', ['pull-via-proxy.py', 'postgres']), \
                    patch.object(puller, 'open_with_retry', side_effect=RuntimeError('stop before network')):
                with self.assertRaisesRegex(RuntimeError, 'stop before network'):
                    puller.main()
            self.assertFalse((root / 'var/sim').exists())
            self.assertTrue((root / 'var/image-cache/sim').is_dir())

    def test_existing_verified_download_cache_is_reused_without_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / 'var/sim/image-cache'
            legacy.mkdir(parents=True)
            sentinel = legacy / 'existing-layer'
            sentinel.write_bytes(b'preserve')
            with patch.object(puller, '__file__', str(root / 'deploy/sim/pull-via-proxy.py')), \
                    patch('sys.argv', ['pull-via-proxy.py', 'postgres']), \
                    patch.object(puller, 'open_with_retry', side_effect=RuntimeError('stop before network')):
                with self.assertRaises(RuntimeError):
                    puller.main()
            self.assertEqual(sentinel.read_bytes(), b'preserve')
            self.assertFalse((root / 'var/image-cache').exists())
