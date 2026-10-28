"""Read-only, fail-closed compatibility gate. It never performs an upgrade."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path

from scripts.check_upstreams import inspect

REQUIRED_PROBES = (
    'plugin_routes_auth', 'sim_graph_wait_resume', 'role_identity_persistence',
    'codex_delegation_identity', 'approval_mapping', 'memory_recall_injection_writeback',
    'project_isolation', 'inflight_reconciliation', 'data_migration_restore',
)
REPOSITORIES = frozenset({'qwenpaw', 'sim'})
MAX_JSON_BYTES = 1_048_576


class UpgradeInputError(ValueError):
    """Only fixed public reason codes may cross the CLI boundary."""


def read_json_bytes(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JSON_BYTES:
            raise UpgradeInputError('invalid_json_file')
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise UpgradeInputError('json_file_too_large')
    return raw


def parse_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise UpgradeInputError('ambiguous_json_object')
            result[key] = value
        return result

    def invalid(_value):
        raise UpgradeInputError('nonfinite_json_number')

    def finite(value):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else invalid(value)

    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                          parse_constant=invalid, parse_float=finite)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise UpgradeInputError('invalid_json_document') from None


def valid_observation_time(value, now):
    if type(value) in (int, float):
        return 0 < value <= now + 300 and math.isfinite(value)
    if not isinstance(value, str) or not re.fullmatch(
            r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}'
            r'(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])', value):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        stamp = parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return False
    return 0 < stamp <= now + 300


def contained(root, value):
    if not isinstance(value, str) or not value:
        raise UpgradeInputError('invalid_path')
    path = (root / value).resolve()
    if not path.is_relative_to(root) or path == root:
        raise UpgradeInputError('path_outside_installation')
    return path


def pin_valid(pin):
    return (isinstance(pin, dict) and isinstance(pin.get('remote'), str)
            and bool(pin['remote']) and isinstance(pin.get('commit'), str)
            and re.fullmatch(r'[0-9a-f]{40}', pin['commit']) is not None)


def evaluate(root: Path, manifest: dict) -> dict:
    """Validate pinned checkouts and supplied results, without executing probe code.

    The caller must obtain evidence from trusted real probe runs. A hash binds
    bytes, not their truth; this is not a signature or an independent live test.
    """
    report = {'compatibilityApproved': False, 'upgradeExecuted': False,
              'scope': 'read_only_compatibility_preflight',
              'requiredProbes': list(REQUIRED_PROBES), 'repositories': {},
              'probes': {}, 'errors': []}
    try:
        root = Path(root).resolve()
        observed_now = datetime.now(timezone.utc).timestamp()
        lock = parse_json(read_json_bytes(root / 'upstreams.lock.json'))
        if (not isinstance(lock, dict) or type(lock.get('schemaVersion')) is not int
                or lock['schemaVersion'] != 1 or not isinstance(lock.get('repositories'), dict)
                or set(lock['repositories']) != REPOSITORIES):
            raise UpgradeInputError('invalid_current_lock')
        if (not isinstance(manifest, dict) or type(manifest.get('schemaVersion')) is not int
                or manifest['schemaVersion'] != 1):
            raise UpgradeInputError('invalid_manifest')
        current = lock['repositories']
        candidates = manifest.get('candidates', {})
        if not isinstance(candidates, dict) or set(candidates) != REPOSITORIES:
            raise UpgradeInputError('candidate_set_must_match_current_lock')
        for name in sorted(REPOSITORIES):
            old, new = current[name], candidates[name]
            if not pin_valid(old) or not pin_valid(new):
                raise UpgradeInputError('invalid_repository_pin')
            errors = []
            old_path = contained(root, old.get('path'))
            new_path = contained(root, new.get('path'))
            if not old_path.is_relative_to(root / 'upstream'):
                errors.append('current_checkout_outside_upstream')
            errors.extend('current_' + error for error in inspect(old_path, old))
            errors.extend('candidate_' + error for error in inspect(new_path, new))
            if new['remote'] != old['remote']:
                errors.append('candidate_remote_changed')
            report['repositories'][name] = {'currentCommit': old['commit'],
                'candidateCommit': new['commit'], 'currentPath': old['path'],
                'candidatePath': new['path'], 'errors': errors}
        current_commits = {n: p['commit'] for n, p in current.items()}
        candidate_commits = {n: p['commit'] for n, p in candidates.items()}
        probes = manifest.get('probes', {})
        if not isinstance(probes, dict):
            raise UpgradeInputError('invalid_probe_results')
        for name in REQUIRED_PROBES:
            record = probes.get(name)
            result = {'status': 'unverified', 'evidencePath': None, 'errors': []}
            report['probes'][name] = result
            if not isinstance(record, dict):
                result['errors'].append('missing_required_evidence')
                continue
            try:
                evidence_path = contained(root, record.get('evidencePath'))
                raw = read_json_bytes(evidence_path)
                result['evidencePath'] = str(evidence_path.relative_to(root))
                if hashlib.sha256(raw).hexdigest() != record.get('sha256'):
                    result['errors'].append('evidence_hash_mismatch')
                evidence = parse_json(raw)
                if not isinstance(evidence, dict):
                    raise ValueError('invalid_evidence_object')
                status = evidence.get('status')
                result['status'] = status if status in ('passed', 'failed', 'unverified') else 'unverified'
                if evidence.get('probeId') != name:
                    result['errors'].append('probe_identity_mismatch')
                if evidence.get('currentCommits') != current_commits:
                    result['errors'].append('current_evidence_mismatch')
                if evidence.get('candidateCommits') != candidate_commits:
                    result['errors'].append('candidate_evidence_mismatch')
                if evidence.get('status') != 'passed':
                    result['errors'].append('probe_not_passed')
                if evidence.get('mode') != 'live':
                    result['errors'].append('probe_not_live')
                details = evidence.get('details')
                if (not valid_observation_time(evidence.get('observedAt'), observed_now)
                        or not ((isinstance(details, str) and details.strip())
                                or (isinstance(details, dict) and details))):
                    result['errors'].append('evidence_context_missing')
            except (OSError, ValueError, TypeError, RecursionError, OverflowError):
                result['errors'].append('invalid_or_unavailable_evidence')
    except (OSError, ValueError, KeyError, TypeError, RecursionError, OverflowError) as exc:
        # Report a bounded reason, never file contents, credentials, or subprocess output.
        report['errors'].append(str(exc) if isinstance(exc, UpgradeInputError)
                                else 'invalid_or_unavailable_manifest_or_lock')
    report['compatibilityApproved'] = (
        not report['errors'] and len(report['repositories']) == len(REPOSITORIES)
        and len(report['probes']) == len(REQUIRED_PROBES)
        and all(not r['errors'] for r in report['repositories'].values())
        and all(not p['errors'] for p in report['probes'].values())
    )
    return report


def main():
    parser = argparse.ArgumentParser(description='只读升级兼容性准入检查，不拉取或修改上游')
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        manifest = parse_json(read_json_bytes(args.manifest))
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        print(json.dumps({'compatibilityApproved': False, 'upgradeExecuted': False,
                          'errors': ['invalid_or_unavailable_manifest']}))
        return 1
    report = evaluate(args.root, manifest)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['compatibilityApproved'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
