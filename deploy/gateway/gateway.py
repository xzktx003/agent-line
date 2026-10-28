"""Same-origin deployment wrapper; no upstream source or response-body rewriting."""
import asyncio
import json
from http.cookiejar import CookieJar, DefaultCookiePolicy
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).resolve().parent / 'qwen-console'
HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
       'te', 'trailer', 'transfer-encoding', 'upgrade'}


def target(path):
    if path == '/qwenpaw/api' or path.startswith('/qwenpaw/api/'):
        state = json.loads((ROOT / 'var/qwenpaw/agent-line-process.json').read_text())
        return f"http://127.0.0.1:{int(state['port'])}", path[len('/qwenpaw'):]
    if path.startswith('/socket.io/'):
        return 'http://127.0.0.1:3192', path
    if path.startswith(('/extension/v1/', '/workbench/')):
        return 'http://127.0.0.1:3193', path
    return 'http://127.0.0.1:3190', path


def clean_headers(headers):
    blocked = HOP | {v.strip().lower() for k, value in headers if k.lower() == 'connection' for v in value.split(',')}
    return [(k, v) for k, v in headers if k.lower() not in blocked]


def forwarded(scope):
    headers = clean_headers(list(scope.headers.items()))
    headers = [(k, v) for k, v in headers if k.lower() not in {'host', 'forwarded'} and not k.lower().startswith('x-forwarded-')]
    headers.extend([('x-forwarded-host', scope.headers.get('host', '')),
                    ('x-forwarded-proto', scope.url.scheme.replace('ws', 'http')),
                    ('x-forwarded-for', scope.client.host if scope.client else '127.0.0.1')])
    return headers


class NoSharedCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=None), follow_redirects=False, trust_env=False, cookies=CookieJar(policy=NoSharedCookies())) as client:
        app.state.client = client
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get('/agent-line-gateway/config')
async def public_config():
    return {'simEditorPath': '/workspace', 'workbenchPath': '/workbench/'}


@app.api_route('/{path:path}', methods=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
async def proxy(request: Request, path: str):
    fullpath = '/' + path
    if fullpath == '/qwenpaw' or (fullpath.startswith('/qwenpaw/') and fullpath != '/qwenpaw/api' and not fullpath.startswith('/qwenpaw/api/')):
        relative = fullpath.removeprefix('/qwenpaw').lstrip('/')
        candidate = (STATIC / relative).resolve()
        if not candidate.is_relative_to(STATIC.resolve()):
            return Response(status_code=404)
        if not candidate.is_file():
            if relative.startswith('assets/'):
                return Response(status_code=404)
            candidate = STATIC / 'index.html'
        return FileResponse(candidate)
    origin, upstream_path = target(fullpath)
    url = origin + upstream_path + ('?' + request.url.query if request.url.query else '')
    try:
        outgoing = httpx.Request(request.method, url, headers=forwarded(request), content=request.stream())
        upstream = await request.app.state.client.send(outgoing, stream=True)
    except (httpx.HTTPError, OSError, ValueError):
        return Response('上游服务暂不可用', status_code=502, media_type='text/plain')
    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
    response = StreamingResponse(body(), status_code=upstream.status_code)
    response.raw_headers = [(k.encode('latin-1'), v.encode('latin-1')) for k, v in clean_headers(upstream.headers.multi_items())]
    return response


@app.websocket('/{path:path}')
async def websocket_proxy(socket: WebSocket, path: str):
    origin, upstream_path = target('/' + path)
    url = origin.replace('http://', 'ws://') + upstream_path + ('?' + socket.url.query if socket.url.query else '')
    headers = [(k, v) for k, v in forwarded(socket) if not k.lower().startswith('sec-websocket-') and k.lower() != 'origin']
    protocols = [s.strip() for s in socket.headers.get('sec-websocket-protocol', '').split(',') if s.strip()]
    try:
        async with connect(url, origin=socket.headers.get('origin'), additional_headers=headers, subprotocols=protocols or None, max_size=8*1024*1024, proxy=None) as upstream:
            await socket.accept(subprotocol=upstream.subprotocol)
            async def to_upstream():
                while True:
                    event = await socket.receive()
                    if event['type'] == 'websocket.disconnect':
                        return
                    await upstream.send(event.get('text') if event.get('text') is not None else event['bytes'])
            async def to_browser():
                async for value in upstream:
                    if isinstance(value, bytes):
                        await socket.send_bytes(value)
                    else:
                        await socket.send_text(value)
            tasks = [asyncio.create_task(to_upstream()), asyncio.create_task(to_browser())]
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except (OSError, ConnectionClosed, InvalidHandshake, WebSocketDisconnect):
        pass
    finally:
        try:
            await socket.close()
        except RuntimeError:
            pass
