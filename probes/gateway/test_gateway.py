"""Local proxy contract tests. Mock transport is not browser evidence."""
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location('gateway', Path(__file__).resolve().parents[2] / 'deploy/gateway/gateway.py')
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)

class FixtureStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: real transport fixture\n\n'

class GatewayTests(unittest.TestCase):
    def test_distinct_routes(self):
        self.assertEqual(gateway.target('/api/auth/test'), ('http://127.0.0.1:3190', '/api/auth/test'))
        self.assertEqual(gateway.target('/socket.io/')[0], 'http://127.0.0.1:3192')
        with patch.object(gateway.Path, 'read_text', return_value='{"port": 12345}'):
            self.assertEqual(gateway.target('/qwenpaw/api/version'), ('http://127.0.0.1:12345', '/api/version'))
    def test_hop_headers(self):
        self.assertEqual(gateway.clean_headers([('Connection','X-Remove'), ('X-Remove','secret'),('Content-Security-Policy',"frame-ancestors 'self'")]), [('Content-Security-Policy',"frame-ancestors 'self'")])
    def test_proxy_preserves_security_and_cookies(self):
        def handler(request):
            self.assertEqual(request.headers['origin'], 'http://testserver')
            self.assertEqual(request.headers['x-forwarded-for'], 'testclient')
            return httpx.Response(200, stream=FixtureStream(), headers=[('content-type','text/event-stream'), ('x-frame-options','SAMEORIGIN'),('content-security-policy',"frame-ancestors 'self'"),('set-cookie','a=1; Path=/'),('set-cookie','b=2; Path=/')])
        with TestClient(gateway.app) as client:
            original = gateway.app.state.client
            gateway.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            response = client.get('/api/test', headers={'Origin':'http://testserver','x-forwarded-for':'attacker'})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.headers['x-frame-options'],'SAMEORIGIN')
            self.assertEqual(len(response.headers.get_list('set-cookie')),2)
            self.assertIn('data: real transport fixture',response.text)
            gateway.app.state.client = original
    def test_cookies_never_cross_requests(self):
        observed=[]
        def handler(request):
            observed.append(request.headers.get('cookie'))
            return httpx.Response(200,stream=FixtureStream(),headers=[('set-cookie','session=upstream-secret; Path=/'),('set-cookie','other=second; Path=/')])
        with TestClient(gateway.app) as client:
            original=gateway.app.state.client
            # Even a pre-contaminated shared client must not inject its jar.
            gateway.app.state.client=httpx.AsyncClient(transport=httpx.MockTransport(handler),cookies={'session':'contaminated'})
            for cookie in ['session=alice','session=bob',None]:
                client.cookies.clear()
                response=client.get('/api/test',headers={'cookie':cookie} if cookie else {})
                self.assertEqual(len(response.headers.get_list('set-cookie')),2)
            self.assertEqual(observed,['session=alice','session=bob',None])
            gateway.app.state.client=original

    def test_public_config_contains_no_workspace_or_credentials(self):
        with TestClient(gateway.app) as client:
            self.assertEqual(client.get('/agent-line-gateway/config').json(),
                             {'simEditorPath':'/workspace', 'workbenchPath':'/workbench/'})

    def test_assets_do_not_fallback(self):
        with TestClient(gateway.app) as client:
            self.assertEqual(client.get('/qwenpaw/assets/not-real.js').status_code,404)
            self.assertEqual(client.get('/qwenpaw/agents').status_code,200)

if __name__ == '__main__':
    unittest.main()
