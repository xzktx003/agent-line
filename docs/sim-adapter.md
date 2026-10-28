# Sim v2 恢复回执身份适配

固定上游源码 `apps/sim/app/api/v2/workflows/[workflowId]/runs/[runId]/resume/route.ts`
第 94–124 行把 `result.executionId` 写入响应 `data.runId` 和 `X-Run-Id`。
它表示恢复后的子执行，不保证等于请求路径中的原 run。同步 200 返回 workflowId
和执行状态；异步/排队 202 返回子执行的 statusUrl。
`lib/api/contracts/v2/workflows.ts` 定义响应头名称为 `X-Run-Id`。

Agent Line 的 SimClient 已按此契约区分两种操作：

- execute：data.runId 必须是调用方已持久化的 run；若有 X-Run-Id 必须一致。
- resume：data.runId 必须是有效 ID，并与必需的 X-Run-Id 一致。将它作为
  resumedRunId 独立保存；receipt.workflowId/runId/contextId 继续使用原请求身份。
- 响应如带 workflowId、contextId，必须与原请求匹配；如带 statusUrl，其路径必须
  指向该 workflow 的响应 run。只核对 URL，不跟随 URL，不要求公网 hostname 与
  内部调用地址一致。
- 缺失、畸形或矛盾字段保持 SubmissionUncertain；不以回执的 completed 自述
  代替实际工作流观察，verifiedComplete 仍为 false。

SimJournal 仍以原 workflow/run/context 作为唯一持久恢复键。子执行 ID 不替换
原键，不改变后继调度，不允许自动第二次 resume。历史 uncertain 记录不迁移为
accepted，也不补发请求。

## 回归及真实验证

先新增回归复现了正确子执行回执被错误标 unknown，以及缺失 runId 被默认接受。
修复后覆盖同步/异步恢复、响应头矛盾、错误 workflow/context/statusUrl、畸形 ID、
execute 身份严格匹配、超时和不重试。

`probes/approvals/transport_receipt.py` 使用现有审批图
`51ef43d9-7e40-430c-9572-59dcebb65426` 的原发布版本，新建两条隔离执行；
数据目录 `var/approvals-transport-v2`，主服务和旧 uncertain 账本均不修改。

| 决定 | 原 run | resumedRunId | 投递状态 |
| --- | --- | --- | --- |
| approve | 689d3142-4b5d-4418-81f9-d659e8c4761a | 09d08fca-841f-43d2-8a29-5fcaa67b0171 | accepted |
| reject | 8d22348e-d87a-43c7-a532-fa42716c9283 | e8dafa16-6929-4c8a-be6f-ddcc68a66817 | accepted |

两条原 run 实际完成 approved/rejected 分支。每条决定在同进程重复调用 HTTP
只产生一次 resume；重开进程执行同一探针，新增 resume 为 0。完整证据为该目录
的 state.json、report.json、transport-report.json 和 journal.sqlite。
