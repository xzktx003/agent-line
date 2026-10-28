"""Portable static templates compiled by the actual existing graph compiler."""
import copy
import json
from pathlib import Path
import subprocess
import unittest

from agent_line.sim_authoring import compile_document, decode_document


ROOT = Path(__file__).resolve().parents[1]
ROLES = [{'roleBindingId': slot, 'roleAgentId': 'fixture-' + slot,
          'memoryPolicyId': 'native-role', 'outputSchemaId': 'task-result'}
         for slot in ('planner', 'developer', 'reviewer')]


def javascript(action, **arguments):
    source = "const t=require('./frontend/workflow-templates.js'); const a=JSON.parse(require('fs').readFileSync(0,'utf8')); "
    source += "try { let value; " + action + "; console.log(JSON.stringify({ok:true,value})); } catch(e) { console.log(JSON.stringify({ok:false,error:e.message})); }"
    result = subprocess.run(['node', '-e', source], cwd=ROOT, input=json.dumps(arguments),
                            text=True, capture_output=True, timeout=10, check=True)
    return json.loads(result.stdout)


class WorkflowTemplateTests(unittest.TestCase):
    def instantiate(self, template, mapping=None, roles=None, prefix='fresh_template'):
        mapping = mapping or ({'developer': 'developer'} if template == 'bounded-repair-v1'
                              else {slot: slot for slot in ('planner', 'developer', 'reviewer')})
        return javascript('value=t.instantiate(a.template,a.mapping,a.roles,a.prefix)',
                          template=template, mapping=mapping, roles=ROLES if roles is None else roles, prefix=prefix)

    def test_both_templates_roundtrip_through_real_compiler_and_keep_human_gate(self):
        for template in ('delivery-review-v1', 'bounded-repair-v1'):
            with self.subTest(template=template):
                response = self.instantiate(template)
                self.assertTrue(response['ok'], response)
                document = response['value']
                self.assertEqual(decode_document(compile_document(document, 'owned-workflow')), document)
                self.assertEqual(document['variables'], {})
                human = next(node for node in document['nodes'] if node['type'] == 'human')
                decisions = [node for node in document['nodes'] if node['type'] == 'condition'
                             and any(rule.get('source') == human['id'] for rule in node['config']['rules'])]
                self.assertEqual(len(decisions), 1)
                self.assertEqual(decisions[0]['config']['rules'][0]['field'], 'result.approval.approved')
                self.assertIs(decisions[0]['config']['rules'][0]['value'], True)
                for node in document['nodes']:
                    if node['type'] == 'agent':
                        self.assertEqual(node['config']['memoryPolicyId'], 'native-role')
                        self.assertFalse(node['config']['requireMemoryCommit'])

    def test_team_uses_distinct_existing_ids_and_explicit_failed_verification_exits(self):
        document = self.instantiate('delivery-review-v1')['value']
        agents = [node for node in document['nodes'] if node['type'] == 'agent']
        self.assertEqual({node['config']['roleBindingId'] for node in agents}, {'planner', 'developer', 'reviewer'})
        failed = next(node for node in document['nodes'] if node['name'] == '验收未通过')
        verification = [node for node in document['nodes'] if node['type'] == 'condition'
                        and node['config']['rules'][0]['field'] == 'result.verificationStatus']
        self.assertEqual(len(verification), 3)
        for node in verification:
            self.assertEqual([edge['target'] for edge in document['edges']
                              if edge['source'] == node['id'] and edge.get('branch') == 'else'], [failed['id']])

    def test_repair_uses_native_three_round_loop_and_real_verification_field(self):
        document = self.instantiate('bounded-repair-v1')['value']
        loop = next(node for node in document['nodes'] if node['type'] == 'loop')
        self.assertEqual(loop['config']['maxIterations'], 3)
        body = next(node for node in document['nodes'] if node['id'] == loop['config']['body'])
        self.assertTrue(body['config']['allowFailureBranch'])
        self.assertFalse(any(edge['source'] == body['id'] for edge in document['edges']))

    def test_missing_cross_project_or_duplicate_formal_role_mappings_are_rejected(self):
        for mapping in ({'planner':'planner'}, {'planner':'other', 'developer':'developer', 'reviewer':'reviewer'},
                        {'planner':'developer', 'developer':'developer', 'reviewer':'reviewer'}):
            self.assertFalse(self.instantiate('delivery-review-v1', mapping=mapping)['ok'])
        roles = copy.deepcopy(ROLES); roles[2]['roleAgentId'] = roles[0]['roleAgentId']
        self.assertFalse(self.instantiate('delivery-review-v1', roles=roles)['ok'])
        roles = copy.deepcopy(ROLES); roles[1]['memoryPolicyId'] = 'untrusted-global-memory'
        self.assertFalse(self.instantiate('delivery-review-v1', roles=roles)['ok'])


    def test_each_instantiation_has_independent_graph_identity_and_unknown_template_is_rejected(self):
        left = self.instantiate('bounded-repair-v1', prefix='first_template')['value']
        right = self.instantiate('bounded-repair-v1', prefix='second_template')['value']
        self.assertFalse({node['id'] for node in left['nodes']} & {node['id'] for node in right['nodes']})
        self.assertFalse(self.instantiate('unknown-template')['ok'])
        self.assertFalse(self.instantiate('bounded-repair-v1', prefix='bad<script>')['ok'])
