# 原生混合循环与人工审批 HTTP 探针

2026-10-22 北京时间，`probes/sim/mixed_approval_probe.py` 完成两条真实 Sim + 本地 TestClient HTTP 路径。机器结果明确是 synthetic，只证明循环、人工确认和审批传输，不代表模型完成业务任务。

独立目录为 `var/sim-mixed-approval/`，所有证据文件已核实 0600。没有创建 QwenPaw 角色，没有调用模型或记忆，没有更改主服务/准入。只配置一个无执行器的占位角色绑定供编辑发布验证；TaskStore 最终任务数为 0，controlled_execution 保持 unknown，TaskFlowRuntime 不启动 worker。

## 已验证链路

真实 AuthoringAPI 创建、保存、发布，正式版本回读 decode/recompile 相等；PublishedWorkflowCatalog 自动提供 binding；WorkflowGateway HTTP 启动并冻结 machine + human 策略。ApprovalDiscovery 与 task_flow 共用 WorkflowRunLedger，ApprovalCoordinator 与 gateway 共用 SimJournal 和 transport。create_app 从实际装配关系启用 approval capability，没有手工改标志或 monkeypatch。

图为 Start → 单个原生修复 loop（Agent + 确定性 guard，最多 3 次）→ human → condition。条件 source 指向 human，field 为 `result.approval.approved`、eq true；两条 End 分别输出固定 `syntheticProbeDecision=approved/rejected`。

- workflowId：`59ebf025-9cc1-48a9-9c3d-5676f9547a68`
- 发布 revision：1
- deploymentVersionId：`1162762a-c516-4c91-8d28-7e1046eeee98`

| 用例 | 原父 runId | synthetic 机器过程 | 真实审批/原生结果 |
| --- | --- | --- | --- |
| 批准 | 1c78371e-2562-517e-85a6-3de02d20e07e | 两轮：验证 failed → passed，iteration 0/1 | 独立人工 token POST approve，completed，approved End |
| 拒绝 | 529d9803-1b95-5abc-93b7-b8972d7349da | 一轮：passed，iteration 0 | 独立人工 token POST reject，completed，rejected End |

每次机器暂停由 SimWaitClient 显式 context `_loopN` 读取实际 `{input:{},iteration:N}`，根 run 正式发布图证明通过，且调用 ApprovalDiscovery 不产生人工审批。机器结果经已持久意图的 SimJournal 手工传入，标记 `synthetic:true`，不会调用自动任务执行桥。

进入真正 human 暂停后，ApprovalDiscovery 自动绑定，无手工 ledger.bind。审批 HTTP 使用仅含 decisionId/decision 的请求，Coordinator 形成实际 `resumeInput.approval.approved`。最终 GET 真实 resume 子运行，核对条件 `selectedPath.blockId` 和唯一已走 End 的 `.data` 与最终输出一致。

## 权限、审计与重放

每例在决定前测试服务 token 得到 401、已过期的独立审批 token 得到 403，两个拒绝都没有原生写。有效审批的 decidedAt 小于绑定 expiresAt，主体与 decisionId 来自真实审批账本，见 `lineage-and-expiry.json`。这里验证了凭据过期和绑定期限内决策，没有伪称真实等待七天后测试了绑定超时。

同 decisionId/decision 再次 HTTP POST，只回放原决定；审批账本记录完全相同，新增原生 POST 为 0。全部外部写先持久操作 payload/intent；已有未知 intent 不自动重发或换新 ID。

首次完整运行产生 10 次原生写：create/save/deploy 各一次、两次 execute、三次 synthetic machine resume、两次真实人工 resume。权限拒绝和决定重放均不新增原生写。默认重复运行只读取原父/子身份，实际 7 次 GET、0 次写，priorResult=passed_live。

## 父子版本证明的准确边界

两个根 run 的 `/api/v2/logs/{rootRun}?includeWorkflowState=true` 均通过 PublicationVerifier，实际图和确切发布版本相等。每个 resume 子 ID 来自正式 accepted 回执，`/api/v2/workflows/{wid}/runs/{child}` 可读取其身份、终态与输出。

补充只读检查发现当前实例对这些 resume 子 ID 的 `/api/v2/logs/{child}?includeWorkflowState=true` 返回 404；因此**不能声称对子 run 独立完成了 logs 版本图证明**。证据保留为 unavailable，没有用当前 active deployment 冒充。原父精确发布证明与正式恢复回执链成立；子 logs 的缺口需单独处理。

## 复查

```bash
.venv/bin/python probes/sim/mixed_approval_probe.py
.venv/bin/python -m unittest tests.test_mixed_publication tests.test_human_conditions -q
.venv/bin/python -m flake8 --select E9,F63,F7,F82 probes/sim/mixed_approval_probe.py
```

执行前定向 11 项测试通过、静态检查通过；真实会话 35713 正常退出。证据包括 state.json、report.json、observation.json、lineage-and-expiry.json，以及独立 tasks/runs/approvals/journal/pauses SQLite。没有后台进程或监听端口遗留。`--continue-known` 只用于人工核实后的有界续接，不能绕过未知提交；常规复查不要带该参数。
