# 编译静态并行的原生 Sim 与真实账本组合验证

`probes/sim/compiled_parallel_probe.py` 验证完整的编译和结果回传路径：工作流文档 → 既有 `compile_document` → 原生 Sim 保存、发布、读回 `decode_document` → 原生双暂停 → `ExternalTaskDiscovery` 登记两个真实本地 TaskStore 任务与 PauseBindings → 受信测试接口注入明确的合成结果 → 原有 `CompletionRelay` 和 `SimJournal` 恢复各自原 context → 原生 Function 汇合与 Response。

此探针不调用 QwenPaw、Codex、模型、原生记忆或真实执行器。它不能证明真实 Agent 已完成工作、真实独立命令测试通过、Docker 已退出，不能代替 QwenPaw/Codex 闭环或完整 PRD 验收。QwenPaw 仍是主程序，原生角色、会话和记忆系统均不改动；没有新增常驻服务、依赖或上游补丁。

当前源码已接通静态并行的中文配置、原生图编译、任务发现、回传及终态核对；主实例没有自动开启新的执行准入。旧持久记录默认不获得多暂停能力，需新发布记录显式冻结 `externalTasks.staticParallel: true`，运行记录保存同名标志，原暂停绑定保存 `allow_multiple: true`。这些均沿用现有 JSON 账本，不新增表或数据库迁移。已通过四个真实 Sim 组合场景，执行数据仍明确为合成；现场与后续限制见文末。

## 合成数据的明确边界

所有本地运行数据位于新的 `var/sim-compiled-parallel/` 目录，目录 0700、文件 0600，与生产配置和原有角色分离。临时 project 和 role 配置显式 `synthetic: true`，role 为 `synthetic-no-executor`，无 Dispatcher。只有此局部 discovery 对象启用 `allow_submissions=True`；不改变正式实例的执行准入。

Agent 配置保持 `memoryPolicyId=native-role`、`outputSchemaId=task-result`，但测试冻结 `requireMemoryCommit=False`。本地任务记忆状态必须保持 `not_requested`，没有调用任何记忆 API，也不伪造 committed。

合成注入仅用 TaskStore 原有受信接口 `claim/accept/finish/confirm_resources_released/record_verification`。执行身份只有显式带 `synthetic-` 前缀的 `upstreamTaskId`，没有 `codexThreadId` 或 `codexTurnId`。执行证据、输出、验证 receipt 和 check 都带 `synthetic: true`，资源证据明确 `noExecutorCreated: true`。不会构造 Docker 退出回执，不会把合成验证称作真实命令测试。

这是一种与单元测试 fixture 一致的私有测试数据注入，目的是让真实账本与 relay 在原有状态约束下工作。代码不会新增可供产品用户或模型提交任意结果的接口。

## 四个场景与验收事实

同一个专用工作流依次使用三个固定发布版本；每个场景拥有独立 root run ID 和 nonce。图为开始 → 固定并行 Function → 甲/乙两个 Agent 的原生 Human v2 等待 → 原生汇合 Function → 结束。两个 Agent 的输入由 Sim 解析自 `开始.input`，发现器逐字段核对完整嵌套输入，不由探针自行选择后继节点。

| 场景 | 甲合成验证 | 乙合成验证 | 策略 | 预期原生终态 |
| --- | --- | --- | --- | --- |
| all_pass | passed | passed | all_success | completed，保留双方结果 |
| all_verification_failure | passed | failed | all_success | failed，join_policy_unsatisfied |
| partial_verification_failure | passed | failed | allow_partial | completed，保留乙的 failed 与全部证据 |
| required_verification_failure | failed | passed | required_success，必需甲 | failed，join_policy_unsatisfied |

每次执行前，用真实 deployment ID、revision、完整 Agent 策略和 `staticParallel: true` 冻结 `WorkflowRunLedger`，并实际调用 `PublicationVerifier` 校验发布定义及原始运行的完整编译图快照。没有显式静态并行标志时，原有多暂停准入仍然关闭。

两个节点同时真实 paused/snapshotReady 后，原 `ExternalTaskDiscovery` 必须登记两个不同任务与原始 PauseBindings，且绑定显式允许 multiple。重建四个账本对象后再次发现，必须复用同两个任务，不能产生替代身份。

探针故意先回传乙，再回传甲。首次回传后必须观察乙 resumed、甲仍 paused；原生私有快照的实际 blockStates/executedBlocks/blockLogs 均不得包含汇合或结束。第二次回传后，同时读取根与恢复子运行，核对最后子运行真实终态。成功输出与原生汇合输出都要完整保留原 TaskStore 的 taskId、合成 executionIdentity、验证状态、nonce、output 和 evidence。失败不能只凭状态断言，要看到明确的 `join_policy_unsatisfied` 原因。

下游 Function 失败时上游可能将甲 context 标记 failed，探针保存真实状态与 resumedCount，不将其改写为 resumed。原生恢复回执仍只表示接受，不等于汇合成功。

## 持久化与再次运行

原生 create/save/publish 每次提交前，先将请求载荷和 intent 以 0600 原子写入并 fsync 文件和目录。execute 的固定 root run ID、nonce 和完整输入先持久保存；原 `SimJournal` 在任何 execute/resume HTTP 请求前事务提交完整输入和身份。relay 使用原 TaskStore 任务和 PauseBindings，不直接构造恢复请求替代既有实现。

每次 relay 后重建 journal/relay，并再次递交完全相同的冻结任务，只能返回原回执，原生请求数量不得增加。单元测试另覆盖收到超时/uncertain 后重建仍不得再次提交。

默认调用仅观察已登记的原身份。已有 state 时即便传 `--execute` 也保持零原生写入；仅下文首次发布前中断有一次严格限定的继续入口，没有通用恢复。中断、未知 intent、失联或不完整证据均保留原身份，不能删除 state、换 ID、重投或重建账本来覆盖旧结果。局部账本缺失也不会由观察操作自动重建。

原生 run 基础 GET 不请求未执行节点的 selectedOutputs；Sim 对不存在输出会返回 400。只有已完成 Response 明确证明汇合执行后，才读取汇合输出。对短暂尚未可读的根/子 run 或暂停详情 404，仅轮询同身份；超时不重新提交。

## 运行

从仓库根目录使用现有 `var/sim/probe-auth.json`，权限必须 0600。

```bash
# 无凭证图预览，不联网
.venv/bin/python probes/sim/compiled_parallel_probe.py --graph
# 默认只读，首次无 state 时不创建任何原生对象
.venv/bin/python probes/sim/compiled_parallel_probe.py
# 经审查后，首次创建一个专用工作流并运行四个合成场景
.venv/bin/python probes/sim/compiled_parallel_probe.py --execute
# 再次观察必须 nativeWritesThisInvocation=0
.venv/bin/python probes/sim/compiled_parallel_probe.py
```

四场景完整执行最多 19 个原生修改请求：create 一次、三个版本各 save/publish 一次、execute 四次、resume 八次。所有输入均为合成测试数据，模型和记忆调用为零。

私有证据包括 `state.json`、四个 SQLite 账本、`report.json`、重复只读的 `observation.json` 和独立 `failure-*.json`。原始暂停快照含内部执行数据，不能公开输出或当作稳定 v2 API 契约；该宿主只读路由和固定编译图均需升级兼容检查。

## 已执行验证

新增 `tests/test_compiled_parallel_integration.py` 的十二个跨层单元测试已通过：实际编译器、真实临时 SQLite 账本、实际发现器/输入适配/relay/journal，HTTP 明确为 MockTransport；编译出来的固定汇合 Function 在 Node 中真实执行。覆盖三种策略、保留失败分支、重建任务身份、uncertain 不重投、发布不符和缺少静态标志拒绝，以及重复合成注入拒绝。

Python 语法、错误级 flake8 与图预览通过。首次真实调用已获得 create/save 回执；原生读回仅改变 nodes 数组的序列化顺序，节点值、边和编译结果完全相同，原探针因此在发布前停止。尚无发布、execute 或 resume，四账本业务/原生操作记录均为空。保留原失败与只读核对证据。

探针现用 `compile_document(decoded, workflowId) == compile_document(expected, workflowId)` 比较读回和发布文档。编译块本来是按 ID 索引的映射，字典顺序没有图语义；所有生成代码、位置、配置和完整边列表仍逐值比较。不改核心编译器、decoder 或 DSL，不放宽原生执行图校验。新增离线回归明确接受纯节点顺序变化，并拒绝位置和指令变化。

## 首次发布前中断的一次限定继续

仅原工作流 `fcad35eb-e5ed-421d-a2e7-3071c16bcaf1` 可使用：

```bash
.venv/bin/python probes/sim/compiled_parallel_probe.py --continue-initial
```

它必须恰好只有 `create`、`all_success-save` 两个 received 步骤，完整原 save 载荷和回执身份不变，runs 为空，没有任何 publish/execute/resume intent、publication 或之前 recovery。四个原私有 DB 必须已存在且 schema 正确；tasks、leases、events、admission_guard、runs、pauses、bindings 和 journal operations 全部为零，不自动初始化缺失 DB。原 synthetic 配置与工作区必须匹配。

继续前只读核对原 workflow、当前 draft 编译结果、未部署状态和空版本列表；通过后先持久保存一次性 recovery 标记。首次 save 使用原已收到的回执，**不再次 create/save**，从 first publish 继续原四场景。后续任何中断都不能再次满足此窗口，也不能借它重投 execute/resume。默认观察仍为零原生写入。

离线测试覆盖该唯一窗口、空账本和原身份复用，并拒绝 intent、后续阶段、已有运行、重复 recovery、不同 workflow 及任何已有 journal 操作。该限定继续现已完成，不能再次使用；没有重发 create/save。

## 当前真实组合结果（2026-10-22）

上述原工作流通过三个发布版本完成四个场景。共19次原生修改均有 received 回执；8个独立任务及原 context 绑定、重建后复用、四个本轮 nonce、完整结果和证据均核对通过。全部成功与允许部分失败分别 completed；全成功策略遇验证失败、必需甲失败分别 failed，明确错误为 `join_policy_unsatisfied`。乙先恢复后，实际执行集合仍没有汇合或结束节点；第二次恢复后才出现对应终态。

实测发现失败恢复子运行可能让原根运行保留历史 paused。现有发现器已补齐只读核对：所有原生暂停点都已 resumed/failed、每个机器任务资源已释放、原 journal 回执接受且结果与任务事实一致、原发布图核验通过，并查询最后已接受子运行确实终态后，才关闭原运行记录。仍有其他暂停点时不能用较早完成的子运行提前关闭；更新的未知恢复不能被旧回执掩盖。这项修复在原四次运行上核对，新增原生写入0，没有重跑任务。

根代理独立复核原生状态、完整发布/运行图、原 context→子运行/恢复输入、冻结结果、8个明确合成任务与记忆 not_requested、4个本地完成记录和租约0；见 `var/sim-compiled-parallel/root-check.json`。终态修复的独立记录为 `terminal-reconciliation.json`。默认再次运行只有 GET，原生写入0。主实例九份配置摘要与守卫不变，活动owned执行容器0。

本轮新增66项回归，全量 **1087项通过（47.044秒）**；错误级flake8、JS/shell、上游完整性及diff检查通过。中文编辑器离线浏览器27项通过，6次保存内容均经过真实编译器，三种策略均通过；见 [编辑器验证](editor-static-parallel.md)。离线界面与本原生API现场是两份独立证据，未冒充同一次浏览器端到端运行。

当前只支持一对静态分发/汇合、2–20个独立 Agent 等待，分支数不能超过声明上限；同工作区仍由原租约串行或拒绝并发，不实现额外队列调度器。真实 QwenPaw/Codex 多分支执行、动态批量、全局预算、嵌套拓扑与固定子流程版本仍待完成；AC-31、完整P1及完整PRD不记通过。
