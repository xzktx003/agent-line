"""The isolated parent needs the operator's network route, not extra secrets."""
from pathlib import Path
import unittest
from unittest.mock import patch
from probes.qwenpaw.luna_candidate import private_env


class CandidateEnvironmentTests(unittest.TestCase):
    def test_proxy_route_is_preserved_without_unrelated_secret_environment(self):
        proxy_names=('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY',
                     'http_proxy','https_proxy','all_proxy','no_proxy')
        configured={name:'fixture-route-'+name for name in proxy_names}
        with patch.dict('os.environ',{**configured,'UNRELATED_API_KEY':'must-not-forward','HOME':'/fixture-home'},clear=True):
            env=private_env(Path('/fixture-install'))
        self.assertEqual({name:env.get(name) for name in proxy_names},configured)
        self.assertNotIn('UNRELATED_API_KEY',env)
        self.assertEqual(env['QWENPAW_WORKING_DIR'],'/fixture-install/var/qwenpaw')

    def test_unconfigured_proxy_is_not_invented(self):
        with patch.dict('os.environ',{'PATH':'/fixture-bin'},clear=True):
            env=private_env(Path('/fixture-install'))
        self.assertNotIn('HTTP_PROXY',env)
        self.assertNotIn('NO_PROXY',env)
