# 真实自动 Agent 工作流探针

入口 `probes/full_workflow/automatic.py`，独立数据位于忽略目录 `var/automatic-task-flow/`，JSON 配置、请求意图和证据均为 0600。使用既有项目 native-memory-roundtrip、角色绑定 native-loop-parent、原生角色 agent-line-native-loop-parent；不新建角色、不修改记忆引擎、不安装插件、不解除主服务准入。

## 验收链路

新中文文档 Start → Agent → End，由现有编译器将 Agent 转为原生 Human 机器等待。探针通过真实 SimAuthoringClient 创建、保存、发布并读取确切版本。调用正式 load_task_flow，显式初始化独立 WorkflowRunLedger、PauseBindings、SimJournal、TaskStore 和 RoleDispatchLedger。

HTTP start 经 TestClient 调用真实 WorkflowGateway。后台 ExternalTaskDiscovery 读取已暂停图和持久 taskInput，登记任务并绑定 pause；现 ExecutionWorker/Dispatcher 经 RoleDispatchGateway 调用既有 QwenPaw 原生协调角色，由实际 Codex 生成唯一新文件。探针仅负责独立 ArtifactPolicy 精确字节校验；后台 CompletionWorker 返回结果，Sim 自己推进到 End。脚本没有手工调用 register、bind、CompletionRelay 或 dispatcher.submit。

End 为常量，只证明图走完，不能用作任务成功证据。最终验收另外核对 task identity、独立文件校验、资源释放、原生 parent finished，以及原生机器 wait 的 resumeInput.taskId/executionIdentity/output 与 TaskStore 完全一致。

## 准入与重复运行

首次启动必须 quiet_host 无 Codex descendants/owned executor 容器，pinned 上游完整性检查通过，现 execution_bridge 配置启用 container_control，已有 native-memory attempt4 真实证明 verified 且租约/容器为零。执行工作区从桥配置读取；不将协调角色 cwd 当作执行工作区。

仅此独立探针程序化配置 controlled_execution=confirmed；不改变主服务 guard，不能据此宣称生产隔离验收。真实父任务超时 900 秒，只允许一个明确新任务，不创建自动新 attempt。指令要求一个新文件，使用新的独立验收标记，不要求模型构造固定业务结果 schema。

create/save/deploy 写入意图先 fsync，启动 payload 和确定性 runId 在 POST 前持久化。已有 state 重跑脚本只查询原 Sim run 和原任务，不再次创建、执行或重投。异常保留原身份与 failure.json；不会把未知状态当作可重试。

## 证据状态

最终以 `var/automatic-task-flow/report.json` 的 status 为准；不存在 passed_live 报告时不得宣称全链路完成。运行中的 task、worker 报告和 Sim 观察保存在 state.json。此文档只描述真实探针边界，不冒充浏览器 UI 验收或全 PRD 完成。

## 最终实测结果

本轮 `report.json.status=passed_live`，真实进程 session 7003 正常退出（exit 0）。

| 身份/证据 | 值 |
|---|---|
| workflowId | `0205cba2-1904-4564-abf1-9a09596ba101` |
| runId | `30da5f5c-7f23-582b-ba84-6992301862ef` |
| taskId | `task-df9a2924bc6743cd8ab276752c0f7be3` |
| 原生父任务 | `task-3ca9fd9e5ba2`，finished |
| 唯一新文件 | `automatic-flow-0b471b2ad77c402c.txt` |
| 文件 SHA-256 | `a06ee9f9c994298069b4185c6035d87e6478c82cdc0bf0e73bd12738041d3861` |
| 任务/独立校验 | succeeded / passed |
| Sim | completed |
| 资源、租约、容器 | resourcesReleased=true，activeLeaseCount=0，ownedActiveContainerCount=0 |

后台发现器自动登记恰一个任务，后台执行器取得 codexThreadId/codexTurnId/upstreamTaskId，后台回传器完成恢复。探针没有手工登记、绑定或调用 relay。原生 wait 的 resumeInput 与最终任务逐字段核对 taskId、executionIdentity、output、executionStatus、verificationStatus，`nativeResumeMatchesTask=true`。正向容器退出证据随 task evidence 保存在 state.json；最终 quiet_host 另外确认无 Codex descendants 和活动 owned 容器。

真实 Sim 传输统计只有一次 create POST、一次 state PUT、一次 deploy POST、一次 execute POST、一次 resume POST。重复独立调用脚本返回 observation_only、newSubmissions=0、taskCount=1，没有再次启动后台提交循环。所有顶层 JSON 文件权限实查为 0600；定向 flake8 通过。

本结果只覆盖一个现有正式角色的单 Agent 节点、一个专用新文件、独立探针运行时。未验证浏览器搭建/启动、多个 Agent 串联的自动任务执行、业务失败分支、自动记忆写回或生产隔离；并发版本发布仍受现有发布证明约束。原生记忆引擎保持 remelight，requireMemoryCommit=false；不得把本测试说成记忆写回闭环。
