# 已发布流程自动执行探针

运行入口：`.venv/bin/python probes/full_workflow/published.py`。仅使用独立 `var/published-task-flow/` 数据库和凭证；使用现有正式 QwenPaw 角色 `agent-line-native-loop-parent`、原生 ReMe，以及已绑定的隔离 Codex 执行器。探针不改主配置、角色、记忆或维护状态。

首次运行通过真实 AuthoringAPI HTTP 创建、保存、发布流程。WorkflowGateway 的静态 bindings 为空；发布回执生成目录 binding，GET 目录核对后才启动。图为 Start → Agent → 验收条件 → 成功/失败 End，两个 End 都引用 Agent 动态结果。

后台负责发现暂停、登记任务、父角色委派及完成回传。独立 ArtifactPolicy 校验唯一新文件的精确字节后才登记验收通过。成功判据同时包括：原生容器退出凭据、工作区租约清空、Sim 成功分支 selectedOutputs、最终 agentLineResult 与持久任务身份/验收/输出逐项一致。仅有 completed 不算通过。

所有有副作用的编辑和启动动作前保存意向；任何已有 state 的再次运行只读取同一身份，不重新提交。日志与证据文件为 0600。当前结果以 `report.json` 或 `failure.json` 为准；没有成功报告不可宣称闭环通过。

范围限制：这是单项目、串行固定执行器的一次实际探针，不证明跨项目隔离、自动记忆提炼或生产全面就绪。独立探针能力确认不改变主服务准入。Sim 外部编辑者仍可能在活动版本检查后切换发布版本；机器任务另有原生运行快照验证，不宣称公共执行 API 提供原子版本锁定。

## 实际结果

本次真实闭环通过，证据为 `var/published-task-flow/report.json` 与 `finalize-evidence.json`：

- workflow：`07e81523-ad64-42d2-ae30-204afa313e2a`
- 自动发布 binding：`published-4470c25e13e509a8b95afe9931e55195926c79a83f5e9c15`
- run：`98686d70-26d6-5af0-a4bf-6df3e2601f4d`
- task：`task-739016b9ad2a485b9a27346ffd4b0818`
- 父任务：`task-337baeeea51a`；原生 Codex 任务：`task-54c24b8303f4`
- Codex thread：`01a15f9e-8bef-7753-ad6a-a457b0b5ca0d`；turn：`01a15f9e-9f1c-7800-a6bf-765be52a7fce`

任务 `succeeded`、独立验收 `passed`、资源已释放，父任务 finished。原生条件输出 `selectedOption=passed`，`selectedPath.blockId` 精确匹配成功 End；该 End 的 `agentLineResult` 和最终运行输出与原任务的 taskId、执行身份、执行状态、验收状态和 output 一致。当前租约数、活跃自有容器数均为 0。

首次脚本尾部曾同时请求成功/失败 End 的 selectedOutputs，Sim 因未执行节点返回 HTTP 400；这不是任务执行失败。`failure.json`、原 state.failure 及原生调用列表保留未覆盖。随后运行 `--finalize`，仅对原 workflow/run 读取整个条件块及已执行成功 End，完成上述证据校验，没有重新发布、执行或恢复任务。未来已有 state 的 `--finalize` 也只读，不运行模型。

普通再次运行已实测 observation_only，`newSubmissions=0`、`nativeWritesThisInvocation=0`。全部 JSON 证据权限核对为 0600；静态致命规则检查通过。
