"""Ordinary human pauses coexist with a compiler-owned machine repair loop."""
import copy
import unittest

from agent_line.publication import PublicationUnverified
from agent_line.sim_authoring import native_id
from tests import test_loop_publication as fixtures


class MixedPublicationTests(unittest.TestCase):
    def setUp(self):
        f = fixtures.LoopPublicationTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.f = f
        self.doc = fixtures.document()
        self.doc['nodes'].insert(-1, {'id':'approval','type':'human','name':'人工确认',
            'position':{'x':800,'y':0},'config':{'requiredBoolean':'approved'}})
        self.doc['edges'][-1]['target'] = 'approval'
        self.doc['edges'].append({'id':'d','source':'approval','target':'end'})
        self.human = native_id('workflow','approval')
        f.graph = f.native(self.doc)
        f.blocks.append(self.human)
        f.version['state'] = copy.deepcopy(f.graph)
        f.run['workflowState'] = copy.deepcopy(f.graph)
        f.snapshot['humanBlockIds'] = list(f.blocks)

    def test_declared_human_after_loop_definition_and_run_are_proven(self):
        self.assertIs(self.f.definition(), True)
        self.assertIs(self.f.verifier.verify_run(self.f.snapshot), True)
        self.assertTrue(all(r.method == 'GET' for r in self.f.requests))

    def test_declared_human_before_loop_is_also_ordinary_dag(self):
        self.doc['edges'] = [
            {'id':'a','source':'start','target':'approval'},
            {'id':'b','source':'approval','target':'loop'},
            {'id':'c','source':'loop','target':'first','branch':'body'},
            {'id':'d','source':'loop','target':'end','branch':'done'}]
        graph = self.f.native(self.doc)
        self.f.version['state'] = graph
        self.f.run['workflowState'] = copy.deepcopy(graph)
        self.assertTrue(self.f.definition())
        self.assertTrue(self.f.verifier.verify_run(self.f.snapshot))

    def test_all_machine_and_human_waits_must_be_declared(self):
        for blocks in ([self.human], [b for b in self.f.blocks if b != self.human], self.f.blocks + ['unknown']):
            with self.subTest(blocks=blocks), self.assertRaises(PublicationUnverified):
                self.f.verifier.check_definition('workflow','1','deployment',blocks,loop_policies=self.f.policies)

    def test_human_in_container_parallel_or_cycle_stays_rejected(self):
        container = next(iter(self.f.graph['loops']))
        def inside(graph):
            graph['blocks'][self.human]['data'] = {'parentId':container,'extent':'parent'}
            graph['loops'][container]['nodes'].append(self.human)
        changes = [inside,
            lambda graph:graph.update(parallels={'parallel':{'nodes':[self.human]}}),
            lambda graph:graph['edges'].append({'id':'back','source':self.human,'target':self.human})]
        for change in changes:
            self.f.version['state'] = copy.deepcopy(self.f.graph)
            change(self.f.version['state'])
            with self.assertRaises(PublicationUnverified):self.f.definition()

    def test_unknown_guard_and_changed_actual_human_parameters_rejected(self):
        guard = next(k for k,b in self.f.graph['blocks'].items() if b['type']=='function')
        self.f.version['state']['blocks'][guard]['subBlocks']['code']['value'] = 'return true'
        with self.assertRaises(PublicationUnverified):self.f.definition()
        self.f.version['state'] = copy.deepcopy(self.f.graph)
        changed = copy.deepcopy(self.doc)
        next(n for n in changed['nodes'] if n['id']=='approval')['config']['requiredBoolean']='differentDecision'
        self.f.run['workflowState'] = self.f.native(changed)
        with self.assertRaises(PublicationUnverified):self.f.verifier.verify_run(self.f.snapshot)
