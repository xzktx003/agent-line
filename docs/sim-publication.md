# Sim 发布版本证明

`agent_line.publication.PublicationVerifier` 只读取正式 Sim v2 API，不发布、执行、激活或恢复工作流。它复用 `SimClient` 的连接与认证，禁止跟随重定向，不返回或记录原始图、响应正文及凭据。

## 接口

```python
verifier = PublicationVerifier(sim_client)
verifier.check_definition(
    workflow_id, revision, deployment_version_id, human_block_ids,
    require_active=True,
)
verifier.verify_run(snapshot)
```

成功严格返回 `True`。无法确认时抛出 `PublicationUnverified`，`code` 仅包含固定诊断：`invalid_binding`、`unavailable`、`invalid_response`、`version_mismatch`、`inactive_version`、`missing_graph`、`human_node_mismatch`、`run_version_mismatch`、`unsupported_topology`、`repeated_human_activation`。调用者必须保持未验证，不创建审批绑定，不能将读取失败当作证明。

`revision` 必须是无前导零的正十进制字符串，最多 15 位；`deployment_version_id` 为不可变发布 ID。非空 Human 节点白名单不得重复。两个图中白名单节点均须存在、字典键与节点 `id` 相同、`type` 严格为 `human_in_the_loop_v2`、`enabled` 严格为 `True`。没有要求其他非白名单 Human 节点自动获得审批权限。

`snapshot` 至少包含 `workflowId/runId/revision/deploymentVersionId/humanBlockIds`，这些字段必须在执行提交之前由可信配置冻结，不能来自模型输出。

## 正式证据链与限制

1. `GET /api/v2/workflows/{workflowId}/versions/{revision}` 提供 `data.id/version/state`，验证数字版本和不可变发布 ID 的对应关系及发布图。启动前传 `require_active=True`，额外要求 `data.isActive is True`。
2. `GET /api/v2/logs/{runId}?includeWorkflowState=true` 提供实际运行的 `data.runId/workflowId/deploymentVersionId/workflowState`。核对三项身份，再检查实际运行图中的允许节点。
3. 后台 `verify_run` 还会检查数字版本定义，但不要求仍活动。发布新版本不能改变已经暂停的旧运行对应的版本事实。

`GET /api/v2/workflows/{id}/runs/{runId}` 本身没有版本字段；当前活动版本也不能证明历史运行版本。本模块不使用这两者替代运行日志。

当前 Sim 的公开 execute 不接受版本 pin 参数。启动前 active 检查不能消除检查后发布、执行前切换的竞态；本模块能拒绝把新版运行登记成旧版审批，不能阻止首次 execute 在新版执行节点。严格的执行前版本钉住需要上游 API 支持或实际控制全部发布入口。两次 active 读数相等也不能证明期间没有切换。

同一 Human context 的重复激活无法可靠区分，因此两个图均执行拓扑限制：读取官方 `loops/parallels[*].nodes` 和 `block.data.parentId`，拒绝 Human 位于循环或并行容器；读取 `edges.source/target`，扩展容器与成员的连接，拒绝从 Human 可返回自身的路径。普通条件 DAG、与 Human 无回路关联的 Agent 环或容器仍允许。缺失 edges/loops/parallels、无法解析的成员或边等返回 unsupported_topology，不猜测安全。此检查保守处理容器边界，不取代 Sim 调度，不支持循环审批。官方结构依据 `packages/workflow-types/src/workflow.ts` 的 Loop/Parallel 与父容器字段。

## 源码与验证

核对上游 checkout `27b53561def7db4eaa9ad2f12c04ea1a0eaaf91c`：

- `apps/sim/lib/api/contracts/v2/workflows.ts`：execute body 严格，deployment pin 明确为服务端内部选项；versions state 为发布快照。
- `apps/sim/app/api/v2/logs/[runId]/route.ts`、`lib/logs/public-queries.ts`：从指定 executionId 的日志取 deploymentVersionId 和 stateSnapshotId。
- `apps/sim/lib/workflows/executor/execution-core.ts`：执行图与 deploymentVersionId 一同传入日志。

2026-10-21 验证：12 项单元测试通过，涵盖 GET 路径、历史非活动版本、错 run/workflow/deployment、版本类型、两种图缺失或节点不符、错误配置、HTTP/重定向/超时及有界异常，以及条件 DAG、无关环、Human 自环/间接环、嵌套容器和未知拓扑。目标文件 flake8 E9/F63/F7/F82 通过。

已有真实审批运行 `c1269b8d-8626-477c-9f9b-f6ef38c41867` 只读验证通过：工作流 `51ef43d9-7e40-430c-9572-59dcebb65426`，发布 revision `1`，deploymentVersionId `fd04df2e-5d44-4980-beea-d72ecc30955b`，Human 节点 `approval-5af35440e701-approval`。`check_definition` 与 `verify_run` 均返回 True。本次未提交任何执行或审批决定，未修改旧流程。
