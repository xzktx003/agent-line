# 原生执行权限审批接入

继续以 QwenPaw 为主程序，复用官方 ApprovalService。Sim 人工业务节点审批与 Codex 执行权限审批分别处理；批准权限只回应原生请求，不能据此标记编码、测试或流程完成，也不调用 Sim resume。

## 接入与身份

已有适配服务同时配置 `approvalFile` 和 `executionFile` 时，沿用原人工审批凭证注册以下接口，不增加配置文件、运行服务或依赖：

```text
GET  /extension/v1/node-tasks/{taskId}/execution-approvals
POST /extension/v1/node-tasks/{taskId}/execution-approvals/{requestId}/decision
```

POST 只接受 `decisionId`、`decision`（`approve` 或 `reject`）、`expectedIdentity`。后者含原查询的 `upstreamTaskId`、`codexThreadId`、`codexTurnId`；决定人从认证身份取得。任务所属项目、工作流、运行、节点、执行尝试、父角色均从服务端原任务派生，浏览器不能指定目标角色、会话、授权范围或决定人。项目范围不匹配返回404，服务凭证不能替代独立人工凭证。

已有 RoleDispatchGateway 复用原 QwenPaw 登录和服务凭证，调用插件任务级列表与决定接口。插件从原回执和角色绑定导出唯一 `agent-line-{taskId}` 会话与固定 Codex 目标，检查原生会话、root session、owner/agent、来源、有效期与 Future 状态。

官方当前审批协议包含 `threadId`、`turnId`、`itemId`；[OpenAI App Server 文档](https://learn.chatgpt.com/docs/app-server#approvals)说明了这三个字段及按当前轮次回应的方式。当前固定版 QwenPaw 的公开列表不含完整原生关联，所以扩展在已有 launcher 中补充有限的身份记录，不修改上游适配器。每条只保存 RPC 请求标识、method、thread/turn/item 及是否已响应，最多256条，不保存命令正文。先持久化，再转发给 QwenPaw。

插件调用原容器控制器的 `approvals` 动作核对原 owner、完整容器ID、正在运行的进程和唯一活动 turn，并匹配官方 pending 的 provider item。缺字段、歧义、其他活动 turn、过期、取消或关联变化均不能批准。旧 launch 没有这些记录时明确无法核对，不追认当前角色或重建请求。

## 提交、恢复与范围

原插件 schema4 的 `receipts.record` JSON 增加 `approvalDecisions`；没有新表、迁移或第二套审批服务。事务先保存不可变的决定人、决定标识、决定、执行身份、原生关联与提交意图，然后才调用官方 `resolve_request`，范围固定 `EXACT`。Codex 原生适配器仍决定如何将其映射为本次命令、文件或本轮权限回应，不扩成会话全局授权。

同一请求只有一个意图可获准投递；同一决定重读原历史，不再次 resolve。冲突决定拒绝；进程中断留下 `intent`，异常留下 `uncertain`，不会因请求消失而重投。原生请求重启后不存在时只能说明未观察到，不能推断已批准或重跑编码。官方明确返回已接收决定才记录 `accepted`；到期竞态返回 `unavailable`，不伪称批准。

外层复用维护、原运行准入及任务锁：停止/暂停先发生时拒绝新决定；审批已进入投递时，任务取消等它退出后再提交。读取历史不启动新执行。主实例的 `unknown` 能力和维护守卫未因本实现而解除。

## 页面与验证

现有中文人工审批页新增执行权限区，共用页面内人工凭证；任务详情可跳转并仅预填任务标识。页面确认原项目、运行、节点、尝试与 Codex 身份，未知回执仅查询。见[界面验证](native-approval-ui.md)。

- `tests/test_executor_approval_binding.py`：启动器持久化先于转发，三个方法、双向RPC同ID隔离、resolved状态、歧义与边界。使用假子进程和 Docker，不代替真实审批。
- `tests/test_native_approval_plugin.py`：真实官方 ApprovalService 与临时原回执库；批准/拒绝、有效期、错误身份、并发、意图恢复及异常防重。
- `tests/test_native_approval_api.py`、`test_native_approval_transport.py`：独立人工权限、项目边界、上下文、过期/取消/维护/运行控制、回执未知、投递与取消交错以及双认证运输。
- `tests/test_native_approval_integration.py`：真实外层 API、任务库、RoleDispatchGateway、插件路由/回执、官方审批 Future 串联；模拟HTTP运输、Codex观察和控制证明。覆盖批准/拒绝、并发与应用重建，不使用假执行成功冒充真实编码。

以上代码与自动化测试不能替代一次实际 Codex 权限触发、原生沙箱能力、宿主重启后的请求有效性或完整PRD验收。真实现场状态单列于[native-approval-live.md](native-approval-live.md)；未取得现场证据前不扩大能力声明。

较早隔离现场仅批准通过，第二任务在审批前遇到 `Codex app-server exited`，原未知任务保留。随后插件增加 launcher 退出与官方 Runtime 收尾，真实双节点连续执行已通过。修复后的独立审批现场现已验证批准命令 exit0、拒绝命令在创建进程前被拦截，两个原任务均已退出且原 Runtime 收尾，见[native-approval-live.md](native-approval-live.md)。拒绝证据明确采用同一turn内唯一工具与审批关联，原资料没有直接item→callId映射；原探针“declined条目缺失”的状态原样保留，独立只读审计没有重跑任务。父角色仅作持久身份绑定，尚未覆盖原生审批在途重启或完整Sim审批组合验收。
