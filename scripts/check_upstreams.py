"""Read-only upstream integrity gate. Never fetches, resets, or prints secrets."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def inspect(path: Path, pin: dict) -> list[str]:
    def git(*args):
        return subprocess.check_output(
            ['git', '-C', str(path), *args], text=True, stderr=subprocess.DEVNULL
        ).strip()
    try:
        # Reject an ancestor repository accidentally discovered for a missing checkout.
        if Path(git('rev-parse', '--show-toplevel')).resolve() != path.resolve():
            return ['repository_unavailable']
        commit = git('rev-parse', 'HEAD')
        remote = git('remote', 'get-url', 'origin')
        tracked = git('status', '--porcelain', '--untracked-files=no')
        untracked = git('ls-files', '--others', '--exclude-standard')
    except (OSError, subprocess.CalledProcessError):
        return ['repository_unavailable']
    errors = []
    if commit != pin['commit']:
        errors.append('commit_mismatch')
    if remote != pin['remote']:
        errors.append('remote_mismatch')
    if tracked:
        errors.append('tracked_changes')
    if untracked:
        errors.append('untracked_files')
    return errors


def main():
    try:
        lock = json.loads((ROOT / 'upstreams.lock.json').read_text())
        if not lock.get('repositories'):
            raise ValueError('No pinned repositories')
        result = {}
        for name, pin in lock['repositories'].items():
            path = (ROOT / pin['path']).resolve()
            if not path.is_relative_to(ROOT / 'upstream'):
                raise ValueError('Checkout outside upstream directory')
            result[name] = inspect(path, pin)
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({'ok': False, 'error': 'invalid_or_missing_lock'}))
        return 1
    ok = not any(result.values())
    print(json.dumps({'ok': ok, 'repositories': result}, indent=2))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
