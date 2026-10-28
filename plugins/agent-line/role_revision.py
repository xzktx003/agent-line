"""Conservative, version-pinned role behavior revisions; no memory-body hashing.

Checks do not make QwenPaw's independent configuration writes transactional with
model calls. They detect drift at plugin admission boundaries, not arbitrary
host changes after a check.
"""
import hashlib
import json
import os
import stat
from pathlib import Path
import re

PREFIX = 'qwenpaw-role-v1:'
PATTERN = r'^qwenpaw-role-v1:[0-9a-f]{64}$'
MODEL_CONFIGURATION_ERRORS = frozenset({
    'Effective model provider or selected model disabled',
    'Selected model requires renewed availability or payment confirmation',
    'Explicit Codex model required; CLI inheritance cannot be pinned',
    'Effective model selection unavailable',
    'Effective model provider unavailable',
})


def model_configuration_failure(exc):
    """True only for a missing, disabled, or unconfirmed configured model.

    Callers must keep the original model identity. This does not select a fallback.
    """
    return isinstance(exc, ValueError) and str(exc) in MODEL_CONFIGURATION_ERRORS
# Names joined by ReMe and AgentMdManager onto the role workspace. Defaults match
# ReMeLightMemoryConfig. Bodies under these directories are never revision inputs.
MEMORY_DIRECTORY_DEFAULTS = {
    'metadata_dir': 'mem_metadata',
    'session_dir': 'mem_session',
    'mem_session_dir': 'mem_agent',
    'resource_dir': 'resource',
    'daily_dir': 'memory',
    'digest_dir': 'digest',
}


def _relative_memory_subdirectory(value):
    if not isinstance(value, str) or value == '' or '\x00' in value:
        return False
    path = Path(value)
    if path.is_absolute() or value.startswith(('/', '\\')):
        return False
    return all(part != '..' for part in path.parts)


def memory_directory_values(reme):
    """Fill omitted names with official relative defaults. None means unset."""
    if reme is None:
        return None
    if not isinstance(reme, dict):
        raise ValueError('Memory directories must stay inside the role workspace')
    return {key: reme[key] if key in reme else default
            for key, default in MEMORY_DIRECTORY_DEFAULTS.items()}


def memory_directories_within_workspace(workspace, directories):
    """True when every configured memory directory resolves inside workspace.

    Absolute paths, non-strings and '..' are rejected even if normalization
    would land inside. Relative subdirectories such as memory/imports/proj stay.
    """
    if not isinstance(directories, dict) or set(MEMORY_DIRECTORY_DEFAULTS) - set(directories):
        return False
    root = Path(workspace)
    if not root.is_absolute():
        return False
    try:
        root_resolved = root.resolve(strict=False)
    except OSError:
        return False
    for name in MEMORY_DIRECTORY_DEFAULTS:
        if not _relative_memory_subdirectory(directories[name]):
            return False
        try:
            resolved = (root / directories[name]).resolve(strict=False)
            resolved.relative_to(root_resolved)
        except (OSError, RuntimeError, ValueError):
            return False
    return True


def valid_revision(value):
    return isinstance(value, str) and re.fullmatch(PATTERN, value) is not None


def _dump(value):
    if hasattr(value, 'model_dump'):
        return value.model_dump(mode='json')
    if isinstance(value, dict):
        return dict(value)
    raise ValueError('Revision requires explicit structured configuration')


def selected_model_snapshot(provider, slot):
    """Freeze effective execution inputs, excluding catalog observation data."""
    from qwenpaw.providers.provider import agent_thinking_level
    model_id = slot['model']
    value = _dump(provider)
    if value.get('enabled') is not True or model_id in value.get('removed_model_ids', []):
        raise ValueError('Effective model provider or selected model disabled')
    selected = (provider.get_model_info(model_id), provider.get_discovered_model_info(model_id))
    if any(model is not None and (model.remote_missing or model.requires_paid_confirmation) for model in selected):
        raise ValueError('Selected model requires renewed availability or payment confirmation')
    model = _dump(provider.resolve_model_info(model_id))
    provider_fields = ('id', 'enabled', 'base_url', 'chat_model', 'auth_mode', 'custom_headers',
                       'is_local', 'require_api_key')
    if 'max_inline_media_bytes' in value:
        provider_fields += ('max_inline_media_bytes',)
    model_fields = ('id', 'model_type', 'effective_max_input_length', 'input_token_limit', 'max_output_length',
                    'supports_multimodal', 'supports_image', 'supports_audio', 'supports_video', 'supports_tool_calling',
                    'relay_reasoning', 'thinking_enabled', 'thinking_budget', 'reasoning_effort',
                    'thinking_param_style', 'reasoning_effort_options', 'thinking_budget_range')
    # Role thinking settings are frozen separately. Ignore an ambient request's
    # ContextVar when resolving provider/model base generation parameters.
    with agent_thinking_level('inherit'):
        generation = provider.get_effective_generate_kwargs(model_id)
    return {'slot': slot, 'provider': {key: value[key] for key in provider_fields},
            'model': {key: model[key] for key in model_fields}, 'generation': generation,
            'protocol': provider.model_protocol(model_id), 'thinkingControl': _dump(provider.thinking_control(model_id))}


def snapshot(profile, global_config, provider_manager):
    role = _dump(profile)
    global_value = _dump(global_config)
    workspace = Path(role['workspace_dir']).resolve(strict=True)
    running = role.get('running')
    reme = running.get('reme_light_memory_config') if isinstance(running, dict) and 'reme_light_memory_config' in running else None
    directories = memory_directory_values(reme)
    # Containment only. Directory contents stay out of the revision hash.
    if directories is not None and not memory_directories_within_workspace(workspace, directories):
        raise ValueError('Memory directories must stay inside the role workspace')
    if role.get('backend') not in ('qwenpaw', 'codex'):
        raise ValueError('Unsupported role revision backend')
    # These are presentation, dispatch bookkeeping, or unrelated delivery data.
    for key in ('name', 'description', 'last_dispatch', 'channels', 'mail', 'template_id'):
        role.pop(key, None)
    inherited = {}
    for key in ('tools', 'security', 'mcp'):
        inherited[key] = global_value.get(key)
        if inherited[key] is None:
            raise ValueError('Global permission defaults unavailable')
    agents = global_value.get('agents')
    if not isinstance(agents, dict):
        raise ValueError('Global agent defaults unavailable')
    inherited['agents'] = {key: agents.get(key) for key in ('defaults', 'running', 'system_prompt_files')}
    models = []
    if role['backend'] == 'qwenpaw':
        active = role.get('active_model')
        if active is None:
            active = _dump(provider_manager.get_active_model())
        role['active_model'] = active
        slots = [active, *role.get('fallback_models', [])]
        slots += [role[key] for key in ('decision_model', 'subagent_model') if role.get(key)]
        for slot in slots:
            if not isinstance(slot, dict) or not slot.get('provider_id') or not slot.get('model'):
                raise ValueError('Effective model selection unavailable')
            provider = provider_manager.get_provider(slot['provider_id'])
            if provider is None:
                raise ValueError('Effective model provider unavailable')
            models.append(selected_model_snapshot(provider, slot))
    if role['backend'] == 'codex':
        settings = role.get('backend_settings')
        if not isinstance(settings, dict) or not isinstance(settings.get('model'), str) or not settings['model'].strip():
            raise ValueError('Explicit Codex model required; CLI inheritance cannot be pinned')
        role['effectiveCodexDefaults'] = {'sandbox': settings.get('sandbox') or 'workspace-write',
                                         'approval_policy': settings.get('approval_policy') or 'on-request'}
    files = role.get('system_prompt_files')
    if not isinstance(files, list) or any(not isinstance(name, str) for name in files):
        raise ValueError('Explicit prompt file order required')
    if len(files) > 64:
        raise ValueError('Too many role prompt sources')
    prompts = []
    total = 0
    for name in files:
        if Path(name).name != name or not name.endswith('.md') or name in ('MEMORY.md', 'memory.md'):
            raise ValueError('Unsupported prompt source; memory bodies are not role revisions')
        path = workspace / name
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            content = None
        else:
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_size > 1_048_576:
                    raise ValueError('Unsupported or oversized prompt file')
                chunks = []; size = 0
                while True:
                    chunk = os.read(fd, min(65536, 1_048_577-size))
                    if not chunk:
                        break
                    chunks.append(chunk); size += len(chunk)
                    if size > 1_048_576:
                        raise ValueError('Prompt file exceeds read limit')
                after = os.fstat(fd)
                current = os.stat(path, follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino):
                    raise ValueError('Prompt path changed while being read')
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise ValueError('Prompt changed while being read')
                total += size
                if total > 4_194_304:
                    raise ValueError('Prompt sources exceed total read limit')
                content = b''.join(chunks).decode('utf-8')
            finally:
                os.close(fd)
        if content is not None and name == 'AGENTS.md':
            content = re.sub(r'<!-- memory:start -->.*?<!-- memory:end -->', '', content, flags=re.S)
        prompts.append({'file': name, 'content': content})
    return {'role': role, 'inherited': inherited, 'models': models, 'prompts': prompts}


def role_revision(role_id, target_id):
    from qwenpaw.config.utils import load_agent_config, load_config
    from qwenpaw.providers.provider_manager import ProviderManager
    global_config = load_config()
    # Avoid official loader's fallback initialization for missing agent.json.
    for agent_id in (role_id, target_id):
        reference = global_config.agents.profiles.get(agent_id)
        if reference is None or not (Path(reference.workspace_dir) / 'agent.json').is_file():
            raise ValueError('Registered role configuration unavailable')
        if reference.enabled is not True:
            raise ValueError('Registered role is disabled')
    parent, target = load_agent_config(role_id), load_agent_config(target_id)
    if parent.backend != 'qwenpaw' or target.backend != 'codex':
        raise ValueError('Role backend differs from execution contract')
    manager = ProviderManager.get_instance()
    value = {'revisionSchema': 2, 'parent': snapshot(parent, global_config, manager),
             'executor': snapshot(target, global_config, manager)}
    data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    return PREFIX + hashlib.sha256(data.encode()).hexdigest()
