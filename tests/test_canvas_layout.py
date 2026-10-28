import copy
import json
import subprocess
import unittest

from agent_line.sim_authoring import compile_document, decode_document


LAYOUT = "frontend/canvas-layout.js"


def node(kind, node_id, config=None):
    return {'id': node_id, 'type': kind, 'name': node_id,
            'position': {'x': 0, 'y': 0}, 'config': config or {}}


def run_layout(document):
    source = f"const api=require('./{LAYOUT}');let input='';process.stdin.on('data',x=>input+=x);process.stdin.on('end',()=>{{try{{console.log(JSON.stringify({{ok:true,doc:api.layout(JSON.parse(input))}}))}}catch(e){{console.log(JSON.stringify({{ok:false,error:e.message}}))}}}});"
    result = subprocess.run(['node', '-e', source], input=json.dumps(document),
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


class CanvasLayoutTests(unittest.TestCase):
    def test_small_fork_merge_layout_compiles_with_same_semantics(self):
        document = {'schemaVersion': 1, 'variables': {}, 'nodes': [
            node('start', 'start'),
            node('parallel', 'fork', {'branches': ['left', 'right'], 'join': 'merge', 'maxConcurrency': 2}),
            node('agent', 'left', {'roleBindingId': 'developer', 'instruction': 'work',
                 'inputFrom': {'nodeId': 'start', 'field': 'input'}, 'memoryPolicyId': 'native',
                 'outputSchemaId': 'result', 'requireMemoryCommit': False, 'allowFailureBranch': True}),
            node('agent', 'right', {'roleBindingId': 'developer', 'instruction': 'work',
                 'inputFrom': {'nodeId': 'start', 'field': 'input'}, 'memoryPolicyId': 'native',
                 'outputSchemaId': 'result', 'requireMemoryCommit': False, 'allowFailureBranch': True}),
            node('join', 'merge', {'parallelId': 'fork', 'policy': 'all_success', 'requiredBranches': []}),
            node('end', 'end', {'resultFrom': 'merge'})], 'edges': [
            {'id': 'a', 'source': 'start', 'target': 'fork'},
            {'id': 'b', 'source': 'fork', 'target': 'left'}, {'id': 'c', 'source': 'fork', 'target': 'right'},
            {'id': 'd', 'source': 'left', 'target': 'merge'}, {'id': 'e', 'source': 'right', 'target': 'merge'},
            {'id': 'f', 'source': 'merge', 'target': 'end'}]}
        before = compile_document(document, 'layout-test')
        positioned = run_layout(document)
        self.assertTrue(positioned['ok'], positioned)
        laid_out = positioned['doc']
        after = compile_document(laid_out, 'layout-test')
        self.assertEqual(decode_document(after), laid_out)
        self.assertEqual(before['edges'], after['edges'])
        self.assertEqual(before['variables'], after['variables'])
        self.assertEqual({k: v['subBlocks'] for k, v in before['blocks'].items()},
                         {k: v['subBlocks'] for k, v in after['blocks'].items()})
        self.assertNotEqual([n['position'] for n in document['nodes']],
                            [n['position'] for n in laid_out['nodes']])
        self.assertEqual(document['nodes'][1]['position'], {'x': 0, 'y': 0})

    def test_long_chain_and_wide_fanout_fit_canvas_without_overlap(self):
        chain = {'nodes': [node('agent', f'n{i}') for i in range(200)],
                 'edges': [{'source': f'n{i}', 'target': f'n{i + 1}'} for i in range(199)]}
        fanout = {'nodes': [node('agent', f'n{i}') for i in range(200)],
                  'edges': [{'source': 'n0', 'target': f'n{i}'} for i in range(1, 200)]}
        for document in (chain, fanout):
            result = run_layout(document)
            self.assertTrue(result['ok'], result)
            positions = [n['position'] for n in result['doc']['nodes']]
            self.assertEqual(len({(p['x'], p['y']) for p in positions}), 200)
            for p in positions:
                self.assertGreaterEqual(p['x'], 0); self.assertLessEqual(p['x'], 3000 - 170)
                self.assertGreaterEqual(p['y'], 0); self.assertLessEqual(p['y'], 3000 - 64)
            for i, a in enumerate(positions):
                for b in positions[i + 1:]:
                    self.assertTrue(a['x'] + 170 <= b['x'] or b['x'] + 170 <= a['x'] or
                                    a['y'] + 64 <= b['y'] or b['y'] + 64 <= a['y'])

    def test_duplicate_positions_are_replaced_and_configs_remain_immutable(self):
        document = {'nodes': [node('start', 'start'), node('end', 'end')],
                    'edges': [{'source': 'start', 'target': 'end'}]}
        original = copy.deepcopy(document)
        result = run_layout(document)
        self.assertTrue(result['ok'], result)
        laid_out = result['doc']
        self.assertIsNot(laid_out, document)
        for old, new in zip(document['nodes'], laid_out['nodes']):
            self.assertIsNot(old, new)
            self.assertIsNot(old['position'], new['position'])
            self.assertEqual(old['config'], new['config'])
        self.assertEqual(document, original)

    def test_invalid_empty_duplicate_id_dangling_edge_and_cycle_rejected_in_chinese(self):
        cases = [
            {'nodes': [], 'edges': []},
            {'nodes': [node('agent', 'same'), node('agent', 'same')], 'edges': []},
            {'nodes': [node('agent', 'a'), node('agent', 'b')],
             'edges': [{'source': 'a', 'target': 'missing'}]},
            {'nodes': [node('agent', 'a'), node('agent', 'b')],
             'edges': [{'source': 'a', 'target': 'b'}, {'source': 'b', 'target': 'a'}]},
        ]
        for document in cases:
            with self.subTest(document=document):
                result = run_layout(document)
                self.assertFalse(result['ok'])
                self.assertIn('画布布局失败', result['error'])


if __name__ == '__main__':
    unittest.main()
