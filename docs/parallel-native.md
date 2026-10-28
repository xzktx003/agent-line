# Sim 原生静态并行与汇合探针

此探针验证固定版本 Sim 的真实 DAG 并发和汇合，不调用模型、QwenPaw、Codex、角色或记忆接口。它不增加服务、依赖或上游补丁，也不能单独证明产品已支持并行 Agent 节点。QwenPaw 仍是主程序，原生记忆系统保持不变。

`probes/sim/parallel_native_probe.py` 只通过官方 API 创建一个新的专用工作流：开始同时指向甲、乙两个 Human v2 节点，两个节点共同指向原生 JavaScript Function 汇合，再进入 Response 结束节点。它使用普通静态 DAG，不使用动态 parallel 容器。Human 的通知为空，恢复输入为探针生成的合成结果，不代表真实人工审批或 Agent 执行。

## 核验内容

同一个发布图顺序运行五个场景，每个场景有独立固定 root run ID、随机 nonce 和两个不同的静态 context：

| 场景 | 甲结果 | 乙结果 | 策略 | 必须观察到的结果 |
| --- | --- | --- | --- | --- |
| all_success | true | true | all_success | completed，含两个原始结果与本轮 nonce |
| all_failure | true | false | all_success | failed，错误含 parallel_policy_failed |
| partial_success | true | false | allow_partial | completed，保留乙的 false，不伪装全成功 |
| required_failure | false | true | required_success，必需甲 | failed，错误含 parallel_policy_failed |
| missing_result | true | 缺少 ok 字段 | all_success | failed，错误含 parallel_result_invalid |

Function 对两个结果分别验证对象类型、branch、严格 boolean 类型和当前 nonce，再显式应用策略。只有成功策略才返回汇合结果。拓扑走到汇合、HTTP 202 或工作流结束都不能替代此验证。

每个场景先观察两个节点**同时**具有 `resumeStatus=paused`、`snapshotReady=true`。冻结原始 context 后，故意先恢复乙，再恢复甲。第一次恢复后必须看到乙已 resumed、甲仍 paused，根运行及恢复子运行都没有汇合/结束输出。未提前汇合还通过私有原生 executionSnapshot 中 blockStates、executedBlocks、blockLogs 的实际执行集合交叉核对；不以图中存在节点当作节点已执行。第二次恢复后读取根运行及两个恢复子运行，核对最后子运行真实终态、两原始 context 的状态与恢复计数、成功输出或失败原因。成功场景两个 context 都必须 resumed；Function 失败时上游可将最后恢复的甲标记 failed，此时乙仍须 resumed，计数必须与真实 resumed 数量一致，不能把失败伪装成已成功恢复。成功场景还逐字段核对 Function 原生输出和 Response 输出，包含双方结果及本轮 nonce。

## 调用边界

发布和执行使用固定 Sim 的 v2 API。暂停详情额外依赖 `GET /api/resume/{workflowId}/{rootRunId}`；这是上游现有宿主/internal 路由，**不是 v2 稳定公开契约**。它使用同一个 `x-api-key`，返回裸详情及全部 pausePoints。每阶段实际调用薄适配器 `SimWaitClient.read_pauses`，仅投影身份、严格恢复计数和必要暂停字段；探针同时保留独立原始读取作私有证据。轮询可能跨越状态变化，判断只使用单次完整投影，不混用两次读取的字段。v2 run 摘要只展示一个 active context，不能据此认定另一个分支丢失。该依赖需要升级兼容检查。

探针只观察或恢复自己的专用工作流及原始身份，不修改既有工作流、主服务配置、原角色或记忆。私有原始详情可能包含 executionSnapshot，保存在 0600 证据文件，不能公开输出。

## 使用方式

从仓库根目录运行。认证文件复用 `var/sim/probe-auth.json`，权限必须为 0600。默认私有证据目录为 `var/sim-parallel-native/`，目录权限为 0700。

```bash
# 只显示无凭证的图，不联网
.venv/bin/python probes/sim/parallel_native_probe.py --graph
# 默认只读：无 state 时不创建工作流；有 state 时只观察原身份
.venv/bin/python probes/sim/parallel_native_probe.py
# 首次显式执行，创建一个专用工作流并运行上述五个合成场景
.venv/bin/python probes/sim/parallel_native_probe.py --execute
# 再次执行也只 GET 原身份，nativeWritesThisInvocation 必须为 0
.venv/bin/python probes/sim/parallel_native_probe.py
```

每个修改请求前，探针先将方法、路径、完整载荷、固定运行/context 身份和 intent 写入 0600 文件，并 fsync 文件及目录，再提交一次。回执与 resumedRunId 也立即持久保存。不存在修改请求重试；已有 state 时即使带 `--execute` 也只观察。超时、失联、未知 intent 或不完整证据均停止；不能删除 state、换 ID 或重投来隐藏原任务结果。观察超时不会触发新的执行或恢复。

首次完整执行最多创建、保存、发布各一次，并执行五次、恢复十次，共 18 个原生修改请求；无真实模型调用。成功输出 `passed_live` 只代表上述原生静态图范围。详细证据为 `state.json`、`report.json`；再次只读核对写入 `observation.json`；失败保存在独立 `failure-*.json`，不覆盖旧失败。

## 首次观测中断的限定继续

首次真实执行已完成 create/save/publish 和首个 all_success execute；原 run `fb72d1fa-1f16-4a18-9b56-16cd620b006c` 保留在双暂停状态。第一次观察错误地请求尚未执行的 join/end selectedOutputs，Sim 返回 400，探针在任何 resume 前停止。此失败证据保留。当前观察已改为不带 selectedOutputs 的基础 GET；只有成功终态的实际 Response 已证明 Function 执行后，才单独选择它的结果，负场景不请求不存在的输出。

仅对这个已明确核对的首次只读中断，增加一次窄范围继续入口：

```bash
.venv/bin/python probes/sim/parallel_native_probe.py --continue-known
```

它必须同时满足：精确四个原修改步骤且全部 received，唯一的上述原 all_success run，无 children/context/任何 resume intent，没有先前继续标记；原执行回执、输入、双方合成结果、nonce 必须完全匹配；当前 draft 与原发布图相同、原版本仍 active；原 run 仍为同身份双 paused/snapshotReady，快照没有执行 join/end。验证完成后先持久化一次性继续标记，再恢复原 run 的乙和甲，随后完成原计划其余四个场景。**不会再次 execute 首 run，不换 ID、不创建替代工作流。** 后续任何中断不符合此窄窗口，不能再次继续；默认调用仍为零原生写入的观察。

## 当前验证

2026-10-22，五场景在固定 Sim 服务全部通过。专用工作流为 `189706bb-5cb2-411d-bacc-733f360cc403`。上述限定继续窗口已经完成，首 run 没有再次 execute；总计18个原生修改请求均已 received。默认再次运行实测0原生写入，旧400失败证据保留。

每个场景均先观察到两个 ready/paused，再逆序恢复；首分支恢复后的实际快照没有 join/end 执行。全部成功和允许部分成功的原生输出准确包含双方结果及本轮 nonce，其余三个负例按明确错误失败。负例的当前 context 可能被上游标为 `failed`，探针保留真实计数，不伪装成两次成功恢复。

根代理独立查询原生终态、原根运行发布版本、`latestResumeEntry` 的 context→子运行关联、实际输入输出与错误，五场景复核通过，见 `var/sim-parallel-native/root-check.json`。子运行 v2 日志详情返回404，版本核对使用原根日志，子运行关系使用原生暂停账本，不宣称读到了子运行日志。

新增暂停列表适配器的8项回归先失败后通过，相关36项及全量1021项通过（42.723秒）。错误级flake8、JS/shell、上游完整性及diff检查通过。主九份配置/守卫未变，活动owned执行容器0；本轮没有调用 QwenPaw、Codex 或记忆接口。

## 产品接入进展与剩余范围

后续切片已在源码接通静态编译、中文编辑器、多暂停任务发现及结果回传，并完成真实Sim与本地账本的四个组合场景；执行数据明确为合成。见 [编译图组合验证](compiled-parallel.md) 与 [编辑器验证](editor-static-parallel.md)。旧记录默认保持单暂停边界，新发布/运行/暂停绑定须明确冻结多暂停能力；主执行准入未自动开放。共用工作区仍由原租约串行或拒绝并发，不增加调度服务或记忆引擎。

动态容器、批量、全局并发预算、真实多 Agent/Codex 并行与子流程仍待实现；AC-31/P1不记通过。另已确认固定 Sim 子流程默认调用当前活动版本，公开 v2 不接受历史 deployment pin；AC-33需另完成版本及预算接入，不能直接放开原生节点冒充满足要求。

2026-10-22只读源码补核：原生`workflow_input`定义见
`upstream/sim/apps/sim/blocks/blocks/workflow_input.ts`；工具
`tools/workflow/executor.ts`只传`workflowId`与`inputMapping`。
`executor/handlers/workflow/handler.ts`按部署上下文读取当前活动子部署；
`lib/execution/call-chain.ts`的深度上限固定25，不能替代PRD要求的可配置深度及嵌套总预算。
内部MCP的deployment版本字段没有开放到这个子流程块。此核对没有启用子流程或修改上游，
AC-33保持未验证；也不能把只读来源确认当作版本固定或预算终止已实现。
