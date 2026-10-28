#!/usr/bin/env python3
"""Prepare managed jobs for Sim's existing supercronic; never install or run them.

Cron matches the existing scheduler timezone. Identity uses one UTC minute read
at script start, so a scheduler delay across a minute boundary changes the slot;
there is no catch-up, retry, or claim that cron delivers every occurrence.
"""
import argparse
from datetime import datetime
import ipaddress
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]


def private_read(path):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('Private input cannot contain symlinks')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 1048576):
        os.close(fd)
        raise ValueError('Expected owner-held 0600 regular input')
    with os.fdopen(fd, 'rb') as handle:
        contents = handle.read(1048577)
        if len(contents) > 1048576:
            raise ValueError('Private input too large')
        return contents


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field')
        result[key] = value
    return result


def cron(value):
    if not isinstance(value, str) or len(value) > 160:
        raise ValueError('Expected five cron fields')
    fields = value.split(' ')
    if len(fields) != 5:
        raise ValueError('Expected five cron fields')
    for field, (minimum, maximum) in zip(fields, ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))):
        for item in field.split(','):
            if re.fullmatch(r'(?:\*|[0-9]+(?:-[0-9]+)?)(?:/[0-9]+)?', item) is None:
                raise ValueError('Invalid cron syntax')
            base, _, step = item.partition('/')
            if step and not 1 <= int(step) <= maximum - minimum + 1:
                raise ValueError('Invalid cron step')
            if base != '*':
                numbers = [int(number) for number in base.split('-')]
                if not minimum <= numbers[0] <= numbers[-1] <= maximum:
                    raise ValueError('Invalid cron range')
                if step and len(numbers) != 2:
                    raise ValueError('Cron steps require wildcard or range')
    return value


def origin(value, trusted_http_hosts):
    if not isinstance(value, str) or re.search(r'[\s"\\\x00-\x1f\x7f]', value):
        raise ValueError('Invalid adapter origin')
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment or parsed.port == 0
            or '?' in value or '#' in value):
        raise ValueError('Adapter URL must be a bare HTTP(S) origin')
    host = parsed.hostname
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        if any(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) is None
               for label in host.split('.')):
            raise ValueError('Expected plain DNS hostname or IP address')
        local = host == 'localhost'
    if parsed.scheme == 'http' and not local and host not in trusted_http_hosts:
        raise ValueError('HTTP adapter host must be explicitly trusted')
    return value


SCRIPT = '''#!/bin/sh
set -eu
job=$1
case "$job" in *[!0-9]*|'') exit 64;; esac
base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
slot=$(date -u '+%Y-%m-%dT%H:%M:00Z')
case "$slot" in ????-??-??T??:??:00Z) ;; *) exit 65;; esac
once=$(cat "$base/$job.once")
[ -z "$once" ] || [ "$once" = "$slot" ] || exit 0
status=$({ printf '{"slot":"%s","input":' "$slot"; cat "$base/$job.input.json"; printf '}'; } |
    curl -q --config "$base/$job.curl" --data-binary @- --write-out '%{http_code}')
[ "$status" = 202 ]
'''


def prepare(config_path, output_dir, upstream_crontab=None, *, trusted_http_hosts=(),
            mount_path='/agent-line-triggers'):
    config_path = Path(config_path).absolute()
    config = json.loads(private_read(config_path), object_pairs_hook=unique_object,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON number')))
    if (not isinstance(config, dict) or set(config) != {'schemaVersion', 'adapterBaseUrl', 'tokenFile', 'jobs'}
            or type(config['schemaVersion']) is not int or config['schemaVersion'] != 1):
        raise ValueError('Expected cron configuration schema 1')
    base_url = origin(config['adapterBaseUrl'], trusted_http_hosts)
    if not isinstance(config['tokenFile'], str) or not config['tokenFile']:
        raise ValueError('Expected private tokenFile')
    token = private_read(config_path.parent / config['tokenFile']).decode('ascii')
    if re.fullmatch(r'[A-Za-z0-9._~+/-]{32,}=*', token) is None:
        raise ValueError('Expected at least 32 token characters without whitespace')
    jobs = config['jobs']
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= 100:
        raise ValueError('Expected 1..100 jobs')
    if re.fullmatch(r'/[A-Za-z0-9_/-]+', mount_path) is None or '..' in mount_path.split('/'):
        raise ValueError('Expected simple absolute container mount path')
    prepared = []; seen = set()
    for job in jobs:
        if (not isinstance(job, dict) or set(job) not in (
                {'triggerId', 'cron', 'input'}, {'triggerId', 'cron', 'input', 'onceSlot'})
                or not isinstance(job['triggerId'], str)
                or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', job['triggerId']) is None
                or job['triggerId'] in seen or not isinstance(job['input'], dict)):
            raise ValueError('Invalid or duplicate managed job')
        seen.add(job['triggerId'])
        once = job.get('onceSlot', '')
        if 'onceSlot' in job:
            if not isinstance(once, str) or re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00Z', once) is None:
                raise ValueError('Expected exact UTC onceSlot')
            datetime.strptime(once, '%Y-%m-%dT%H:%M:00Z')
        prepared.append((cron(job['cron']), job['triggerId'], once,
                         json.dumps(job['input'], ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)))
    original = Path(upstream_crontab or ROOT / 'upstream/sim/docker/crontab').read_bytes()
    directory = Path(output_dir).absolute()
    if any(part.is_symlink() for part in directory.parents):
        raise ValueError('Output parent cannot contain symlinks')
    directory.mkdir(mode=0o700)  # Exclusive: never overwrite an existing installation.

    def write(name, value, mode=0o600):
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(value.encode() if isinstance(value, str) else value)

    write('send.sh', SCRIPT, 0o700)
    suffix = '\n# Agent Line managed schedule ingress; original jobs preserved above.\n'
    for index, (schedule, trigger_id, once, inputs) in enumerate(prepared):
        write(f'{index}.once', once)
        write(f'{index}.input.json', inputs)
        write(f'{index}.curl', f'url = "{base_url}/extension/v1/triggers/{trigger_id}/schedule"\n'
              f'header = "Authorization: Bearer {token}"\nheader = "Content-Type: application/json"\n'
              'request = "POST"\nconnect-timeout = 5\nmax-time = 20\nretry = 0\n'
              'noproxy = "*"\nproto = "=http,https"\ngloboff\nsilent\nfail\noutput = "/dev/null"\n')
        suffix += f'{schedule} /bin/sh {mount_path}/send.sh {index}\n'
    write('crontab', original + suffix.encode())
    files = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(directory.iterdir())}
    write('manifest.json', json.dumps({'schemaVersion': 1, 'files': files,
          'upstreamCrontabSha256': hashlib.sha256(original).hexdigest()}, sort_keys=True))
    return directory


def verify_prepared(output_dir, upstream_crontab=None):
    """Read-only admission for an explicitly selected deployment directory."""
    directory = Path(output_dir).absolute()
    info = directory.lstat()
    if (directory.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError('Expected private prepared cron directory')
    manifest = json.loads(private_read(directory / 'manifest.json'), object_pairs_hook=unique_object)
    if (not isinstance(manifest, dict) or set(manifest) != {'schemaVersion', 'files', 'upstreamCrontabSha256'}
            or type(manifest['schemaVersion']) is not int or manifest['schemaVersion'] != 1
            or not isinstance(manifest['files'], dict)):
        raise ValueError('Invalid prepared cron manifest')
    names = set(manifest['files'])
    indexes = {name.split('.')[0] for name in names if re.fullmatch(r'[0-9]+\.once', name)}
    if (not 1 <= len(indexes) <= 100 or indexes != {str(i) for i in range(len(indexes))}
            or names != {'send.sh', 'crontab'} | {i + suffix for i in indexes
                                                   for suffix in ('.once', '.input.json', '.curl')}
            or {p.name for p in directory.iterdir()} != names | {'manifest.json'}):
        raise ValueError('Prepared cron files changed')
    for name, digest in manifest['files'].items():
        if not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None:
            raise ValueError('Invalid prepared cron digest')
        path = directory / name
        if name == 'send.sh':
            info = path.lstat()
            if (path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise ValueError('Prepared sender permissions changed')
            contents = path.read_bytes()
            if contents != SCRIPT.encode():
                raise ValueError('Prepared sender differs from this version')
        else:
            contents = private_read(path)
        if hashlib.sha256(contents).hexdigest() != digest:
            raise ValueError('Prepared cron content changed')
    original = Path(upstream_crontab or ROOT / 'upstream/sim/docker/crontab').read_bytes()
    tab = (directory / 'crontab').read_bytes()
    if (manifest['upstreamCrontabSha256'] != hashlib.sha256(original).hexdigest()
            or not tab.startswith(original)):
        raise ValueError('Official cron template changed; prepare against the selected version')
    lines = tab[len(original):].decode().splitlines()
    if (lines[:2] != ['', '# Agent Line managed schedule ingress; original jobs preserved above.']
            or len(lines) != len(indexes) + 2):
        raise ValueError('Unexpected managed crontab')
    for index, line in enumerate(lines[2:]):
        schedule, separator, command = line.partition(' /bin/sh ')
        if not separator or command != f'/agent-line-triggers/send.sh {index}':
            raise ValueError('Deployment requires the fixed managed mount path')
        cron(schedule)
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config')
    parser.add_argument('--output')
    parser.add_argument('--verify', help='Read-only verification of a prepared deployment directory')
    parser.add_argument('--trusted-http-host', action='append', default=[])
    parser.add_argument('--mount-path', default='/agent-line-triggers')
    args = parser.parse_args()
    if args.verify:
        if args.config or args.output or args.trusted_http_host or args.mount_path != '/agent-line-triggers':
            parser.error('Verification cannot prepare or change configuration')
        verify_prepared(args.verify)
        print('Prepared cron directory verified; services were not changed.')
        return
    if not args.config or not args.output:
        parser.error('Preparation requires --config and --output')
    prepare(args.config, args.output, trusted_http_hosts=args.trusted_http_host, mount_path=args.mount_path)
    print('Private cron files prepared; scheduler and services were not changed.')


if __name__ == '__main__':
    main()
