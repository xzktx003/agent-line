# 原生 ReMe 两任务闭环验收

本探针保留 QwenPaw 正式角色、原生 `remelight` 和官方记忆接口。角色为 `agent-line-native-loop-parent`，使用现有 Kilo 模型与固定容器 Codex 执行器；不引入自定义记忆后端，不重启主宿主、不解除主维护门禁。

## 验收链路

1. 独立 TaskStore / RoleDispatchLedger 登记任务 A；原生父角色委派 Codex 写入独有文件，校验精确字节和容器退出证据。
2. 由**独立探针审核身份**通过实际 MemoryAPI 审核该任务经验，调用官方记忆 PUT / GET，并显式原生 BM25 reindex。这是受授权探针审核，不声称用户亲自审核。
3. 同一正式角色的新持久任务与会话 B，只获得检索线索和新文件名；唯一标记不能出现在 B 的输入提示。
4. 核对真实 memory_search 调用、同 call_id 工具结果与 A 的来源文件、delegate 入参、Codex 原生 user input、B 精确产物、独立 ArtifactPolicy 和退出证明。
5. B 前后重建外层 API 实例，证明角色、workspace 与原生记忆不需重建。

`manualApprovedWriteback=true` 只在全链通过后记录。自动提炼和跨项目执行隔离分别保持 `automaticConsolidationValidated=false`、`crossProjectIsolationValidated=false`。新项目串行复用固定 executor；这不是多项目执行隔离证明。

## 幂等和超时

入口为 `probes/qwenpaw_memory/native_roundtrip.py`：

```bash
.venv/bin/python probes/qwenpaw_memory/native_roundtrip.py --attempt 3 --parent-timeout-seconds 900
```

仅在已经明确授权的新尝试下改变 `--attempt`。每次尝试固定任务身份、随机标记、正文及父任务超时；提交前持久 intent，已有 intent 只观察，不再次 POST。观察窗口为父任务预算加 300 秒。到期也不伪造原生终态，而是保存原身份取消意图并按既有证明链核对资源。

`--parent-timeout-seconds` 默认 900，允许 1–3600，准备时即落盘；已有尝试传入不同值拒绝。历史 attempt 1/2 没有该字段，仍按 300 秒解释，复查须显式传 `--parent-timeout-seconds 300`，不能静默扩大旧任务预算。

所有尝试使用同一数据库；新尝试先确认前次任务终态与资源释放，启动前检查宿主没有 Codex 子进程、没有活动 owned 容器。未知结果不会通过新建数据库或更换 ID 绕过。

## 已知证据

私有证据位于 `var/native-memory-roundtrip/`，不公开凭证或完整模型正文。

- attempt 1：Codex 回合结束、容器退出，但产物不存在，业务验收 failed。发现父协调限制误传给 Codex；通用 RoleDispatchGateway 提示随后修复，未改旧证据。
- attempt 2：父模型 300 秒超时。日志显示首个 memory_search 在约 245 秒才出现，原生 ReMe 同秒返回空结果，后续未完成委派。经正式取消证明链收敛为 canceled / resourcesReleased，executionIdentity 为空、租约释放，没有伪造 Codex 身份。
- attempt 3：使用 900 秒预算，A `task-a5ab05f138354353a2f8ecc8f993cb52` 执行成功、产物验收 passed、资源退出。独立身份 `native-roundtrip-probe-reviewer` 经 MemoryAPI 审核写回 committed，官方读回及 reindex 完成。
- B `task-65d51b9bc9df451f909fcca5abcb7167` 的真实 memory_search 成功结果包含 A 来源文件和唯一标记；后续 delegate 及对应 Codex 原生 user input 均含该标记，**已验证部分记忆召回与注入链路**。
- B 业务验收仍 failed：父角色把自己的 QwenPaw workspace 添加到委派要求，Codex 容器内该目录不存在，shell 的 cd 退出 1，目标文件未生成。原生回合 task_complete 不能替代 ArtifactPolicy 通过。
- 父角色共调用 delegate 四次：首次受理；后续三次改变了委派指令，均因原任务已绑定其他载荷而明确拒绝，没有实际新执行。原任务/绑定/投递/rollout/turn各一个，身份保持。这验证受控去重与载荷冲突保护，不构成业务重试成功。
- 最终 B 父任务 finished/completed，Codex 原生终态与容器退出证据齐全，独立任务库租约数 0、owned 活动容器数 0。此轮收集时未发 attempt 4，未改角色模型、插件或主维护门禁。

最终私有分项报告为 `attempt3-report.json`：`manualApprovedWriteback=true` 仅表示独立探针审核写回已完成；`fullRoundtripVerified=false` 明确完整两任务业务闭环未通过。真实父调用证据在 `attempt3-B-parent.json`。旧尝试与失败产物验收不改写。

下一步应修通用协调提示的工作区边界：协调角色不能把自身 workspace 当作执行器 cwd，不能猜绝对目录或转发 cd 指令；交给执行器的是相对文件要求，执行器使用已有服务端绑定 cwd，不能扩权。终态业务失败应如实报告，不能同 task 重复 delegate 伪装新的尝试。

回归测试：`tests/test_native_memory_roundtrip_probe.py` 验证工具证据关联、防叙述冒充、来源与身份匹配、持久提交防重及超时身份不可变。


## attempt 4：通用提示修复后的完整通过

在保留 attempt 3 失败事实后，新增两项回归先复现提示缺口，再仅修改 RoleDispatchGateway 通用协调提示：明确协调 workspace 不等于 Codex cwd，不猜测或转发协调绝对路径、不给执行器添加 cd；保持用户已授权项目范围和服务端绑定目录的权限边界；读取原生终态后只汇报，不重复 delegate 假装新业务尝试。

随后明确授权的 attempt 4 使用同角色、同数据库、原生 ReMe 和 900 秒预算，通过真实两任务闭环：

- A：`task-b3014c27509442dd9ee01b013be585cd`，执行 succeeded、ArtifactPolicy passed、资源退出。
- 记忆：独立探针身份 `native-roundtrip-probe-reviewer` 审核 A，经真实 MemoryAPI 写回官方原生文件，GET 正文一致，BM25 reindex 完成。
- B：`task-f658c3b0d3e34c7f9818d300e073e49e`，新任务提示不含验收标记；真实 memory_search 输出来自 A 的任务来源文件；关联 delegate 与 Codex 原生 user input 均含召回标记；B 精确文件验收 passed，资源退出。
- 两个原生父任务最终官方 GET 均为 finished/completed；任务数据库租约 0，owned 活动容器 0。B 前后重建外层审核 API，正式角色 workspace 与记忆仍可复用。

私有结果：`var/native-memory-roundtrip/attempt4-report.json`，完整状态与引用在 `state-attempt-4.json`，最终父任务快照在 `attempt4-A-parent-final.json`、`attempt4-B-parent-final.json`。

该结果中 `verified=true`、`manualApprovedWriteback=true`；审核来自独立探针身份，不是用户亲自点击审核。`automaticConsolidationValidated=false`、`crossProjectIsolationValidated=false`。未创建 attempt 5，没有改原生记忆引擎、上游代码或执行桥权限。

定向验证：25 项角色调度测试、11 项探针证明/超时测试及静态检查通过。

2026-10-22 独立复查 AC-38：直接核对上述 attempt3 的原父工具事件、原回执与绑定、原投递行及唯一 Codex rollout/turn，后3次为不同载荷冲突拒绝，未新增执行。31项定向回归以真实 SQLite 与明确原生调用替身验证丢回执后不重投、同载荷去重、改载荷409、重开账本及并发；实际网络丢回执未现场注入。该条以真实重复委派场景通过，记录 `var/acceptance/delegation-review-AC38.json`；原失败产物验收及旧未知任务/隔离租约不变。

## 真实宿主重启后的同角色记忆复用

`probes/qwenpaw_memory/after_restart.py` 复用上述 attempt4 的原经验，不创建角色、不改记忆后端、不重新写入或 reindex 该经验。原 committed 事件、独立审核、原任务与产物证据、原父会话完成时间均已核对；经验提交和旧会话结束都早于当前真实 QwenPaw 进程出生。当前宿主来自此前官方插件部署的实际重启，本探针不再重启其他服务。

新任务 `task-397b827829604379ab3397654fcec8ee` 使用原角色与新会话，提示只有原检索线索及新文件名，不含验收标记。实际原生 memory_search 输出来自原 A 任务的 digest 文件；关联 delegate 与实际 Codex user 输入含相同标记。rollout 的 session ID、消息 turn 元数据以及 task_started、turn_context、task_complete 窗口均匹配本次 thread/turn，不能把旧 turn 的文字充作本次注入。

实际新文件51字节精确通过独立 SHA/大小验收；任务、父投递与插件绑定的三个角色修订一致。原执行回执、容器/启动器退出和官方 Runtime.stop 持久收尾匹配，新任务租约0，主九份配置与原守卫不变。独立审查 AC-10 通过，证据 `var/native-memory-after-restart/{state,root-check,acceptance-review-AC10}.json`。旧未知任务与隔离租约保持，不因新成功任务被清理。

默认运行不发请求、不创建目录；首次显式 `--execute` 才能提交。已有 state 时，即使再次传入 `--execute` 也只 GET 原父任务、SQLite 只读观察，不初始化或重新提交；真实重复验证为新提交0、原生变更0，原 state/任务库/投递库/凭证字节保持。失败或未知保留原意图，不能靠删 state 或换身份重投。

本条只证明 QwenPaw 真实宿主重启后的原生记忆→新 Codex 执行链，审核来自独立探针身份。没有新的 Sim 发布/运行快照，不冒称完整工作流“五处冻结”；没有本轮全服务重启、在途恢复、跨项目隔离、经验修正/删除或完整 R-09 的验收。相关原生记忆与探针回归31项通过。
