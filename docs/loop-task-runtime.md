# 有界修复循环的任务接线

使用一个 Sim 原生 doWhile、一个既有角色 Agent 和固定结果校验节点。
不复制三套节点，不在 Python 中选择回边，也不新建记忆系统或运行服务。
中文配置为 `loop.config={body:Agent节点ID,maxIterations:1..3}`，循环体无普通出线，
完成出口进入后续节点。当前仅支持单层、单循环、单 Agent 循环体；完整复杂编排仍待实现。

Agent 需要明确启用允许已确认失败继续处理，输入来源必须是循环外的必经节点。
编辑器不自动改变角色或失败策略。每轮复用相同的原生正式角色，创建不同任务和会话。
QwenPaw 的原生记忆、角色身份与配置保持原实现。

## 原生结果校验与退出

固定 Function 先验证 `executionStatus`；成功执行还需明确的 `verificationStatus`。
passed 提前退出、failed 继续修复，缺字段或错误类型抛出原生错误。
循环条件再判断原生下一轮 index 小于上限，以及 Function `result.needsRepair`。
不能只填 count，也不能把校验放在会吞异常的 loop 条件内。用户不能编辑这个固定代码。

达到上限时 Sim 结束循环；最后任务仍保留其实际失败结论。End 的动态结果不把原生
completed 当成业务验收通过，用户可在循环后配置结果条件。混合人工审批发布尚未接通。
原生版本 1 错误引用的历史和修复版 2 的实际证明见 finite-loop-guard-probe.md。

## 发布与任务身份

发布回执的 `externalTasks.loopPolicies` 精确冻结 body 原生 ID、loop 原生 ID 和上限，
保存在已有 WorkflowRunLedger 的快照列。SimJournal 的发布身份和任务策略一同冻结。
PublicationVerifier 只对显式策略放行完整可无损识别的编译器图；成员、固定校验代码、
连线和派生 loops 必须一致。实际运行图与发布图也完整核对。
无策略的旧路径仍拒绝容器、重复激活和并行；不接任意原生 Function 或任意循环图。

每轮使用原生 context `body_loop0/1/2`，同时核对本轮输入中的整数 iteration。
SimWaitClient 在同一次读取中核对 paused context 和完整块输出中的 context，
不能把块“最新输出”猜作旧轮输入。任务的 nodeExecutionId 为精确 context，
attemptId 为 iteration+1；同一轮重读、重开和并发登记复用一个任务。
原无循环任务的幂等身份保持兼容。

第 N 轮登记前，必须找到同运行第 N-1 轮的持久暂停绑定、真实已结束任务、资源释放
及需要修复的实际结果。前轮还必须有已接受的原生恢复回执，工作流、原运行、context
及恢复子身份均匹配；未知回执、跳轮、成功后又进入修复、越界及缺证据均不登记。
输入的 `repair.previousResult` 来自该次已提交的冻结结果，核对任务身份、实际状态、
产物及证据后交给角色；允许后来正常的记忆提交，但不改写原轮结果。

CompletionRelay 仍只恢复绑定的精确 context。已记录的旧恢复只返回原回执，
未记录但已经不活动的旧 context 保持等待；不会把旧结果送入新一轮。
工作区租约、能力准入、维护限制、角色与项目权限均继续生效。

取消意图由 TaskStore 的持久事件派生，独立于最后的原生执行状态；真实终态可以仍为
failed 或 succeeded，不能因此清掉用户取消请求并自动修复。尚未投递的结果被取消守卫
阻止，下一轮也拒绝登记；历史已接受恢复只返回原记录，不补发。后来的正常记忆提交
或附加证据不会改写已投递的冻结结果。该守卫不宣称原生进程已被取消，也不是完整的
工作流暂停/停止接口。相关回归见 `tests/test_loop_cancellation.py`。

## 证据与剩余范围

### 2026-10-22 回传激活身份收紧计划

范围仅为CompletionRelay回传边界及对应现有fixture：在查询历史恢复回执或调用Sim之前，额外核对任务workflow.nodeId与binding.block_id、nodeExecutionId与binding.context_id。原自动登记已提供这两项；缺失字段或错误绑定一律拒绝，不猜测当前激活、不修改旧数据、不另建调度机制。历史探针的正式payload也已携带完整身份。按用户要求不新增或执行测试，只读调用方与AST静态审查；本计划不构成AC-39真实验收证据。

源码已按此收紧，现有三个fixture同步补全其模拟任务的节点/激活身份，未新增测试方法。原发现器和full_workflow正式探针已提供完整字段，无需改写。缺失身份的旧记录不会自动补字段或借用当前暂停点：后台回传报告delivery_unavailable，保留历史和任务结果，需人工核对原身份。指定gpt-6-luna独立只读审查确认登记与回传身份一致，未发现其他需补齐的调用方fixture。四个变更Python文件仅做AST语法解析，未执行模块、测试或真实工作流；主后端及QwenPaw均未重载，因此尚不是当前部署行为证明。

- `tests/test_loop_task_discovery.py`：逐轮去重、前轮真实反馈、未知回执、资源、越界、
  错误输入和旧结果隔离；真实 SQLite 库配模拟上游。
- `tests/test_loop_workflows.py`：真实 authoring HTTP 到目录启动，发布证明参数和快照
  必须在 execute 前固定，策略变化不能重放。
- `docs/finite-loop-guard-probe.md`：真实 Sim 的三轮、早退出和坏结果失败。
- `docs/editor-finite-loop.md`：真实浏览器保存、发布和重新读取，未启动任务。
- `docs/automatic-loop-flow.md`：真实 Codex 多轮探针，以实际 report 为准，不能用模拟测试替代。

当前主实例没有启用新配置，原准入守卫不变。完整多节点循环、循环中的人工审批、
跨重启现场、网络故障与长期并发验收仍需继续；本模块不是完整 PRD 的完成声明。
