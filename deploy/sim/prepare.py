#!/usr/bin/env python3
"""Validate Sim configuration; creating a new installation is always explicit."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import secrets
import re
import shlex
import stat
import sys
import subprocess
import time


ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / 'var/sim'
SECRET_NAMES = (
    'POSTGRES_PASSWORD', 'BETTER_AUTH_SECRET', 'ENCRYPTION_KEY',
    'API_ENCRYPTION_KEY', 'INTERNAL_API_SECRET', 'CRON_SECRET',
)
DATA_NAMES = ('postgres', 'redis', 'uploads')
BOOTSTRAP_WAIT_SECONDS = 90
CID_PATTERN = re.compile(r'[0-9a-f]{64}')


def check_parents(path):
    """Reject symbolic links without following them or repairing any path."""
    for parent in reversed((path, *path.parents)):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('Sim 启动受阻：原数据路径缺失、不是目录或含符号链接')


def check_restore_marker(path):
    for parent in (path, *path.parents):
        marker = parent / 'RESTORE-DO-NOT-START.json'
        if marker.exists() or marker.is_symlink():
            raise ValueError('Sim 启动受阻：恢复隔离标记尚未解除，请先核对原安装恢复记录')


def check_environment():
    check_parents(STATE)
    path = STATE / 'sim.env'
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'r', encoding='utf-8') as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)):
            raise ValueError('Sim 配置受阻：原环境文件必须是当前用户持有的私有普通文件')
        contents = handle.read(65537)
    if len(contents) > 65536:
        raise ValueError('Sim 配置受阻：环境文件过大')
    values = {}
    for raw in contents.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        key = key.strip()
        if not separator or not key.isidentifier() or key in values:
            raise ValueError('Sim 配置受阻：环境文件格式无效或有重复字段')
        values[key] = value.strip()
    for name in SECRET_NAMES:
        try:
            tokens = shlex.split(values.get(name, ''), comments=True, posix=True)
        except ValueError:
            tokens = []
        if len(tokens) != 1 or not tokens[0] or '$' in tokens[0]:
            raise ValueError('Sim 配置受阻：原秘密缺失、为空或依赖外部插值，禁止自动重新生成')


PROBE_NAME = '.agent-line-write-probe'
PROBE_TOKEN = b'agent-line-durable-probe\n'
UNWRITABLE = 'Sim 启动受阻：安装目录不能持久写入，未启动容器'


def prove_durable_write(directory):
    """Write, fsync, read back, and remove a probe. Never follow a symlink."""
    probe = Path(directory) / PROBE_NAME
    try:
        info = probe.lstat()
    except FileNotFoundError:
        info = None
    if info is not None:
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError(UNWRITABLE)
        fd = os.open(probe, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            content = os.read(fd, len(PROBE_TOKEN) + 1)
        finally:
            os.close(fd)
        if content != PROBE_TOKEN:
            raise ValueError(UNWRITABLE)
        os.unlink(probe)
    try:
        fd = os.open(probe, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise ValueError(UNWRITABLE) from None
    try:
        if os.write(fd, PROBE_TOKEN) != len(PROBE_TOKEN):
            raise ValueError(UNWRITABLE)
        os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        if os.read(fd, len(PROBE_TOKEN) + 1) != PROBE_TOKEN:
            raise ValueError(UNWRITABLE)
    except ValueError:
        os.close(fd)
        _remove_regular_probe(probe)
        raise
    except OSError:
        os.close(fd)
        _remove_regular_probe(probe)
        raise ValueError(UNWRITABLE) from None
    os.close(fd)
    try:
        os.unlink(probe)
    except OSError:
        raise ValueError(UNWRITABLE) from None
    if probe.exists() or probe.is_symlink():
        raise ValueError(UNWRITABLE)


def _remove_regular_probe(probe):
    try:
        if probe.is_file() and not probe.is_symlink():
            probe.unlink()
    except OSError:
        pass


def check_start():
    check_environment()
    fd = os.open(STATE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Sim 启动受阻：首次数据库初始化尚未完成') from None
        check_restore_marker(STATE)
        for name in DATA_NAMES:
            check_parents(STATE / name)
        prove_durable_write(STATE)
        # An interrupted explicit init can outlive its Python directory lock.
        # Inspect all db oneoffs, including stopped containers awaiting cleanup.
        try:
            oneoffs = _docker([
                'ps', '--all', '--no-trunc', '--filter',
                'label=com.docker.compose.project=agent-line', '--filter',
                'label=com.docker.compose.service=db', '--format',
                '{{.ID}} {{.Label "com.docker.compose.oneoff"}}',
            ])
            if oneoffs.returncode:
                raise ValueError('临时数据库状态不可确认')
            for line in oneoffs.stdout.splitlines():
                fields = line.split()
                if (len(fields) != 2 or not CID_PATTERN.fullmatch(fields[0])
                        or fields[1].lower() != 'false'):
                    raise ValueError('临时数据库尚未清理')
        except (OSError, ValueError, subprocess.SubprocessError):
            raise ValueError('Sim 启动受阻：临时数据库容器尚未排除，请先核对首次初始化及清理结果。') from None
    finally:
        os.close(fd)


def _docker(arguments, timeout=15):
    return subprocess.run(['docker', *arguments], capture_output=True,
                          text=True, timeout=timeout, check=False)


def _owned_container(name, cid=None):
    # Exact-name listing distinguishes confirmed absence from an inspect failure.
    listed = _docker(['container', 'ls', '--all', '--no-trunc',
                      '--filter', f'id={cid}' if cid else f'name=^/{name}$',
                      '--format', '{{.ID}}'])
    if listed.returncode:
        raise ValueError('临时容器状态不可确认')
    ids = listed.stdout.split()
    if not ids:
        return None
    if len(ids) != 1 or not CID_PATTERN.fullmatch(ids[0]) or (cid and ids[0] != cid):
        raise ValueError('临时容器身份不可确认')
    template = ('{"id":{{json .Id}},"name":{{json .Name}},'
                '"labels":{{json .Config.Labels}},"mounts":{{json .Mounts}},'
                '"running":{{json .State.Running}}}')
    inspected = _docker(['inspect', '--format', template, ids[0]])
    if inspected.returncode:
        raise ValueError('临时容器身份不可确认')
    value = json.loads(inspected.stdout)
    if (not isinstance(value, dict) or not isinstance(value.get('labels'), dict)
            or not isinstance(value.get('mounts'), list)
            or type(value.get('running')) is not bool):
        raise ValueError('临时容器身份不可确认')
    labels = value['labels']
    if (value.get('id') != ids[0] or value.get('name') != '/' + name
            or labels.get('com.docker.compose.project') != 'agent-line'
            or labels.get('com.docker.compose.service') != 'db'
            or str(labels.get('com.docker.compose.oneoff')).lower() != 'true'
            or not any(isinstance(mount, dict) and mount.get('Type') == 'bind'
                       and mount.get('Source') == str(STATE / 'postgres')
                       and mount.get('Destination') == '/var/lib/postgresql/data'
                       for mount in value.get('mounts', []))):
        raise ValueError('临时容器身份不可确认')
    return value


def _bootstrap_database():
    name = 'agent-line-db-init-' + secrets.token_hex(16)
    cid = None
    failed = False
    try:
        started = _docker([
            'compose', '--project-name', 'agent-line', '--env-file',
            str(STATE / 'sim.env'), '-f', str(ROOT / 'deploy/sim/compose.yaml'),
            'run', '--detach', '--rm', '--no-deps', '--pull', 'never',
            '--name', name, '--entrypoint', 'docker-entrypoint.sh', 'db', 'postgres',
        ], timeout=45)
        if started.returncode or not CID_PATTERN.fullmatch(started.stdout.strip()):
            raise ValueError('数据库容器启动未确认')
        cid = started.stdout.strip()
        if _owned_container(name, cid) is None:
            raise ValueError('数据库容器已退出')
        deadline = time.monotonic() + BOOTSTRAP_WAIT_SECONDS
        while True:
            try:
                probe = _docker(['exec', cid, 'sh', '-c',
                                 'test "$(cat /proc/1/comm)" = postgres && '
                                 'pg_isready -U agent_line -d agent_line_sim'], timeout=5)
            except subprocess.TimeoutExpired:
                probe = None  # Continue observing this CID; never launch another.
            if probe is not None and probe.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise ValueError('数据库初始化等待超时')
            time.sleep(0.5)
    except (OSError, ValueError, subprocess.SubprocessError):
        failed = True
    finally:
        try:
            owned = _owned_container(name, cid)
            if owned is not None:
                stopped = _docker(['stop', '--time', '10', owned['id']], timeout=20)
                remaining = _owned_container(name, owned['id'])
                if remaining is not None:
                    if stopped.returncode or remaining['running']:
                        raise ValueError('临时容器仍在运行')
                    _docker(['rm', owned['id']])
                if _owned_container(name, owned['id']) is not None:
                    raise ValueError('临时容器删除未确认')
        except (OSError, ValueError, subprocess.SubprocessError):
            raise ValueError('Sim 初始化受阻：临时数据库容器未确认清理；请核对本次初始化容器，保留现有目录，禁止重新初始化。') from None
    if failed:
        raise ValueError('Sim 数据库初始化失败：保留本次私有目录，请核对初始化环境；禁止自动重试或重建。') from None


def initialize():
    if STATE.exists() or STATE.is_symlink():
        raise ValueError('Sim 初始化受阻：安装目录已存在，禁止补建秘密或重置原安装')
    check_parents(ROOT)
    check_restore_marker(STATE)
    parent = STATE.parent
    if parent.exists() or parent.is_symlink():
        check_parents(parent)
    else:
        parent.mkdir(mode=0o700)
    try:
        existing = _docker(['ps', '-aq', '--filter',
                            'label=com.docker.compose.project=agent-line'])
        if existing.returncode or existing.stdout.strip():
            raise ValueError('原 Compose 实例尚未排除')
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ValueError('Sim 初始化受阻：已有 Compose 容器或容器状态不可确认；未创建新安装。') from None
    STATE.mkdir(mode=0o700)
    fd = os.open(STATE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        contents = ''.join(f'{name}={secrets.token_hex(32)}\n' for name in SECRET_NAMES)
        contents += 'SIM_PUBLIC_URL=http://127.0.0.1:3190\n'
        contents += 'SIM_SOCKET_URL=http://127.0.0.1:3192\n'
        env_fd = os.open(STATE / 'sim.env', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(env_fd, 'w') as handle:
            handle.write(contents)
        for name in DATA_NAMES:
            (STATE / name).mkdir(mode=0o700)
        _bootstrap_database()
    finally:
        os.close(fd)
    # Failed initialization retains its private directory; init never retries it.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', default='check',
                        choices=('check', 'check-start', 'initialize'))
    args = parser.parse_args()
    try:
        if args.action == 'initialize':
            initialize()
            print('Sim 私有配置和数据库已显式初始化；临时数据库容器已清理，业务服务尚未启动。')
        elif args.action == 'check-start':
            check_start()
        else:
            check_environment()
    except (OSError, ValueError) as error:
        message = str(error) if isinstance(error, ValueError) and not isinstance(error, UnicodeError) else 'Sim 启动受阻：原配置或数据不可读取，请恢复原路径及权限；未自动初始化。'
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
