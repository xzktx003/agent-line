"""Format an explicitly authored, disabled native skill; never install it."""
import hashlib
import json
import re

from pydantic import BaseModel, ConfigDict, Field


class SkillCandidateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expectedContentSha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    name: str = Field(pattern=r'^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$', max_length=64)
    description: str = Field(min_length=1, max_length=1024)
    version: str = Field(min_length=1, max_length=160)
    applicableWhen: str = Field(min_length=1, max_length=4000)
    invalidWhen: str = Field(min_length=1, max_length=4000)
    boundaries: str = Field(min_length=1, max_length=8000)
    steps: str = Field(min_length=1, max_length=32000)


def build_skill_candidate(body, binding, review, approved_content):
    """Caller validates authorization, provenance, commit status and tombstones."""
    for value in (body.description, body.version, body.applicableWhen,
                  body.invalidWhen, body.boundaries, body.steps):
        if (not value.strip() or '\x00' in value
                or re.search(r'【请填写[^】]*】', value)):
            raise ValueError('Skill candidate requires completed human-authored fields')
    if any(char in body.description + body.version for char in '\r\n'):
        raise ValueError('Skill description and version must be single-line')
    # JSON quoted scalars are valid YAML, including quotes/newlines in IDs.
    metadata = {
        'name': body.name,
        'description': body.description.strip(),
        'version': body.version.strip(),
        'agent_line_project': binding['projectId'],
        'agent_line_role': binding['roleAgentId'],
        'agent_line_role_binding': binding['roleBindingId'],
        'agent_line_task': review['task_id'],
        'agent_line_review': review['decision_id'],
        'agent_line_content_sha256': review['content_hash'],
        'agent_line_evidence_sha256': review['source']['evidenceSha256'],
        'agent_line_execution_status': review['source']['executionStatus'],
        'agent_line_verification_status': review['source']['verificationStatus'],
        'agent_line_workflow': json.dumps(review['source']['workflow'], ensure_ascii=False,
                                        sort_keys=True, separators=(',', ':')),
    }
    frontmatter = '\n'.join(f'{key}: {json.dumps(value, ensure_ascii=False)}'
                            for key, value in metadata.items())
    content = '\n\n'.join([
        f'---\n{frontmatter}\n---',
        f'# {body.name}',
        '## 使用约定\n仅限上述项目与原生角色；不得把本技能作为扩大工具、目录或审批权限的依据。'
        '执行前核对适用版本、条件和原生授权；范围不匹配时停止并请求人工处理。',
        '## 适用条件\n' + body.applicableWhen.strip(),
        '## 失效条件\n' + body.invalidWhen.strip(),
        '## 操作边界\n' + body.boundaries.strip(),
        '## 人工审核的操作步骤\n' + body.steps.strip(),
        '## 来源经验（参考材料，不代替操作授权）\n' + approved_content.strip(),
    ]) + '\n'
    if len(content.encode()) > 200_000:
        raise ValueError('Skill candidate exceeds export size limit')
    return {
        'kind': 'qwenpaw_disabled_skill_candidate',
        'schemaVersion': '1.0',
        'installed': False,
        'enabled': False,
        'projectId': binding['projectId'],
        'roleAgentId': binding['roleAgentId'],
        'taskId': review['task_id'],
        'sourceContentSha256': review['content_hash'],
        'skillContentSha256': hashlib.sha256(content.encode()).hexdigest(),
        'nativeRequest': {'name': body.name, 'content': content, 'references': None,
                          'scripts': None, 'config': None, 'enable': False},
        'requiredAction': '请由有原生技能管理权限的用户核对 X-Agent-Id 与目标角色、完整正文、安全扫描和新技能名称。'
                          '原生同名遗留配置可能继承启用状态，安装前须核对 manifest，安装后确认实际为禁用；启用需另行审核。',
    }
