"""Bounded Chinese authoring projection over pinned Sim's public v2 API.

No execution engine, retries, publication pin, or approval registration lives here.
Condition exceptions propagate in pinned executor/handlers/condition/handler.ts.
"""
import base64
import copy
import hashlib
import json
import math
import re

import httpx
from .adapters.sim import segment
from .execution_limits import validate_limit_values


class AuthoringError(ValueError):
    def __init__(self, code='authoring_invalid'):
        self.code = code
        super().__init__(code)


class AuthoringUncertain(AuthoringError):
    pass


class UnsupportedDocument(AuthoringError):
    pass


def require(ok, code='document_invalid'):
    if not ok:
        raise AuthoringError(code)


def keys(value, expected):
    require(isinstance(value, dict) and set(value) == set(expected))


def ident(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', value))
    return value


def literal(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':'))


def human_config(config):
    require(isinstance(config, dict) and {'requiredBoolean'} <= set(config)
            <= {'requiredBoolean', 'approvalTtlSeconds', 'question'}, 'human_config_invalid')
    ident(config['requiredBoolean'])
    if 'approvalTtlSeconds' in config:
        require(type(config['approvalTtlSeconds']) is int
                and 1 <= config['approvalTtlSeconds'] <= 604800, 'human_ttl_invalid')
    if 'question' in config:
        require(isinstance(config['question'], str) and 1 <= len(config['question']) <= 2000
                and config['question'] == config['question'].strip() and '\0' not in config['question'],
                'human_question_invalid')
    if 'approvalTtlSeconds' in config or 'question' in config:
        require(config['requiredBoolean'] not in ('agentLineHumanSpec', 'agentLineSpec'), 'human_config_invalid')
    return config


def human_ttls(document, namespace):
    return {native_id(namespace, node['id']): human_config(node['config'])['approvalTtlSeconds']
            for node in document['nodes'] if node['type'] == 'human'
            and 'approvalTtlSeconds' in node['config']}


def human_questions(document, namespace):
    return {native_id(namespace, node['id']): human_config(node['config'])['question']
            for node in document['nodes'] if node['type'] == 'human'
            and 'question' in node['config']}


def expression(rule, start, nodes=None):
    result_source=isinstance(rule,dict) and 'source' in rule
    keys(rule, ('id', 'operator', 'field', 'value', 'source') if result_source else ('id', 'operator', 'field', 'value'))
    if result_source:
        ident(rule['source'])
        require(isinstance(nodes,dict) and rule['source'] in nodes,'result_source_invalid')
        source=nodes[rule['source']]
        if source['type']=='subflow_input':
            require(isinstance(rule['field'], str)
                    and re.fullmatch(r'input(?:\.[A-Za-z_][A-Za-z0-9_]*)+', rule['field']),
                    'subworkflow_input_field_invalid')
        elif source['type']=='human':
            require(rule['field']=='result.approval.approved'
                    and rule['operator'] in ('eq','ne') and type(rule['value']) is bool,
                    'human_condition_invalid')
        else:
            source=_result_source(rule['source'],nodes)
            require(isinstance(rule['field'],str) and re.fullmatch(r'result\.(?:verificationStatus|output(?:\.[A-Za-z_][A-Za-z0-9_]*)+)',rule['field']),'result_field_invalid')
            if source['type'] == 'agent' and source['config'].get('outputSchemaId') == 'fields-result-v1' and rule['field'].startswith('result.output.'):
                from .output_contract import validate_output_fields
                definitions = validate_output_fields(source['config'].get('outputFields'))['fields']
                field = next((item for item in definitions
                              if rule['field'] == 'result.output.result.' + item['name']), None)
                require(field is not None and field['required'] and field['type'] != 'string_array', 'result_field_invalid')
                value = rule['value']
                compatible = (isinstance(value, str) if field['type'] == 'string' else
                              type(value) is bool if field['type'] == 'boolean' else
                              type(value) in (int, float))
                require(compatible, 'result_field_type_invalid')
            if source['type'] == 'agent' and source['config'].get('outputSchemaId') == 'fields-result-v2' and rule['field'].startswith('result.output.'):
                from .structured_fields import scalar_fields
                definitions = scalar_fields(source['config'].get('outputFields'))
                field = next((item for item in definitions
                              if rule['field'] == 'result.output.result.' + '.'.join(item['path'])), None)
                require(field is not None, 'result_field_invalid')
                value = rule['value']
                compatible = (isinstance(value, str) if field['type'] == 'string' else
                              type(value) is bool if field['type'] == 'boolean' else
                              type(value) in (int, float))
                require(compatible, 'result_field_type_invalid')
        reference=('<'+source['name']+'.result.input>' if source['type']=='subflow_input'
                   else _result_reference(source))
    else:
        reference='<'+start+'.input>'
        require(isinstance(rule['field'], str) and re.fullmatch(r'input(?:\.[A-Za-z_][A-Za-z0-9_]*)+', rule['field']))
    ident(rule['id']); require(rule['id'] != 'else')
    ops = {'eq': '===', 'ne': '!==', 'gt': '>', 'gte': '>=', 'lt': '<', 'lte': '<='}
    require(rule['operator'] in ops)
    path = rule['field'].split('.')[1:]
    require(not any(p in ('__proto__', 'constructor', 'prototype') for p in path))
    value = rule['value']
    require(value is None or type(value) in (bool, str, int, float))
    if type(value) in (int, float):
        # Sim evaluates JavaScript numbers; never silently round a large integer
        # comparison constant from Python's arbitrary-precision JSON input.
        require(abs(value) <= 9_007_199_254_740_991 and math.isfinite(value),
                'condition_number_out_of_range')
    if rule['operator'] not in ('eq', 'ne'): require(type(value) in (int, float))
    kind = 'null' if value is None else 'boolean' if type(value) is bool else 'number' if type(value) in (int, float) else 'string'
    encoded_value = literal(value)
    if isinstance(value, str): encoded_value = encoded_value.replace('<', r'\u003c').replace('{', r'\u007b')
    marker = base64.urlsafe_b64encode(literal(rule).encode()).decode()
    check = 'v !== null' if kind == 'null' else f'typeof v !== "{kind}"' + (' || !Number.isFinite(v)' if kind == 'number' else '')
    # Only a fixed reference, escaped JSON literals and allowlisted operators become code.
    return (f'/*agent-line-rule:{marker}*/(() => {{ let v = {reference}; '
            f'for (const k of {literal(path)}) {{ if (v === null || typeof v !== "object" || '
            '!Object.prototype.hasOwnProperty.call(v,k)) throw new Error("condition_field_missing"); v=v[k]; } '
            f'if ({check}) throw new Error("condition_type_mismatch"); return v {ops[rule["operator"]]} {encoded_value}; }})()')


def _result_source(source, nodes):
    ident(source)
    require(isinstance(nodes,dict) and source in nodes and nodes[source]['type'] in ('agent','join','subflow_output'),'result_source_invalid')
    return nodes[source]


def _result_reference(source):
    return '<'+source['name']+('.resumeInput>' if source['type'] in ('agent','human') else '.result>')


def native_id(namespace, node_id):
    require(isinstance(namespace, str) and 1 <= len(namespace) <= 160, 'namespace_invalid')
    ident(node_id)
    return 'al_' + hashlib.sha256(namespace.encode()).hexdigest()[:32] + '--' + node_id


def _agent_config(config, nodes, node_id):
    fields = {'roleBindingId','instruction','inputFrom','memoryPolicyId','outputSchemaId',
              'requireMemoryCommit','allowFailureBranch'}
    require(isinstance(config, dict) and fields <= set(config) <= fields | {'limits', 'outputFields', 'batchItem'})
    if 'batchItem' in config:
        from .batch_item import batch_node_id, validate_batch_item
        try:
            batch_item = validate_batch_item(config['batchItem'])
            require(node_id == batch_node_id(batch_item), 'batch_item_invalid')
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise AuthoringError('batch_item_invalid') from None
    if config.get('outputSchemaId') in ('fields-result-v1', 'fields-result-v2') or 'outputFields' in config:
        from .output_contract import supported_output_schema
        require(supported_output_schema(config.get('outputSchemaId'), config.get('outputFields')),
                'agent_output_fields_invalid')
        require(config.get('outputSchemaId') in ('fields-result-v1', 'fields-result-v2'), 'agent_output_fields_invalid')
    if 'limits' in config:
        try:
            validate_limit_values(config['limits'])
        except ValueError:
            raise AuthoringError('agent_limits_invalid') from None
    for key in ('roleBindingId','memoryPolicyId','outputSchemaId'):
        try: segment(config[key])
        except ValueError: raise AuthoringError('agent_binding_invalid') from None
    require(isinstance(config['instruction'],str) and bool(config['instruction'].strip()) and len(config['instruction'])<=4000,'agent_instruction_invalid')
    require(type(config['requireMemoryCommit']) is bool and type(config['allowFailureBranch']) is bool,'agent_policy_invalid')
    return _input_reference(config['inputFrom'],nodes,node_id)


def _input_reference(source,nodes,node_id):
    keys(source,('nodeId','field'));ident(source['nodeId'])
    require(source['nodeId'] in nodes and source['nodeId']!=node_id,'agent_source_invalid')
    source_node=nodes[source['nodeId']]
    require((source_node['type'] in ('start','subflow_input') and source['field']=='input')
            or (source_node['type'] in ('agent','join','subflow_output') and source['field']=='output'),
            'agent_source_invalid')
    suffix=('.result.input>' if source_node['type']=='subflow_input' else '.input>') if source['field']=='input' else (
        '.resumeInput.output>' if source_node['type']=='agent' else '.result.output>')
    return '<'+source_node['name']+suffix


def _subflow_function_code(node,nodes):
    config=node['config'];kind=node['type']
    encoded=base64.urlsafe_b64encode(literal(config).encode()).decode()
    require(len(encoded)<=524288,'subworkflow_marker_too_large')
    marker='/*AL_'+kind.upper()+'_V1:'+encoded+'*/'
    if kind=='subflow_input':
        source=_input_reference(config['inputFrom'],nodes,node['id'])
        if 'inputMapping' in config:
            from .subworkflow_mapping import compile_mapping
            return marker+compile_mapping(config['inputMapping'],source,result_key='input')
        return marker+'return {input: '+source+'};'
    if 'resultFrom' in config:
        source=_result_source(config['resultFrom'],nodes)
        output='{"agentLineResult": '+_result_reference(source)+'}'
    else:
        require(isinstance(config.get('output'),dict),'subworkflow_output_invalid')
        output=literal(config['output'])
        require('<' not in output and '{{' not in output,'output_template_unsupported')
    if 'outputMapping' in config:
        from .subworkflow_mapping import compile_mapping
        return marker+compile_mapping(config['outputMapping'],output,result_key='output')
    return marker+'return {output: '+output+'};'


MAX_STATIC_PARALLEL_GROUPS = 10


def _static_parallel_pairs(nodes, loop_nodes):
    forks=[n for n in nodes.values() if n['type']=='parallel']
    joins=[n for n in nodes.values() if n['type']=='join']
    require((not forks and not joins) or (1 <= len(forks) == len(joins)
            <= MAX_STATIC_PARALLEL_GROUPS and not loop_nodes),
            'parallel_shape_unsupported')
    pairs=[]; used_joins=set(); used_branches=set()
    for fork in forks:
        cfg=fork['config'];keys(cfg,('branches','join','maxConcurrency'))
        branches=cfg['branches']
        require(isinstance(branches,list) and 2<=len(branches)<=20,'parallel_branches_invalid')
        for branch in branches:ident(branch)
        require(len(set(branches))==len(branches) and all(
            branch in nodes and nodes[branch]['type']=='agent' for branch in branches),
            'parallel_branches_invalid')
        require(not used_branches.intersection(branches),'parallel_branch_shared')
        used_branches.update(branches)
        require(type(cfg['maxConcurrency']) is int and len(branches)<=cfg['maxConcurrency']<=20,
                'parallel_bound_invalid')
        ident(cfg['join'])
        join=nodes.get(cfg['join'])
        require(join is not None and join['type']=='join' and join['id'] not in used_joins,
                'parallel_join_invalid')
        used_joins.add(join['id'])
        jc=join['config'];keys(jc,('parallelId','policy','requiredBranches'));ident(jc['parallelId'])
        require(jc['parallelId']==fork['id'],'parallel_join_invalid')
        require(jc['policy'] in ('all_success','allow_partial','required_success'),'join_policy_invalid')
        required=jc['requiredBranches']
        require(isinstance(required,list),'join_required_invalid')
        for branch in required:ident(branch)
        require(len(set(required))==len(required) and set(required)<=set(branches)
                and (bool(required) if jc['policy']=='required_success' else not required),
                'join_required_invalid')
        require(all(isinstance(nodes[branch]['config'],dict)
                    and nodes[branch]['config'].get('allowFailureBranch') is True for branch in branches),
                'parallel_failure_branch_required')
        pairs.append((fork,join))
    return pairs


def _static_function_code(node, nodes):
    kind=node['type'];cfg=node['config']
    marker='/*AL_'+kind.upper()+'_V1:'+base64.urlsafe_b64encode(literal(cfg).encode()).decode()+'*/'
    if kind=='parallel':return marker+'return {parallel:true};'
    branches=nodes[cfg['parallelId']]['config']['branches']
    values=','.join('['+literal(branch)+',<'+nodes[branch]['name']+'.resumeInput>]' for branch in branches)
    return (marker+'const entries = ['+values+']; const successful = []; '
            'for (const [id,r] of entries) { '
            'if (r === null || typeof r !== "object" || Array.isArray(r)) throw new Error("join_result_invalid"); '
            'for (const key of ["taskId","executionStatus","verificationStatus"]) '
            'if (!Object.prototype.hasOwnProperty.call(r,key)) throw new Error("join_result_missing"); '
            'if (typeof r.taskId !== "string" || !r.taskId.trim()) throw new Error("join_task_invalid"); '
            'if (r.executionStatus === "succeeded") { '
            'if (r.verificationStatus === "passed") successful.push(id); '
            'else if (r.verificationStatus !== "failed") throw new Error("join_verification_invalid"); '
            '} else if (r.executionStatus === "failed") { '
            'if (r.verificationStatus !== "pending" && r.verificationStatus !== "failed") '
            'throw new Error("join_verification_invalid"); '
            '} else throw new Error("join_execution_invalid"); } '
            'const policy = '+literal(cfg['policy'])+'; const required = '+literal(cfg['requiredBranches'])+'; '
            'const ok = policy === "all_success" ? successful.length === entries.length : '
            'policy === "allow_partial" ? successful.length > 0 : required.every(id => successful.includes(id)); '
            'if (!ok) throw new Error("join_policy_unsatisfied"); '
            'return {executionStatus:"succeeded",verificationStatus:"passed",'
            'output:{policy,branches:Object.fromEntries(entries),successfulBranches:successful}};')


def _loop_guard_name(loop_id):
    return 'ALLoopGuard'+hashlib.sha256(loop_id.encode()).hexdigest()[:16]


def _loop_guard_code(name):
    return ('const r = <'+name+'.resumeInput>; '
            'if (r === null || typeof r !== "object" || Array.isArray(r)) throw new Error("loop_result_invalid"); '
            'if (!Object.prototype.hasOwnProperty.call(r,"executionStatus")) throw new Error("loop_execution_invalid"); '
            'let retry; if (r.executionStatus === "failed") retry = true; '
            'else if (r.executionStatus === "succeeded") { '
            'if (!Object.prototype.hasOwnProperty.call(r,"verificationStatus")) throw new Error("loop_verification_invalid"); '
            'if (r.verificationStatus === "passed") retry = false; '
            'else if (r.verificationStatus === "failed") retry = true; '
            'else throw new Error("loop_verification_invalid"); } '
            'else throw new Error("loop_execution_invalid"); '
            'return {result:r,needsRepair:retry};')


def _loop_condition(guard_name, maximum):
    # Pinned native loop swallows VM errors: validation belongs in the Function.
    return '<loop.index> < '+str(maximum)+' && <'+guard_name+'.result.needsRepair> === true'


def loop_policies(document, namespace):
    compile_document(document, namespace)
    return {native_id(namespace,n['config']['body']):
            {'loopId':native_id(namespace,n['id']),'maxIterations':n['config']['maxIterations']}
            for n in document['nodes'] if n['type']=='loop'}


def _derived_loops(blocks):
    # Exact pinned workflow-persistence/subflow-helpers.ts conversion.
    return {bid:{'id':bid,'nodes':[key for key,b in blocks.items() if b.get('data',{}).get('parentId')==bid],
                 'iterations':block['data']['count'],'loopType':'doWhile','enabled':True,
                 'forEachItems':'','whileCondition':'','doWhileCondition':block['data']['doWhileCondition']}
            for bid,block in blocks.items() if block['type']=='loop'}


def compile_document(doc, namespace):
    require(isinstance(namespace, str) and 1 <= len(namespace) <= 160)
    try:
        return _compile(doc, native_id(namespace,'start')[:-len('start')])
    except AuthoringError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise AuthoringError('document_invalid') from None


def _compile(doc, prefix):
    keys(doc, ('schemaVersion', 'nodes', 'edges', 'variables'))
    require(type(doc['schemaVersion']) is int and doc['schemaVersion'] == 1)
    require(isinstance(doc['nodes'], list) and 2 <= len(doc['nodes']) <= 200)
    require(isinstance(doc['edges'], list) and len(doc['edges']) <= 1000)
    require(isinstance(doc['variables'], dict))
    # Variable definitions have their own native schema; not yet a safe editable projection.
    if doc['variables']: raise UnsupportedDocument('variables_unsupported')
    nodes = {}; starts = []
    for n in doc['nodes']:
        keys(n, ('id', 'type', 'name', 'position', 'config')); ident(n['id'])
        require(n['id'] not in nodes); nodes[n['id']] = n
        require(isinstance(n['name'], str) and 0 < len(n['name']) <= 255 and re.fullmatch(r'[\w\u4e00-\u9fff]+',n['name']), 'node_name_invalid')
        keys(n['position'], ('x', 'y'))
        require(all(type(v) in (int, float) and math.isfinite(v) for v in n['position'].values()))
        if n['type'] not in ('start', 'condition', 'human', 'agent', 'end', 'loop', 'parallel', 'join', 'subflow_input', 'subflow_output'): raise UnsupportedDocument('node_type_unsupported')
        if n['type'] == 'start': starts.append(n['id'])
    require(len(starts) == 1 and any(n['type'] == 'end' for n in nodes.values()))
    require(len({n['name'].lower() for n in nodes.values()})==len(nodes), 'node_name_duplicate')
    if any(n['type'] in ('subflow_input','subflow_output') for n in nodes.values()):
        from .subworkflow_expansion import validate_groups
        validate_groups(doc)
    loop_nodes=[n for n in nodes.values() if n['type']=='loop']
    require(len(loop_nodes)<=1,'loop_shape_unsupported')
    parallel_pairs=_static_parallel_pairs(nodes,loop_nodes)
    bodies={}
    for loop in loop_nodes:
        cfg=loop['config'];keys(cfg,('body','maxIterations'));ident(cfg['body'])
        require(type(cfg['maxIterations']) is int and 1<=cfg['maxIterations']<=3,'loop_bound_invalid')
        require(cfg['body'] in nodes and nodes[cfg['body']]['type']=='agent','loop_body_invalid')
        require(nodes[cfg['body']]['config'].get('allowFailureBranch') is True,'loop_failure_branch_required')
        bodies[cfg['body']]=loop['id']
    blocks = {}; ports = {}; types = {'start':'start_trigger','condition':'condition','human':'human_in_the_loop_v2','agent':'human_in_the_loop_v2','end':'response','loop':'loop','parallel':'function','join':'function','subflow_input':'function','subflow_output':'function'}
    for nid, n in nodes.items():
        cfg = n['config']; fields = {}
        if n['type'] == 'start': keys(cfg, ())
        elif n['type'] == 'condition':
            keys(cfg, ('rules',)); require(isinstance(cfg['rules'], list) and 1 <= len(cfg['rules']) <= 50)
            conditions = [{'id': r['id'], 'title': 'if' if i == 0 else 'else if', 'value': expression(r, nodes[starts[0]]['name'],nodes)} for i, r in enumerate(cfg['rules'])]
            ids = [c['id'] for c in conditions]; require(len(ids) == len(set(ids)))
            ports[nid] = set(ids + ['else'])
            conditions.append({'id':'else','title':'else','value':''})
            fields = {'conditions': ('condition-input', literal(conditions))}
        elif n['type'] == 'loop':
            ports[nid]={'body','done'}
        elif n['type'] in ('parallel','join'):
            fields={'language':('dropdown','javascript'),'code':('code',_static_function_code(n,nodes))}
        elif n['type'] in ('subflow_input','subflow_output'):
            fields={'language':('dropdown','javascript'),'code':('code',_subflow_function_code(n,nodes))}
        elif n['type'] == 'agent':
            reference=_agent_config(cfg,nodes,nid)
            if nid in bodies:reference='{"input": '+reference+', "iteration": <loop.index>}'
            spec='AL_AGENT_V1:'+base64.urlsafe_b64encode(literal(cfg).encode()).decode()
            fields={'inputFormat':('input-format',[
                {'id':'taskInput','name':'taskInput','type':'object','value':reference},
                {'id':'agentLineSpec','name':'agentLineSpec','type':'string','value':spec}]),
                'notification':('tool-input',[]),'builderData':('response-format',[])}
        elif n['type'] == 'human':
            human_config(cfg)
            inputs = [{'id':cfg['requiredBoolean'],'name':cfg['requiredBoolean'],'type':'boolean','value':True}]
            if 'approvalTtlSeconds' in cfg or 'question' in cfg:
                marker = 'AL_HUMAN_V1:' + base64.urlsafe_b64encode(literal(cfg).encode()).decode()
                inputs.append({'id':'agentLineHumanSpec','name':'agentLineHumanSpec','type':'string','value':marker})
            fields = {'inputFormat': ('input-format', inputs), 'notification':('tool-input',[]), 'builderData':('response-format',[])}
        else:
            if isinstance(cfg,dict) and 'resultFrom' in cfg:
                keys(cfg,('resultFrom',));source=_result_source(cfg['resultFrom'],nodes)
                output='{"agentLineResult": '+_result_reference(source)+'}'
            else:
                keys(cfg, ('output',)); require(isinstance(cfg['output'], dict))
                output = literal(cfg['output'])
                # Native resolver interprets templates even in JSON strings.
                require('<' not in output and '{{' not in output, 'output_template_unsupported')
            fields = {'dataMode':('dropdown','json'),'data':('code',output),'status':('short-input','200')}
        bid = prefix + nid
        blocks[bid] = {'id':bid,'type':types[n['type']],'name':n['name'],'position':copy.deepcopy(n['position']), 'subBlocks':{k:{'id':k,'type':t,'value':v} for k,(t,v) in fields.items()},'outputs':{},'enabled':True}
        if n['type']=='loop':
            blocks[bid]['data']={'loopType':'doWhile','count':cfg['maxIterations'],
                'doWhileCondition':_loop_condition(_loop_guard_name(nid),cfg['maxIterations']),
                'width':500,'height':300}
        elif nid in bodies:blocks[bid]['data']={'parentId':prefix+bodies[nid],'extent':'parent'}
    edges = []; used = set(); adj = {n:[] for n in nodes}; outgoing = {n:[] for n in nodes}
    for e in doc['edges']:
        require(isinstance(e,dict) and set(e) in ({'id','source','target'}, {'id','source','target','branch'}))
        ident(e['id']); require(e['id'] not in used); used.add(e['id'])
        s,t = e['source'],e['target']; require(s in nodes and t in nodes and t != starts[0])
        ne = {'id':prefix+e['id'],'source':prefix+s,'target':prefix+t}
        if s in ports:
            require(e.get('branch') in ports[s])
            ne['sourceHandle']=({'body':'loop-start-source','done':'loop-end-source'}[e['branch']] if nodes[s]['type']=='loop' else 'condition-'+e['branch'])
            if nodes[s]['type']=='loop':require((t==nodes[s]['config']['body']) == (e['branch']=='body'),'loop_edge_invalid')
        else: require('branch' not in e)
        if t in bodies:require(s==bodies[t] and e.get('branch')=='body','loop_body_entry_invalid')
        adj[s].append(t); outgoing[s].append(e.get('branch')); edges.append(ne)
    for nid,n in nodes.items():
        if nid in ports: require(len(outgoing[nid]) == len(ports[nid]) and set(outgoing[nid]) == ports[nid])
        else: require(len(outgoing[nid]) == (len(n['config']['branches']) if n['type']=='parallel' else 0 if n['type']=='end' or nid in bodies else 1))
    proven_join_references=set()
    if parallel_pairs:
        incoming={nid:[] for nid in nodes}
        for source,targets in adj.items():
            for target in targets:incoming[target].append(source)
    for fork,join in parallel_pairs:
        branches=fork['config']['branches']
        # Native DAG scheduling is an AND barrier only for this exact closed shape.
        # Duplicated edges are rejected by lengths, even when their IDs differ.
        require(len(adj[fork['id']])==len(branches) and set(adj[fork['id']])==set(branches)
                and len(incoming[join['id']])==len(branches) and set(incoming[join['id']])==set(branches)
                and all(incoming[branch]==[fork['id']] and adj[branch]==[join['id']] for branch in branches),
                'parallel_edges_invalid')
        proven_join_references.update((join['id'],branch) for branch in branches)
    visiting=set(); done=set()
    def visit(n):
        require(n not in visiting, 'graph_cycle')
        if n in done: return
        visiting.add(n)
        for target in adj[n]: visit(target)
        visiting.remove(n); done.add(n)
    visit(starts[0]); require(len(done)==len(nodes), 'graph_unreachable')
    # For dominance only, native loop completion follows its body at least once.
    # Document edges remain acyclic; the executor alone implements the loop-back.
    flow=copy.deepcopy(adj)
    for body,loop_id in bodies.items():
        after=next(e['target'] for e in doc['edges'] if e['source']==loop_id and e.get('branch')=='done')
        flow[loop_id]=[body];flow[body]=[after]
    # Removing a dominator disconnects every route to its consumer in this DAG.
    references=[]
    for nid,n in nodes.items():
        if n['type'] in ('agent','subflow_input'):references.append((nid,n['config']['inputFrom']['nodeId'],'agent_source_not_dominator'))
        elif n['type']=='condition':
            references.extend((nid,r['source'],'result_source_not_dominator') for r in n['config']['rules'] if 'source' in r)
        elif n['type'] in ('end','subflow_output') and 'resultFrom' in n['config']:
            references.append((nid,n['config']['resultFrom'],'result_source_not_dominator'))
        elif n['type']=='join':
            references.extend((nid,branch,'result_source_not_dominator') for branch in nodes[n['config']['parallelId']]['config']['branches'])
    for nid,source,code in references:
        require(source!=nid,code)
        if (nid,source) in proven_join_references:continue
        reachable=set();pending=[starts[0]]
        while pending:
            current=pending.pop()
            if current==source or current in reachable:continue
            reachable.add(current);pending.extend(flow[current])
        require(nid not in reachable,code)
    for body,loop_id in bodies.items():
        guard_id=prefix+loop_id+'--guard';guard_name=_loop_guard_name(loop_id)
        require(guard_name.lower() not in {n['name'].lower() for n in nodes.values()},'loop_guard_name_reserved')
        position=nodes[body]['position']
        blocks[guard_id]={'id':guard_id,'type':'function','name':guard_name,
            'position':{'x':position['x']+220,'y':position['y']},
            'subBlocks':{'language':{'id':'language','type':'dropdown','value':'javascript'},
                         'code':{'id':'code','type':'code','value':_loop_guard_code(nodes[body]['name'])}},
            'outputs':{},'enabled':True,'data':{'parentId':prefix+loop_id,'extent':'parent'}}
        edges.append({'id':prefix+loop_id+'--guard-edge','source':prefix+body,'target':guard_id})
    return {'blocks':blocks,'edges':edges,'variables':{}}


def decode_document(nativeState):
    """Decode only losslessly recognized compiler graphs; normalized UI extras reject."""
    try:
        require(isinstance(nativeState,dict) and set(nativeState) <= {'blocks','edges','variables','loops','parallels','lastSaved'})
        require(not nativeState.get('parallels'))
        nativeState=copy.deepcopy(nativeState)
        if 'lastSaved' in nativeState:
            require(type(nativeState['lastSaved']) in (int,float) and math.isfinite(nativeState['lastSaved']))
            nativeState.pop('lastSaved')
        # Pinned prepare-state.ts defaults plus normalized-table read defaults.
        defaults={'horizontalHandles':True,'height':0,'advancedMode':False,'errorEnabled':False,'triggerMode':False,'data':{},'locked':False}
        for block in nativeState['blocks'].values():
            if block.get('type')=='loop' and isinstance(block.get('data'),dict):
                # Pinned workflow-persistence/load.ts adds only these empty defaults.
                for key in ('collection','whileCondition'):
                    if block['data'].get(key)=='' :block['data'].pop(key)
            for key,value in defaults.items():
                if key in block and type(block[key]) is type(value) and block[key]==value: block.pop(key)
        for edge in nativeState['edges']:
            if edge.get('type')=='default': edge.pop('type')
            if edge.get('data')=={}: edge.pop('data')
        blocks=nativeState['blocks']; require(isinstance(blocks,dict) and bool(blocks))
        first=next(iter(blocks)); match=re.fullmatch(r'(al_[0-9a-f]{32}--)([A-Za-z][A-Za-z0-9_]{0,63})',first); require(match is not None)
        prefix=match[1]; nodes=[]
        guard_ids={bid+'--guard' for bid,b in blocks.items() if b.get('type')=='loop'}
        types={'start_trigger':'start','condition':'condition','human_in_the_loop_v2':'human','response':'end','loop':'loop','function':'function'}
        for bid,b in blocks.items():
            if bid in guard_ids:continue
            require(bid.startswith(prefix) and b['type'] in types)
            kind=types[b['type']]; sub=b['subBlocks']; cfg={}
            if kind=='function':
                marker=re.match(r'/\*AL_(PARALLEL|JOIN|SUBFLOW_INPUT|SUBFLOW_OUTPUT)_V1:([A-Za-z0-9_=-]+)\*/',sub['code']['value'])
                require(marker is not None)
                if marker[1].startswith('SUBFLOW_'):
                    require(len(marker[2])<=524288,'subworkflow_marker_too_large')
                cfg=json.loads(base64.b64decode(marker[2],altchars=b'-_',validate=True))
                kind=marker[1].lower()
            elif kind=='loop':
                children=[key for key,b in blocks.items() if b.get('data',{}).get('parentId')==bid and key not in guard_ids]
                require(len(children)==1 and children[0].startswith(prefix))
                cfg={'body':children[0][len(prefix):],'maxIterations':b['data']['count']}
            elif kind=='condition':
                conditions=json.loads(sub['conditions']['value']); rules=[]
                for c in conditions[:-1]:
                    m=re.match(r'/\*agent-line-rule:([A-Za-z0-9_=-]+)\*/',c['value']); require(m is not None)
                    rules.append(json.loads(base64.urlsafe_b64decode(m[1])))
                cfg={'rules':rules}
            elif kind=='human':
                fields=sub['inputFormat']['value']
                if any(isinstance(field,dict) and field.get('name')=='agentLineSpec' for field in fields):
                    require(len(fields)==2 and fields[1].get('name')=='agentLineSpec')
                    value=fields[1].get('value')
                    require(isinstance(value,str) and re.fullmatch(r'AL_AGENT_V1:[A-Za-z0-9_=-]+',value))
                    cfg=json.loads(base64.b64decode(value[len('AL_AGENT_V1:'):],altchars=b'-_',validate=True))
                    kind='agent'
                elif any(isinstance(field,dict) and field.get('name')=='agentLineHumanSpec' for field in fields):
                    require(len(fields)==2 and fields[1].get('name')=='agentLineHumanSpec')
                    value=fields[1].get('value')
                    require(isinstance(value,str) and len(value)<=32768
                            and re.fullmatch(r'AL_HUMAN_V1:[A-Za-z0-9_=-]+',value))
                    cfg=json.loads(base64.b64decode(value[len('AL_HUMAN_V1:'):],altchars=b'-_',validate=True))
                    human_config(cfg)
                    require('approvalTtlSeconds' in cfg or 'question' in cfg, 'human_config_invalid')
                else:cfg={'requiredBoolean':fields[0]['name']}
            elif kind=='end':
                value=sub['data']['value']
                result=re.fullmatch(r'\{"agentLineResult": <([\w\u4e00-\u9fff]+)\.(?:resumeInput|result)>\}',value)
                if result:
                    matches=[key for key,block in blocks.items() if block.get('name')==result[1]]
                    require(len(matches)==1 and matches[0].startswith(prefix))
                    cfg={'resultFrom':matches[0][len(prefix):]}
                else:cfg={'output':json.loads(value)}
            nodes.append({'id':bid[len(prefix):],'type':kind,'name':b['name'],'position':b['position'],'config':cfg})
        edges=[]
        for e in nativeState['edges']:
            if e.get('id') in {gid+'-edge' for gid in guard_ids}:continue
            require(all(e[k].startswith(prefix) for k in ('id','source','target')))
            ne={k:e[k][len(prefix):] for k in ('id','source','target')}
            if 'sourceHandle' in e: ne['branch']={'loop-start-source':'body','loop-end-source':'done'}.get(e['sourceHandle'],e['sourceHandle'].removeprefix('condition-'))
            edges.append(ne)
        doc={'schemaVersion':1,'nodes':nodes,'edges':edges,'variables':nativeState['variables']}
        expected=_compile(doc,prefix)
        require(all(nativeState[k]==v for k,v in expected.items()), 'native_normalization_unsupported')
        if 'loops' in nativeState:require(json.dumps(nativeState['loops'],sort_keys=True,allow_nan=False)==json.dumps(_derived_loops(expected['blocks']),sort_keys=True,allow_nan=False),'native_loops_mismatch')
        return doc
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        raise UnsupportedDocument('native_projection_unsupported') from None


class SimAuthoringClient:
    def __init__(self, sim_client, workspace_id):
        self.sim=sim_client
        try: self.workspace_id=segment(workspace_id)
        except ValueError: raise AuthoringError('workspace_invalid') from None

    def _request(self, method, path, *, body=None, params=None, validate=lambda x: True):
        write=method!='GET'; error=AuthoringUncertain if write else AuthoringError
        try:
            r=self.sim._client.request(method,self.sim.base_url+'/api/v2/workflows'+path,json=body,params=params,
                headers={'x-api-key':self.sim._key},follow_redirects=False)
            if r.status_code in (400,401,403,404,422): raise AuthoringError('upstream_rejected')
            if r.status_code not in (200,201): raise error('upstream_outcome_unknown' if write else 'upstream_read_failed')
            data=r.json()
            require(isinstance(data,dict) and 'data' in data and set(data)<= {'data','nextCursor'}, 'upstream_schema_invalid')
            require(validate(data), 'upstream_schema_invalid')
            return data
        except httpx.HTTPError: raise error('upstream_transport_failed') from None
        except (ValueError,TypeError,KeyError,AttributeError) as exc:
            if isinstance(exc,AuthoringError) and exc.code=='upstream_rejected': raise
            raise error('upstream_schema_invalid') from None

    def _path(self,wid):
        try: return '/'+segment(wid)
        except ValueError: raise AuthoringError('workflow_invalid') from None

    def _identity(self,data,wid=None):
        return isinstance(data,dict) and isinstance(data.get('id'),str) and (wid is None or data['id']==wid) and data.get('workspaceId')==self.workspace_id and isinstance(data.get('name'),str)

    def _page(self,cursor,limit):
        require(type(limit) is int and 1<=limit<=100,'pagination_invalid')
        require(cursor is None or isinstance(cursor,str) and 0<len(cursor)<=4096,'pagination_invalid')
        return {'limit':limit,**({'cursor':cursor} if cursor else {})}

    def list_workflows(self,cursor=None,limit=50):
        return self._request('GET','',params={'workspaceId':self.workspace_id,**self._page(cursor,limit)},validate=lambda p:isinstance(p['data'],list) and all(self._identity(d) for d in p['data']) and (p.get('nextCursor') is None or isinstance(p['nextCursor'],str)))

    def list_archived(self, cursor=None, limit=50):
        """Official archived scope; cursors stay scoped by the upstream API."""
        return self._request('GET', '', params={
            'workspaceId': self.workspace_id, 'scope': 'archived', **self._page(cursor, limit)},
            validate=lambda p: isinstance(p['data'], list)
            and all(self._identity(item) for item in p['data'])
            and (p.get('nextCursor') is None or isinstance(p['nextCursor'], str)))

    def runs(self, wid, cursor=None, limit=100):
        self.get_detail(wid)
        return self._request('GET', self._path(wid) + '/runs', params=self._page(cursor, limit),
            validate=lambda p: isinstance(p['data'], list) and all(
                isinstance(item, dict) and isinstance(item.get('runId'), str)
                and item.get('workflowId') == wid and isinstance(item.get('status'), str)
                for item in p['data'])
            and (p.get('nextCursor') is None or isinstance(p['nextCursor'], str)))

    def get_run_audit(self, wid, run_id):
        self.get_detail(wid)
        return self._request('GET', self._path(wid) + '/runs/' + segment(run_id),
            params={'includeOutput': 'true'}, validate=lambda p: isinstance(p['data'], dict)
            and p['data'].get('workflowId') == wid and p['data'].get('runId') == run_id
            and isinstance(p['data'].get('status'), str))['data']

    def archive(self, wid):
        """Soft archive only; caller must persist intent before invoking once."""
        self.get_detail(wid)
        return self._request('DELETE', self._path(wid), validate=lambda p:
            isinstance(p['data'], dict) and p['data'].get('id') == wid
            and p['data'].get('deleted') is True
            and p['data'].get('archived') is True)['data']

    def create(self,name,description=None):
        require(isinstance(name,str) and 0<len(name.strip())<=255,'name_invalid')
        require(description is None or isinstance(description,str) and len(description)<=50000,'description_invalid')
        return self._request('POST','',body={'workspaceId':self.workspace_id,'name':name,'description':description},validate=lambda p:self._identity(p['data']))['data']

    def get_detail(self,wid):
        return self._request('GET',self._path(wid),validate=lambda p:self._identity(p['data'],wid))['data']

    def get_deployment(self, wid):
        """Read live version, including rollbacks, without replaying publication."""
        self.get_detail(wid)

        def valid(payload):
            data = payload['data']
            if (not isinstance(data, dict) or data.get('id') != wid
                    or type(data.get('isDeployed')) is not bool
                    or type(data.get('needsRedeployment')) is not bool
                    or 'activeDeployment' not in data):
                return False
            active = data['activeDeployment']
            if not data['isDeployed']:
                return active is None
            return (isinstance(active, dict)
                    and isinstance(active.get('deploymentVersionId'), str)
                    and bool(active['deploymentVersionId'])
                    and type(active.get('version')) is int and active['version'] > 0)

        return self._request('GET', self._path(wid) + '/deployment', validate=valid)['data']

    def get_state(self,wid):
        self.get_detail(wid)
        return self._request('GET',self._path(wid)+'/state',validate=lambda p:isinstance(p['data'],dict) and set(p['data'])=={'blocks','edges','variables','loops','parallels'} and isinstance(p['data']['blocks'],dict) and isinstance(p['data']['edges'],list) and all(isinstance(p['data'][k],dict) for k in ('variables','loops','parallels')))['data']

    def save_state(self,wid,state,*,dry_run=False):
        require(type(dry_run) is bool)
        decode_document(state)  # Only supported, lossless graphs may be replaced.
        self.get_detail(wid)
        return self._request('PUT',self._path(wid)+'/state',params={'dryRun':'true' if dry_run else 'false'},body={k:state[k] for k in ('blocks','edges','variables')},validate=lambda p:isinstance(p['data'],dict) and p['data'].get('id')==wid and p['data'].get('dryRun') is dry_run and type(p['data'].get('needsRedeployment')) is bool and isinstance(p['data'].get('warnings'),list) and isinstance(p['data'].get('lint'),dict))['data']

    def deploy(self,wid):
        decode_document(self.get_state(wid))
        return self._request('POST',self._path(wid)+'/deploy',body={},validate=lambda p:isinstance(p['data'],dict) and p['data'].get('id')==wid and p['data'].get('isDeployed') is True and type(p['data'].get('version')) is int and p['data']['version']>0 and isinstance(p['data'].get('activeDeployment'),dict))['data']

    def versions(self,wid,cursor=None,limit=50):
        self.get_detail(wid)
        return self._request('GET',self._path(wid)+'/versions',params=self._page(cursor,limit),validate=lambda p:isinstance(p['data'],list) and all(isinstance(d,dict) and isinstance(d.get('id'),str) and type(d.get('version')) is int and d['version']>0 and type(d.get('isActive')) is bool for d in p['data']) and (p.get('nextCursor') is None or isinstance(p['nextCursor'],str)))

    def get_version(self,wid,revision):
        require((type(revision) is int and revision>0) or (isinstance(revision,str) and re.fullmatch(r'[1-9][0-9]*',revision)), 'revision_invalid')
        self.get_detail(wid)
        # Official response has no workflowId: scope is the fixed authenticated path.
        return self._request('GET',self._path(wid)+'/versions/'+str(revision),validate=lambda p:isinstance(p['data'],dict) and isinstance(p['data'].get('id'),str) and bool(p['data']['id']) and type(p['data'].get('version')) is int and p['data']['version']==int(revision) and type(p['data'].get('isActive')) is bool and isinstance(p['data'].get('state'),dict) and p['data'].get('workflowId',wid)==wid)['data']


def is_initial_state(state):
    """Only pinned defaults.ts's untouched Start or a genuinely empty graph.

    This is not ownership/admission: the caller must also verify accepted-create
    ownership and the exact current state hash before replacing this graph.
    """
    try:
        require(isinstance(state,dict) and {'blocks','edges','variables'} <= set(state) <= {'blocks','edges','variables','loops','parallels'})
        require(state['edges']==[] and type(state['edges']) is list)
        require(all(type(state.get(k,{})) is dict and state.get(k,{})=={} for k in ('variables','loops','parallels')))
        blocks=state['blocks'];require(type(blocks) is dict)
        if not blocks:return True
        require(len(blocks)==1)
        bid,block=next(iter(blocks.items()))
        pattern=r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
        require(isinstance(bid,str) and re.fullmatch(pattern,bid))
        field_id=block['subBlocks']['inputFormat']['value'][0]['id']
        require(isinstance(field_id,str) and re.fullmatch(pattern,field_id))
        expected={'id':bid,'type':'start_trigger','name':'Start','position':{'x':0,'y':0},
            'subBlocks':{'inputFormat':{'id':'inputFormat','type':'input-format','value':[{'id':field_id,'name':'','type':'string','value':'','collapsed':False}]},'runMetadata':{'id':'runMetadata','type':'switch','value':False}},
            'outputs':{'files':{'type':'file[]','description':'User uploaded files'},'input':{'type':'string','description':'Primary user input or message'},'conversationId':{'type':'string','description':'Conversation thread identifier'}},
            'enabled':True,'horizontalHandles':True,'height':0,'advancedMode':False,'errorEnabled':False,'triggerMode':False,'data':{},'locked':False}
        return json.dumps(block,sort_keys=True,allow_nan=False)==json.dumps(expected,sort_keys=True,allow_nan=False)
    except (AuthoringError,TypeError,ValueError,KeyError,IndexError,AttributeError):
        return False
