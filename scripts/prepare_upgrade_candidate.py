#!/usr/bin/env python3
"""Prepare an isolated, pinned source candidate; never install or execute it."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

OFFICIAL_REMOTES = {
    'qwenpaw': 'https://github.com/agentscope-ai/QwenPaw.git',
    'sim': 'https://github.com/simstudioai/sim.git',
}

class CandidateError(RuntimeError):
    pass


def git(args, cwd=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0', GIT_NO_LAZY_FETCH='1')
    try:
        result = subprocess.run(
            ['git', '-c', 'core.hooksPath=' + os.devnull, '-c', 'submodule.recurse=false', '-c', 'core.fsmonitor=false', *args],
            cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=600, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CandidateError('Git operation could not finish: ' + type(error).__name__) from error
    if result.returncode:
        # Do not store arbitrary server responses, credentials, or terminal escapes.
        raise CandidateError(f'Git {args[0]} failed with exit status {result.returncode}')
    return result.stdout.strip()


def save(path, state):
    descriptor, temporary = tempfile.mkstemp(prefix='.candidate-state-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(state, stream, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def verify(target, remote, sha):
    if target.is_symlink() or not target.is_dir() or (target / '.git').is_symlink() or not (target / '.git').is_dir():
        raise CandidateError('Existing candidate is not an independent checkout directory')
    if (target / '.git/objects/info/alternates').exists() or (target / '.git/commondir').exists():
        raise CandidateError('Candidate must not share an external Git object store')
    if git(['rev-parse', '--show-toplevel'], target) != str(target.resolve()):
        raise CandidateError('Candidate is not its own checkout root')
    if git(['remote', 'get-url', '--all', 'origin'], target) != remote:
        raise CandidateError('Candidate origin differs from locked official remote')
    if git(['rev-parse', 'HEAD'], target) != sha:
        raise CandidateError('Candidate HEAD differs from requested commit')
    if git(['status', '--porcelain', '--untracked-files=all', '--ignored=matching'], target):
        raise CandidateError('Candidate is dirty or contains extra files; never reset automatically')


def local_cache(root, repository, remote, commit):
    """Select only canonical ready candidates; never consult arbitrary cache paths."""
    base = root / 'var/upgrade-candidates'
    for statepath in sorted(base.glob(repository + '-*.state.json')):
        if statepath.is_symlink(): continue
        match = re.fullmatch(re.escape(repository) + r'-([0-9a-f]{40})\.state\.json', statepath.name)
        if match is None: continue
        cache_commit = match.group(1)
        cache = base / (repository + '-' + cache_commit)
        try:
            state = json.loads(statepath.read_text())
            if (state.get('status') != 'ready' or state.get('repository') != repository
                    or state.get('commit') != cache_commit or state.get('remote') != remote
                    or state.get('path') != str(cache.relative_to(root))):
                continue
            verify(cache, remote, cache_commit)
            git(['cat-file', '-e', commit + '^{commit}'], cache)
        except (CandidateError, OSError, ValueError, AttributeError):
            continue
        return cache, cache_commit
    raise CandidateError('No verified local candidate contains the requested commit; network fallback is disabled')


def prepare(root, repository, commit, *, use_local_cache=False):
    if not isinstance(use_local_cache, bool):
        raise CandidateError("Local cache selection must be boolean")
    root = Path(root).resolve()
    if repository not in OFFICIAL_REMOTES:
        raise CandidateError('Only qwenpaw and sim are supported')
    if not re.fullmatch(r'[0-9a-fA-F]{40}', commit):
        raise CandidateError('Provide an explicit 40-character hexadecimal commit SHA')
    commit = commit.lower()
    lock = json.loads((root / 'upstreams.lock.json').read_text())
    remote = lock['repositories'][repository]['remote']
    if remote != OFFICIAL_REMOTES[repository]:
        raise CandidateError('Lock remote is not the supported official repository')
    base = root / 'var/upgrade-candidates'
    if (root / 'var').is_symlink() or base.is_symlink():
        raise CandidateError('Candidate storage cannot be a symbolic link')
    base.mkdir(parents=True, exist_ok=True)
    target = base / f'{repository}-{commit}'
    statepath = base / f'{repository}-{commit}.state.json'
    mutex = base / f'{repository}-{commit}.lock'
    try:
        descriptor = os.open(mutex, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise CandidateError('Candidate preparation already locked; inspect the existing attempt') from error
    os.close(descriptor)
    state = {'repository': repository, 'commit': commit, 'remote': remote,
             'path': str(target.relative_to(root)), 'status': 'preparing'}
    try:
        if statepath.is_file() and not statepath.is_symlink():
            try:
                prior = json.loads(statepath.read_text())
            except (OSError, ValueError):
                prior = {}
            if (isinstance(prior, dict) and prior.get('status') == 'ready'
                    and all(prior.get(key) == state[key] for key in ('repository', 'commit', 'remote', 'path'))
                    and prior.get('acquisition') == 'verified_local_cache'
                    and isinstance(prior.get('cacheCommit'), str)
                    and re.fullmatch(r'[0-9a-f]{40}', prior['cacheCommit'])):
                state.update(acquisition='verified_local_cache', cacheCommit=prior['cacheCommit'])
        save(statepath, state)
        existed = target.exists() or target.is_symlink()
        if not existed:
            if use_local_cache:
                cache, cache_commit = local_cache(root, repository, remote, commit)
                git(['clone', '--no-checkout', '--no-local', '--', str(cache), str(target)])
                git(['remote', 'set-url', 'origin', remote], target)
                verify(cache, remote, cache_commit)
                state.update(acquisition='verified_local_cache', cacheCommit=cache_commit)
            else:
                git(['clone', '--no-checkout', '--no-local', '--', remote, str(target)])
            git(['checkout', '--detach', commit, '--'], target)
        verify(target, remote, commit)
        state.update(status='ready', reused=existed, runtimeValidated=False)
        save(statepath, state)
        return state
    except Exception as error:
        state.update(status='failed', error=str(error) if isinstance(error, CandidateError) else type(error).__name__)
        save(statepath, state)
        raise
    finally:
        mutex.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repository', choices=sorted(OFFICIAL_REMOTES))
    parser.add_argument('commit', help='Explicit full 40-character Git commit SHA')
    parser.add_argument('--use-local-cache', action='store_true', help='Use only a verified ready candidate in this project; never fall back to network')
    args = parser.parse_args()
    try:
        result = prepare(Path(__file__).resolve().parents[1], args.repository, args.commit, use_local_cache=args.use_local_cache)
    except (CandidateError, OSError, ValueError, KeyError) as error:
        print('Candidate preparation failed: ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0

if __name__ == '__main__':
    sys.exit(main())
