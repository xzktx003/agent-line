# 人工确认账本与 Sim 决策投递

`agent_line/approvals.py` 处理明确的人类业务确认，不处理 Codex 原生权限请求，不把 Agent 提问或 external_task_result 等待当作人工批准。此次仅实现独立模块、单元测试与契约；没有接 HTTP/UI、运行真实审批图或调用模型。

## 可信绑定与身份

```python
ledger = ApprovalLedger.initialize('/private/new-approvals.sqlite')
binding = ApprovalBinding(
    approval_id='approval-id', project_id='project-id',
    workflow_id='workflow-id', run_id='run-id',
    block_id='human-block', context_id='pause-context',
    revision='published-revision', activation_id='activation-id', attempt_id='attempt-id',
    purpose='human_approval', expires_at=expiry_timestamp,
)
ledger.bind(binding)
coordinator = ApprovalCoordinator(ledger, sim_client, sim_journal, authorize=live_authorization_check)
```

账本必须显式初始化，已有数据库只打开，缺失不会静默创建。绑定由可信发布/运行登记层提供，不能把整个请求 body 或模型输出直接当绑定。project、workflow、run、block、context、发布 revision、activation、attempt、用途及失效时间一旦绑定不可变；同一 workflow/run/context 不允许重绑到其他审批、项目、版本或尝试。

调用服务根据已认证用户构造 `TrustedPrincipal(subject, frozenset(project_scopes), frozenset(capabilities), expires_at)`。subject 是稳定认证主体，不能取请求 actor 字段。必须具有当前项目 scope 与明确 `approve` 能力，并通过每次投递调用的实时授权回调。普通服务 token、模型输出和执行成功不自动获得 approve 权限。当前模块不负责实现登录系统或判断调用者是否实际点击了 UI，集成服务必须保持这一信任边界。

## 不可变决定

```python
result = coordinator.decide(
    'approval-id', decision_id='immutable-decision-id',
    decision='approve',  # 或 reject，不能用泛化的 answered/success
    principal=authenticated_principal,
)
```

scope/能力/主体时效/实时撤销检查先于上游观察与本地决定写入。观察暂停后、提交前再次检查。已失效授权不会产生决定或恢复请求。若授权在已合法记录决定后才撤销，原决定保留为历史审计，但后续恢复投递被拒绝；不能删除历史掩盖授权变化。

审批决定、decisionId、认证主体一起不可变。同审批同决定的重试幂等；修改其中任意一项冲突。decisionId 不能再绑定其他审批。SQLite 事务和唯一约束使并发批准/拒绝只有一个决定胜出。

## 精确暂停核对与 Sim 契约

在新的恢复投递前，通过真实 SimClient.observe 核对 workflowId、runId、status=paused、contextId、blockedOnBlockId、pauseKind=human、pausePointCount=1。缺少字段、未知结果、不同身份、多暂停点或旧上下文都不写新决定、不恢复，只返回 waiting。模块不选择新的 context 来补救，也不选择后继节点。

恢复输入固定为：

```json
{
  "approval": {
    "approvalId": "approval-id",
    "decisionId": "immutable-decision-id",
    "decision": "reject",
    "approved": false,
    "decidedBy": "authenticated-subject",
    "projectId": "project-id",
    "revision": "published-revision",
    "activationId": "activation-id",
    "attemptId": "attempt-id"
  }
}
```

批准发送 decision=approve、approved=true；拒绝发送 decision=reject、approved=false。后继 Sim 条件节点必须针对 `<human.resumeInput.approval.decision>` 分支，并显式配置拒绝路径。恢复 Human 原语不等于批准业务事项；拒绝也是让 Sim 执行其拒绝分支所需的结构化输入。服务不通过自己挑选后继节点来实现分支。

`SimJournal.resume` 先持久化出站 intent，再调用上游。超时或崩溃留下 uncertain/intent，后续同决定操作只读取原意图，不重发 POST。决定记录已存在但尚无出站 intent 时，重新核对当前 pause 和授权后才允许首次恢复调用。其他用途已占用该 journal context 时冲突，不能把外部任务结果回执改造为人类批准。

返回的 deliveryStatus 是传输状态，runStatus 是观察状态，二者独立。accepted 不等于流程完成；uncertain 后观察到 completed 仍保留原传输不确定性，不伪改旧回执。`reconcile(approval_id, principal=...)` 仅核对当前上游与 journal，不决定、不恢复。有效授权用户可查看已过期关卡历史，但 decide 不会自动重放过期动作。

## 发布版本与重复激活边界

当前官方 Sim v2 run GET 不返回发布 revision。revision 必须来自可信的发布/启动记录，模块仅保证持久绑定和不可变，不伪称从 pause 响应验证了版本。服务接入必须验证该发布版本声明的审批输入契约和拒绝分支。

当前按 workflow/run/context 唯一关联，不能支持无法区分不同激活的重复 context。若循环或外部操作可能让同 run 的同 block/context 复用，单靠 pauseKind/count 无法证明是原激活，必须在发布/登记层拒绝该配置，或增加可靠上游激活证据后再启用。本模块不会给重复 context 自动生成新审批来绕过边界。activation/attempt 字段是可信关联记录，不是凭空获得的上游证明。

## 验证与未验

`tests/test_approvals.py` 先失败再实现，16 项通过：明确拒绝字段、并发同决定一次 POST、并发相反决定仅一份、决定/主体/decisionId 冲突、跨关卡 decisionId 冲突、授权失效/过期/观察期间撤销无副作用、用途拒绝、版本/attempt 重绑拒绝、暂停身份/类型/数量错误、缺失数据库、出站未知跨重开不重发、决定后崩溃与新 context 拒绝、actor 注入接口拒绝、reconcile 只读。

这些使用模拟 Sim 与真实 SQLite/SimJournal，不能算真实人工审批 UI 或真实拒绝分支验收。尚未交付 HTTP 授权接线、审批台列表、真实 Sim approve/reject 两分支运行、部署 revision 核验、循环重复上下文支持或 Codex 权限请求映射。未修改 service/runtime/dispatcher，也未干扰正在进行的 QwenPaw 重启探针。

## 人工审批 HTTP 接线

部署通过独立 `ApprovalAccess({token: TrustedPrincipal(...)})` 注册人工身份。
`TrustedPrincipal` 的 subject、project_scopes、capabilities 和 expires_at 均来自
服务端私有配置。普通服务凭证不能兼作人工审批凭证；`create_app` 拒绝重叠，
后续 `replace_credentials` 也检查已注册的服务令牌。每次认证比较所有令牌并使用
constant-time 比较。`revoke(token)` 可撤销身份，Coordinator 使用同一 registry
的 `authorize` 在观察暂停后、记录决定及投递前重新检查权限。

装配方式：

```python
access = ApprovalAccess(credentials)
coordinator = ApprovalCoordinator(ledger, sim_client, sim_journal,
                                  authorize=access.authorize)
approval_api = ApprovalAPI(access, coordinator)
app = create_app(task_store, service_config, approvals=approval_api)
```

审批 ledger 必须已存在；本接口不初始化数据库，也不开放绑定创建路由。
服务配置和生命周期由 runtime 装配器管理。没有配置此可选组件时不注册审批路由。
人工凭证不得通过普通服务令牌自动换取，前端应仅在内存中保留人工令牌。

- `GET /extension/v1/approvals?limit=50&after=<approvalId>`：按 ID 稳定分页，
  limit 为 1..500，只返回 principal 的项目范围；返回 `approvals` 和 `nextCursor`。
  列表读取本地账本，不查询每条上游执行；不支持客户端创建或改写绑定。
- `GET /extension/v1/approvals/{approvalId}`：实时查询绑定执行与暂停身份，
  返回 runStatus、boundPauseObserved、deliveryStatus，不执行 resume。
- `POST /extension/v1/approvals/{approvalId}/decision`：只接收
  `{"decisionId":"固定决定标识","decision":"approve或reject"}`。
  actor、principal、项目、workflow、context、revision 等额外字段一律拒绝。

各条记录公开 approvalId、projectId、workflowId、runId、blockId、contextId、revision、
activationId、attemptId、purpose、expiresAt、status、decision、decidedAt。
status 为 waiting、expired 或 decision_recorded。decision 记录受信 decidedBy 和明确
approve/reject；reject 不等于投递失败。deliveryStatus 为 not_submitted、intent、
accepted、rejected 或 uncertain；其中 rejected 表示上游拒绝恢复请求。
不返回 journal 原始请求、上游原始响应、私有路径或凭证。

未认证返回 401；无 approve 能力、凭证过期或请求过期返回 403；不存在及跨项目
访问统一返回 404；不可变决定冲突返回 409；输入不合法返回 422；不可确认上游
状态返回脱敏 503。unknown/intent/uncertain 必须继续查询原审批；原 SimJournal
保留写入意图，同一决定再次请求也不会重发恢复调用。

验证范围：TestClient 实际 HTTP 请求结合临时 SQLite、模拟 Sim，覆盖认证隔离、
分页过滤、主体伪造拒绝、过期、运行时撤权、approve/reject 决策、幂等与未知投递。
该层测试不代表真实浏览器产品验收或实际 Sim 人工审批链路已经接通。
