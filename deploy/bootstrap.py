"""Fresh-install dependency preparation; no service, account or data initialization."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
REPOSITORIES = {
    'qwenpaw': ('upstream/qwenpaw', 'https://github.com/agentscope-ai/QwenPaw.git'),
    'sim': ('upstream/sim', 'https://github.com/simstudioai/sim.git'),
}
LOCK = ROOT / 'requirements-runtime.lock.txt'
PINS = ROOT / 'upstreams.lock.json'


class PreparationError(RuntimeError):
    def __init__(self, code, report=None):
        super().__init__(code)
        self.code, self.report = code, report


def unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise PreparationError('ambiguous_repository_lock')
        value[key] = item
    return value


def inputs():
    raw_pins, raw_requirements = PINS.read_bytes(), LOCK.read_bytes()
    if len(raw_pins) > 64000 or len(raw_requirements) > 128000:
        raise PreparationError('oversized_dependency_lock')
    pins = json.loads(raw_pins, object_pairs_hook=unique_pairs)
    if (type(pins) is not dict or type(pins.get('schemaVersion')) is not int
            or pins['schemaVersion'] != 1 or type(pins.get('repositories')) is not dict
            or set(pins['repositories']) != set(REPOSITORIES)):
        raise PreparationError('invalid_repository_lock')
    for name, (path, remote) in REPOSITORIES.items():
        record = pins['repositories'][name]
        if (type(record) is not dict or record.get('path') != path or record.get('remote') != remote
                or type(record.get('commit')) is not str
                or re.fullmatch(r'[0-9a-f]{40}', record['commit']) is None):
            raise PreparationError('invalid_repository_pin')
    packages = set()
    for line in raw_requirements.decode('utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*)', line)
        if match is None:
            raise PreparationError('requirements_must_use_exact_versions')
        name = re.sub(r'[-_.]+', '-', match[1]).lower()
        if name in packages:
            raise PreparationError('duplicate_requirement')
        packages.add(name)
    if not {'fastapi', 'httpx', 'pydantic', 'uvicorn', 'agentscope', 'reme-ai'} <= packages:
        raise PreparationError('incomplete_dependency_lock')
    return pins['repositories'], raw_pins, raw_requirements, len(packages)


def owned_directory(path, *, create=False):
    if not path.is_relative_to(ROOT):
        raise PreparationError('path_outside_repository')
    root_info = ROOT.lstat()
    if (not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.getuid()
            or root_info.st_mode & 0o022):
        raise PreparationError('unsafe_installation_directory')
    current = ROOT
    for part in path.relative_to(ROOT).parts:
        current = current / part
        if create and not current.exists() and not current.is_symlink():
            current.mkdir(mode=0o700)
        info = current.lstat()
        if (not stat.S_ISDIR(info.st_mode) or current.resolve() != current
                or info.st_uid != os.getuid() or info.st_mode & 0o022):
            raise PreparationError('unsafe_installation_directory')


def environment(session):
    value = {key: item for key, item in os.environ.items() if not key.startswith(('PIP_', 'GIT_'))}
    for name in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONPYCACHEPREFIX', 'VIRTUAL_ENV'):
        value.pop(name, None)
    value.update(PIP_CONFIG_FILE=os.devnull, PIP_INDEX_URL='https://pypi.org/simple',
                 PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_NO_INPUT='1', PIP_KEYRING_PROVIDER='disabled',
                 PIP_CACHE_DIR=str(session / 'cache'), PYTHONPYCACHEPREFIX=str(session / 'pycache'),
                 TMPDIR=str(session / 'tmp'), PYTHONNOUSERSITE='1',
                 GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                 GIT_TERMINAL_PROMPT='0')
    return value


def git_read(path, *args, env):
    try:
        return subprocess.check_output(['git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
                                        '-C', str(path), *args], env=env, stderr=subprocess.DEVNULL,
                                       text=True, timeout=30).strip()
    except (OSError, subprocess.SubprocessError):
        raise PreparationError('repository_inspection_failed') from None


def inspect_checkout(path, pin, env):
    owned_directory(path)
    if (Path(git_read(path, 'rev-parse', '--show-toplevel', env=env)).resolve() != path
            or git_read(path, 'rev-parse', 'HEAD', env=env) != pin['commit']
            or git_read(path, 'remote', 'get-url', 'origin', env=env) != pin['remote']
            or git_read(path, 'status', '--porcelain', '--untracked-files=all', env=env)):
        raise PreparationError('existing_checkout_does_not_match_pin')


def run(args, log, env, *, cwd=ROOT, output=None):
    try:
        result = subprocess.run(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=output if output is not None else log, stderr=log, timeout=1800)
        if result.returncode:
            raise PreparationError('dependency_command_failed')
    except (OSError, subprocess.SubprocessError):
        raise PreparationError('dependency_command_unavailable_or_interrupted') from None


def write_json(path, value):
    with path.open('x', encoding='utf-8') as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=True, indent=2)
        stream.write('\n')


def install(pins, raw_pins, requirements):
    if (sys.version_info[:2] != (3, 12) or platform.python_implementation() != 'CPython'
            or sys.platform != 'linux' or platform.machine() != 'x86_64'):
        raise PreparationError('requires_cpython312_linux_x86_64')
    target = ROOT / '.venv'
    if target.exists() or target.is_symlink():
        raise PreparationError('existing_environment_must_not_be_replaced')
    owned_directory(ROOT)
    # Inspect existing sources before creating or downloading anything.
    initial_env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    initial_env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0')
    for pin in pins.values():
        path = ROOT / pin['path']
        if path.exists() or path.is_symlink():
            inspect_checkout(path, pin, initial_env)
    owned_directory(ROOT / 'var', create=True)
    session = Path(tempfile.mkdtemp(prefix='bootstrap-', dir=ROOT / 'var'))
    session.chmod(0o700)
    for name in ('tmp', 'cache'):
        (session / name).mkdir(mode=0o700)
    env = environment(session)
    frozen_lock = session / 'requirements-runtime.lock.txt'
    frozen_lock.write_bytes(requirements)
    frozen_lock.chmod(0o600)
    phase = 'repositories'
    try:
        with (session / 'install.log').open('xb') as log:
            os.chmod(log.name, 0o600)
            owned_directory(ROOT / 'upstream', create=True)
            for pin in pins.values():
                path = ROOT / pin['path']
                if not path.exists():
                    path.mkdir(mode=0o700)  # Exclusive; partial clones remain visible on failure.
                    run(['git', 'init', str(path)], log, env)
                    run(['git', '-C', str(path), 'remote', 'add', 'origin', pin['remote']], log, env)
                    run(['git', '-C', str(path), 'fetch', '--depth=1', 'origin', pin['commit']], log, env)
                    run(['git', '-C', str(path), 'checkout', '--detach', pin['commit']], log, env)
                inspect_checkout(path, pin, env)
            phase = 'python_environment'
            target.mkdir(mode=0o700)  # Refuse a competing installation rather than merge.
            run([sys.executable, '-m', 'venv', str(target)], log, env)
            python = str(target / 'bin/python')
            phase = 'locked_dependencies'
            run([python, '-m', 'pip', 'install', '--disable-pip-version-check', '-r', str(frozen_lock)], log, env)
            phase = 'qwenpaw_source_archive'
            source = session / 'qwenpaw-source'
            source.mkdir(mode=0o700)
            archive = session / 'qwenpaw-source.tar'
            with archive.open('xb') as stream:
                os.chmod(archive, 0o600)
                run(['git', '-C', str(ROOT / pins['qwenpaw']['path']), 'archive', '--format=tar',
                     pins['qwenpaw']['commit']], log, env, output=stream)
            with tarfile.open(archive) as bundle:
                bundle.extractall(source, filter='data')
            phase = 'qwenpaw_install'
            run([python, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-deps',
                 '--constraint', str(frozen_lock), str(source)], log, env)
            phase = 'dependency_consistency'
            run([python, '-m', 'pip', 'check'], log, env)
            for pin in pins.values():
                inspect_checkout(ROOT / pin['path'], pin, env)
            if PINS.read_bytes() != raw_pins or LOCK.read_bytes() != requirements:
                raise PreparationError('dependency_locks_changed_during_installation')
        write_json(session / 'result.json', {
            'status': 'dependencies_prepared', 'scope': 'python_and_pinned_sources_only',
            'repositories': {key: pin['commit'] for key, pin in pins.items()},
            'requirementsSha256': hashlib.sha256(requirements).hexdigest(),
            'repositoryLockSha256': hashlib.sha256(raw_pins).hexdigest(),
            'pythonVersion': platform.python_version(), 'servicesStarted': False,
            'accountsOrMemoryInitialized': False, 'businessReadiness': 'unverified',
        })
    except BaseException:
        write_json(session / 'failure.json', {'status': 'incomplete', 'phase': phase,
                   'automaticRetryAllowed': False, 'existingServicesModified': False})
        raise PreparationError('installation_incomplete_' + phase,
                               str((session / 'failure.json').relative_to(ROOT))) from None
    return {'status': 'dependencies_prepared', 'report': str((session / 'result.json').relative_to(ROOT)),
            'servicesStarted': False, 'businessReadiness': 'unverified'}


def main():
    parser = argparse.ArgumentParser(description='仓库内全新依赖准备；默认仅显示计划，不替换现有环境。')
    parser.add_argument('action', nargs='?', choices=('plan', 'install'), default='plan')
    args = parser.parse_args()
    try:
        pins, raw_pins, requirements, count = inputs()
        if args.action == 'plan':
            result = {'action': 'plan', 'executed': False, 'requiredPlatform': 'CPython 3.12 / Linux x86_64',
                      'environmentExists': (ROOT / '.venv').exists() or (ROOT / '.venv').is_symlink(),
                      'pinnedPackages': count, 'steps': [
                          '核对或拉取锁定上游（不重置已有仓库）', '仅创建全新.venv',
                          '安装已有版本锁依赖', '从锁定Git归档安装QwenPaw', '检查依赖一致性'],
                      'notIncluded': ['Console构建', 'Sim镜像/持久数据', 'Codex环境与执行镜像',
                                      '账户/角色/项目接线', '服务启动', '真实业务验收'],
                      'next': 'docs/fresh-install.md'}
        else:
            result = install(pins, raw_pins, requirements)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (OSError, ValueError, PreparationError) as error:
        result = {'status': 'refused_or_incomplete', 'servicesStarted': False,
                  'reason': error.code if isinstance(error, PreparationError) else 'invalid_or_unavailable_installation_inputs'}
        if isinstance(error, PreparationError) and error.report is not None:
            result['report'] = error.report
        print(json.dumps(result))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
