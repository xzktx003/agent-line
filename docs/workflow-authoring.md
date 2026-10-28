# 中文流程搭建接入

QwenPaw 仍是主程序；此功能在现有适配服务中增加配置接口，复用 Sim 官方 v2
接口保存草稿、发布版本。不修改上游，不增加运行服务，也不处理 QwenPaw 的原生记忆。

## 当前范围

文档编译器支持开始、Agent、确定性条件、人工确认、结束节点。条件支持有限比较操作、
明确的默认分支；必需字段缺失和类型错误会中止运行。中文名称、位置和连线随
Sim 草稿持久化。Agent 编译为原生机器等待，运行接线和真实证据见
[自动 Agent 探针](automatic-task-flow.md)；有限循环尚未支持，不能据此声称完整 PRD 已交付。人工节点可以保存；发布前尚缺独立审批绑定时返回
`approval_binding_required`，不把一个布尔表单当作已完成审批。

Agent 发布现根据实际执行、结果回传、角色版本、验收器及所需记忆提交能力拦截，
草稿保存保持可用。具体 outcome、真实 Sim 拒绝证据与当前限制见
[发布能力检查](publication-capabilities.md)。

接口只读取本项目经扩展创建且已收到确认回执的流程。即使两个项目使用同一个
Sim 工作区，也不会获得彼此流程的编辑权。既有原生流程不会被自动收编。

## 部署配置

在已有 `sim` 配置的服务配置中增加 `authoringFile`，指向同目录的私有配置文件。
配置文件必须由当前用户持有、权限为 0600；不自动创建或迁移数据库。示意：

```json
{
  "schemaVersion": 1,
  "workspaces": {"YOUR_PROJECT_ID": "YOUR_NATIVE_SIM_WORKSPACE_ID"},
  "credentials": [{
    "token": "REPLACE_WITH_A_PRIVATE_RANDOM_TOKEN_AT_LEAST_32_CHARS",
    "subject": "owner",
    "projectScopes": ["YOUR_PROJECT_ID"],
    "capabilities": ["edit_workflows", "publish_workflows"],
    "expiresAt": 2000000000
  }]
}
```

替换示例标识并设置实际到期时间。编辑凭证与服务凭证、Sim API key 分开；发布权限
可省略，省略后只能编辑。复用原有 Sim 客户端与提交日志，所有修改服从现有维护门禁。

## 接口和结果

接口前缀 `/extension/v1/authoring`；所有请求通过 Bearer 编辑凭证认证。

| 方法与相对路径 | 功能 |
| --- | --- |
| `GET /projects` | 授权项目、支持的节点类型和发布权限 |
| `GET /projects/{p}/workflows?after=...&limit=30` | 本项目流程分页，沿用返回的 nextCursor |
| `POST /projects/{p}/workflows` | 以 operationId、name、description 创建流程 |
| `GET /projects/{p}/workflows/{w}` | 文档、草稿指纹和是否可编辑；不回传未识别的原生图 |
| `POST /projects/{p}/workflows/{w}/save` | operationId、expectedStateSha256、document 保存草稿 |
| `POST /projects/{p}/workflows/{w}/publish` | operationId、expectedStateSha256 发布并核对确切版本图 |
| `GET /projects/{p}/operations/{op}?kind=create` | 查询原 create/save/publish 操作，不再次提交 |

写请求返回 202 仅表示操作回执。必须同时核对 `status` 与 `receipt.outcome`：
`created`、`saved`、`published` 分别表示对应阶段已确认；`conflict` 表示草稿指纹
变化；`readback_mismatch` / `publication_mismatch` 表示实际图与提交图不符。
`intent` 或 `uncertain` 保留原操作身份，仅查询，不能自动换编号重试。

同一操作编号和载荷重放返回原回执，不再调用上游；载荷变化返回 409。日志在任何
上游修改之前提交，回执未知时不自动重发。操作日志保存身份、请求和证据，草稿及
发布图仍以 Sim 为准。

## 并发与证据边界

本适配层的写入使用提交日志旁的单个 `.authoring.lock` 串行，避免其自身并发保存
相互覆盖；网络调用期间不持有 SQLite 写事务。Sim 原生编辑器不参与此锁，原生接口
没有已验证的原子 CAS。因此同时保留保存前指纹检查、保存后读回，以及确切发布
版本的图核对；不能保证其他编辑者并发改图完全不发生。

发布成功也不等于后续 execute 请求能指定固定版本；该上游限制仍见
[发布版本核对](sim-publication.md)。完整运行绑定、浏览器与角色节点验收分别追踪，
不能用当前接口测试替代。

原生接口实测见 [公开 API 探针](sim-authoring-probe.md)；扩展 HTTP 与重开后的
幂等回放证据见 [HTTP 探针](sim-authoring-http.md)。浏览器验收结果需另行记录。


## 条件读取本轮 Agent 结果与动态结束

旧条件规则保持原样，例如 `{id:"yes",operator:"eq",field:"input.flag",value:true}`。
它继续读取开始节点的 input；不能在没有 source 的规则中使用 result 字段。

新增规则仅多一个 source 字段，值为文档中的 Agent 节点 ID：

```json
{"id":"passed","operator":"eq","source":"implement","field":"result.verificationStatus","value":"passed"}
```

允许字段为 `result.verificationStatus` 或 `result.output.xxx`，后者可以继续使用点分
字面字段路径，例如 `result.output.review.score`。不接受数组表达式、原型字段、函数
调用或任意代码；operator 仍仅 eq/ne/gt/gte/lt/lte。类型必须与规则 value 严格相同，
缺失字段、非有限数字或类型不符均抛错，不能静默进入默认分支。读取对象来自原生
`<Agent名称.resumeInput>`，是本轮可信完成回传的结果，不读取历史记忆或 draft 配置。

结束节点两种 config **互斥**：

```json
{"output":{"message":"固定结果"}}
```

或：

```json
{"resultFrom":"implement"}
```

后者编译为原生 Response JSON `{"agentLineResult": <Agent名称.resumeInput>}`，对象引用
不加引号，输出包含完整本轮恢复对象而非 JSON 字符串。旧 literal output 语义不变，
不能把手写模板混入 literal 模式。

source/resultFrom 必须是该条件或结束节点的前序 Agent，且位于从开始到消费者的
**每一条路径**上；仅某个分支执行过的 Agent 不能被合流节点引用。未来节点、非 Agent、
不存在的 ID 和不确定来源均拒绝。校验依据图结构，与 nodes 数组排列顺序无关。

反解只接受精确受控模板和编码规则，重编译后逐字段比对原生图；未知字段、被改写的
表达式或加引号的动态 End 不会被静默当作有效文档。本层仍不执行条件或推进节点，
所有实际分支和后继选择由 Sim 执行。本切片验证为编译/反解与 Node 执行受控表达式
测试，没有新增真实流程、模型调用或部署操作；不能用这些测试替代真实 UI 验收。

## 有界原生循环文档契约（首个受限形状）

新增 `type: "loop"`，配置严格为 `{"body":"实现节点ID","maxIterations":3}`，maxIterations 必须是整数 1..3（不接受布尔值）。首版一张图最多一个循环，循环体只能是唯一 Agent，且其 `allowFailureBranch` 必须为 true。不支持嵌套、并行、任意回边或自定义 JavaScript。

文档边分别为 `loop → body`、`branch:"body"` 和 `loop → after`、`branch:"done"`。body 没有 outgoing，原生 Sim 负责经生成的验证节点隐式回到循环判断；其他文档节点不允许直接进入 body。循环体 inputFrom 必须来自循环外必经的 Start.input 或前序 Agent.output；不能引用自身、未来节点或绕过路径的 Agent。

编译为一个原生 doWhile 容器，data 含 loopType/count/doWhileCondition/width=500/height=300；body 的 data 含 parentId/extent=parent，边使用 loop-start-source/loop-end-source。body 的 taskInput 为原生 JSON 对象模板 `{"input": <来源引用>, "iteration": <loop.index>}`，AL_AGENT_V1 的七字段角色配置保持原样，不增加凭据或执行宿主信息。

编译器在 Agent 后插入一个确定性的原生 Function 验证节点，仍在同一个 loop 内。它不是用户可配置节点：ID 为原生 loop ID 加 `--guard`，名称由 loop 文档 ID 哈希生成，代码固定，不接受用户 JS。Function 严格验证本轮 resumeInput 是对象；executionStatus=failed 允许修复，succeeded 则 verificationStatus 必须是 passed 或 failed，其他状态或类型抛错。验证后输出 `{result:原结果,needsRepair:布尔值}`。原生 Function 将返回值包装在 result 字段，loop 的条件只读取 guard.result.needsRepair 并与下一轮 index 小于上限合取。

固定 Sim 的 loop.ts:784–813 将条件 VM 异常转换为 false，因此验证必须由原生 Function 承担，不能移回 loop 条件假装异常终止。原生成员精确为 Agent + 生成 guard；反解核对 guard 的代码、类型、位置、父容器、边和派生 loops 全部字段。Node 单元测试已验证严格结果/上限，guard 的实际 Sim 错误收尾已在独立无模型修复版探针确认，详见 `finite-loop-guard-probe.md`。失败可继续的真实性仍由可信 CompletionRelay 证明，编译器不会判断执行器失败真伪。count 本身不是可靠自定义 doWhile 上限，受控条件不可删除。

循环体至少执行一次。仅在 loop 完成后，且该 loop 所在路径必经时，后继条件/End/Agent 可以引用 body 本轮最终结果；绕过循环的合流不允许读取该结果。旧普通 DAG、条件和字面量 End 编译形状保持不变。

`loop_policies(document, namespace)` 先验证整个文档，返回 `{nativeBodyId: {loopId: nativeLoopId, maxIterations: N}}`。这供发布和运行策略冻结使用，不是调度器。原生反解要求精确匹配编译结果；若 native state 提供 loops，必须与固定上游由 block.data 派生的所有字段严格等价，未知 loop 字段、条件、成员或输入模板保持不可编辑。

本节只说明编译契约及单元验证。自动任务逐激活登记、发布证明、前轮结果隔离和完整产品循环验收由相应模块接入，不能仅凭编译成功宣称全部有限修复循环已经交付。
