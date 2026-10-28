# 原生权限批准与拒绝：修复后的独立验证

2026-10-22 新的 `var/native-permissions-retirement/` 环境完成两个真实原生任务：批准后原 CommandExecution completed/exit0，stdout 中出现唯一 nonce；拒绝后原工具层返回 CreateProcess Rejected/rejected by user，命令未创建进程，且没有第二次工具调用。两个任务分别具有独立 thread/turn、launcher/container 和持久 Runtime 收尾事实，最终该环境租约0、活动owned容器0、临时宿主已停止。主九份配置摘要/守卫保持，旧 attempt2 的未知任务及1个隔离租约保留，未重投。

独立根核对为 `var/native-permissions-retirement/root-check.json`，状态 `passed_post_fix_native_approve_reject_read_only`。这是正式角色绑定、真实 Codex、原生审批与独立授权操作者身份的直接切片；没有父角色 LLM 推理或 Sim 运行，不算整个工作流审批与在途重启验收。

拒绝时 Codex 并未持久保存一个 `CommandExecution.status=declined` 条目。原探针因此以 `native_item_evidence_incomplete` 收尾，原 state、意图和账本完整保留。随后通过只读解析原 rollout、原 accepted 拒绝决定与原 launcher 绑定确认结果，没有重复执行。`permission_evidence.py` 只接受完整单turn、唯一工具调用/原始工具失败回执、精确 nonce 命令与唯一已resolved审批；回归拒绝模型自述、多次调用、伪造或成功结果。

原生 `providerItemId=exec-…` 与外层 exec 的 callId 是不同标识，原始资料没有直接映射字段。本证据明确标记 `correlation=unique_single_tool_and_approval_same_turn`、`directItemToCallIdMapping=false`，不冒充直接ID连接。当前解析仅用于此固定单命令探针，任意多工具任务仍需另行证明映射。

```bash
# 默认静态检查；已有现场 --execute 同样只读，不重新调用模型
.venv/bin/python probes/qwenpaw/native_permissions.py --check --retirement-validation
.venv/bin/python probes/qwenpaw/native_permissions.py --retirement-validation --execute
```

第二条在当前已存在现场实际返回 existing_identity_observe_only/writes0。以后新的明确验证窗口仍必须先 bootstrap-only 并核对零模型准备；不允许覆盖原目录或换ID重投未知任务。连续会话修复见 [Codex Runtime 收尾](codex-runtime-retirement.md)。

## 较早现场：批准通过、第二任务未到审批

最终 session 52273 已退出（exit 0，仅代表脚本正常收尾），state=incomplete；临时 PID 2412111 已关闭且 /proc 不存在。`execution-report.json` 实测：主线九份 hash 与 guard 一致，main quiet，owned 容器 0；批准任务 succeeded/resourcesReleased=true；拒绝任务 accepted/resourcesReleased=false，独立 TaskStore 保留 1 个租约，不清理、不重投。拒绝 decision intent 不存在。没有进入第三个任务或新 attempt。

第二次模型窗口沿固定身份执行（exec 52273）。此前 exec 98027 在调用模型前因 general 插件 phase2 尚未加载、descriptor GET 404 而中止；该次 model/cases/execute intents 均 0，完整 state 保留 `pre-submit-interruption.json`。经主线核验后同 attempt2 只读就绪轮询恢复，无角色/未知任务重投。后续 CLI 增加显式 `--execute --resume-pre-submit` 严格零任务恢复入口。

批准任务 `task-a1510a5ba878428283552c73a0bb3fca`：原生 task `task-5872c933fe37`，thread `01a1605c-9b5b-7a31-9408-bba648d21e09`，turn `01a1605c-a7a2-71f2-bba8-e11ce3c9322b`。真实 pending 经外层独立人工凭据批准，deliveryStatus=accepted。原 CommandExecution item `exec-f4c8c3b1-823b-42be-8ab1-81ab46955c69` 是 completed/exit_code=0；`aggregated_output` 中有独立一行 nonce（不是仅命令参数匹配），保存在 `approve-native-items.json`。执行 succeeded、资源正向退出、原 target finished、租约 0 后才开始第二任务。代码现严格只匹配 stdout/aggregated_output/output 的 nonce 行。

拒绝任务 `task-e001b9ba4c1342b8a3ae790db6ee3d3c` 已提交一次，原生 task `task-2c848af03eaa`；官方只读任务查询 status=finished，但 result.status=failed，错误 `harness_error: Codex app-server exited`。没有 thread/turn、没有审批请求或拒绝决定。原始查询保存在 `reject-native-target-status.json`。桥不能据此虚构完整执行身份/退出证明，TaskStore 仍 accepted/resourcesReleased=false，保留租约；没有用新 ID 重投或换 sandbox/model。

因此本轮只能证明批准 slice，**不能声称拒绝已验证或整个探针通过**。父 QwenPaw 角色仅持久绑定，没有父 LLM 推理；也不是 Sim 闭环。下面保留初始化和隔离修复证据。

后续源代码仅在既有控制器和执行桥增加启动器退出确认，全量1013项回归通过。该确认晚于上述现场，不将旧脚本副本或原资源证明追认为新版本验证。根代理最终只读复核九份主配置/守卫一致、临时宿主已停、活动owned容器0、原隔离租约1；见 `var/native-permissions-attempt2/root-final-check.json`。以下“尚未执行”等描述均为历史准备状态。

# 第二次隔离初始化实测结果

第二次 `--bootstrap-only` 已完成（exec session 3284，退出 0），未执行模型阶段。临时宿主 PID **2383434**、startTicks **166119013**、loopback 端口 **35085**；finally 已退出，`/proc/2383434` 不存在。

`var/native-permissions-attempt2/state.json` 最终 `status=bootstrap_ready`、`hostStopped=true`、`baselineUnchanged=true`、`modelInvocations=0`。官方 descriptor 确认 target `agent-line-permission-codex`、parent `agent-line-permission-parent`、独立 workspace `var/native-permissions-attempt2/var/executor/workspace`、`ownedContainer=true`，冻结 revision `qwenpaw-role-v1:841e8d11f8a4bd6372f4cc4437ee6dd09127cce061fdc387632b8c8a2158ae64`。

`bootstrap-check.json` 已核对主线原九份文件 SHA 全一致，guard 全一致（guarded，activeExecutions=0，queuedTasks=0），原主宿主无 Codex 子进程、owned 容器 0；cases 0，execute/decision intents 0。三个复制脚本的逐字节 SHA 在 state.executorSourceHashes，未更改正式源码。桥 schema4 库已初始化，但无模型任务。

**模型窗口尚未执行。** 下一步由主线独立复核后才运行明确 `--execute`。下文为设计与第一次失败审计，历史“未启动”描述仅对应当时准备阶段。

# 第二次尝试：独立执行根（代码就绪，尚未启动）

主线已从精确备份原子恢复第一次覆盖的 `agent.json`；九份主配置 hash 与 guard 已核对一致。第一次 state 和审计文件保留，不重新执行或覆盖。

当前 runner 固定使用 `var/native-permissions-attempt2/`。运行时将正式 `deploy/executor/{launcher,control,lifecycle}.py` 原字节复制到该测试根同样的目录，每份 SHA256 必须与原文件匹配；不改正式源码。脚本原有 ROOT 计算自然指向测试根，独立 `var/executor/config.json` 仅改变 workspace，沿用原固定镜像/binary/codex_home。因此 target 的 `agent.json`、执行 cwd、launch receipt 全部在第二次测试根内；凭据及 rollout 存储明确复用授权 codex_home，不声称完全存储隔离。

两个阶段严格分开：首次必须 `--bootstrap-only`，只建官方角色和 descriptor，finally 停止宿主并验证基线；必须有状态 bootstrap_ready、baselineUnchanged=true、hostStopped=true 才允许随后明确 `--execute`。execute 不允许首次顺带初始化并启动模型。任何已执行/未知状态重复调用均只读，不重投。

准备已通过 py_compile、flake8 与只读 --check；第二次宿主、角色及模型尚未实际启动。下一次窗口先验证 bootstrap，不直接调用模型。以下保留第一次阻断及实现说明作为审计背景；其中第一次固定路径不适用于第二次尝试。

# 当前阻断：共享工作目录的角色配置冲突

2026-10-22 实际 bootstrap-only 已运行，模型调用为 0。官方创建隔离 target 时 `workspace_dir` 指向既有执行工作目录，会写该目录的 `agent.json`，使原角色文件发生变化。此方案不能声称隔离成功。原字节 hash 为 `172c91dbe78a12e71faf8d66f7b0a218abb1cc521cd55a2d561e0fd9f5e1a9fc`，已定位完全匹配备份 `var/plugin-upgrades/role-revision-v1/backup/roles/agent-line-container-codex/agent.json`，交主线恢复。其它记录的主配置/角色 hash 未变，主 guard 前后仍 guarded，活动与队列 0。

临时宿主 PID 2356477 / startTicks 166095218 / port 44773，已按 owner 退出。exec session 23049 已结束。官方插件、metadata-only provider 和 descriptor 成功不抵消这个配置冲突；两条模型任务均未登记或提交。

runner 已新增 `shared_workspace_role_config_collision` 启动前拦截；`--check` 返回 `liveRunnerReady:false`。当前不能打开模型窗口。需要主线选择在既有隔离能力内给测试 target 独立执行 workspace/launcher 配置，或另一经验证的官方角色配置隔离方法；不能改原 target 或忽略 baseline 变化。以下为已实现路径和边界说明，**不表示已通过或已可执行**。

# 原生权限审批隔离验证准备

runner 已实现，但尚未启动隔离宿主、调用模型或证明批准/拒绝闭环。`probes/qwenpaw/native_permissions.py --check` 返回 `liveRunnerReady:false,liveValidated:false`。默认只读，只有显式 `--execute` 才进入已授权执行窗口；已有 state 时该开关也只读取原状态，不重新启动宿主或重投。

## 已核对的接入契约

- `probes/upgrade/qwenpaw_runtime.py:Runtime` 复用现有虚拟环境，要求固定官方候选源码及 ready 标记，实例只允许 `var/upgrade-runtime/<name>`。该门禁专为升级，后续本探针优先直接复用已验证 `.venv` 官方 CLI 与 `deploy/qwenpaw` 隔离环境方式，将临时宿主放 `var/native-permissions/isolated-host`，不修改或绕过 Runtime。必须先核对固定上游完整性，不复制主宿主凭据配置。当前 marker 检查只是信息，不证明候选运行验收。
- 官方 `upstream/qwenpaw/src/qwenpaw/app/routers/agents.py:CreateAgentRequest` 接受 `backend`、`workspace_dir`、`backend_settings`。新 target 使用现有 launcher 的 workspace 和固定本地 model，仅设置 `sandbox=workspace-write`、`approval_policy=on-request`。原 target 不改。新 parent 是原生 QwenPaw 角色，仅作正式身份绑定，不调用 parent LLM。
- 插件 `ExecutionBridge` 要求独立 schema4 receipt 库、私有 service token、managed role、target、workspace、codex_home、container_launcher/control。双认证 HTTP 后先 `/api/agent-line/delegations/bind`，固定 task、`agent-line-<taskId>` session、原生 roleRevision；再 `/executions`。绑定及执行各需先落盘 intent，未知不重投。
- 原 launcher/config 与 `lifecycle.STATE=var/executor/launches` 固定，继续复用不修改。因而只能声称桥/TaskStore/parent ledger 隔离，不能声称底层 launch receipt 也在独立目录。原启动器已有 owner/container/thread/turn 关联。
- 审批通过现有 `NativeApprovalAPI`：`GET /extension/v1/node-tasks/{task}/execution-approvals`，`POST /{request}/decision`，正文 `{decisionId,decision,expectedIdentity:{upstreamTaskId,codexThreadId,codexTurnId}}`。独立人工身份，不能拿服务 token 代替。`control.py approvals` 提供当前真实运行容器的 item/thread/turn/RPC 绑定；不能拿模型自述补缺字段。

## 执行窗口的准确步骤

1. 检查已知宿主、main guard、无在途模型/owned 容器/租约；记录原配置摘要。验证固定上游完整性及当前依赖，使用现有官方 CLI 初始化仅 loopback 临时宿主，安装本地插件只到该实例。
2. 私有 intent 固定两个 task/decision/nonce；建立新 parent/target、独立 bridge/schema4 库、TaskStore、RoleDispatchLedger、人工权限配置。通过原生角色接口获取并冻结 roleRevision，不伪造。
3. 每次仅一个 task。正式注册任务后用双认证 bridge 提交；外层使用既有 gateway 查询/审批，不伪造父模型已完成。先观察真实 Pending，再核对 control receipt 三层身份；没有唯一 binding 就停在不可批准。
4. 批准与拒绝各一次。命令固定为 `python3 -c "print(一次性非秘密nonce)"`，指令显式要求 `sandbox_permissions=require_escalated`。命令本身无需提权，不能保证模型发出审批请求。无审批只记 `native_approval_not_observed`，不得切换 sandbox、模型或身份补试。
5. 读取原 thread 的持久 rollout，仅匹配真实 turn 和 providerItemId：批准要求原生 completed、exit 0 且工具证据包含 nonce；拒绝要求原生 item 的 declined/rejected 状态。模型自述、其他普通命令、容器 tmpfs 消失都不能替代该证据。当前严格解析已实现，若实际 rollout 字段不同，则保留原始对应 items 并报告 `native_item_evidence_incomplete`，不猜测通过。
6. 观察原任务 terminal 和正向 exit receipt、parent绑定状态、租约释放和 owned 容器清理。最后仅停止本次记录且核对启动时间的 owned PID。若尚在等待审批/退出未知，保留固定身份及证据；不能简单杀宿主后宣称执行资源已退出。

本切片不是 Sim 工作流验收，也不是 parent 模型闭环。真实执行 runner 已接线，当前 pending 表示共享角色文件冲突尚未解决。若 Linux sandbox、内核或 app-server 拒绝启动，保留一次失败，不能放宽安全参数掩盖失败。不改 QwenPaw 原程序、原生记忆、原角色、主准入或 executor 配置；不引入产品常驻服务。

## 静态验证与调用

```bash
.venv/bin/python -m py_compile probes/qwenpaw/native_permissions.py
.venv/bin/python -m flake8 --select E9,F63,F7,F82 probes/qwenpaw/native_permissions.py
.venv/bin/python probes/qwenpaw/native_permissions.py --check
# 仅明确执行窗口调用一次：
.venv/bin/python probes/qwenpaw/native_permissions.py --execute
```

以上前三项已通过；最后一项未执行。runner 直接构造 NativeApprovalAPI 所属 TestClient，无额外监听适配服务；只有临时原版 QwenPaw 宿主监听 loopback。所有步骤 intent 写入私有 state 并 fsync；网络请求不自动重试。新目标从现有官方角色配置读取同一 model，未修改原角色。执行证据在 `approve-native-items.json` / `reject-native-items.json`，状态在 `state.json`；bootstrap/host 日志均 0600，不输出凭据。

必须区分：parent 角色/session 的正式绑定真实存在，但没有 parent LLM task；parent ledger 标记 directNativeSlice，parentTaskId 保持空，绝不伪造 parent 完成。验收 finished 指 Codex target 原生任务。第一条未真实退出、对应原生工具证据不足，第二条都不会开始。异常也在 finally 通过 PID、startTicks、uid 和 QWENPAW_WORKING_DIR 核对后停止本次宿主，停止超时保留 unknown。宿主停止不等于容器退出，报告只依据原 exit receipt。
