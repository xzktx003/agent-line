# 人工审批后台发现

本模块只登记、观察和绑定人工暂停，不选择后继、不作决定、不调用 resume。
运行时应以独立后台循环调用 tick/run，不能依赖用户打开列表才发现审批。

装配接口：

```python
runs = WorkflowRunLedger(existing_private_path)  # missing DB fails
# One-time deployment only: WorkflowRunLedger.initialize(new_path)
runs.reserve(project_id=project, workflow_id=workflow, run_id=run,
             revision=published_revision, deployment_version_id=published_id,
             human_block_ids=explicit_human_blocks,
             ttl_seconds=3600)  # commit BEFORE sending execute
runs.record_submission(workflow, run, 'accepted')  # or uncertain/rejected
finder = ApprovalDiscovery(runs, approval_ledger, sim_client,
                           publication_verifier=verified_publication_reader, batch_size=50)
finder.run(stop_event, interval=1.0, report=callback)
```

`get(workflow_id, run_id)` 返回 projectId、workflowId、runId、revision、deploymentVersionId、humanBlockIds、
ttlSeconds、seq、submissionStatus、complete、registrationStatus、registrationReason。`page(after=0,limit=50)` 按 seq 返回
未完成运行，包含上述字段（不含 complete）。snapshot 不可变；重复 reserve 的
任一项目、版本、白名单或 TTL 不一致即冲突。构造不会静默初始化丢失数据。

发现时核对真实 GET 响应的 workflowId/runId、paused、pauseKind=human、严格整数
pausePointCount 与 resumedCount 非负且差为 1，以及 block 位于该运行冻结的人工白名单。未知、外部等待节点、
多暂停、身份错误均不绑定。approvalId 从原 workflow/run/context 确定性派生。
首次发现先将完整 ApprovalBinding（含到期时间）持久化到运行账本，再幂等写入
ApprovalLedger；两库之间崩溃后使用原绑定重试，不延长 TTL。

attempt 固定 one，activation 绑定原 run。该上游上下文不提供可验证的循环激活 ID；
因此同一上下文离开后重现或已有决定后重现只报告 ambiguous_activation，不新建
审批、不清空旧决定、不猜测下一次 attempt。轮询之间未观察到的上下文复用无法
据此证明新激活，这类循环图不应配置为受支持人工审批流程。

每轮有限分页，单条失败不阻塞后续运行。已观察终态且提交记录明确 accepted 或
rejected 的运行标 complete；intent/uncertain 仍保留，不因一次 GET 就抹除提交不明。
后台循环可由 stop_event 停止，不会发送取消/恢复，也不会更改 Sim 执行。

发布版本门禁强制依赖 PublicationVerifier。每次准备绑定暂停前，verify_run(snapshot)
必须明确返回 True；缺少验证器拒绝构造，缺失或矛盾证据均不绑定。验证器读取正式
logs/{runId}?includeWorkflowState=true 的实际 deploymentVersionId 和运行图，并与
冻结的数字 revision/发布 ID/人工节点白名单核对；当前 active 版本不能替代原运行版本。
execute 无版本 pin 参数，启动预检仍存在重发版竞争，但实际暂停版本未证实绝不注册。

每次 tick 结果持久化为有界 registrationStatus 和 registrationReason，版本缺失或
不匹配统一为 version_unverified / run_publication_not_verified，不保存远端原始错误。
其他状态包含 bound、waiting、unknown、not_allowed_human_pause、ambiguous_activation、
terminal_observed。运行时可将这两个字段显示给用户，避免静默等待。

本切片测试覆盖本地账本和注入验证器的契约，未执行真实工作流；PublicationVerifier
的真实上游查询验证和最终运行时装配由其他切片提供。

后续混合流程已补充机器与人工用途的独立白名单，复用同一个运行账本；机器用途
不再一律跳过，但只有显式 `approvalBlockIds` 的节点可登记人工确认。实现与限制见
[Agent 与人工确认共用流程](mixed-workflows.md)。本节的“未执行”仅指最初模块切片。

## 已接入的运行服务配置

审批私有配置可增加 `runLedgerPath`，指向已显式初始化的 0600
WorkflowRunLedger 数据库。正常启动丢库报错，不自动重建。省略该字段时保持原审批
API 行为，但受保护工作流的新启动返回 503。

服务的 `sim.bindings.<bindingId>` 增加以下可信配置：

```json
{
  "projectId": "your-project",
  "workflowId": "published-workflow-id",
  "humanApproval": {
    "revision": "1",
    "deploymentVersionId": "published-deployment-id",
    "blockIds": ["human-node-id"],
    "ttlSeconds": 86400
  }
}
```

运行服务在 lifespan 启动独立发现线程；退出时先停止并等待该线程，再关闭共享
Sim 连接。发现不依赖列表/详情 GET。新启动先核实 active 发布定义，再持久化
冻结配置，随后提交 Sim；重复幂等启动返回原记录，不按修改后的配置重新投递。
观察接口增加 approvalRegistration 状态。受保护流程的通用 resume 返回 403；
即使移除当前 binding 的 humanApproval，原运行账本仍保留审批保护。

目前拒绝人工节点位于循环/并行容器或可返回自身的图，原因是缺少可验证的逐次
激活身份。与人工节点无关的 Agent 循环不因此被禁用。该限制仍是完整 PRD 的缺口，
不能将当前无环人工审批验收推广成任意流程验收。

## 当前真实验证

`.venv/bin/python probes/approvals/automatic.py` 使用已存在的无模型审批图，经
load_app 的真实后台线程登记，不手工 bind、不调用 tick；批准和拒绝均完成对应
Sim 分支。关闭并重建应用后原运行/审批身份不变，普通执行凭证 resume 被拒绝。
探针采用持久幂等键，重复执行查询原运行和原决定。
证据：var/approvals-automatic/report.json。

主适配服务也已启用发现，配置绑定 human-approval-check；通过真实 HTTP 启动、
后台自动登记、独立审批凭证批准和最终分支检查通过。
证据：var/approvals-automatic/main-service-report.json。
真实验证没有执行模型任务，也没有改动上游源码。
