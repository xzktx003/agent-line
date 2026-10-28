#!/usr/bin/env python3
"""Private installation data bundles, with fail-closed writer quiescence checks."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import socket
import sqlite3
import subprocess
import tarfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = ('var', 'agent_line', 'deploy', 'plugins', 'frontend', 'upstreams.lock.json', 'requirements-runtime.lock.txt')
EXCLUDED = ('var/sim/postgres', 'var/codex-g0/tmp', 'var/executor/codex-home/tmp')
WRITERS = ('simstudio', 'realtime', 'cron', 'redis')


def run(command, **kwargs):
    kwargs.setdefault('stderr', subprocess.PIPE)
    return subprocess.run(command, check=True, **kwargs)


def private_json(path, value):
    with open(path, 'x', encoding='utf-8') as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, indent=2)


def sha(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def container(name):
    return json.loads(run(['docker', 'inspect', name], stdout=subprocess.PIPE).stdout)[0]


def ancestors():
    result = set(); pid = os.getpid()
    while pid > 1:
        result.add(pid)
        try: pid = int(Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1].split()[1])
        except OSError: break
    return result


def project_writers(root):
    """Reject project-local service/executor processes, including detached workers."""
    excluded = ancestors(); found = []
    for process in Path('/proc').iterdir():
        if not process.name.isdigit() or int(process.name) in excluded: continue
        try:
            cmd = (process / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
            env = (process / 'environ').read_bytes()
            cwd = (process / 'cwd').resolve()
        except (OSError, PermissionError): continue
        bound = str(root) in cmd or str(root).encode() in env or cwd == root or root in cwd.parents
        if bound and any(word in cmd for word in ('qwenpaw', 'codex', 'uvicorn', 'agent_line.worker', 'agent_line.runtime', 'deploy/gateway/gateway.py')):
            found.append(int(process.name))
    return found


def verify_quiesce(root, attestation):
    proof = json.loads(Path(attestation).read_text())
    if (proof.get('root') != str(root) or proof.get('confirmedNoExternalWriters') is not True
            or not 0 <= time.time() - proof.get('issuedAt', 0) <= 600):
        raise ValueError('Fresh explicit quiescence attestation required')
    if project_writers(root): raise ValueError('Project service/executor process still alive')
    for port in (3190, 3192, 3193, 3194):
        with socket.socket() as sock:
            sock.settimeout(.2)
            if sock.connect_ex(('127.0.0.1', port)) == 0:
                raise ValueError('Project service listener still alive')
    owned = [json.loads(line) for line in run(['docker', 'ps', '-a', '--filter', 'label=com.docker.compose.project=agent-line', '--format', '{{json .}}'], stdout=subprocess.PIPE).stdout.decode().splitlines() if line]
    if not owned: raise ValueError('Expected project containers missing')
    for item in owned:
        detail = container(item['Names'])
        service = detail['Config'].get('Labels', {}).get('com.docker.compose.service')
        if service != 'db' and detail['State']['Running']:
            raise ValueError('Project container writer still alive')
    for service in WRITERS:
        detail = container('agent-line-' + service + '-1')
        if detail['State']['Running']: raise ValueError('Expected writer must be stopped')
    running = run(['docker', 'ps', '--format', '{{.Names}}'], stdout=subprocess.PIPE).stdout.decode().splitlines()
    if any(name.startswith('agent-line-exec') for name in running):
        raise ValueError('Isolated executor still alive')
    db = container('agent-line-db-1')
    if not db['State']['Running']: raise ValueError('PostgreSQL must remain available for consistent logical dump')
    active = run(['docker', 'exec', 'agent-line-db-1', 'psql', '-U', 'agent_line', '-d', 'agent_line_sim', '-Atc', "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND backend_type='client backend'"], stdout=subprocess.PIPE).stdout.strip()
    if active != b'0': raise ValueError('PostgreSQL application connections remain')
    storepath = root / 'var/adapter/tasks.sqlite'
    with sqlite3.connect(storepath.as_uri() + '?mode=ro', uri=True) as sql:
        unresolved = sql.execute("SELECT count(*) FROM tasks WHERE execution_status NOT IN ('succeeded','failed','canceled') OR resources_released=0").fetchone()[0]
    if unresolved: raise ValueError('TaskStore still has unresolved tasks/resources')
    return {'checkedAt': time.time(), 'writersStopped': list(WRITERS), 'databaseImage': db['Image']}


def safe_members(archive):
    seen = set()
    for member in archive:
        path = PurePosixPath(member.name)
        if path.is_absolute() or '..' in path.parts or not path.parts or member.name in seen:
            raise ValueError('Unsafe or duplicate archive path')
        if not member.isfile() and not member.isdir():
            raise ValueError('Links and special files cannot be restored')
        seen.add(member.name)
        yield member


def inventory(archive_path):
    result = []
    with tarfile.open(archive_path, 'r:') as archive:
        for member in safe_members(archive):
            item = {'path': member.name, 'size': member.size, 'kind': 'file' if member.isfile() else 'directory'}
            if member.isfile():
                item['sha256'] = hashlib.file_digest(archive.extractfile(member), 'sha256').hexdigest()
            result.append(item)
    return result


def postgres_counts(name):
    query = """CREATE TEMP TABLE backup_counts(name text, total bigint);
DO $$ DECLARE r record; BEGIN
FOR r IN SELECT tablename FROM pg_tables WHERE schemaname='public' LOOP
EXECUTE format('INSERT INTO backup_counts SELECT %L,count(*) FROM %I.%I',r.tablename,'public',r.tablename);
END LOOP; END $$;
SELECT COALESCE(json_object_agg(name,total),'{}'::json) FROM backup_counts;"""
    output = run(['docker', 'exec', name, 'psql', '-U', 'agent_line', '-d', 'agent_line_sim', '-qAt', '-v', 'ON_ERROR_STOP=1', '-c', query], stdout=subprocess.PIPE).stdout
    return json.loads(output)


def create(root, target, attestation):
    root, target = Path(root).resolve(), Path(target).resolve()
    if target == root or root in target.parents:
        raise ValueError('Bundle must be outside live installation')
    before = verify_quiesce(root, attestation)
    for name in REQUIRED:
        if not (root / name).exists(): raise ValueError('Required installation component missing')
    target.mkdir(mode=0o700)
    counts_before = postgres_counts('agent-line-db-1')
    dump = target / 'sim-postgres.dump'
    with open(dump, 'xb') as stream:
        os.chmod(dump, 0o600)
        run(['docker', 'exec', 'agent-line-db-1', 'pg_dump', '-U', 'agent_line', '-d', 'agent_line_sim', '--format=custom', '--serializable-deferrable'], stdout=stream)
    archive = target / 'installation.tar'
    # Root helper only reads this project's bind mount; no Docker socket/network.
    command = ['docker', 'run', '--rm', '--network=none', '--read-only', '--user', '0:0',
               '--mount', f'type=bind,src={root},dst=/source,readonly', '--entrypoint', 'tar',
               before['databaseImage'], '-C', '/source', '--format=posix']
    command += ['--exclude=' + item for item in EXCLUDED]
    command += ['-cf', '-', *REQUIRED]
    with open(archive, 'xb') as stream:
        os.chmod(archive, 0o600); run(command, stdout=stream)
    members = inventory(archive)
    after = verify_quiesce(root, attestation)
    if postgres_counts('agent-line-db-1') != counts_before:
        raise ValueError('Database row counts changed during snapshot')
    manifest = {'formatVersion': 1, 'scope': 'installation-data-and-extension-code', 'createdAt': time.time(),
                'sourceRoot': str(root), 'quiesceBefore': before, 'quiesceAfter': after,
                'excluded': list(EXCLUDED), 'files': {p.name: {'sha256': sha(p), 'size': p.stat().st_size} for p in (dump, archive)},
                'members': members, 'postgresTableRows': counts_before, 'postgresImage': before['databaseImage'],
                'restorePolicy': 'isolated-directory-no-service-start; admission-closed; reconcile-external-identities-before-cutover',
                'limitations': ['No upstream git source trees or Python/Node environments/images bundled; pinned locks included', 'No original Hermes or external project workspaces', 'Quiescence requires coordinated writers; checks do not replace host administrative exclusion']}
    private_json(target / 'manifest.json', manifest)
    return {'status': 'created', 'bundle': str(target), 'fileCount': len(members)}


def validate(bundle):
    bundle = Path(bundle)
    manifest = json.loads((bundle / 'manifest.json').read_text())
    if manifest.get('formatVersion') != 1 or set(manifest.get('files', {})) != {'installation.tar', 'sim-postgres.dump'}:
        raise ValueError('Unsupported installation bundle')
    for name, expected in manifest['files'].items():
        p = bundle / name
        if p.is_symlink() or sha(p) != expected['sha256'] or p.stat().st_size != expected['size']:
            raise ValueError('Bundle checksum mismatch')
    if inventory(bundle / 'installation.tar') != manifest['members']:
        raise ValueError('Archive member inventory mismatch')
    return manifest


def restore(bundle, target):
    bundle, target = Path(bundle).resolve(), Path(target).resolve()
    manifest = validate(bundle)
    for source in (Path(manifest['sourceRoot']), ROOT):
        if target == source or source in target.parents or target in source.parents:
            raise ValueError('Restore must not overlap original installation')
    target.mkdir(mode=0o700)
    with tarfile.open(bundle / 'installation.tar', 'r:') as archive:
        for member in safe_members(archive):
            path = target / member.name
            if member.isdir(): path.mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with open(path, 'xb') as stream:
                    os.chmod(path, 0o600)
                    source_stream = archive.extractfile(member)
                    while chunk := source_stream.read(1024 * 1024): stream.write(chunk)
    for directory in target.rglob('*'):
        if directory.is_dir(): directory.chmod(0o700)
    # Prevent the restored extension DB being treated as immediately admissible.
    database = target / 'var/adapter/tasks.sqlite'
    if not database.is_file(): raise ValueError('Required restored TaskStore missing')
    for candidate in target.rglob('*'):
        if candidate.is_file():
            with candidate.open('rb') as stream: header = stream.read(16)
            if header == b'SQLite format 3\x00':
                with sqlite3.connect(candidate.as_uri() + '?mode=ro', uri=True) as check:
                    if check.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('Restored SQLite integrity check failed')
    with sqlite3.connect(database.as_uri() + '?mode=rw', uri=True) as sql:
        sql.execute("INSERT OR REPLACE INTO admission_guard VALUES (1,?)", ('installation_restore_requires_external_reconciliation',))
    private_json(target / 'RESTORE-DO-NOT-START.json', {'sourceBundle': str(bundle), 'admissionClosed': True,
                 'absolutePathsRequireRebinding': True, 'externalNativeIdentitiesMustNotReplay': True})
    return {'status': 'restored_offline', 'target': str(target), 'servicesStarted': False}


def restore_postgres(bundle, target):
    """Import into a new network-isolated PG container, then stop/remove it."""
    bundle, target = Path(bundle).resolve(), Path(target).resolve()
    manifest = validate(bundle)
    for source in (Path(manifest['sourceRoot']), ROOT):
        if target == source or source in target.parents or target in source.parents:
            raise ValueError('Isolated PostgreSQL target overlaps live installation')
    marker = target / 'RESTORE-DO-NOT-START.json'
    if not marker.exists() or json.loads(marker.read_text()).get('sourceBundle') != str(bundle):
        raise ValueError('Restore this bundle to the target directory first')
    data = target / ('postgres-validation-' + uuid.uuid4().hex[:12]); data.mkdir(mode=0o700)
    name = 'agent-line-restore-' + uuid.uuid4().hex[:12]
    try:
        run(['docker', 'run', '-d', '--name', name, '--network=none', '--mount', f'type=bind,src={data},dst=/var/lib/postgresql/data',
             '-e', 'POSTGRES_HOST_AUTH_METHOD=trust', '-e', 'POSTGRES_USER=agent_line', '-e', 'POSTGRES_DB=agent_line_sim', manifest['postgresImage']], stdout=subprocess.DEVNULL)
        for _ in range(60):
            checked = subprocess.run(['docker', 'exec', name, 'pg_isready', '-U', 'agent_line', '-d', 'agent_line_sim'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if checked.returncode == 0: break
            time.sleep(1)
        else: raise ValueError('Isolated restore PostgreSQL did not become ready')
        # pg_dump clears search_path. Sim's vector IS DISTINCT FROM trigger
        # requires the public extension operator during CREATE TRIGGER parsing.
        # Preserve every schema object; only alter the isolated restore session.
        sql_path = target / (name + '.sql')
        raw_path = target / (name + '.raw.sql')
        with open(bundle / 'sim-postgres.dump', 'rb') as source, open(raw_path, 'xb') as sql:
            os.chmod(raw_path, 0o600)
            run(['docker', 'exec', '-i', name, 'pg_restore', '--no-owner', '--file=-'], stdin=source, stdout=sql)
        replacements = 0
        with open(raw_path, 'rb') as source, open(sql_path, 'xb') as sql:
            os.chmod(sql_path, 0o600)
            for line in source:
                if line.rstrip() == b"SELECT pg_catalog.set_config('search_path', '', false);":
                    line = b"SELECT pg_catalog.set_config('search_path', 'public', false);\n"
                    replacements += 1
                sql.write(line)
        if replacements != 1: raise ValueError('Unexpected pg_restore search_path preamble')
        with open(sql_path, 'rb') as source:
            run(['docker', 'exec', '-i', name, 'psql', '-U', 'agent_line', '-d', 'agent_line_sim',
                 '-v', 'ON_ERROR_STOP=1', '--single-transaction', '-q'], stdin=source, stdout=subprocess.DEVNULL)
        restored_counts = postgres_counts(name)
        if not restored_counts or restored_counts != manifest['postgresTableRows']:
            raise ValueError('Restored PostgreSQL table/row inventory mismatch')
        result = {'status': 'postgres_restored_isolated', 'publicTableCount': len(restored_counts),
                  'tableRowCountsMatched': True, 'restoreSessionSearchPath': 'public', 'publishedPorts': [], 'network': 'none'}
        private_json(target / 'postgres-restore-report.json', result)
        return result
    except subprocess.CalledProcessError as exc:
        private_json(target / ('postgres-restore-error-' + uuid.uuid4().hex[:12] + '.json'),
                     {'returncode': exc.returncode, 'operation': exc.cmd[0:4], 'stderr': (exc.stderr or b'').decode(errors='replace')})
        raise
    finally:
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['create', 'validate', 'restore', 'restore-postgres'])
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--target', type=Path)
    parser.add_argument('--attestation', type=Path)
    args = parser.parse_args()
    if args.action == 'create': result = create(ROOT, args.bundle, args.attestation)
    elif args.action == 'validate':
        manifest = validate(args.bundle); result = {'status': 'validated', 'members': len(manifest['members'])}
    elif args.action == 'restore': result = restore(args.bundle, args.target)
    else: result = restore_postgres(args.bundle, args.target)
    print(json.dumps(result))


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        print(json.dumps({'status': 'refused_or_failed', 'errorType': type(exc).__name__}))
        raise SystemExit(1)
