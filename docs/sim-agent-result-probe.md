# Sim Agent 结果条件与动态结束的真实验证

2026-10-22（北京时间），`probes/sim/agent_result_probe.py` 已在隔离 Sim 上完成六个无模型用例。图由现有编译器生成，Sim 自己推进：开始 → Agent 机器等待 → 条件 → 三个动态结束节点。机器结果由探针明确提供；这不是 Codex 执行、独立成果验证或记忆验收。

工作流 `bcd7725f-5eb1-41d1-a376-738a04dfbdad`，发布版本 1，deploymentVersionId `7dfab9e8-ab2b-489d-b8b7-287fb823389f`。正式版本 GET 回读图经 decode 与原文档完全比较，通过后才启动运行。

| 输入结果 | 原生选择 | resume 子运行状态 |
| --- | --- | --- |
| verificationStatus=passed，needsReview=false | passed / 通过结果 | completed |
| verificationStatus=failed，needsReview=true | review / 复核结果 | completed |
| verificationStatus=failed，needsReview=false | else / 其他结果 | completed |
| 缺 verificationStatus | condition_field_missing | failed |
| failed 且缺 output.needsReview | condition_field_missing | failed |
| verificationStatus 为布尔值 | condition_type_mismatch | failed |

三条成功路径均核对条件块的 `selectedPath.blockId` 和 `selectedOption`，并核对真实 End 的 `data` 与最终运行输出：`{"agentLineResult": suppliedResumeInput}`，包括嵌套中文值，保持对象而非 JSON 字符串。异常用例核对明确错误和最终 output=null，没有把缺字段/类型错误当成 else 成功。条件按顺序求值；首个条件命中时不要求未求值分支的字段。

## 复查与写入守卫

从仓库根目录运行：

```bash
.venv/bin/python probes/sim/agent_result_probe.py
.venv/bin/python -m flake8 --select E9,F63,F7,F82 probes/sim/agent_result_probe.py
```

已有 `var/sim-agent-result/state.json` 时，默认只 GET 原父运行和已知恢复子运行，不创建流程、不执行、不恢复。真实重复运行已得到 `nativeWritesThisInvocation=0`、六个原身份、`priorResult=passed_live`。凭据沿用权限 0600 的 `var/sim/probe-auth.json`，不打印密钥。证据文件均为 0600；流程没有绑定执行角色，不调用 QwenPaw，不修改主准入。

首次流程累计收到 15 次写回执：create/save/deploy 各一次，六个 execute 和六个 resume。运行分三次人工续接完成，写次数分别为 5、6、4；最终 report 的本次计数为 4，累计 acknowledged 计数为 15。每个写入先持久 payload/intent，成功才记 received；读回修复没有重发已确认写操作。`--continue-known` 仅用于同一证据目录的有界恢复：只复用 payload 完全相同的 received 回执，任何未确认 intent 都拒绝重发。不要并发运行同一探针，不要删除证据目录后重新执行。

私有证据：`state.json`（全部原身份、意图、回执、父子状态）、`published.json`、`report.json`、`observation.json`。`failure.json` 保留中途取证失败历史，不代表最终六项验证失败。

## 两个原生 API 边界

1. `GET /api/v2/workflows/{wid}/runs/{rid}?selectedOutputs=...` 的 selector 必须对应该次执行实际存在的块；同时请求未走到的 End 会 400。探针修为选择条件块完整 ID，以及唯一预期 End 的 `<id>.data`。分支不匹配仍报失败，不靠换输入重跑。
2. resume 回执提供新的 `resumedRunId`。成功时本次父运行也显示 completed；条件抛错时父运行仍显示 paused 且当前 context=null，而恢复子运行是 failed。探针以已收到回执中的真实子身份验收错误，保留父状态，不宣称父运行已经 failed，也不重复恢复原暂停。应用后续失败收尾必须考虑该上游行为。

本轮只新增探针和本文档，没有修改生产编译器、上游、现有流程、QwenPaw 和记忆配置。
