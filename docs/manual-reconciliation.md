# 主动核对原执行

运行管理页查询一个正在提交、受理、结果未知、取消待确认，或执行结束但资源未确认退出的任务后，可点击“主动核对原执行”。普通“查询任务”仍读取本地记录。排队任务不提供这个按钮，核对不会代替启动任务；资源已确认退出的终态任务也不需要再查原生执行。

`POST /extension/v1/recovery/{task_id}/reconcile` 使用原运行 Bearer 凭证，按任务原项目授权。只核对时请求仍只有严格整数 `expectedVersion`；版本不符返回409，不调用执行后端。要留下人工裁决时，必须同时提交单行 `finding`、单行 `decision` 和布尔 `allowNewAttempt`。缺一项返回422，也不调用执行后端。任务库仍是版本 2 时，带裁决的请求在核对前返回409，不自动迁移，也不登记新尝试。版本 3 会在原核对之后写入一条 `recovery_records`，响应增加 `recoveryRecorded=true` 和 `recoveryRecord`；`allowNewAttempt=true` 只表示裁决允许以后另开尝试，不会创建任务。当前未配置核对后端时返回503，保留原任务。返回固定 `source=original_execution_reconciliation`、`taskId`、`submissionAttempted=false`、`outcome` 和当前 `task`；响应 `no-store`，不返回原始上游错误。`GET /extension/v1/recovery/{task_id}/records` 只读取版本 3 的裁决，版本 2 返回409。

该入口复用 `Dispatcher.reconcile`，不调用 `submit`、扫描队列或新建 Agent/Codex 执行，不推进 Sim 后继、执行独立验收或写入记忆。它不属于纯只读操作：原流程停止、已有取消请求、原超时策略及确认终态后的资源收尾会沿既有路径处理。维护保护仍阻止新准入，但允许原执行核对与收尾；兼容降级时沿原中间件拒绝新的变更请求。

结果含义：`observed` 只表示取得原执行的可信观察，执行、验收、记忆和资源状态仍各自判断；`unknown` 保留未知和租约；`rate_limited`、`unavailable`、`authentication` 分别表示原服务限流、不可用或认证问题；`not_queried` 表示原核对链没有发起本次原生查询，不能当作恢复成功。

后台巡检可能在请求版本检查后并发更新同一原任务。因此版本检查只防止入路由时已经过期的请求，不承诺整个网络查询期间的版本锁定；返回前重新读取当前持久任务，不持有 SQLite 事务跨网络。现有事务和执行身份守卫处理终态冲突与资源释放，不覆盖旧终态、不释放未知执行的写租约。失败、超时和未知回执均不会自动重发编码。

只核对的按钮仍只提交 `expectedVersion`，并拒绝带 `recoveryRecord` 的回执。保存人工裁决会同时提交发现、裁决和是否允许新尝试；回执必须带同一任务的 `recoveryRecord`，且 `submissionAttempted=false`。前端验证固定来源、原任务/项目/角色/工作流六项身份和不回退版本。请求期间禁用按钮，改任务标识、重新查询、切换选择、退出或替换详情会使旧响应失效。说明和失败反馈使用固定中文，凭证沿原页面内存请求，不进入URL或存储。

当前只有源码/Python AST及指定 gpt-6-luna 独立静态复核，没有运行测试、Node、HTTP、浏览器、模型、lint/typecheck/build或真实任务核对。实际恢复与界面仍待验收，不提升完整 PRD 或 AC 状态。
