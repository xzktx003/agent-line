# Agent 任务与人工确认共用流程

QwenPaw 继续负责正式角色、会话和原生记忆；Sim 保存和调度流程。人工确认使用已有
审批接口和发现线程，Agent 使用已有任务发现、执行与结果回传，不增加运行服务。

## 发布与运行

服务同时配置 `authoringFile`、`approvalFile` 与 `taskFlowFile` 时，审批配置的
`runLedgerPath` 必须与任务流程配置指向同一个既有私有运行账本。审批和任务共用
Sim 连接及提交日志。代码根据实际装配启用人工节点发布；浏览器或配置声明不能
直接把能力设为可用。缺少配置或账本不同会阻止混合流程发布或新启动。

发布回执分别记录 `externalTasks` 和 `humanApproval`：前者保存 Agent 策略，后者保存
人工节点 ID、版本、部署身份和到期策略。节点 ID 从解码并核实的图派生，两个范围
互不重叠。发布目录读取原有日志，不需要用户另填一份工作流绑定。

机器流程的原快照继续使用 `purpose=external_task_result`。`humanBlockIds` 保留为所有
原生等待节点的全集，供发布图核对；新增可选 `approvalBlockIds`、`approvalTtlSeconds`
只授予对应人工节点审批用途。没有这两个字段的旧机器运行不会获得人工审批权限。
快照仍使用原 schema 1 的 JSON 列，不迁移或重建运行账本。

开始前先核对发布图，再一次冻结两类策略，随后登记并提交 Sim 执行意图。
重复触发只返回原记录；修改人工 TTL、节点范围或机器策略后重放会冲突，不重新执行。
机器与人工发现器遇到属于对方的合法暂停时等待，避免覆盖对方已登记的状态。

## 人工决定与分支

人工审批仍要求独立且有效的项目审批凭证，普通服务凭证不能代替。真实暂停的
workflow、run、block、context 必须逐项匹配，且严格整数 `pausePointCount-resumedCount`
等于 1。Sim 的总计数包含先前机器节点；第二次以后的人工暂停也可正常处理。

决定仍经原接口提交 `decisionId` 与 `approve` 或 `reject`，身份由服务端给出。
原生恢复数据是 `{"approval": {"approved": true或false, ...审计身份}}`。
后继条件可选择所有路径必经的人工节点，字段固定为 `result.approval.approved`，
仅允许布尔值与等于/不等于。缺字段或错误类型明确失败，不能静默进入默认路径。
拒绝是否结束或进入另一条路径由已发布的条件图决定，不由适配器代选。

决定、提交意图与回执继续持久保存。同一决定重放不再投递；不同决定或操作者冲突。
过期、错误暂停和未知提交保持原身份核对，不能创建新决定绕过。通用 resume 不开放给
人工或机器受保护运行。

## 验证与剩余范围

`test_mixed_workflow_gateway.py`、`test_mixed_runtime.py`、`test_mixed_publication.py`、
`test_human_authoring_api.py`、`test_human_conditions.py` 和既有审批回归覆盖冻结、
用途隔离、真实服务装配、计数、严格表达式和不重复投递。中文条件表单的离线浏览器
验证见 [人工条件编辑](editor-human-conditions.md)，不代替真实 Sim 或 Codex 验收。

两次真实 Codex 执行、字节验收后批准与拒绝的证据见
[自动混合流程](automatic-mixed-approval.md)；根代理的独立只读复核位于
`var/automatic-mixed-approval/root-check.json`。循环后人工确认的原生验证见
[模拟结果探针](mixed-approval-native.md)，其中机器结果明确为 synthetic，不计模型任务。

当前人工节点位于普通无环路径，可置于受支持的有界 Agent 循环之前或之后；不支持
人工节点本身位于循环、并行容器或反复激活。循环仍限定单 Agent 循环体与最多三轮。
模型执行过程中的 Codex 原生工具审批、全流程暂停/停止、完整重启故障注入、正式准入
与升级验收仍需继续，不能将工作流级人工确认等同于这些能力全部完成。
