"""Model-proposed portable drafts; no model transport, persistence or scheduling."""
import json


MAX_DRAFT_BYTES = 64_000


def validate_generated_template(value):
    # Keep these imports local: template compilation validates output schema IDs
    # through output_contract, whose business-result parser calls this function.
    from .workflow_template import validate_template
    from .store import canonical
    template = validate_template(value)
    if len(canonical(template).encode('utf-8')) > MAX_DRAFT_BYTES:
        raise ValueError('workflow_draft_too_large')
    if any(node['type'] in ('subflow_input', 'subflow_output')
           or node['type'] == 'agent' and 'batchItem' in node['config']
           for node in template['document']['nodes']):
        raise ValueError('workflow_draft_fixed_identity_unsupported')
    return template


def result_instruction():
    example = {
        'format': 'agent-line-workflow-template', 'schemaVersion': 1,
        'document': {
            'schemaVersion': 1,
            'nodes': [
                {'id': 'start', 'type': 'start', 'name': '开始',
                 'position': {'x': 50, 'y': 70}, 'config': {}},
                {'id': 'work', 'type': 'agent', 'name': '处理需求',
                 'position': {'x': 280, 'y': 70},
                 'config': {'roleBindingId': 'slot_1', 'instruction': '按输入处理获准任务',
                            'inputFrom': {'nodeId': 'start', 'field': 'input'},
                            'memoryPolicyId': 'native-role', 'outputSchemaId': 'task-result',
                            'requireMemoryCommit': False, 'allowFailureBranch': False}},
                {'id': 'end', 'type': 'end', 'name': '结束',
                 'position': {'x': 510, 'y': 70}, 'config': {'resultFrom': 'work'}},
            ],
            'edges': [{'id': 'e1', 'source': 'start', 'target': 'work'},
                      {'id': 'e2', 'source': 'work', 'target': 'end'}],
            'variables': {},
        },
        'roleSlots': [{'id': 'slot_1', 'name': '角色 1'}],
    }
    return (
        '\n本任务结果契约为workflow-draft-v1。根据本次自然语言需求提出流程草稿，'
        '不要实施草稿内的任务，不修改文件，不创建角色，不保存、发布或运行流程。'
        '最终答复只输出完整JSON对象，不使用Markdown或附加说明。'
        '必须使用下例的可移植模板信封和节点格式，不输出原生Sim JSON或JavaScript。'
        'roleSlots按Agent首次出现顺序列出slot_1、slot_2等，名称严格为角色 1、角色 2等；'
        'Agent仅引用这些槽，不填真实角色/项目/工作流/部署身份或凭证。'
        '所有Agent使用native-role记忆；用户后续会明确绑定已有角色。'
        '最多200节点、1000连线，JSON及规范化模板各最多64000个UTF-8字节。'
        '节点与边id以英文字母开头，仅含英文字母数字下划线且最多64字符；'
        '节点名称仅含中文、字母、数字和下划线，不含空格或标点，忽略大小写后唯一。'
        'document.variables必须为空对象，position坐标绝对值不超过10000。'
        '必须有一个start和至少一个end，所有节点可从start到达，不使用循环连线。'
        '优先使用start/agent/condition/human/end。condition.config为rules数组，'
        '每条规则有id、field、operator、value；引用前序结果时增加source节点id。'
        'source须在所有到达路径上；例如Agent验收字段result.verificationStatus，'
        'operator=eq、value=passed；每条规则出口的branch等于规则id，另有branch=else。'
        '开始输入条件不写source，field使用input.xxx。human.config为'
        '{"requiredBoolean":"approved"}，审批结果字段result.approval.approved。'
        '不要生成subflow_input/subflow_output、batchItem或固定子版本引用。'
        '其他节点字段必须符合原编辑器契约；无法确定时保留人工确认步骤，不能伪造能力。'
        '完整示例仅演示格式，应按需求调整节点职责、角色槽和分支：'
        + json.dumps(example, ensure_ascii=False, separators=(',', ':'))
    )
