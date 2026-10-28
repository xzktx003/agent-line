"""Explicit compiler-owned loop proofs; no native service or model execution."""
import copy
import unittest

import httpx

from agent_line.adapters.sim import SimClient
from agent_line.publication import PublicationVerifier, PublicationUnverified
from agent_line import sim_authoring


def document():
    def node(name, kind, config):
        return dict(id=name, type=kind, name=name, position={'x':0,'y':0}, config=config)
    return {'schemaVersion':1, 'nodes':[
        node('start','start',{}), node('loop','loop',{'body':'first','maxIterations':3}),
        node('first','agent',{'roleBindingId':'role','instruction':'Do bounded work',
             'inputFrom':{'nodeId':'start','field':'input'},'memoryPolicyId':'native-role',
             'outputSchemaId':'task-result','requireMemoryCommit':False,'allowFailureBranch':True}),
        node('end','end',{'resultFrom':'first'})],
        'edges':[{'id':'a','source':'start','target':'loop'},
                 {'id':'b','source':'loop','target':'first','branch':'body'},
                 {'id':'c','source':'loop','target':'end','branch':'done'}], 'variables':{}}


class LoopPublicationTests(unittest.TestCase):
    def setUp(self):
        self.doc = document()
        self.graph = self.native(self.doc)
        self.policies = sim_authoring.loop_policies(self.doc, 'workflow')
        self.blocks = sorted(self.policies)
        self.version = {'id':'deployment','version':1,'isActive':True,'state':copy.deepcopy(self.graph)}
        self.run = {'runId':'run','workflowId':'workflow','deploymentVersionId':'deployment',
                    'workflowState':copy.deepcopy(self.graph)}
        self.snapshot = {'workflowId':'workflow','runId':'run','revision':'1',
                         'deploymentVersionId':'deployment','humanBlockIds':self.blocks,
                         'loopPolicies':self.policies}
        self.requests = []
        def handle(request):
            self.requests.append(request)
            return httpx.Response(200,json={'data':self.run if request.url.path=='/api/v2/logs/run' else self.version})
        client = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(client.close)
        self.verifier = PublicationVerifier(SimClient('http://sim','test-only-key',client=client))

    @staticmethod
    def native(doc):
        graph = sim_authoring.compile_document(doc, 'workflow')
        graph['loops'] = sim_authoring._derived_loops(graph['blocks'])
        graph['parallels'] = {}
        return graph

    def definition(self, policies=None):
        return self.verifier.check_definition('workflow','1','deployment',self.blocks,
                                             loop_policies=self.policies if policies is None else policies)

    def test_explicit_policy_proves_definition_and_actual_snapshot(self):
        self.assertIs(self.definition(),True)
        self.assertIs(self.verifier.verify_run(self.snapshot),True)
        self.assertTrue(all(request.method=='GET' for request in self.requests))

    def test_absent_policy_keeps_original_container_fence(self):
        with self.assertRaises(PublicationUnverified):
            self.verifier.check_definition('workflow','1','deployment',self.blocks)
        snapshot = dict(self.snapshot); snapshot.pop('loopPolicies')
        with self.assertRaises(PublicationUnverified):self.verifier.verify_run(snapshot)

    def test_strict_policy_identity_max_and_field_shapes(self):
        body = self.blocks[0]
        cases = [{},[],{body:{'loopId':'other','maxIterations':3}},
                 {body:{**self.policies[body],'maxIterations':True}},
                 {body:{**self.policies[body],'maxIterations':0}},
                 {body:{**self.policies[body],'maxIterations':4}},
                 {body:{**self.policies[body],'extra':1}},
                 {'other':self.policies[body]}]
        for policies in cases:
            with self.subTest(policies=policies),self.assertRaises(PublicationUnverified):
                self.definition(policies)

    def test_native_wait_disabled_type_or_unregistered_wait_rejected(self):
        for field,value in [('enabled',False),('type','agent'),('id','other')]:
            self.version['state']=copy.deepcopy(self.graph)
            self.version['state']['blocks'][self.blocks[0]][field]=value
            with self.subTest(field=field),self.assertRaises(PublicationUnverified):self.definition()

    def test_member_container_and_parallel_tampering_rejected(self):
        container = next(iter(self.graph['loops']))
        cases = [lambda g:g['loops'][container].update(nodes=[]),
                 lambda g:g['loops'][container].update(nodes=self.blocks*2),
                 lambda g:g['loops'][container].update(iterations=99),
                 lambda g:g.update(loops={}),
                 lambda g:g.update(parallels={'other':{'nodes':self.blocks}}),
                 lambda g:g['blocks'][self.blocks[0]].update(data={'parentId':'other'})]
        for mutation in cases:
            self.version['state']=copy.deepcopy(self.graph);mutation(self.version['state'])
            with self.subTest(mutation=mutation),self.assertRaises(PublicationUnverified):self.definition()

    def test_unrecognized_expression_extra_human_and_feedback_edge_rejected(self):
        container = next(iter(self.graph['loops']))
        def expression(graph):
            graph['blocks'][container]['data']['doWhileCondition'] = 'true'
            graph['loops'][container]['doWhileCondition'] = 'true'
        def extra_human(graph):
            graph['blocks']['extra'] = {'id':'extra','type':'human_in_the_loop_v2','enabled':True}
        def feedback(graph):
            graph['edges'].append({'id':'extra','source':self.blocks[0],'target':container})
        for mutation in (expression, extra_human, feedback):
            self.version['state'] = copy.deepcopy(self.graph)
            mutation(self.version['state'])
            with self.subTest(mutation=mutation),self.assertRaises(PublicationUnverified):
                self.definition()

    def test_only_generated_guard_code_edges_and_members_are_allowed(self):
        guards = [key for key, block in self.graph['blocks'].items() if block['type'] == 'function']
        self.assertEqual(len(guards), 1, 'Loop must include a generated native validator')
        guard = guards[0]
        container = next(iter(self.graph['loops']))
        self.assertEqual(set(self.graph['loops'][container]['nodes']), {self.blocks[0], guard})
        def code(graph):
            graph['blocks'][guard]['subBlocks']['code']['value'] = 'return {valid:true};'
        def missing_member(graph):
            graph['loops'][container]['nodes'].remove(guard)
        def disabled(graph):
            graph['blocks'][guard]['enabled'] = False
        def wrong_type(graph):
            graph['blocks'][guard]['type'] = 'agent'
        def missing_edge(graph):
            graph['edges'] = [edge for edge in graph['edges'] if edge['target'] != guard]
        def extra_member(graph):
            graph['loops'][container]['nodes'].append('unrecognized')
        for mutation in (code, missing_member, disabled, wrong_type, missing_edge, extra_member):
            self.version['state'] = copy.deepcopy(self.graph)
            mutation(self.version['state'])
            with self.subTest(mutation=mutation), self.assertRaises(PublicationUnverified):
                self.definition()

    def test_same_version_identity_different_valid_graph_is_rejected(self):
        changed=copy.deepcopy(self.doc);changed['nodes'][2]['config']['instruction']='Different valid instruction'
        self.run['workflowState']=self.native(changed)
        with self.assertRaisesRegex(PublicationUnverified,'run_graph_mismatch'):
            self.verifier.verify_run(self.snapshot)

    def test_official_log_projection_defaults_only_preserve_full_graph_proof(self):
        original = copy.deepcopy(self.run['workflowState'])
        projected = self.run['workflowState']
        projected.pop('variables')
        for edge in projected['edges']:
            edge.setdefault('sourceHandle', None)
            edge.setdefault('targetHandle', None)
        frozen = copy.deepcopy(projected)
        self.assertIs(self.verifier.verify_run(self.snapshot), True)
        self.assertEqual(projected, frozen, 'Do not mutate the received snapshot')
        self.assertEqual(self.version['state'], original, 'Never normalize the publication in place')
        for bad in (None, {'unexpected':'value'}):
            projected['variables'] = bad
            with self.assertRaises(PublicationUnverified):self.verifier.verify_run(self.snapshot)
        projected.pop('variables')
        ordinary = next(edge for edge in projected['edges'] if edge['sourceHandle'] is None)
        ordinary['sourceHandle'] = 'unknown-port'
        with self.assertRaises(PublicationUnverified):self.verifier.verify_run(self.snapshot)
        ordinary['sourceHandle'] = None
        ordinary['targetHandle'] = 'unknown-port'
        with self.assertRaises(PublicationUnverified):self.verifier.verify_run(self.snapshot)

    def test_run_and_published_loop_policy_both_must_match(self):
        changed=copy.deepcopy(self.doc);changed['nodes'][1]['config']['maxIterations']=2
        for target,key in [(self.run,'workflowState'),(self.version,'state')]:
            target[key]=self.native(changed)
            with self.assertRaises(PublicationUnverified):self.verifier.verify_run(self.snapshot)
            target[key]=copy.deepcopy(self.graph)

    def test_old_inactive_definition_remains_readable_only(self):
        self.version['isActive']=False
        self.assertIs(self.verifier.verify_run(self.snapshot),True)
        with self.assertRaisesRegex(PublicationUnverified,'inactive_version'):
            self.verifier.check_definition('workflow','1','deployment',self.blocks,
                require_active=True,loop_policies=self.policies)
