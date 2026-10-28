# 持久父角色投递适配器

`agent_line/role_dispatch.py` 提供 Dispatcher 兼容 gateway：通过正式 HTTP 绑定固定 QwenPaw 父角色/会话，调用父角色 console 后台任务，由受控委派工具启动 Codex。它不选择后继节点，不更改 TaskStore，也不直接替代 Sim 调度。

## 接口

```python
ledger = RoleDispatchLedger.initialize('/private/new-role-dispatch.sqlite')
# 已存在的库必须显式打开，不能再次 initialize。
gateway = RoleDispatchGateway(
    qwen_origin, user_token, execution_service_token,
    role_agent_id='configured-parent',
    deployment_id='stable-installation-id',
    ledger=ledger,
    memory_query='由部署指定的项目规则与相关经验查询策略',
)
```

实现 `submit(task_id=..., role_session_id=..., instruction=...)`、`observe(task_id)`、`cancel(task_id)`、`close()`。可作为 Dispatcher 的项目 gateway，但必须由可信服务配置绑定 project/role；本模块没有修改运行配置或启用公共任务准入。

roleSessionId 严格为 `agent-line-{taskId}`，角色和稳定安装身份由构造配置注入。模型只获得任务数据，不提供 Agent/workspace 路由参数；真正执行目标与工作区由插件服务端配置固定。会话不得跨任务复用。

## 持久化与未知结果

显式初始化创建 0600 SQLite，包含 deliveries 与 cancellations。首次提交将 taskId、角色、会话、稳定部署标识、任务输入、记忆查询策略和超时固化为 canonical payload，并提交 `intent`，然后才调用：

1. `POST /api/agent-line/delegations/bind`
2. `POST /api/console/chat/task`，父角色通过 `X-Agent-Id` 固定
3. `GET /api/agent-line/executions/{taskId}`，核对原 Codex 执行
4. 如已取得父任务 ID，`GET /api/console/chat/task/{parentTaskId}`，保存父任务观察

同 task 同 payload 只能观察既有记录；不同 payload/role/deployment 冲突。原生父任务 ID 单独持久化，不能覆盖 Codex 的 upstreamTaskId、codexThreadId、codexTurnId。网络超时、绑定失败、提交返回前后崩溃均不重新启动父任务。缺失库直接报错，不初始化空状态。

即使父角色任务 finished，也不能据此报告 Codex 成功。尚未找到 bridge 身份时保持 unknown、terminalConfirmed=false、resourcesReleased=false。父任务查询在 QwenPaw 重启后可能 404；仍先核对固定 taskId 的执行桥，已有 Codex 执行可独立收敛。若父回执丢失且从未委派，当前保守保持 unknown，不实现自动重发或重建父任务。

父 observation 包含真实接口返回的工具记录，属于私有执行证据；应纳入部署的数据保留与备份策略，不作为第二套长期记忆权威。

## 取消与迟到委派

cancel 先写本地持久取消 tombstone，即使父投递尚未发生，后续 submit 也不能启动父任务。之后调用插件的正式服务认证接口：

`POST /api/agent-line/delegations/{taskId}/cancel`，无请求正文。

插件须将 task 级撤销与 bind/claim 原子互斥，禁止取消前绑定、取消后迟到的新委派，并停止固定父会话。随后 gateway 调用 Codex bridge 的固定 task cancel。撤销接口、父停止回执和 Codex 原生终态证据必须分别理解：parentStopAcknowledged 或 delegationRevoked 本身都不证明 Codex 已退出。

只有 fresh delegationRevoked=true 且原生 bridge 返回实际退出/资源释放证据，gateway 才允许 resourcesReleased=true。撤销失败或未知仍保留 false；没有 Codex identity 的情况不以 404 推断“已经安全取消”。

已有 cancel intent 时，observe 会继续按同 task 调用撤销/取消核对，而非只做 GET，确保先前撤销超时后可以收敛；这不会重新提交父任务或 Codex。当前源码要求插件 ledger schema4并保留schema3的撤销记录；显式迁移和角色版本固定见 [角色配置版本](role-revisions.md)，正式实例本轮未迁移。

## 验证与未验边界

`tests/test_role_dispatch.py` 先以模块不存在失败，再实现后 16 项通过：显式初始化、固定角色/会话、父终态不冒充执行成功、四路并发仅一次父提交、丢失回执与崩溃后不重发、payload/role 冲突、错误绑定、Codex 原始身份保留、无父回执仍可观察已委派 Codex、取消前置与提交竞态、撤销超时不释放、后续观察恢复取消收敛。

本切片未修改主线运行配置，现已新增 `probes/qwenpaw/role_dispatch_live.py` 验证本适配器真实集成，见下文。跨 QwenPaw 服务重启后的本适配器恢复，以及真实父取消竞态仍需独立验收；不能用 MockTransport 测试代替。

父提示明确要求先 memory_search 再 agent_line_delegate。任务数据已经给出精确文件内容时，禁止用经验标记覆盖。任务数据没有精确文件内容、并明确要求复用已验证正式约束时，允许把 memory_search 返回的正式约束原样放进唯一一次 Codex instruction。提示不等于强制调用或已注入记忆：本模块不生成 memory verified 标记，也不把模型成功文本当作记忆证据。正式记忆链验收仍需关联真实 memory_search call/result、delegate 参数与结果，以及实际 Codex 输入/产物；当前缺口不能因 gateway 返回 Codex succeeded 而标完成。

## 真实集成验收（2026-10-21）

使用独立 `var/role-dispatch/tasks.sqlite` 与 `roles.sqlite`，固定父角色 `agent-line-g0-parent`，部署已配置的 `agent-line-container-codex` 执行角色。没有解除主库 maintenance、修改角色配置或重启服务。

实际路径：Dispatcher → RoleDispatchGateway → 正式父 console/chat/task → memory_search → agent_line_delegate → 原生容器 Codex → 独立产物校验 → 原始容器退出证明。

|身份|实测值|
|---|---|
|节点任务|`task-00d41e38e770420b8881543dcfa9c903`|
|父任务|`task-10518ce65176`|
|原生 Codex 任务|`task-ef8249e656fb`|
|Codex thread|`01a15e3c-8537-7350-b4ca-3d201db0eded`|
|Codex turn|`01a15e3c-985f-7fe3-a6e2-33fb40535e30`|
|随机新产物|`var/executor/workspace/role-dispatch-8bd3584d9f59.txt`|

验收 marker 只从原生父记忆 `agent-line-execution-plan.md` 取得，首次任务输入未包含实际 marker。探针从父接口工具记录关联真实 memory_search call/result，要求该记忆源及 marker 出现，然后验证 delegate 仅含 instruction 参数、其内容含召回 marker、成功工具返回的 native task 与实际 Codex identity 一致。独立 ArtifactVerifier 校验新文件精确内容/大小。没有复用或覆盖旧测试产物。

资源证明来自 `docker-inspect-before-remove`，包含固定容器 ID、相同工作区、匹配 thread/turn 的 nativeTerminal=true、running=false、pid=0。实际容器在原生 turn 完成后退出；容器退出码不能单独解释为编码成败，编码状态仍由原生 turn 确认。本次两种证据分别齐全。

重开 ledger/store 后通过 Dispatcher 和 gateway 重复同 payload 提交，仅观察原身份；父任务 ID 不变，没有增加 parent POST。随后独立 Python 进程再次运行，当前调用的 parent POST 计数为 0。未重启 QwenPaw 服务，不能将这项进程级重开测试扩写成服务重启验收。

证据位于 `var/role-dispatch/live.json`、两个 SQLite 和 `var/role-dispatch/artifacts/`。运行：

```bash
.venv/bin/python probes/qwenpaw/role_dispatch_live.py
```

存在 state 时只使用原 task/parent 身份，不生成替代节点或新产物。脚本重复执行可能继续核对原未知任务，但不会重投父任务。

这是真实单样本“召回进入委派并影响产物”的证明，不代表所有任务都被代码强制使用记忆，也不包含正式记忆回写。独立 probe store 的工作区租约不与主 store 跨库协调，因此本次先协调无在途任务；正式运行仍需统一 TaskStore 和部署角色绑定策略。

## 实际宿主重启后的核对

主线已停止并重新启动本项目QwenPaw，启动脚本复用记录端口52403，执行配置和网关不用改地址。原生父后台任务查询重启后返回404，扩展ledger保留原parentTaskId及已采集工具结果；原Codex thread/turn、产物及容器退出证明继续可核对。

重跑同一个role_dispatch_live探针为passed_live，父POST为0，未重新创建任务或Agent。官方GET两个角色接口确认父角色与容器Codex角色保持原ID/backend；官方记忆API读取原记忆文件仍含原标记。证据 `var/role-dispatch/host-restart.json`。这验证已完成任务的重启核对和角色/记忆持久化，不证明在途进程崩溃恢复已完成。
