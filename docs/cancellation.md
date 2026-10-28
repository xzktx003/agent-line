# 真实原生取消验收

`probes/cancellation/live.py` 使用独立持久 TaskStore，通过现有 Dispatcher → QwenPawExecutionGateway → 插件 cancel → 官方 console stop。没有修改执行桥或上游源码。角色仍为当前 G0 Codex 目标，不把本轮取消成功当作生产隔离验证。

流程：登记唯一任务和派生session，保存receipt后提交一次；模型在专属工作目录运行第一阶段文件写入、sleep45秒、第二阶段文件写入。探针实际看到第一阶段文件后记录取消意图，再只调用reconcile。复跑读取同一receipt，不创建新任务。

成功条件必须同时满足：持久任务为canceled、resourcesReleased=true；同thread/turn原生rollout存在turn_aborted；超过原sleep窗口后第二阶段文件仍不存在。HTTP202或stopped确认不作为终态证据。失败/未知保留原身份，不重投。

运行（从agent_line目录）：

```bash
.venv/bin/python probes/cancellation/live.py
```

证据保存在 `var/cancellation/receipt.json`，任务数据库为同目录 `tasks.sqlite`。登录凭据只从本实例0600文件读取，不打印。stage1和stage2均为专用验收文件，模型不读取其他文件。取消后的阶段1文件保留作为已开始执行的证据。

## 实测：取消失败，暴露资源释放误判

任务 `task-865dd4118d0a4afe9a2ed71d5ad228fa`，native `task-658172c5d7a8`，thread `01a15e26-38b9-7c02-b953-0a33d7891e00`，turn `01a15e26-3962-7710-be5d-7cd9f74cb52a`。

- 第一阶段写入时间：1792580933.176。
- 持久取消请求时间：1792580933.698。
- 同turn原生 `turn_aborted`：2026-10-21 11:08:53.774 UTC。
- TaskStore观察为canceled，桥接当时给出resourcesReleased=true。
- 第二阶段仍在1792580978.178写出STAGE2，晚于取消约44.48秒。

因此本轮验收明确失败。原生后台task finished加turn_aborted不足以证明工具进程树已退出，不能据此释放workspace租约。probe保存 `verified:false`，重复执行仅核对原任务，仍失败。主线需修正资源释放判据；后续生产取消应核实受监督容器/进程树实际退出，并继续做延迟副作用验证。此探针没有伪造终态，也未修改主线代码来让断言通过。

## 后续容器路径修复验收

旧宿主取消失败未被覆盖。新的受控容器路径通过独立新任务 `task-ddedb25696c24951bc76341b2f607814` 验证：同turn_aborted之后，控制器按绑定owner与完整容器ID强制退出，删除前inspect证明无进程，等待超过原35秒sleep后没有第二阶段写入。证据 `var/executor/bridge-probes/cancel.json`；详细接入与限制见executor-isolation.md。主库原失败任务的资源不确定隔离不自动解除。

## 父角色尚未委派时取消

执行插件回执库 schema 3 增加任务级 `revoked_delegations`。升级须显式调用 `ReceiptLedger.migrate`；已备份正式 schema 2 数据库后完成原地事务迁移，未删除原生执行回执或角色绑定。schema 1/2 均可迁移，未知版本拒绝。

服务认证 `POST /api/agent-line/delegations/{task_id}/cancel` 先持久撤销，再按原绑定角色及会话调用官方停止接口。尚未绑定的任务也保留撤销记录。后续绑定和首次 Codex claim 与撤销检查处于同一 SQLite 写事务，拒绝迟到工作；已 claim 的执行保留原身份，只能继续核对/取消，不重新提交。父停止确认和撤销本身均不作为原生执行终态或资源退出证明。

Dispatcher 在原生 identity 未返回或 observe 超时时也投递取消，不再等待未知身份先变为已知。真实 HTTP 探针 `probes/cancellation/delegation_revocation.py` 已通过：先取消未绑定任务，随后绑定和执行请求均为409，无模型任务启动；证据 `var/cancellation/delegation-revocation.json`。5项账本回归覆盖重启、显式迁移、提交/撤销并发顺序、先claim后撤销和先撤销后绑定。

仍须区分“阻止新的 Codex 委派”和“父角色自身退出”。取消与父 HTTP 投递交错时父调用可能短暂启动，但撤销阻止其工具委派；父任务退出及未委派任务最终状态需要独立协调，不能凭父模型回复或停止ack将未知工作自动判完成。

### 尚未创建 Codex 执行的受信自动收尾

`RoleDispatchGateway.cancel` 每轮先持久化取消意图，再取得新的撤销回执。
只有回执的任务 ID 匹配、`delegationRevoked is True`、`executionClaimed is False`，
且原投递账本保留相同角色/会话及父任务 ID，随后官方父任务 GET 实时返回
`finished`，才生成进程内 `UnstartedCancellationProof`。父响应若携带 task_id 也必须匹配。
父任务查询 404、超时、仅缓存 finished、无父 ID、撤销未知均不能收尾。
原生取消仅调用一次，先读取原始回执再归一化，避免丢失错误任务 ID 或畸形 identity
造成误判。原生执行 404 本身不是不存在证明；原子撤销回执才提供未 claim 的证据。
原生超时、服务异常或任何非空原生 identity 禁止生成此证明。

Dispatcher 仅接受内部类型，JSON/模型输出中的同名字典无效。它调用
`TaskStore.resolve_unstarted_cancellation` 再释放原工作区租约；Store 再次核对
任务/角色/会话、取消状态和无原生 identity。证据是 canceled_without_execution，
不伪造 Codex identity，也不把父模型结束标为执行成功。重复协调是幂等的，
重开角色账本后仍需重新取得实时撤销与父状态，不能从旧 observation 重建证明。
此实现已用 MockTransport 和真实临时 SQLite 测试，未新增真实模型任务或运行态验收。

### 自动路径真实证据

主线和隔离探针在同一原共享任务库验证了父模型超时、Codex从未创建时的自动收敛：只调用request_cancel及新进程Dispatcher.reconcile，未重新submit或手工构造证明。任务变为canceled、resourcesReleased=true、executionIdentity={}，原workspace lease删除，重开数据库保持。证据为var/executor/host-restart/attempt-4/automatic-unstarted-resolution.json。该验证没有进行宿主重启，不替代在途执行恢复验收。

### 执行器退出但原生任务状态未知

可信桥可在持有完整原生 upstreamTaskId/thread/turn 身份、且取得原容器退出实证后，
返回 `executorInterrupted=true`、`nativeStatus=unknown`、`terminalConfirmed=false`、
`resourcesReleased=true` 及 resourceExitReceipt。Dispatcher 只读取可信 gateway
返回值的顶层字段，不读取模型 output 中的同名字段；没有新增 HTTP 写证据接口。

`TaskStore.resolve_executor_interruption(task_id, identity, exit_receipt)` 在事务内
再次检查原身份、原 workspace、退出证据来源、32 位 hex owner、64 位 hex 完整
container ID、严格整数 pid=0、running=false、exited/dead、finishedAt，以及
receipt 中匹配的 thread/turn。owner/container 的原始归属由桥的受控执行账本
验证；TaskStore 不接受任意客户端提供此方法参数。既有 quarantine 事件禁止绕过。

通过后写入 failed、resourcesReleased=true，并保留 execution_interrupted 和
resource_exit 两份证据；不写 execution_terminal，不伪造原生 completed/canceled，
不把独立 verification 改成通过。任务输出保持空，原因固定为
owned_executor_exited_without_native_terminal。取消意图由原 cancel_requested 状态或
持久事件派生为 cancellationRequested；即使最终状态为 failed，该意图仍被保留。
Store 在同一 SQLite 事务内删除与原任务及 canonical workspace 同时匹配的租约；
Dispatcher 不再单独释放，避免终态提交与租约删除之间崩溃残留。同一证据重放幂等，
不会触碰后来任务取得的租约；事务异常同时回滚状态、证据和租约删除。不同身份、退出证明和已有
不同终态均拒绝。

工作流失败分支需要显式政策才能接收这种中断结果；带 cancellationRequested=true
的中断不能作为普通失败自动推进。CompletionRelay 的对应政策由主线独立接入，
不能用虚构的原生 terminal 事件绕过门禁。

回归覆盖正常中断、取消后中断、并发一次提交、重开数据库重放、坏证据、身份
冲突、quarantine、原生终态冲突、模型输出伪造，以及顶层字段相互矛盾。
本切片仅运行测试，未直接修改任何既有运行任务。

## 受信收尾与租约的事务一致性

`confirm_resources_released` 与 `resolve_unstarted_cancellation` 现将结果/证据更新、原任务规范化工作区租约删除和事件写入放入同一SQLite事务。Dispatcher不再在结果提交后另开事务release，避免进程在两步之间退出而留下released任务的旧租约。已有executor_interruption路径复用同一删除边界。

恢复时，后台候选扫描包含仍拥有租约的terminal/released任务；只有通过相同受信证据校验后的同任务reconcile才会删除该任务与原规范化workspace匹配的遗留租约。幂等重放不能删除已经归属新任务的租约，不能释放其他workspace；隔离标记、证据冲突、存量exit证据身份不匹配或缺少登记workspace均拒绝。没有新增schema、没有自动把未知任务标成完成。

回归覆盖两种收尾的原子删除、删除失败回滚、删除后事件写失败全回滚、重开数据库发现并修复旧租约、后继任务租约保留、quarantine/证据冲突以及后台observe恢复不重新submit。旧测试中缺失workspace的人工fixture已补为实际claim路径；实际缺失绑定的数据保持拒绝，不根据现存租约猜测原工作区。此切片仅代码与测试，无真实模型调用、无服务重启或数据收尾操作。
