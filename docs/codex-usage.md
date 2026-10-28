# Codex 已报告用量

本轮只完成源码接线：插件读取原执行已有记录，Dispatcher 构造可信任务证据，原任务详情显示中文用量。没有运行测试、浏览器、Node、模型或探针，没有重载主服务/插件，也没有修改历史任务。主服务此前记录的执行准入仍为未知；本增量不提升 AC-66 验收状态。

## 使用的数据

不使用 QwenPaw 按日期/角色汇总统计或 token 估算，也不从模型结论解析用量。现有插件首先核对专用角色会话到 Codex thread 的映射、唯一 session metadata、工作区路径和唯一受控 turn。新增解析只接收显式 `token_usage_record`，要求 thread_id/turn_id 匹配、session_id/root_turn_id 连续，并处于唯一开始与终止记录之间。分叉记录、缺失身份或不支持格式保持未知。

每条记录须有明确 response_id、单响应 usage 与 turn_token_usage。对唯一响应逐字段相加，必须与每一条 turn 累计完全相等；同一response_id的整条重复记录忽略，冲突重复、乱序或缺前缀拒绝。计数字段严格为六项：input_tokens、cached_input_tokens、cache_write_input_tokens、output_tokens、reasoning_output_tokens、total_tokens；字段缺失或新增均未知，不根据原生反序列化默认值补零。严格非负安全整数，缓存读取不超过输入、推理不超过输出，total须等于input+output。缓存写入单独显示原生值，不额外累加成总量。

thread/turn是强匹配身份；session_id/root_turn_id仅作为同批次一致性检查，不声称它们等于外层角色会话或本turn。原生root可能来自连续目标，不能凭名称强行等同。

旧式 `token_count` 没有明确 turn_id，可能反映会话累计及窗口估算，本接线完全不采用。只有该格式的安装继续显示未知，不填零、不按相邻事件时间推测归属。显式记录也是 best-effort provider usage，不能证明所有调用均完整上报。

## 公开投影与冻结

原回执 JSON 增加 `codexUsage` 与 `codexUsageFrozen`。第一次终态观察在原 BEGIN IMMEDIATE 事务内冻结快照（包含不可得时的null）；并发/后续观察不能改写统计。已确认终态的身份、输出和终态标志同时受保护，旧观察不能将其回退为accepted/unknown；资源退出/退休核对仍可更新。无需新表或自动迁移。旧回执终态不重新采集，旧任务不补写。

Dispatcher 只从可信网关顶层接收严格校验的冻结快照，自行构造 `codex_usage` 证据，不接受模型自报 evidence。已有终态重复核对保留原快照，不因新增统计而破坏终态幂等。验证成功、停止和资源释放仍依据原证据，不由 token 数决定。

节点 API 新增 `usage`：`coverage:partial`，完整 `tokens/modelCalls/cost/parentTokens` 均保持null。`codex` 可包含原thread/turn、`coverage:reported-turn-records`、已报告六项计数和已关联响应记录数；没有唯一合法证据时为null。这里只展示单次受控执行的可得快照，没有对跨节点、重试或父角色用量做估算；既有节点/run预算投影不改其未知用量，也不以本数值实施token/金额硬预算。

详情复用现有定义列表。旧后端缺usage不显示新行；非法响应提示无法核对；新后端无显式记录时显示未知。样本数表述为响应记录数，不称完整模型调用次数。订阅CLI没有可靠逐次价格，费用不计算为账单。

## 原运行的本地汇总（后续源码增量）

既有 `GET /extension/v1/workflows/{binding}/runs/{run}` 在核对授权绑定及原运行后追加 `usage`。TaskStore在单个只读SQLite事务中按projectId、workflowId、runId准确选取任务，不新增数据库表、不读取其他项目正文作为统计，不触发观察、模型或验收。节点、循环和重试只要登记在该相同run作用域，都可参与；不额外遍历不同runId的原生恢复子运行或子流程，不能当作全调用树总量。

返回字段为schemaVersion=1、bindingId、scope、state、localTasks、observedTasks、unmeasuredTasks，以及完整tokens/parentTokens/modelCalls/cost（均null）和codex。state=partial仅表示本地记录投影，不表示全量已知；localTasks=observedTasks+unmeasuredTasks。没有明确响应记录时codex=null，不伪造零token。存在明确记录时codex含source、coverage、六项计数的已报告合计和samplesObserved；样本数不是完整模型调用次数。

单任务统计还须有唯一匹配Codex turn的可信终态证据。非终态/来源损坏/重复或矛盾用量证据、跨任务复用同一thread+turn、非法JSON或超过安全整数的合计都会使整组state=unknown，数量与统计均null，不重复累计或取较大值掩盖冲突。未知统计不会覆盖Sim原运行状态或输出。

中文运行结果复用定义列表，显示当前范围和本地登记/可测/缺失数量。绑定列表新增授权workflowId，页面交叉核对绑定、项目、workflow与run；目录无法读取时仍以同源运行API的授权scope为依据，不凭名称推测。绑定元数据只保留当前身份的project/workflow标识，换凭证或退出清空，迟到请求不能覆盖新身份。旧后端缺字段不显示新行。

这一批未新增或执行测试、Node、浏览器、模型、探针或真实运行请求；仅源码和Python AST审查。既有目录敏感字段保护断言同步workflowId，未增加方法。主适配进程3034199未再次重载，所以本节不能当作已部署接口或运行验收；AC-66继续in_progress。计划见 [run-usage-plan.md](run-usage-plan.md)。

## 协议依据与限制（单任务采集）

通过 agent-reach 的 gh 只读读取官方固定提交 `9a8a85bc539325f2819dd0717f88b6ba6286495e`：

- [TokenUsageRecord/TokenUsage/TokenCountEvent](https://github.com/openai/codex/blob/9a8a85bc539325f2819dd0717f88b6ba6286495e/codex-rs/protocol/src/protocol.rs)：显式响应、turn与thread身份和计数字段。
- [RolloutItemWire](https://github.com/openai/codex/blob/9a8a85bc539325f2819dd0717f88b6ba6286495e/codex-rs/history/src/rollout_payload.rs)：snake_case序列化的token_usage_record。
- [SessionState.record_token_usage](https://github.com/openai/codex/blob/9a8a85bc539325f2819dd0717f88b6ba6286495e/codex-rs/core/src/state/session.rs)：同turn累加usage到turn_token_usage。

只查协议源码，不说明本机已输出该格式。固定安装的真实记录覆盖、父角色准备/总结用量、所有失败调用、端到端重启、并发和完整预算验收继续待完成。未运行的检查不得称通过。
