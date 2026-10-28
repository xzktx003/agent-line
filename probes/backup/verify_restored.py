"""Read-only logical data audit of restored SQLite stores against archived bytes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile


def table_counts(path):
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite integrity failure')
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {name: db.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in tables}


def audit(bundle, target):
    manifest = json.loads((bundle / 'manifest.json').read_text())
    checked = 0; sqlite_paths = []; differences = []
    for item in manifest['members']:
        if item['kind'] != 'file': continue
        checked += 1; path = target / item['path']
        if path.exists():
            with path.open('rb') as stream:
                header = stream.read(16); stream.seek(0)
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if header == b'SQLite format 3\x00': sqlite_paths.append(item['path'])
        else: actual = None
        if actual != item['sha256']: differences.append(item['path'])
    allowed = {'var/adapter/tasks.sqlite'}
    allowed.update(p + suffix for p in sqlite_paths for suffix in ('-wal', '-shm'))
    if any(p not in allowed for p in differences):
        raise ValueError('Unexpected restored file change')
    counts = {}
    with tempfile.TemporaryDirectory(prefix='agent-line-sqlite-audit-') as temporary, tarfile.open(bundle / 'installation.tar') as archive:
        names = {member.name for member in archive.getmembers()}
        for index, relative in enumerate(sqlite_paths):
            copy = Path(temporary) / f'{index}.sqlite'
            for suffix in ('', '-wal'):
                name = relative + suffix
                if name in names:
                    destination = Path(str(copy) + suffix)
                    with destination.open('xb') as output:
                        os.chmod(destination, 0o600)
                        source = archive.extractfile(name)
                        while chunk := source.read(1024 * 1024): output.write(chunk)
            old, new = table_counts(copy), table_counts(target / relative)
            if old != new: raise ValueError('SQLite logical table counts differ')
            counts[relative] = {'tables': len(old), 'rowCountsMatched': True}
    report = {'status': 'verified_offline_data', 'ordinaryFilesChecked': checked,
              'expectedSQLiteCheckpointAndGuardChanges': differences, 'sqliteStores': counts,
              'applicationServicesStarted': False, 'redisStartupTested': False,
              'scope': 'File hashes and SQLite integrity/row counts; not full application behavior'}
    with (target / 'restore-data-report.json').open('x') as stream:
        os.chmod(stream.name, 0o600); json.dump(report, stream, indent=2)
    print(json.dumps({'status': report['status'], 'ordinaryFilesChecked': checked, 'sqliteStoresVerified': len(counts)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser();parser.add_argument('--bundle',type=Path,required=True);parser.add_argument('--target',type=Path,required=True)
    args = parser.parse_args();audit(args.bundle.resolve(),args.target.resolve())
