# 最多三次原生修复循环：只读可行性核对

结论：固定 Sim 源码已经实现循环内 Human 的逐次暂停身份与持久恢复，有条件通过现有编译器、适配器和插件扩展接入；不需要另建调度器或记忆引擎。但当前实现明确只接受 DAG，不能仅解除 PublicationVerifier 的限制便宣称循环可用。本轮没有执行循环、写真实数据、修改生产代码或启动模型。以下是源码支持的设计和仍需真实证明的边界，不是验收通过报告。

下述上游路径均以 `upstream/sim/apps/sim/` 为根；行号对应当前固定 checkout。

## 原生身份与持久状态

| 问题 | 当前源码证据 | 含义 |
| --- | --- | --- |
| 每次 Human 的 context | `executor/human-in-the-loop/utils.ts:12–27` 在基础块 ID 后追加 `_loop${loopScope.iteration}`；`:48–54` 从当前 loop execution 取 iteration | 单层顺序循环可区分 `wait_loop0/1/2`。不是重复同一个 context。不要只按 block 去重，也不能把后缀剥掉后恢复。 |
| 完整循环作用域 | `executor/handlers/human-in-the-loop/handler.ts:187–199` 暂停元数据含 blockId/contextId/loopScope；`lib/workflows/executor/human-in-the-loop-manager.ts:569–583` 按 context 持久保存 loopScope 和 response | 原生暂停记录有 loopId/iteration，不必用本地计数猜。嵌套循环只编码最近 loop iteration，不能直接推导全祖先激活唯一性；首版拒绝嵌套及并行。 |
| 重启恢复 | `executor/execution/snapshot-serializer.ts:142–160,213–221` 序列化 blockStates 与 loopExecutions，scope 包括 iteration 和迭代结果；manager `:703–705` 更新 executionSnapshot 和暂停点 | 原生快照承担循环推进，不由 Python 重建或重跑图。 |
| 暂停/恢复计数 | manager `:669–705` 合并按 context 索引的暂停点并计算 resumedCount；`lib/workflows/executor/execution-status.ts:414–427` 返回最早有效暂停和 points.length/resumedCount | 计数是当前 paused execution 的累计点数，不是某节点的 iteration，也不应直接当 attempt。顺序图可额外要求只剩一个未恢复点，但仍需精确 context。 |
| resume 子运行 | `lib/workflows/executor/resume-execution.ts:200–215,240–265` 生成新 resumeExecutionId，并保留 parentExecutionId | 要保存每条父 run/context→子 run 回执；不把子 ID 当成新 workflow 启动，不凭状态猜下一代身份。 |
| 循环继续 | `executor/orchestrators/loop.ts:305–326` 清本轮输出、评估下一 iteration 后递增；`executor/orchestrators/node.ts:343–360` 原生 loop_continue 清执行标志并恢复边 | 这是实际原生循环，同一节点重复激活。把图静态复制三遍不等价。 |

已有无循环结果探针曾真实观察：恢复子 run 失败而父 run 仍 paused/context=null。循环读者必须处理父子链，不能只看父状态便重投旧 context。此现象见 `docs/sim-agent-result-probe.md`；不能据此推定下一循环暂停一定挂在父还是子 run，本轮尚未实测该定位。

## selectedOutputs 的限制和可用取证

`lib/workflows/executor/execution-status.ts:432–456` 按 selector 的块 ID 获取输出；`lib/logs/execution/trace-store.ts:413–472` 只从 `executionState.blockStates[blockId].output` 取值。`executor/execution/state.ts:149–159` 用同一块键覆盖最新输出。公开 selector 没有“第 N 轮”参数，也不能把 context 后缀当 block ID。

因此，`wait.taskInput` 单独可读不等于它属于当前迭代。最小读者应一次请求真实 block 的 taskInput 和 `response.resume.contextId`（或整块），核对 pause.contextId、blockedOnBlockId、workflow/run 及该输出中的原生 context。Human 在 `handler.ts:214–235` 把 resumeLinks 放到 response.resume，并输出已解析 inputFormat 字段。随后将当前输入与哈希持久冻结到该激活记录；不可以后再读“最新块输出”作为旧任务输入。隐藏、截断、缺字段或 context 不一致一律拒绝登记；读取前后状态变化需重新只读核实，绝不提交旧激活结果。

详细暂停数据还有官方宿主路由 `GET /api/resume/{workflowId}/{executionId}`、`GET .../{contextId}`：`lib/api/contracts/workflows.ts:535–560,953–990` 声明 pausePoints、executionSnapshot、单 pausePoint；manager `:3740–3780` 暴露 loopScope、resumeStatus、snapshotReady。对应 `app/api/resume/[workflowId]/[executionId]/route.ts:1–29` 使用 internalWorkflowReadAuth；context 路由使用官方认证。这些是**官方 internal/legacy 路由，不是 v2 稳定公开契约**。不能把内部 snapshot 当作已有 v2 保证。最小首版优先 v2 输出里的原生 context 加冻结发布图证明；若实际输出无法充分关联迭代，应先验证并显式接纳该只读宿主路由依赖，否则保持不支持，不绕到数据库读写。

## 三次上限不能只填写 iterations

`executor/orchestrators/loop.ts:193–207`：while 直接采用 whileCondition；doWhile 一旦填写 doWhileCondition，就不进入设置 maxIterations 的分支。`:330–335` 的硬上限检查又要求 doWhile 且 maxIterations 已定义。因此“iterations=3 + 自定义继续条件”并不能由这段代码证明最多三次。

可实施首版是一个单层原生 doWhile，编译器生成受控表达式，将原生 loop index 小于 3 与“本轮需修复”的严格布尔判定合取，不允许用户任意 JS。索引解析见 `executor/variables/resolvers/loop.ts:179–181`；条件在下一轮索引上求值见 `orchestrators/loop.ts:307–315,379–395`。需要真实验证 0/1/2 三次以及成功提前退出；缺失结果、错误类型、未知验证状态应抛错或明确终止，不自动进入下一修复。若后续不采用此条件策略，必须重新证明另一种原生有界结构，不在 Python 自行推进循环。

## 最小适配契约

1. **发布冻结**：只新增一个有界、非嵌套、非并行循环形状；冻结 deploymentVersionId、loopId、允许 wait blockId、上限 3、受控条件和角色策略。`agent_line/publication.py:70–115` 当前保守拒绝 Human 容器/回环。扩展须单独证明允许形状，其余循环继续拒绝；人类审批与机器等待仍严格区分。
2. **激活身份**：采用 `(project,workflow,deploymentVersionId,rootRunId,nativePauseRunId,blockId,contextId)`，并核对本轮 loopId/iteration。`iteration` 只能为 0..2；首次/后继必须有原生证据，不通过全局 resumedCount 推算。冻结输入哈希，记录前驱的 resume 子身份，出现跳代、同 context 输入变化、未知父子关系均停止登记。
3. **任务和幂等**：`agent_line/task_discovery.py:66–95` 现要求 context==block，nodeExecutionId=block、attemptId='1'，幂等键也含固定 '1'。改为真实激活 context 和明确业务轮次；每轮一个新 task，执行重试与修复轮次分开。不要修改旧 task 输入或复用上一轮结果。角色/项目作用域、资源退出和现有工作区租约继续生效。
4. **暂停绑定/回执**：`pause_bindings.py:28` 已按 workflow/run/context 唯一；`sim_journal.py:109–156` 同样按完整 context 持久意图。对单层 `_loopN` 无须降低其唯一约束或清空旧日志。未知提交只观察原身份，旧回执不能恢复新 context。任务输出身份须精确命中该激活和 taskId。
5. **结果交付**：`completion_relay.py:152–169` 已核对精确 context/块和单一有效暂停；保留这层检查并接入新的可信循环绑定。每次 resume 前再核对当前激活；前轮迟到结果应只读返回旧投递状态或冲突，不自动送下一轮。`SimWaitClient` 现有 context==block 限制需改成经过发布证明的精确允许 context，而不是任何正则后缀都放行。
6. **原生后继发现**：既有 discovery/worker 继续观察 Sim 选择的暂停，不计算边、不决定下一节点。只沿已确认 resume 回执和正式查询确认的原生 lineage 找后继暂停，维护单调激活记录；QwenPaw/Codex 继续完成单个任务，原生记忆保持原实现。

## 必须完成的真实验收

先单独无模型新图，不改现有流程：同一个 Human 位于单层原生循环；三轮输入携带不同嵌套值。记录每次 pause.context、block、scope/可用原生输出 context、计数、父子 run、解析输入、正式发布身份。第一轮和第二轮返回需修复，第三轮仍需修复，必须终止且没有第四次暂停。另测首轮/第二轮成功提前退出。

在第二次暂停停止并重启适配器（后续授权窗口再测 Sim 重启），仍只能登记该激活一个 task。重放第一轮结果、重复 resume 请求、并发观察、输入 hash 不同、错误 context、版本变化、缺字段和错误类型都不能启动新轮或覆盖新结果。模拟响应丢失只观察同身份，不创建新 attempt。

随后才做真实单角色 Codex：同一源任务最多三次，前轮资源释放后下一轮才能进入；独立验证决定结果，失败路径有界退出。每轮输入和产物分别冻结，记忆写回遵循现有策略。当前说明不声称以上循环试验已完成，也不声称已支持嵌套、并行、无限循环或完整历史输出随机访问。
