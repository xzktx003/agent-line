#!/usr/bin/env python3
"""Read-only source compatibility probe. Not a runtime or model acceptance test."""
import ast
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[2] / 'upstream/qwenpaw'
base = root / 'src/qwenpaw'
def source(path):
    return (base / path).read_text()
api = ast.parse(source('plugins/api.py'))
cls = next(n for n in api.body if isinstance(n, ast.ClassDef) and n.name == 'PluginApi')
methods = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
required = {'register_tool', 'register_http_router', 'register_middleware', 'register_memory_backend', 'register_shutdown_hook'}
console = source('app/routers/console.py')
codex = source('harnesses/codex/adapter.py')
checks = {
    'official_plugin_extension_methods': required <= methods,
    'background_tasks_are_in_memory_not_durable': '_bg_tasks: Dict[str, _BackgroundTask] = {}' in console,
    'background_submit_generates_random_id': 'task-{uuid.uuid4().hex[:12]}' in console,
    'native_codex_thread_start': '"thread/start"' in codex,
    'native_codex_turn_start': '"turn/start"' in codex,
    'native_codex_approval_bridge': 'item/commandExecution/requestApproval' in codex and 'service.wait_for_approval' in codex,
    'native_codex_thread_mapping_written': 'write_json_atomic_async' in codex,
    'memory_search_contract': 'async def memory_search(' in source('agents/memory/base_memory_manager.py'),
}
result = {'kind': 'source-contract-only', 'commit': subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip(), 'checks': checks, 'runtime_verified': False}
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(0 if all(checks.values()) else 1)
