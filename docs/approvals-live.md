# 人工审批真实集成验收

2026-10-21，`probes/approvals/live.py` 已在隔离 Sim workspace 创建并发布专用流程：
`51ef43d9-7e40-430c-9572-59dcebb65426`。

图为 Start → Human v2 → Condition → 两个 Response；条件精确使用
`<approval.resumeInput.approval.approved> === true`。通过分支返回
`{"decision":"approved"}`，否则返回 `{"decision":"rejected"}`。
本图无模型调用、无通知，不复用或修改旧的外部任务等待图。

真实执行路径是 TestClient 发 HTTP 请求 → ApprovalAPI 独立人工凭证认证 →
ApprovalCoordinator 核验真实暂停 → 持久决策账本 → SimJournal 写入意图 →
正式 Sim v2 resume → 原 run 实际完成。没有绕过协调器直接提交人工结果。
工作流发布响应和 revision 保存在 state.json，每条绑定使用该发布 revision，
run/context/block 均取自实时暂停响应；purpose 固定 human_approval。

首次执行两条 run 分别 approve、reject，两个真实输出与预期一致。每条决定立即
用相同 decisionId 再次发 HTTP，请求成功但没有第二次 Sim resume。跨项目令牌
对详情和决定请求均返回 404。整轮实际 resume 共 2 次。
随后重启探针进程，重开所有 SQLite，沿固定原 run 只查询：两个 run 仍完成，
本次 resume 为 0。脚本在首次投递前保存 decisionRequested，未知结果重跑不会
创建替代 run 或再次提交决定；未完成时保留原身份等待核对。

证据（项目目录下，均在私有 var 中）：

- `var/approvals-live/state.json`：一次性创建/保存/发布记录、真实暂停绑定及执行观察。
- `var/approvals-live/report.json`：两分支响应、API 状态、首轮与重跑的恢复调用计数。
- `var/approvals-live/approvals.sqlite`、`journal.sqlite`：不可变决定及恢复意图。
- `var/approvals-live/credentials.json`：仅此探针人工及服务凭证，0600，不输出内容。

运行：`.venv/bin/python probes/approvals/live.py`。探针使用
`var/sim/probe-auth.json` 已配置的隔离 workspace/API key。目录 0700，JSON/SQLite
0600；不修改主服务配置，也不启动新的公开服务。

边界：此证据覆盖真实 Sim 和 extension HTTP 路由（进程内 TestClient），不覆盖
已部署主服务网络路由或浏览器 UI。现有两条 run 已完成；UI 验收应在同一发布图
建立新的固定 run 和可信人工绑定，而不能将已完成上下文当新审批复用。

## 已部署 UI 验收的可信测试准备

经主线授权，`probes/approvals/prepare_ui.py` 在同一发布 revision `1` 下创建两条
新的真实暂停，并通过内部 ApprovalLedger.bind 注册到
`var/adapter/approvals.sqlite`，项目为 local-validation。脚本不发任何审批决定。

- 待通过：`ui-approve-45bd181031a7`，run `c1269b8d-8626-477c-9f9b-f6ef38c41867`。
- 待拒绝：`ui-reject-c83499351ddc`，run `578ecc36-86e7-4f1e-bcb4-d72ba5e08eef`。

`var/approvals-live/ui-state.json` 保留一次性 execute 意图、实时暂停响应和绑定，
明确标注 `trusted_test_setup_no_decision`。UI 代理独占后续批准/拒绝操作；本准备
步骤不构成 UI 验收成功证据。脚本重跑只观察原 run，不新建替代 run。
