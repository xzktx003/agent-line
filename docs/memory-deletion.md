# 受控记忆删除：接口、启用与边界

本实现通过独立适配层与 QwenPaw 官方插件扩展完成，没有修改上游源码。删除对象固定为某个受管角色的 `digest/agent-line-task-{taskId}.md`。调用者必须指定原审核内容的 SHA-256，不能提交任意路径、角色地址或正文。

**当前原生适配不能证明 ReMe 索引及缓存已全部清除，因此实际文件消失仍应保持 `deleting`，不能宣称已删除。**

2026-10-26：已确认删除的整段正文会在模型调用前和回复后的上下文里被去掉；片段与改写仍不识别。该过滤已随官方插件安装进入原 QwenPaw，并完成一次新的执行准入。这仍不证明下一次运行不会复活旧内容。AC-12 继续 in_progress。

2026-10-26：原数据实例已重启，当前源码的执行准入重新确认。这不证明下一次运行不会复活已删除正文；`deleted` 仍只表示该来源的文件和活索引已经消失。AC-12 继续 in_progress。 `deleted` 要求可信原生回执同时满足 `fileAbsent is True` 和 `indexSourceAbsent is True`，且角色、任务和正文摘要均匹配。单元测试中的双确认是模拟能力，不代表当前部署具备索引清除证明。新增记忆页删除入口仅完成源码接线，未进行本轮运行验收或主后端重载。

## 工作台接线

绑定目录新增 `memoryDeletion:{configured,canRequest}`。两者均为明确布尔值，canRequest仅在该角色配置删除且当前身份具备delete_memory时为真；不返回凭据、路径或宿主配置。旧后端缺字段不开放首次删除，后端仍自行验证权限，不能通过改页面绕过。

核对已提交来源后，独立删除区域显示目标与原摘要，并说明索引未确认时角色可能持续阻断。必须单独勾选确认，首次请求携带固定决定与原摘要；未知回执仅查询，不重发POST。既有删除记录允许普通审核身份查询，不能据此新发起删除。发起意向即清除本页旧正文和技能候选；退出清除决定与摘要，非敏感防重标识留在本页面内存，刷新后仍由后端持久意图防重复。

文件消失、索引消失、角色阻断分别显示。fileAbsent/indexSourceAbsent的false或缺失只表示尚未确认消失，不表示对象存在；只有状态及双重消失的身份均匹配才能称已删除。HTTP202、空搜索或文件删除本身都不足以完成。详情仍保留原审核与编码证据，不修改任务执行结果。

本轮未新增或运行测试、浏览器、Node、模型、探针或真实删除，仅源码/AST审查。原生索引治理、修正/失效版本闭环及完整AC-12继续待完成。

## 三层记录与状态

1. `MemoryReviewLedger` 保留原人工审核人、项目、任务、正文摘要和来源证据，不保存正文。
2. `MemoryCommitStore` schema 2 新增 `memory_deletions`，冻结角色、任务、删除人、决定 ID、预期正文摘要及有界回执。删除意向持久化时，**先清空该提交记录的正文**，再发起原生删除；同时提交状态转为 `deleting`。审核记录和任务执行证据继续保留。
3. 插件独立 SQLite 状态保存角色、任务及摘要的删除意向。原生删除前写入，角色 `roleBlocked` 跨插件/宿主重启保留；状态文件丢失或不可读不能自动初始化为未阻止。

初次请求只提供一次原生 POST 机会。同一决定重放、超时后的协调，以及进程重启后的处理均转为 GET，不重复原生删除。角色/任务、审核人、决定或摘要冲突返回 409。状态含义：

| 状态 | 含义 |
| --- | --- |
| `deleting` | 已有删除意向，文件或索引消失证明尚不完整；不能恢复正文或宣布完成。 |
| `needs_review` | 正文不符或原生要求复核；顶层状态、任务记忆状态与删除记录均应表达需复核。 |
| `deleted` | 身份匹配且文件、索引双重消失得到明确证明。 |

删除不会将编码任务改为成功、失败或取消；`TaskStore.memoryStatus` 独立同步。详情/列表不会展示已清空正文。角色范围的进程文件锁串行化受管写回与删除。该角色任何状态的删除 tombstone 都会阻止同摘要通过另一个任务重新审批写入；被阻止的新记录保存 `needs_review` 和空正文。原任务的旧审核重放也不能复活正文。

摘要按现有记忆适配规则，对去掉首尾空白后的 UTF-8 正文计算 SHA-256。此门禁识别相同摘要，不是语义查重：改写、另一角色副本、外部写入、历史会话和备份不在这一断言内。

## 显式启用

### 原生索引接口边界（源码复核）

固定QwenPaw的served `delete` action仅执行文件删除，ReMe索引更新由watcher另外处理。ReMe组件层 `BaseFileStore.delete(path)` 具有按source删除chunks的契约，但现有QwenPaw公开action未暴露这个组件，插件不私调内部step或替换原生引擎。公开memory reindex接受all/bm25/embedding范围，不接受具体source，且没有按source确认查询缓存全部失效的回执。graph_snapshot不覆盖digest根部目标，top-k搜索空结果同样不能证明全量清除。

只读依据：`upstream/qwenpaw/src/qwenpaw/agents/memory/reme_light_memory_manager.py` 的run_action、`app/routers/agents.py` 的memory/reindex；已装ReMe的 `components/file_store/base_file_store.py`、`local_file_store.py`、`steps/index/update_changes.py`、`steps/file_io/delete.py` 和 `config/default.yaml`。本轮只阅读文件，未调用组件或重建索引。

正常打开 `MemoryCommitStore(path)` 兼容 schema 1 和 2，不隐式迁移。新 `initialize(path)` 创建 schema 2，已存在路径拒绝覆盖。旧库需要维护窗口中显式升级；操作前保留一致性备份，确认没有受管记忆写回或删除在途。

```python
from agent_line.memory import MemoryCommitStore

# 使用部署的真实私有绝对路径；此示例不是自动执行步骤。
store = MemoryCommitStore.upgrade('/absolute/private/memory-commits.sqlite')
assert store.schema_version == 2
```

升级在 SQLite 事务内创建删除表并更新版本；重复升级 schema 2 不清理数据。不要用旧备份覆盖带 tombstone 的现库，也不要删除角色锁或插件阻止状态来解除保护。

在既有 `memoryFile` 私有配置中：

- 给有删除权限的独立人工身份配置 `capabilities: ["review_memory", "delete_memory"]`。仅 `review_memory` 的身份可以查看和协调已有删除，不能新发起删除。工厂内部补充已有身份体系所需的 `approve`，无需在配置中手填。
- 为对应 binding 增加 `deletionServiceTokenFile`，指向拥有者持有的 0600 私有文件，内容为 32–4096 字符、ASCII、无空白的独立随机 token。该 token 必须与人工凭证、普通服务凭证及宿主登录 token 分离。只有 schema 2 可以启用此配置。
- 通过官方插件配置启用 `memory_governance`：`managed_role_ids` 为受管角色列表，`service_token_file` 指向同一原生 token，`state_file` 指向插件持久 SQLite 状态的绝对规范路径。首次状态文件通过插件模块的 `initialize_state(path)` 显式创建，已有路径不覆盖；加载校验私有权限和 schema。
- 以当前插件版本通过官方安装/加载流程更新扩展，并核实实际角色 middleware 与执行桥已经加载治理对象。仅修改配置文件不等于运行进程已生效。

新配置先完成本地身份、文件、schema 校验，再登录宿主核对正式角色。没有配置删除协调器的 binding 返回 503。本文不授权操作任意现有记忆，也没有给出自动批量删除脚本。

配置了治理但其状态库丢失、权限错误或 schema 不受支持时，插件保留拒绝新推理与委派的守卫，治理路由返回 503，健康显示 degraded/memoryGovernance=unavailable。官方加载器不会因这类初始化异常跳过整个插件并移除守卫；不自动新建空库。已有执行状态接口仍可读取。真实 PluginLoader 子进程测试覆盖这三种故障。

## HTTP 操作

接口都从独立人工 Bearer 身份推导操作者及项目。请求体严格拒绝多余字段，浏览器不能指定 `actor`。

```text
POST /extension/v1/memories/{binding}/{task}/delete
{"decisionId":"一次决定的固定ID","expectedContentSha256":"原审核正文的64位小写SHA256"}

GET /extension/v1/memories/{binding}/{task}/deletion
```

先通过记忆详情核对原审核、角色、任务和摘要，再发起一次 POST。HTTP 202 只表示受理，应读 `status` 与 `deletion`；超时或结果未知时保留决定 ID，查询同一删除记录，不换 ID。GET 协调只调用上游 GET，不创建删除意向；没有意向返回 404。GET 协调可能持久化新的观察状态，因此不是数据库零写入的普通详情接口。

普通详情和列表仍是 `GET /extension/v1/memories/{binding}/{task}` 与 `GET /extension/v1/memories`。维护准入覆盖新删除和主动协调：维护拒绝 503，无新 tombstone 或原生请求。项目范围或任务归属错误隐藏为 404，缺删除权限 403，额外 actor 422，摘要或决定冲突 409。

服务到插件使用宿主认证及独立 `X-Agent-Line-Memory-Token`：

```text
POST /api/agent-line/memory/{role}/{task}/delete
GET  /api/agent-line/memory/{role}/{task}/deletion?expectedContentSha256=...
```

原生 POST 的 actor 来自人工身份，角色来自可信 binding；API 客户端不会跟随重定向。插件核对完整官方文件读取与 ReMe read 一致，并在删除前重新核对完整正文，再调用官方 `memory_manager.run_action('delete', path=...)`。没有调用自造文件删除或索引 SQL 清理。

文件在 POST 前已经不存在时，仍登记该受信删除意向和角色阻断，因为索引可能残留。纯 GET 不新增阻断。原生删除调用抛异常时保留意向及阻断，返回未知状态，不重发删除。

## 角色阻止与覆盖范围

`ManagedRoleToolGuard` 在 `on_reply`、`on_reasoning`、`on_model_call` 检查持久阻止；`on_reasoning` 位于原生模型调用记忆注入之前。受管 `memory_search` 和新委派同样受限，执行桥对受管角色的新执行准入也检查该状态。

这些检查拒绝后续进入受管路径的动作，不会撤销已经发送到模型提供方的请求，也不保证终止已经进入工具的调用、既有 Codex 进程或清除其已加载上下文。插件外的原生文件工具、其他扩展、直接写盘、其他角色及管理员操作不能由这一接口全面封锁。QwenPaw 原生删除没有针对并行外部写入的原子 compare-and-delete；两次读回只缩小竞态窗口，不能替代原子保证。

现有官方 search 的 top-k 未命中不等于全量删除；现有 graph snapshot 对 digest 根文件的覆盖也不足。文件 404、watcher 完成或 reindex 返回成功均不能替代索引来源和缓存全部失效证明。活的 ReMe 文件节点、chunk 和关键词 posting 都不再包含该来源时，插件可以返回 `indexSourceAbsent=True`；惰性删除槽位不算，投影读不到则保持 `deleting`。该任务的准入阻止只在这一证明之后解除，正文摘要继续保留；同一正文再次出现时，下一次官方读取会重新阻止角色。其他尚未证明的删除仍阻止整个角色。不要删状态文件来恢复角色。这仍不证明下一次运行不会从旧会话或缓存复活正文。

账本正文列清空是逻辑删除，不是磁盘安全擦除。SQLite WAL/空闲页、备份、快照、日志、旧会话、模型提供方已接收数据，以及派生的其他记忆可能仍保留副本；本实现不声称已擦除这些来源。

## 验证状态

`tests/test_memory_deletion_api.py` 的 13 项测试通过，使用真实临时 SQLite、真实 HTTP 适配类和模拟原生 HTTP；覆盖权限隔离、准入、严格双消失回执、未知结果不重删、内容清空、防旧审核恢复、需复核状态及执行证据不变。该测试不连接真实服务。

2026-10-21 真实探针 `probes/qwenpaw_memory/deletion_http.py` 已运行，文档已核对两份报告：

- `var/memory-review-http/deletion-report.json`：仅删除既有探针生成的记忆，任务 `task-00d41e38e770420b8881543dcfa9c903`、角色 `agent-line-g0-parent`。首次原生删除 POST 1 次、观察 GET 2 次、记忆 PUT 0 次；官方文件读取 404，提交账本正文清空，最终 `deleting`、`indexSourceAbsent=false`。官方插件热加载与适配服务重建后阻止/意向保留，旧审核未恢复正文，任务执行与证据不变。
- `var/memory-review-http/deletion-replay-report.json`：另一独立进程重复探针，原生删除 POST 0 次、观察 GET 3 次、记忆 PUT 0 次。主线额外核对已绑定执行提交因记忆索引删除门禁返回 409，既有执行 GET 仍为 200，执行回执数量没有增加；报告中的 `boundExecutionSubmissionRejected`、`existingExecutionReadable` 均为 true。

只有探针使用的记忆账本经过显式 schema 1→2 升级并保留备份。主适配服务记忆账本仍为 schema 1，删除未启用，原维护门禁保持。插件已通过官方 API 安装并启用 `agent-line-g0-parent` 的治理；该角色在索引未核验前拒绝新推理/委派，当前没有自动解除入口。

浏览器新增删除状态验证：收到删除记录后清除本页审核正文缓存，后续详情返回404也不恢复旧稿。13项交互检查通过，真实分支只读取删除中记录、正文为空；mock写入与真实读取明确分开，浏览器没有真实写入。报告和桌面/移动截图位于 `var/memory-review-http/browser-deletion/`，保留原 `browser/` 的历史审核证据。

主服务更新后经本机局域网地址验证：页面200、匿名接口401、原维护保护拒绝新审核503且审核条数不增。主库仍schema1，删除配置未开启；见 `var/memory-review-http/deletion-main-service-report.json`。这不是第二台设备的局域网验收。

2026-10-26 对同一探针任务只做官方 GET：文件缺失且活索引来源缺失，状态为 `deleted`/`index_source_absent`，角色仍受阻止。该回执不覆盖缓存、旧会话或下一次运行复活；没有新的模型会话召回验证。尚未实现删除 UI 按钮，未完成完整 PRD 的记忆失效与遗忘验收。最终回归数量及独立修复记录见 `PROGRESS.md`。
