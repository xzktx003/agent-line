"""Closed static forks use pinned Sim's DAG and preserve native Task results."""
import copy
import json
import subprocess
import unittest

from agent_line.sim_authoring import (
    AuthoringError, UnsupportedDocument, compile_document, decode_document, native_id,
)


def document(policy='all_success', required=None):
    def node(nid, kind, config):
        return {'id': nid, 'type': kind, 'name': '节点'+nid,
                'position': {'x': 0, 'y': 0}, 'config': config}
    def agent():
        return {'roleBindingId': 'developer', 'instruction': '保留原生角色与记忆',
                'inputFrom': {'nodeId': 'start', 'field': 'input'},
                'memoryPolicyId': 'native-memory', 'outputSchemaId': 'result-v1',
                'requireMemoryCommit': True, 'allowFailureBranch': True}
    return {'schemaVersion': 1, 'variables': {}, 'nodes': [
        node('start', 'start', {}),
        node('fork', 'parallel', {'branches': ['left', 'right'], 'join': 'merge', 'maxConcurrency': 2}),
        node('left', 'agent', agent()), node('right', 'agent', agent()),
        node('merge', 'join', {'parallelId': 'fork', 'policy': policy,
                             'requiredBranches': required or []}),
        node('end', 'end', {'resultFrom': 'merge'})], 'edges': [
        {'id': 'a', 'source': 'start', 'target': 'fork'},
        {'id': 'b', 'source': 'fork', 'target': 'left'},
        {'id': 'c', 'source': 'fork', 'target': 'right'},
        {'id': 'd', 'source': 'left', 'target': 'merge'},
        {'id': 'e', 'source': 'right', 'target': 'merge'},
        {'id': 'f', 'source': 'merge', 'target': 'end'}]}


def result(task, execution='succeeded', verification='passed'):
    return {'taskId': task, 'executionStatus': execution, 'verificationStatus': verification,
            'output': {'unchanged': task}, 'evidence': [{'original': True}], 'memoryStatus': 'committed'}


class StaticParallelCompilerTests(unittest.TestCase):
    def code(self, doc, node='merge'):
        return compile_document(doc, 'wf')['blocks'][native_id('wf', node)]['subBlocks']['code']['value']

    def run_join(self, policy, left, right, required=None):
        code = self.code(document(policy, required))
        for branch, value in [('left', left), ('right', right)]:
            code = code.replace('<节点'+branch+'.resumeInput>',
                                'undefined' if value is ... else json.dumps(value))
        source = 'try { console.log(JSON.stringify({ok:true,value:(()=>{'+code+'})()})); } catch(e) { console.log(JSON.stringify({ok:false,error:e.message})); }'
        proc = subprocess.run(['node', '-e', source], capture_output=True, text=True, check=True)
        return json.loads(proc.stdout)

    def test_roundtrip_uses_native_functions_and_preserves_agent_policy(self):
        doc = document(); state = compile_document(doc, 'wf')
        self.assertEqual(decode_document(state), doc)
        self.assertEqual([b['type'] for b in state['blocks'].values()].count('function'), 2)
        self.assertEqual(set(state), {'blocks', 'edges', 'variables'})
        self.assertEqual(state['blocks'][native_id('wf', 'end')]['subBlocks']['data']['value'],
                         '{"agentLineResult": <节点merge.result>}')
        for branch in ('left', 'right'):
            fields = state['blocks'][native_id('wf', branch)]['subBlocks']['inputFormat']['value']
            self.assertEqual(fields[0]['value'], '<节点start.input>')
        self.assertEqual(compile_document(doc, 'wf'), state)
        self.assertNotEqual(compile_document(doc, 'other'), state)

    def test_normalized_pinned_defaults_roundtrip(self):
        doc = document(); state = compile_document(doc, 'wf')
        state.update(loops={}, parallels={}, lastSaved=123)
        for block in state['blocks'].values():
            block.update(horizontalHandles=True, height=0, advancedMode=False,
                         errorEnabled=False, triggerMode=False, data={}, locked=False)
        for edge in state['edges']:
            edge.update(type='default', data={})
        self.assertEqual(decode_document(state), doc)
        next(iter(state['blocks'].values()))['locked'] = True
        with self.assertRaises(UnsupportedDocument):
            decode_document(state)

    def test_join_three_policies_preserve_all_original_results(self):
        good, bad = result('a'), result('b', 'failed', 'pending')
        for policy, required, left, right, expected in [
            ('all_success', [], good, result('b'), True),
            ('all_success', [], good, bad, False),
            ('allow_partial', [], good, bad, True),
            ('allow_partial', [], bad, bad, False),
            ('required_success', ['left'], good, bad, True),
            ('required_success', ['right'], good, bad, False),
            ('required_success', ['left', 'right'], good, result('b'), True),
            ('allow_partial', [], result('a', 'succeeded', 'failed'), result('b'), True),
            ('allow_partial', [], result('a', 'failed', 'failed'), result('b'), True),
        ]:
            with self.subTest(policy=policy, required=required, left=left, right=right):
                observed = self.run_join(policy, left, right, required)
                self.assertEqual(observed['ok'], expected)
                if expected:
                    successful = [key for key, value in [('left', left), ('right', right)]
                                  if value['executionStatus'] == 'succeeded' and value['verificationStatus'] == 'passed']
                    self.assertEqual(observed['value'], {'executionStatus': 'succeeded',
                        'verificationStatus': 'passed', 'output': {'policy': policy,
                        'branches': {'left': left, 'right': right}, 'successfulBranches': successful}})

    def test_join_requires_every_original_result_and_legal_terminal_fields(self):
        samples = [None, [], True, 'text', {}, ..., {'taskId': 'a'},
                   {'executionStatus': 'succeeded', 'verificationStatus': 'passed'},
                   {'taskId': 'a', 'executionStatus': 'succeeded'},
                   {**result('a'), 'taskId': ''}, {**result('a'), 'taskId': ' '},
                   {**result('a'), 'taskId': 2}, {**result('a'), 'executionStatus': 'running'},
                   {**result('a'), 'executionStatus': 'cancelled'},
                   {**result('a'), 'executionStatus': True},
                   {**result('a'), 'verificationStatus': None},
                   {**result('a'), 'verificationStatus': 'pending'},
                   result('a', 'failed', 'passed')]
        for value in samples:
            with self.subTest(value=value):
                self.assertFalse(self.run_join('allow_partial', value, result('b'))['ok'])
                self.assertFalse(self.run_join('required_success', result('b'), value, ['left'])['ok'])


    def test_bounds_and_configs_reject_unsupported_queue_or_policy(self):
        changes = [
            lambda d: d['nodes'][1]['config'].update(maxConcurrency=1),
            lambda d: d['nodes'][1]['config'].update(maxConcurrency=21),
            lambda d: d['nodes'][1]['config'].update(maxConcurrency=True),
            lambda d: d['nodes'][1]['config'].update(maxConcurrency='2'),
            lambda d: d['nodes'][1]['config'].update(branches=['left']),
            lambda d: d['nodes'][1]['config'].update(branches=['left', 'left']),
            lambda d: d['nodes'][1]['config'].update(branches=['left', 'end']),
            lambda d: d['nodes'][1]['config'].update(branches=['left', 'unknown']),
            lambda d: d['nodes'][1]['config'].update(join='end'),
            lambda d: d['nodes'][1]['config'].update(batch='dynamic'),
            lambda d: d['nodes'][4]['config'].update(parallelId='unknown'),
            lambda d: d['nodes'][4]['config'].update(policy='any'),
            lambda d: d['nodes'][4]['config'].update(requiredBranches=['left']),
            lambda d: d['nodes'][4]['config'].update(policy='required_success'),
            lambda d: d['nodes'][4]['config'].update(policy='required_success', requiredBranches=['left', 'left']),
            lambda d: d['nodes'][4]['config'].update(policy='required_success', requiredBranches=['end']),
            lambda d: d['nodes'][2]['config'].update(allowFailureBranch=False),
        ]
        for change in changes:
            doc = document(); change(doc)
            with self.subTest(doc=doc), self.assertRaises(AuthoringError):
                compile_document(doc, 'wf')
        doc = document(); doc['nodes'][1]['config']['maxConcurrency'] = 20
        self.assertEqual(decode_document(compile_document(doc, 'wf')), doc)

    def test_twenty_static_branches_supported_but_twenty_one_refused(self):
        doc = document(); template = copy.deepcopy(doc['nodes'][2])
        branches = ['branch'+str(i) for i in range(20)]
        doc['nodes'] = [n for n in doc['nodes'] if n['type'] != 'agent']
        doc['nodes'][1]['config'].update(branches=branches, maxConcurrency=20)
        doc['edges'] = [doc['edges'][0], doc['edges'][-1]]
        for branch in branches:
            agent = copy.deepcopy(template); agent.update(id=branch, name='角色'+branch)
            doc['nodes'].append(agent)
            doc['edges'] += [{'id': branch+'in', 'source': 'fork', 'target': branch},
                             {'id': branch+'out', 'source': branch, 'target': 'merge'}]
        self.assertEqual(decode_document(compile_document(doc, 'wf')), doc)
        doc['nodes'][1]['config']['branches'].append('extra')
        with self.assertRaises(AuthoringError):
            compile_document(doc, 'wf')

    def test_join_requires_own_terminal_fields_and_safe_branch_map_keys(self):
        code = self.code(document()).replace('<节点left.resumeInput>',
            'Object.create({taskId:"a",executionStatus:"succeeded",verificationStatus:"passed"})')
        code = code.replace('<节点right.resumeInput>', json.dumps(result('b')))
        proc = subprocess.run(['node', '-e', 'try { (()=>{'+code+'})(); console.log("BAD"); } catch(e) { console.log(e.message); }'],
                              capture_output=True, text=True, check=True)
        self.assertEqual(proc.stdout.strip(), 'join_result_missing')
        doc = document()
        doc['nodes'][1]['config']['branches'][0] = 'constructor'
        doc['nodes'][2]['id'] = 'constructor'
        for edge in doc['edges']:
            for key in ('source', 'target'):
                if edge[key] == 'left': edge[key] = 'constructor'
        code = self.code(doc).replace('<节点left.resumeInput>', json.dumps(result('a')))
        code = code.replace('<节点right.resumeInput>', json.dumps(result('b')))
        proc = subprocess.run(['node', '-e', 'console.log(JSON.stringify((()=>{'+code+'})()))'],
                              capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(proc.stdout)['output']['branches']['constructor'], result('a'))

    def test_exact_closed_fork_shape_rejects_bypasses_extra_entries_duplicates(self):
        changes = [
            lambda d: d['edges'].pop(2),
            lambda d: d['edges'].append({'id': 'extra', 'source': 'fork', 'target': 'left'}),
            lambda d: d['edges'].append({'id': 'extra', 'source': 'start', 'target': 'left'}),
            lambda d: d['edges'].append({'id': 'extra', 'source': 'start', 'target': 'merge'}),
            lambda d: d['edges'].append({'id': 'extra', 'source': 'left', 'target': 'end'}),
            lambda d: d['edges'][4].update(target='end'),
            lambda d: d['edges'][2].update(target='end'),
            lambda d: d['edges'][1].update(branch='body'),
            lambda d: d['edges'][0].update(target='left'),
        ]
        for change in changes:
            doc = document(); change(doc)
            with self.subTest(doc=doc), self.assertRaises(AuthoringError):
                compile_document(doc, 'wf')

    def test_unpaired_multiple_and_mixed_loop_shapes_reject(self):
        for kind in ('parallel', 'join', 'loop'):
            doc = document()
            doc['nodes'].append({'id': 'extra', 'name': '额外', 'type': kind,
                                 'position': {'x': 1, 'y': 2}, 'config': {}})
            with self.subTest(kind=kind), self.assertRaises(AuthoringError):
                compile_document(doc, 'wf')
        for index in (1, 4):
            doc = document(); doc['nodes'][index]['type'] = 'human'
            with self.assertRaises(AuthoringError):
                compile_document(doc, 'wf')

    def test_native_function_code_marker_language_and_fields_cannot_be_tampered(self):
        for nid in ('fork', 'merge'):
            for mutate in (
                lambda b: b['subBlocks']['code'].update(value=b['subBlocks']['code']['value']+' '),
                lambda b: b['subBlocks']['code'].update(value='return {ok:true};'),
                lambda b: b['subBlocks']['language'].update(value='python'),
                lambda b: b.update(type='agent'),
                lambda b: b.update(data={'parentId': 'unexpected'}),
                lambda b: b['subBlocks'].update(extra={'id': 'extra', 'type': 'code', 'value': ''}),
            ):
                state = compile_document(document(), 'wf'); mutate(state['blocks'][native_id('wf', nid)])
                with self.subTest(nid=nid), self.assertRaises(UnsupportedDocument):
                    decode_document(state)
        state = compile_document(document(), 'wf')
        state['parallels'] = {'unknown': {}}
        with self.assertRaises(UnsupportedDocument):
            decode_document(state)
        state = compile_document(document(), 'wf')
        state['blocks'][native_id('wf', 'end')]['subBlocks']['data']['value'] = '{"agentLineResult": <节点merge.resumeInput>}'
        with self.assertRaises(UnsupportedDocument):
            decode_document(state)

    def test_join_results_feed_conditions_and_later_agent_using_function_result(self):
        doc = document(); end = doc['nodes'].pop()
        later = copy.deepcopy(doc['nodes'][2]); later.update(id='later', name='后续')
        later['config']['inputFrom'] = {'nodeId': 'merge', 'field': 'output'}
        gate = {'id': 'check', 'name': '检查', 'type': 'condition', 'position': {'x': 1, 'y': 1},
                'config': {'rules': [{'id': 'ok', 'source': 'merge', 'field': 'result.verificationStatus',
                                      'operator': 'eq', 'value': 'passed'}]}}
        no = copy.deepcopy(end); no.update(id='no', name='未通过', config={'output': {}})
        doc['nodes'] += [later, gate, end, no]
        doc['edges'][-1]['target'] = 'later'
        doc['edges'] += [{'id': 'g', 'source': 'later', 'target': 'check'},
                         {'id': 'h', 'source': 'check', 'target': 'end', 'branch': 'ok'},
                         {'id': 'i', 'source': 'check', 'target': 'no', 'branch': 'else'}]
        state = compile_document(doc, 'wf')
        self.assertEqual(decode_document(state), doc)
        self.assertEqual(state['blocks'][native_id('wf', 'later')]['subBlocks']['inputFormat']['value'][0]['value'],
                         '<节点merge.result.output>')
        expr = json.loads(state['blocks'][native_id('wf', 'check')]['subBlocks']['conditions']['value'])[0]['value']
        self.assertIn('let v = <节点merge.result>', expr)
        gate['config']['rules'][0].update(field='result.output.policy', value='all_success')
        self.assertEqual(decode_document(compile_document(doc, 'wf')), doc)

    def test_closed_join_exception_does_not_allow_direct_downstream_branch_reference(self):
        doc = document(); doc['nodes'][-1]['config']['resultFrom'] = 'left'
        with self.assertRaises(AuthoringError) as ctx:
            compile_document(doc, 'wf')
        self.assertEqual(ctx.exception.code, 'result_source_not_dominator')
        doc = document(); doc['nodes'][3]['config']['inputFrom'] = {'nodeId': 'left', 'field': 'output'}
        with self.assertRaises(AuthoringError) as ctx:
            compile_document(doc, 'wf')
        self.assertEqual(ctx.exception.code, 'agent_source_not_dominator')

    def test_join_source_still_must_dominate_every_downstream_consumer(self):
        doc = document()
        gate = {'id': 'gate', 'type': 'condition', 'name': '前置分支',
                'position': {'x': 0, 'y': 0}, 'config': {'rules': [
                    {'id': 'go', 'operator': 'eq', 'field': 'input.go', 'value': True}]}}
        doc['nodes'].insert(1, gate); doc['edges'][0]['target'] = 'gate'
        doc['edges'] += [{'id': 'go', 'source': 'gate', 'target': 'fork', 'branch': 'go'},
                         {'id': 'skip', 'source': 'gate', 'target': 'end', 'branch': 'else'}]
        with self.assertRaises(AuthoringError) as ctx:
            compile_document(doc, 'wf')
        self.assertEqual(ctx.exception.code, 'result_source_not_dominator')
        doc['nodes'][-1]['config'] = {'output': {}}
        self.assertEqual(decode_document(compile_document(doc, 'wf')), doc)


if __name__ == '__main__':
    unittest.main()
