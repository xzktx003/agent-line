# Sim → Codex → 独立产物验收 → Sim 完成的真实探针

2026-10-21 已在本机真实运行通过。入口 `probes/full_workflow/live.py`，使用现有 Sim v2、TaskStore、Dispatcher、QwenPawExecutionGateway、ArtifactVerifier、PauseBindings、CompletionRelay 和 SimJournal，未改上游源码、未开放公共节点执行准入。

## 专用流程与边界

本次新建图是 Start → Human v2 外部结果等待 → Response。Human v2 是 Sim 原生持久暂停原语；这张新图的用途明确为 `external_task_result`，描述注明不是人工审批，没有通知工具或审批决定字段。其恢复输入字段为 taskId、executionStatus、verificationStatus、memoryStatus。外部可信绑定也固定 purpose，CompletionRelay 不会将普通审批图当作外部任务等待。

Response 使用真实 `<external_wait_00f8e513a6ae.resumeInput.字段名>` 引用返回恢复输入；不是固定写死成功字符串。图源码和创建/保存/发布回执存于私有证据。实际后继节点由 Sim 执行，探针没有自己的图调度器。

这次使用 `require_memory_commit=False`，不包含记忆提交闭环。原 G0 Codex 角色仍为现有隔离验证配置；本次不是生产沙箱证明，也没有宣称任意画布节点可自动注册到适配器。

## 实测身份

|对象|固定身份|
|---|---|
|Sim workflow|`85f184f2-b6ec-4fed-94af-07a1fd661540`|
|Sim run|`fa314452-131d-4e5e-a503-6085a6ba8c9a`|
|等待 block/context|`external_wait_00f8e513a6ae`|
|TaskStore task|`task-cbf78536a36c427c9ab6cf38a3720aee`|
|QwenPaw native task|`task-c6f0bdd2115c`|
|Codex thread|`01a15e22-afed-7631-a563-c3e06e127c8f`|
|Codex turn|`01a15e22-b050-77b3-aea4-3c10f1cdb245`|
|产物|`var/qwenpaw/workspaces/agent-line-g0-codex/full-workflow-00f8e513a6ae.txt`|

真实顺序：

1. 创建、保存、发布专用图，持久化固定 runId 后由 SimJournal 提交一次。
2. GET 观察原运行 paused，核对 block/context 和唯一 pausePoint。
3. 新建 TaskStore 节点，完整绑定 workflow/run/block/context/attempt；PauseBindings 固定外部等待用途及无记忆提交策略。
4. Dispatcher 取得工作区租约，经 QwenPaw 插件执行桥启动原生 Codex，等待可查询原生身份、成功终态和资源释放。
5. 独立检查器实际读取新产物，要求 38 字节及 SHA256 `a133d522423a5a6bbfef73bbeb85c15dc6101d0ebe7b6605df343412763693f5`，通过后由可信 store 方法记录 verification passed。
6. CompletionRelay 从持久绑定回传原暂停上下文，Sim 执行 Response；GET 原 run 确认 completed，返回值为：

```json
{"taskId":"task-cbf78536a36c427c9ab6cf38a3720aee","executionStatus":"succeeded","verificationStatus":"passed","memoryStatus":"not_requested"}
```

这里验证通过只覆盖独立产物的精确字节/大小策略，不代表任意代码质量或全部产品验收。

## 回执不确定与最终观察

本次 relay 即时传输回执被 SimClient 保守记为 `uncertain`。随后对原 run 的真实 GET 确认 `completed` 且输出与绑定任务一致。SQLite journal 保留原 `uncertain`，没有伪改成 accepted，也没有重发 resume。现有客户端会将不同返回 runId、非预期 HTTP 状态或异常信封视为不确定；本次未保存原始恢复 HTTP 响应，因此不能确定是哪一种触发原因。后续可在可信适配器增加脱敏传输诊断，但不得因此自动重试恢复。

因此证据证明的是原流程实际完成及结果关联，不宣称即时恢复回执识别全部兼容。

## 运行与持久恢复约束

```bash
.venv/bin/python probes/full_workflow/live.py run
.venv/bin/python probes/full_workflow/live.py observe
```

首次运行才创建和执行。`var/full-workflow/state.json` 一旦存在，再调用 run 也只观察已有 Sim 身份和 TaskStore 状态，不再注册/提交/验收/恢复。若首次过程未完成，仅观察不会自动完成中断后的待办，需要人工或未来可信恢复协调器核对原身份；不要删除 state 后重跑来绕过不确定性。

已真实复跑 run 和 observe，各返回 `observation_only / completed`；SimJournal 仍为 2 条操作（一次启动、一次恢复），没有替换 run 或第二次恢复意图。观察模式不登录 QwenPaw，也不启动 Codex。

秘密从现有私有配置读入内存，不输出。业务证据保存在 0600 私有文件：

- `var/full-workflow/state.json`：图、暂停、任务/执行身份、验收、relay 回执和最终观察。
- `var/full-workflow/graph-operations.json`：真实创建/保存/发布意图与回执。
- `var/full-workflow/sim-journal.sqlite`：固定身份的两个出站操作。
- `var/full-workflow/pause-bindings.sqlite`：任务与暂停上下文一一关联。
- `var/full-workflow/artifacts/`：独立检查器生成的真实 receipt。

验收范围不含记忆提交、生产隔离、自动为任意 Sim 图建节点、自动恢复中断探针或跨设备可用性。
