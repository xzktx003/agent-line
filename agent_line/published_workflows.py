"""Project-owned published bindings derived from the existing outbound journal.

No second workflow registry: only a verified authoring publication creates a
binding. Historical receipts without this contract remain historical receipts.
"""
import hashlib
import copy
import json
import re

from .adapters.sim import segment
from .sim_authoring import AuthoringError, compile_document, decode_document
from .store import canonical


def binding_id(project, workflow, deployment):
    return 'published-' + hashlib.sha256(canonical([project, workflow, deployment]).encode()).hexdigest()[:48]


def graph_hash(state, workflow, *, native=False):
    if not native:
        state = compile_document(decode_document(state), workflow)
    else:
        fields = {'blocks', 'edges', 'loops', 'parallels', 'variables'}
        if (not isinstance(state, dict) or not fields <= set(state) <= fields | {'lastSaved'}
                or not isinstance(state['blocks'], dict) or not isinstance(state['edges'], list)
                or len(canonical(state).encode()) > 1_000_000):
            raise AuthoringError('native_graph_invalid')
        state = {key: copy.deepcopy(state[key]) for key in fields}
        for edge in state['edges']:
            if not isinstance(edge, dict):
                raise AuthoringError('native_graph_invalid')
            # Native state GET omits empty edge data retained by version GET.
            if edge.get('data') in (None, {}):
                edge.pop('data', None)
    return hashlib.sha256(canonical(state).encode()).hexdigest()


class PublishedWorkflowCatalog:
    def __init__(self, authoring):
        self.authoring = authoring

    def bindings(self, scopes):
        from .authoring_api import namespace
        result = {}
        with self.authoring.journal.connect() as db:
            for project in sorted(set(scopes) & self.authoring.clients.keys()):
                if self.authoring.projects[project].get('enabled', True) is not True:
                    continue
                rows = db.execute(
                    "SELECT p.receipt FROM operations p WHERE p.workflow=? "
                    "AND p.context='author-publish' AND p.status='accepted' "
                    "AND json_extract(p.receipt,'$.outcome')='published' "
                    "AND EXISTS (SELECT 1 FROM operations c WHERE c.workflow=p.workflow "
                    "AND c.context='author-create' AND c.status='accepted' "
                    "AND json_extract(c.receipt,'$.workflowId')=json_extract(p.receipt,'$.workflowId'))",
                    (namespace(project),)).fetchall()
                for row in rows:
                    receipt = json.loads(row['receipt'])
                    # No silent adoption of pre-catalog publications.
                    if 'bindingId' not in receipt or 'graphSha256' not in receipt:
                        continue
                    workflow, deployment = segment(receipt['workflowId']), segment(receipt['deploymentVersionId'])
                    revision = receipt['revision']
                    if (type(revision) is not int or revision < 1
                            or not isinstance(receipt['graphSha256'], str)
                            or not re.fullmatch('[0-9a-f]{64}', receipt['graphSha256'])
                            or receipt['bindingId'] != binding_id(project, workflow, deployment)):
                        raise ValueError('Invalid published workflow receipt')
                    binding = {'projectId': project, 'workflowId': workflow,
                               'publication': {key: receipt[key] for key in
                                   ('revision', 'deploymentVersionId', 'graphSha256')}}
                    if 'graphFormat' in receipt:
                        if receipt['graphFormat'] != 'sim-native-v1':
                            raise ValueError('Unknown published graph format')
                        binding['publication']['graphFormat'] = receipt['graphFormat']
                    if 'externalTasks' in receipt:
                        policy = receipt['externalTasks']
                        if (not isinstance(policy, dict) or policy.get('revision') != str(revision)
                                or policy.get('deploymentVersionId') != deployment):
                            raise ValueError('Published machine policy mismatch')
                        binding['externalTasks'] = policy
                    if 'humanApproval' in receipt:
                        policy = receipt['humanApproval']
                        from .approval_discovery import validate_human_policy
                        machine = binding.get('externalTasks', {}).get('agentPolicies', {})
                        if not isinstance(machine, dict):
                            raise ValueError('Published machine policy mismatch')
                        validate_human_policy(policy, revision=str(revision), deployment=deployment,
                                              agent_policies=machine)
                        binding['humanApproval'] = policy
                    key = receipt['bindingId']
                    if key in result and result[key] != binding:
                        raise ValueError('Conflicting published workflow receipts')
                    result[key] = binding
        return result

    def check_active(self, binding):
        """Public Sim execution selects the active deployment, not a pinned ID.

        Serialize with our publisher; an independent Sim editor can still race.
        Actual machine-task run snapshots are separately checked before dispatch.
        """
        publication = binding['publication']
        version = self.authoring.clients[binding['projectId']].get_version(
            binding['workflowId'], publication['revision'])
        if (not isinstance(version, dict) or version.get('isActive') is not True
                or version.get('id') != publication['deploymentVersionId']
                or type(version.get('version')) is not int
                or version['version'] != publication['revision']
                or graph_hash(version.get('state'), binding['workflowId'],
                              native=publication.get('graphFormat') == 'sim-native-v1') != publication['graphSha256']):
            raise AuthoringError('published_version_changed')

    def execution_scope(self):
        return self.authoring._edit_lock()
