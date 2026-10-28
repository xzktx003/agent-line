# G0：Sim 上游能力核查

核查时间：2026-10-21。只读源码核查，不代表运行验收通过。
上游：`upstream/sim`，提交 `27b53561def7db4eaa9ad2f12c04ea1a0eaaf91c`。
已读取上游根与 `apps/sim/AGENTS.md`；未修改上游、未读取运行凭据、未触发外部业务。

## 结论

1. Sim 可以继续作为唯一流程调度器。当前版本存在正式 v2 创建、图状态、部署、执行、查询、取消和恢复 API。
2. 长任务可采用“API 节点登记外部任务 → Human v2 节点持久暂停 → 外部任务完成后调用 resume”的实现候选。适配层仅负责工作身份和恢复传输，不决定下一条分支。
3. **尚未通过 G0**：数据库重启后的暂停恢复、实际 API 权限、工作流输入/输出映射、重复回调和执行失败窗口必须真实验证。
4. 原版画布不是现成中文组件。未发现常用 i18next/next-intl 翻译入口，明确存在英文硬编码。不能把英文原版 iframe 标为中文工作台验收通过。
5. 跨端口 iframe 默认会被安全头拒绝；必须验证同源网关路径和登录 Cookie，不能直接把 3000 端口嵌入另一个端口宣称成功。

## 启动依赖

- 根 `package.json:3` 固定 `bun@1.4.2`；`apps/sim/package.json:6` 要求 Bun >=1.4.2、Node >=24 且 <25。
- 根 `package.json` 提供 `dev:full`，分别启动 Next 应用与 realtime 服务。
- `docker-compose.local.yml` 给出完整拓扑：Next 3000、realtime 3002、PostgreSQL/pgvector 17、Redis 7，以及运行 `bun run db:migrate` 的迁移服务。
- Redis 在缓存上可能有数据库回退，但该 compose 的注释明确 Pub/Sub 没有回退；实时状态验收不能省略 Redis。
- `apps/sim/.env.example` 列出认证、加密及内部通信配置；部署生成独立秘密，不复用示例弱值，不输出秘密。
- 同文件 `EGRESS_ALLOWED_HOSTS` 用于显式允许访问局域网适配服务；只允许具体服务主机，避免全网绕过 SSRF。
- `NEXT_PUBLIC_CHAT_DISABLED=true` 可以隐藏无关 Chat 模块，但不能自动汉化流程编辑器。

## 官方 API 边界

权威合同为 `apps/sim/lib/api/contracts/v2/workflows.ts`，路由位于 `apps/sim/app/api/v2/workflows/`。

| 操作 | 路径与方法 | 关键约束 |
|---|---|---|
| 创建 | POST `/api/v2/workflows` | `workspaceId,name,description?,folderPath?`；合同行 658、789 |
| 图读取/替换 | GET/PUT `/api/v2/workflows/{workflowId}/state` | 行 2995 起；支持 `dryRun=true` 校验，不能把适配层自定义图直接发过去 |
| 发布 | POST `/api/v2/workflows/{workflowId}/deploy` | 行 1014；可传 name/description；激活异步，需查询 deployment |
| 部署状态 | GET `/api/v2/workflows/{workflowId}/deployment` | 返回当前部署、草稿漂移和部署操作状态 |
| 执行 | POST `/api/v2/workflows/{workflowId}/execute` | 行 1441、1618；`input`，`async:true` 仅支持已部署流程，202 是回执 |
| 查询 | GET `/api/v2/workflows/{workflowId}/runs/{runId}` | 行 1980；包含当前 `paused` 详情与状态 |
| 恢复 | POST `/api/v2/workflows/{workflowId}/runs/{runId}/resume` | 行 1637、1682；`contextId,input?` |
| 取消 | POST `/api/v2/workflows/{workflowId}/runs/{runId}/cancel` | 行 2101；需同时核对适配层实际执行进程 |

认证来自 `lib/api/server/routes/v2-api-key-auth.ts`：`x-api-key` 或支持的 OAuth Bearer，不要假定普通账户密码就是 API key。

### 启动幂等的重要限制

合同行 78–91 明确 `X-Run-Id` 是一次性身份占用，**不是缓存结果式幂等响应**。已占用再提交返回 `409/RUN_ID_CONFLICT`。查询返回 404 也不能证明没有执行，因为身份声明可能已持久化而运行记录尚不可见。适配层必须保存预分配 runId，未知结果时查询核对，不得换 ID 重启。

## 长任务与人工审批

- `blocks/blocks/human_in_the_loop.ts:131` 的 `human_in_the_loop_v2` 是当前节点，旧版已 sunset。
- 节点暴露 `resumeEndpoint`、`resumeInput`、`submission`，允许 API 恢复，不要求一定人工点网页。
- `lib/workflows/executor/pause-persistence.ts:25` 起说明：只有日志完成且 snapshot 有效才持久化暂停，否则标记失败。
- `lib/workflows/executor/human-in-the-loop-manager.ts:550` 的 `persistPauseResult` 用数据库事务维护 `pausedExecutions`；resume 使用 `resumeQueue` 持久队列。
- 恢复 API 路由返回 200 的 completed/failed/paused/cancelled，或 202 的异步/排队回执；200 本身不等于业务成功。

建议模板：开始 → API 登记任务（立即返回 taskId）→ Human v2 等待 → Condition 读取恢复输入中的结果 → 成功/修复/人工分支。由适配层观察 Sim 暂停详情绑定 contextId，避免任务完成速度比暂停持久化快导致回调丢失。完成回执提前到达时先保存在适配层数据库，看到对应暂停点后再投递。每次节点激活使用独立身份；旧 attempt 结果不得推进新 attempt。

这仍需真实证明 API 节点返回数据能够绑定 pause context、重复 resume 不重复执行后续节点、Sim 重启不丢 pause、适配器重启不重复启动 Codex。源码存在不等于这些组合场景全部通过。

## 原版画布与中文界面

`proxy.ts:284,399,415,431` 给 workspace 等页面设置 `X-Frame-Options:SAMEORIGIN`；`lib/core/security/csp.ts:244` 为 `frame-ancestors 'self'`。`next.config.ts:413` 起允许任意 iframe 的例外是 Chat，不是编辑器。

推荐先验证同源网关：Sim 保留它的根路径（`/workspace`、`/_next`、`/api` 等），统一工作台和 QwenPaw 插件使用避冲突前缀；页面、资源、重定向、Cookie、WebSocket 必须共同正确代理。不能仅转发 HTML，不能共享两套应用冲突的 `/api`。

中文化明确存在缺口：Human 节点定义的 name、description、subBlocks title 等硬编码英文。外部 DOM 词典注入可保持上游源码不变，但依赖 DOM 和文本版本，动态弹窗、编辑器、辅助属性与运行错误都可能漏译；不得将这种方案描述为天然无障碍升级。必须有独立词典、版本检查和浏览器覆盖验收，避免改写用户代码或工单正文。

另一候选是独立中文流程编辑器通过正式图状态 API 存储并交给 Sim 执行。这保留唯一 Sim 调度器，但不等同于嵌入 Sim 原画布，需对照 PRD 的原画布接入要求由主线明确取舍，不能暗中替换范围。

## 必须继续完成的动态证据

- 真实账号/API key 创建、保存、发布最小条件图，两个输入走不同分支。
- Human v2 暂停后重启 Sim，再 resume，执行身份与产物保持一致。
- 外部任务先完成/晚完成、重复回调、恢复回执丢失场景。
- 两个浏览器会话的认证边界、同源画布资源/WebSocket 和 LAN 访问。
- 全中文主流程截图/可访问性文本检查，明确记录漏译而非只截图首页。
- 验证前后 `git status --short` 保持上游源码无修改。
