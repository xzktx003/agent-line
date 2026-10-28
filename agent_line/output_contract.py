"""Declared bridge/business output shapes; independent of artifact/test checks."""
import json
import math
import re

OUTPUT_SCHEMAS = ('task-result', 'coding-result-v1', 'review-result-v1', 'fields-result-v1', 'fields-result-v2', 'workflow-draft-v1')


def validate_output_fields(value, schema='fields-result-v1'):
    if schema == 'fields-result-v2':
        from .structured_fields import validate_definition
        return validate_definition(value)
    if schema != 'fields-result-v1':
        raise ValueError('Unsupported output field format')
    if type(value) is not dict or set(value) != {'fields'}:
        raise ValueError('An explicit output field definition is required')
    fields = value['fields']
    if type(fields) is not list or not 1 <= len(fields) <= 32:
        raise ValueError('Between one and 32 output fields are required')
    names, clean = set(), []
    for field in fields:
        if (type(field) is not dict or set(field) != {'name', 'type', 'required'}
                or not isinstance(field['name'], str)
                or re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', field['name']) is None
                or field['name'] in ('constructor', 'prototype', '__proto__')
                or field['name'] in names
                or field['type'] not in ('string', 'boolean', 'number', 'integer', 'string_array')
                or type(field['required']) is not bool):
            raise ValueError('Invalid, unsafe or duplicate output field')
        names.add(field['name'])
        clean.append(dict(field))
    if not any(field['required'] for field in clean):
        raise ValueError('At least one required output field is required')
    return {'fields': clean}


def supported_output_schema(value, fields=None):
    if not isinstance(value, str) or value not in OUTPUT_SCHEMAS:
        return False
    if value not in ('fields-result-v1', 'fields-result-v2'):
        return fields is None
    try:
        validate_output_fields(fields, value)
    except (ValueError, TypeError):
        return False
    return True


def _field_result(result, definition):
    fields = validate_output_fields(definition)['fields']
    names = {field['name'] for field in fields}
    required = {field['name'] for field in fields if field['required']}
    if not required <= result.keys() <= names:
        raise ValueError('Business result fields do not match the declaration')
    for field in fields:
        if field['name'] not in result:
            continue
        value, kind = result[field['name']], field['type']
        valid = False
        if kind == 'string':
            valid = isinstance(value, str) and len(value) <= 4000 and '\0' not in value
        elif kind == 'boolean':
            valid = type(value) is bool
        elif kind in ('number', 'integer'):
            valid = (type(value) in ((int,) if kind == 'integer' else (int, float))
                     and abs(value) <= 9007199254740991 and math.isfinite(value))
        elif kind == 'string_array':
            valid = (type(value) is list and len(value) <= 100
                     and all(isinstance(item, str) and len(item) <= 2000
                             and '\0' not in item for item in value))
        if not valid:
            raise ValueError('Business result field type or limit mismatch')
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate business result field')
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError('Nonfinite business result')


_SINGLE_FENCE = re.compile(
    r'[ \t]*```(?:json)?[ \t]*\r?\n(.*)\r?\n[ \t]*```[ \t]*(?:\r?\n)?\Z',
    re.DOTALL,
)


def _single_fence_body(text):
    """Project one outer Markdown fence around the original JSON body.

    The stored reply is not rewritten. Prose, extra fences, another language
    tag, or any change inside the body is not a cleanup of the same result.
    """
    if type(text) is not str or text.count('```') != 2:
        return None
    match = _SINGLE_FENCE.fullmatch(text)
    if match is None:
        return None
    body = match.group(1)
    if not body.strip() or '```' in body:
        return None
    return body


def _business_result(schema, text, definition=None):
    if len(text.encode('utf-8')) > 64_000:
        raise ValueError('Oversized business result')
    result = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_nonfinite)
    json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if type(result) is not dict:
        raise ValueError('Business result object required')
    if schema == 'workflow-draft-v1':
        from .workflow_draft import validate_generated_template
        return validate_generated_template(result)
    if schema == 'fields-result-v2':
        from .structured_fields import validate_result
        return validate_result(result, definition)
    if schema == 'fields-result-v1':
        return _field_result(result, definition)
    fields = {'summary', 'status', 'changedFiles'} if schema == 'coding-result-v1' else {'summary', 'approved', 'issues'}
    if (set(result) != fields or not isinstance(result['summary'], str)
            or not result['summary'].strip() or len(result['summary']) > 4000 or '\0' in result['summary']):
        raise ValueError('Business result fields invalid')
    if schema == 'coding-result-v1':
        paths = result['changedFiles']
        if (result['status'] not in ('completed', 'blocked', 'failed')
                or type(paths) is not list or len(paths) > 256
                or any(not isinstance(path, str) or not 1 <= len(path) <= 512
                       or '\\' in path or '\0' in path or ':' in path
                       or any(part in ('', '.', '..') for part in path.split('/')) for path in paths)
                or len(set(paths)) != len(paths)):
            raise ValueError('Coding result invalid')
    else:
        issues = result['issues']
        if (type(result['approved']) is not bool or type(issues) is not list or len(issues) > 100
                or any(not isinstance(issue, str) or not issue.strip()
                       or len(issue) > 2000 or '\0' in issue for issue in issues)):
            raise ValueError('Review result invalid')
    return result


def inspect_output(task):
    schema = task['payload'].get('outputSchemaId')
    result = {'schemaId': schema, 'state': 'unknown', 'reason': 'output_schema_not_declared'}
    if schema is None:
        return result, None
    if not supported_output_schema(schema, task['payload'].get('outputFields')):
        return {**result, 'state': 'unsupported', 'reason': 'output_schema_unsupported'}, None
    if task['executionStatus'] != 'succeeded':
        return {**result, 'state': 'not_ready', 'reason': 'execution_not_succeeded'}, None
    output = task.get('output')
    if (type(output) is not dict or set(output) != {'text', 'nativeStatus', 'toolEvidence'}
            or not isinstance(output['text'], str) or not output['text'].strip()
            or output['nativeStatus'] != 'task_complete'
            or type(output['toolEvidence']) is not list
            or any(type(item) is not dict for item in output['toolEvidence'])):
        return {**result, 'state': 'invalid', 'reason': 'output_shape_mismatch'}, None
    try:
        encoded = json.dumps(output, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(encoded) > 1_000_000:
            return {**result, 'state': 'invalid', 'reason': 'output_exceeds_limit'}, None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return {**result, 'state': 'invalid', 'reason': 'output_not_finite_json'}, None
    if schema == 'task-result':
        return {**result, 'state': 'valid', 'reason': 'declared_bridge_shape_matched'}, None
    definition = task['payload'].get('outputFields')
    try:
        structured = _business_result(schema, output['text'], definition)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        body = _single_fence_body(output['text'])
        if body is None or body == output['text']:
            return {**result, 'state': 'invalid', 'reason': 'business_result_invalid'}, None
        try:
            structured = _business_result(schema, body, definition)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return {**result, 'state': 'invalid', 'reason': 'business_result_invalid'}, None
        return {**result, 'state': 'valid', 'reason': 'same_body_fence_unwrapped'}, structured
    return {**result, 'state': 'valid', 'reason': 'declared_business_shape_matched'}, structured


def output_validation(task):
    return inspect_output(task)[0]


def flow_output(task):
    if (task['executionStatus'] != 'succeeded'
            or task['payload'].get('outputSchemaId') not in OUTPUT_SCHEMAS[1:]):
        return task.get('output')
    validation, structured = inspect_output(task)
    if validation['state'] != 'valid' or structured is None:
        raise ValueError('Business output is not validated')
    return {**task['output'], 'result': structured}


def execution_instruction(task, instruction):
    if 'batchItem' in task['payload']:
        from .batch_item import task_batch_item
        task_batch_item(task['payload'])
    schema = task['payload'].get('outputSchemaId')
    if schema not in OUTPUT_SCHEMAS[1:]:
        return instruction
    if schema == 'workflow-draft-v1':
        from .workflow_draft import result_instruction
        instruction += result_instruction()
        if len(instruction) > 28000:
            raise ValueError('Task instruction and declared result contract exceed limit')
        return instruction
    shape = ('{"summary":"本次结果摘要","status":"completed",'
             '"changedFiles":["项目内相对文件路径"]}' if schema == 'coding-result-v1' else
             '{"summary":"审查摘要","approved":true,"issues":["发现的问题"]}')
    if schema in ('fields-result-v1', 'fields-result-v2'):
        definition = validate_output_fields(task['payload'].get('outputFields'), schema)
        shape = json.dumps(definition, ensure_ascii=True, separators=(',', ':'))
        instruction += ('\n本任务结果契约为 ' + schema + '。Codex最终答复仅输出完整JSON对象，'
                        '不使用Markdown，不增加字段。字段定义：' + shape
                        + '。required为true的字段必须提供；可选字段可省略，不能用null代替。'
                        'string最多4000字符，boolean为布尔值，number为有限数值，integer为整数；'
                        '数值绝对值最多9007199254740991。string_array为最多100项的字符串数组，'
                        '每项最多2000字符；所有字符串禁止NUL。JSON正文最多64000个UTF-8字节。'
                        '字段只表达本次事实，不能替代独立测试或人工审批。')
        if schema == 'fields-result-v2':
            instruction += ('object按fields递归提供对象；array按items递归提供最多100项的数组。'
                            '每层禁止额外字段，嵌套最多4层，根是对象；不要把定义当作结果输出。')
        if len(instruction) > 28000:
            raise ValueError('Task instruction and declared result contract exceed limit')
        return instruction
    instruction += ('\n本任务结果契约为 ' + schema + '。请让 Codex 的最终答复只包含一个完整JSON对象，'
                    '不要Markdown围栏或额外说明，不增加字段。格式：' + shape
                    + '。编码结果status仅可为completed、blocked或failed。没有文件改动或问题时使用空数组。'
                    '字段只表达本次事实或审查意见，不能替代独立测试或人工审批。')
    if len(instruction) > 28000:
        raise ValueError('Task instruction and declared result contract exceed limit')
    return instruction


def blocks_success(task):
    # Internal historical fixtures/records predate schema declarations. Do not
    # silently promote them to validated or rewrite previously accepted results.
    return ('outputSchemaId' in task['payload']
            and output_validation(task)['state'] != 'valid')
