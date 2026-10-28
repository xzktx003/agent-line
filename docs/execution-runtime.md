# 常驻执行 Gateway 配置工厂

角色版本增量：可信服务 bindings 可配置 roleRevision；配置后工厂核对实际插件 descriptor，不会自动采用远端最新值。新执行必须携带原任务冻结摘要，未配置摘要的旧工厂仅支持观察。Dispatcher 预检诊断不占工作区，已接受任务仍观察原身份；一次性预检证明和插件执行库schema4边界见 [角色配置版本](role-revisions.md)。下方早期探针保持原有验证范围，不作为新版本协议已在正式实例启用的证明。

`agent_line/execution_runtime.py` 从当前用户拥有的 0600 私有部署文件构造持久父角色 Gateway。它仅验证配置并通过官方 HTTP 登录，不启动任务、不执行探针、不修改受控执行能力或公共准入。

## 接口与关闭责任

```python
gateways = load_execution_gateways(path, projects=service_projects, bindings=service_bindings)
# 返回 {(projectId, roleBindingId): BoundRoleGateway}
```

返回 wrapper 暴露 `role`、`role_agent_id`、`workspace`、`project_id`、`role_binding_id`、`installation_id` 及 `submit/observe/cancel/close`。role/workspace 为固定部署字符串，供 Dispatcher 与持久任务逐项核对；调用者不得根据模型输出替换。

每个 wrapper 独占一个 httpx.Client；RoleDispatchGateway 接受该客户端注入，因此不会自行关闭它。运行时须先停止并等待执行 worker 当前请求结束，再逐个调用 wrapper.close。初始化任何一步失败，会关闭本轮所有已构造客户端，包括刚登录失败的实例。close 可以重复调用；工厂没有后台线程或存活数据库连接。

## 严格配置格式

```json
{
  "schemaVersion": 1,
  "gateways": [
    {
      "projectId": "configured-project",
      "roleBindingId": "configured-role-binding",
      "roleAgentId": "configured-parent-role",
      "workspace": "/absolute/canonical/project-workspace",
      "installationId": "stable-qwen-installation-id",
      "ledgerPath": "/private/existing-role-dispatch.sqlite",
      "baseURL": "http://127.0.0.1:54901",
      "hostCredentialsFile": "/private/host-login.json",
      "serviceTokenFile": "/private/execution-service-token",
      "memoryQuery": "项目规则与相关经验",
      "requestTimeoutSeconds": 30,
      "parentTimeoutSeconds": 180
    }
  ]
}
```

示例端口仅为格式说明，不代表当前实例地址。上游重启后地址变化须由可信部署更新配置；稳定 installationId 与原 ledger 不能随端口变更重新生成，否则失去原任务关联。

除两个 timeout 字段外，示例中的条目字段均必填。顶层仅允许 schemaVersion/gateways；条目拒绝额外字段，包括 controlledExecution、confirmed 或任何能力开关。schemaVersion 严格为整数 1，不能用布尔值。空 gateways 列表可用于不启用执行适配器。

requestTimeoutSeconds 默认为 30，范围 1..120；parentTimeoutSeconds 默认为 180，范围 1..3600，均拒绝字符串和布尔值。memoryQuery 为服务端配置的非空字符串，最多 4000 字符。服务令牌需至少 32 个非空白字符，与插件服务认证要求一致。

## 启动验证顺序

所有条目在第一次 HTTP 请求之前完成以下检查：

1. 部署文件、hostCredentialsFile、serviceTokenFile、ledgerPath 必须为当前用户拥有的 0600 普通文件。路径必须绝对、canonical，拒绝符号链接或父目录别名。
2. workspace 必须是已有目录，与 service projects 的登记目录严格一致；roleBindingId 必须已存在，绑定 projectId 和 roleAgentId 都一致。
3. ledger 必须已显式初始化并具有支持的 schema。缺失时拒绝启动，工厂不会创建空库。
4. baseURL 仅接受 HTTP/HTTPS origin，不含用户密码、路径、查询、fragment 或空白；禁止将凭据附在 URL 上。重定向禁用，HTTP 客户端不读取代理环境变量。
5. 相同 origin 的固定 bridge 不能被配置给不同项目/工作区/安装身份；相同 installationId 也不能通过不同 URL 别名跨项目复用。此步骤限制本地部署映射；随后另行查询远端绑定描述。
6. hostCredentialsFile 严格只有 username/password 两个非空字符串字段，JSON 重复键、未知字段、重复项目/绑定元组和非法超时均拒绝。

验证后，每条绑定调用一次 `POST {baseURL}/api/auth/login`，在进程内存取 token/access_token 构造 RoleDispatchGateway。只有 HTTP 200 且非空 token 才成功。不会打印秘密、请求/响应正文或 HTTP 异常详情。凭据失效、超时、重定向或畸形回执使启动明确失败；无登录自动重试、无隐式凭据刷新。

登录成功后，使用宿主 Bearer 和独立服务令牌查询 `GET /api/agent-line/execution-binding`。远端必须返回整数 schemaVersion=1、ownedContainer=true、与登记完全相同的 workspace、包含指定父角色的 managedRoleIds，以及非空 targetAgentId；缺失、不匹配、超时或错误响应均关闭客户端并拒绝装载，不提交任务。这是当前部署绑定检查，不代表完整执行能力或未来版本兼容已获确认。

运行中凭据失效不能成为重投任务的理由。原 RoleDispatchLedger 的不可重发 intent 继续有效；恢复须由运行时重新装载有效凭据并观察原身份，不删除 ledger 来重建任务。

## 与常驻 worker 和能力门禁的关系

工厂返回配置好的 gateway 不代表执行能力已确认。上层仍须保留 controlledExecution=unknown，并只按维护策略核对已有任务。是否允许新提交由独立真实兼容/权限/隔离验收及运行时准入逻辑决定，不能从配置存在、登录成功或 gateway 创建成功推断。

工厂不读写主 TaskStore、不启动 Dispatcher、不更改 maintenance guard、不创建角色或工作区、不修改上游配置。本切片没有修改实际运行配置。固定远端 bridge 的实际目标、执行权限和 memory 路径隔离仍需要独立证据，不能由本地字符串匹配替代。

## 验证

`tests/test_execution_runtime.py` 先失败再实现，15 项测试覆盖：官方登录、远端绑定核对与固定属性、关闭所有权、未知能力字段拒绝、私有文件权限、缺失 ledger 不初始化、注册表冲突、同 bridge 跨项目/别名拒绝、非 canonical 路径与不合法 origin、全条目先验证、401/超时不重试与失败清理、严格 timeout、短令牌拒绝、空配置无网络。

本切片测试使用 MockTransport，未登录真实服务、改运行配置或触发真实任务。RoleDispatchGateway 的真实执行证据见 `docs/role-dispatch.md`；运行时工厂与常驻 worker 的实际装载/退出仍需独立部署验收。

## 主线常驻接入实测

`runtime.load_app` 可选 `executionFile` 指向独立0600执行配置；工厂返回的gateway按(projectId,roleBindingId)进入Dispatcher。显式binding注册的项目不回退其他角色策略，任务持久role/workspace必须与gateway一致。应用lifespan先启动/停止worker，再关闭其HTTP客户端。

能力unknown时worker使用`allow_submissions=False`，只核对已有投递，不领取registered队列。此模式与HTTP新任务准入门禁一致，不通过加配置字段宣称受控执行已确认。

主线已用真实QwenPaw实例完成登录、TestClient线程启动/退出、准入仍关闭的验证，并重启常驻adapter装载配置（PID2248222，3193；以当前进程状态为准）。var/adapter/execution.json绑定新项目container-validation和container-parent，固定父agent-line-g0-parent及var/executor/workspace，使用独立role-dispatch.sqlite。旧项目不改；主库maintenance仍保留。本次没有通过常驻服务派发新的模型任务，不等价于完整公开任务入口验收。

主线已通过正式认证热安装包含 binding descriptor 的插件，并核对安装文件与扩展源文件一致；真实工厂装载得到一个 gateway，未创建任务。新增回归拒绝把布尔 true 或浮点 1.0 当成整数协议版本。跨版本升级仍须执行独立兼容验收。
